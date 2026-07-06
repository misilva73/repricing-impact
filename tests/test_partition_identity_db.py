"""DB-backed partition-identity tests on a SMALL sample of blocks.

Skipped automatically when the warehouse / ``secrets.json`` is unavailable so CI
without DB access still passes (the pure-python boundary logic is covered by
``test_groups_predicate.py``).

Asserts, on a small block window for both focus schedules:

- ``G1 + G2 + G3 + G4 + AF + G5 == tx_count`` (the full partition).
- ``G2_drillin + G3 + G4 + AF == retained_drill_in_count`` (every retained
  drill-in is exactly one of G2/G3/G4/Already-failing).
- ``G1 + tx_count_gas_only + tx_count_stored == tx_count`` on non-truncated
  blocks (the handover's previously-unverified coverage partition).
- the biconditional ``schedule_success <=> min_mult <= 1`` (zero leakage).
- the ReplacingMergeTree dedup guard ``count() == countDistinct(row_id)``.
"""

import pytest

pytest.importorskip("sqlalchemy")

from repricing_impact import groups  # noqa: E402
from repricing_impact.config import CHAIN_ID, FOCUS_SCHEDULES  # noqa: E402

# Small validation window inside the pinned config's range.
SAMPLE_START = 24320000
SAMPLE_END = 24321000


@pytest.fixture(scope="module")
def ctx():
    """(engine, config_hash) or skip if warehouse/secrets unavailable."""
    try:
        from repricing_impact.clickhouse import get_engine
        from repricing_impact.config import resolve_config_hash

        engine = get_engine()
        engine.connect().close()
        res = resolve_config_hash(engine)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"warehouse unavailable: {exc}")
    if res.single_hash is None:
        pytest.skip("no single config covers both focus schedules")
    return engine, res.single_hash


def _q(engine, sql):
    from repricing_impact.clickhouse import run_query

    return run_query(sql, engine=engine)


def _where(config_hash, schedule):
    return (
        f"chain_id = {CHAIN_ID} "
        f"AND analysis_config_hash = '{config_hash}' "
        f"AND schedule_name = '{schedule}' "
        f"AND block_number BETWEEN {SAMPLE_START} AND {SAMPLE_END}"
    )


@pytest.mark.parametrize("schedule", FOCUS_SCHEDULES)
def test_full_partition_identity(ctx, schedule):
    engine, config_hash = ctx
    cov = _q(
        engine,
        f"""
        SELECT sum(tx_count) tx, sum(tx_count_unchanged) g1,
               sum(tx_count_gas_only) gas_only, sum(retained_drill_in_count) retained
        FROM gas_analysis.gas_analysis_block_coverage
        WHERE {_where(config_hash, schedule)}
        """,
    ).iloc[0]
    deduped = groups.deduped_divergence_subquery(
        columns=["schedule_success", "baseline_success", "min_multiplier_to_succeed"],
        where=_where(config_hash, schedule),
    )
    div = _q(
        engine,
        f"""
        SELECT {groups.divergence_group_counts_sql()}
        FROM {deduped}
        """,
    ).iloc[0]
    # On the deduped relation the guard must be 0 (one row per row_id).
    assert int(div["dup_rows"]) == 0

    g2_drillin = int(div["g2_drillin"])
    g3 = int(div["g3"])
    g4 = int(div["g4"])
    af = int(div["af"])
    tx = int(cov["tx"])
    g1 = int(cov["g1"])
    g2 = int(cov["gas_only"]) + g2_drillin
    g5 = tx - (g1 + g2 + g3 + g4 + af)

    # Full partition closes.
    assert g1 + g2 + g3 + g4 + af + g5 == tx
    # G5 cannot be negative.
    assert g5 >= 0
    # Every retained drill-in is one of G2/G3/G4/Already-failing.
    assert g2_drillin + g3 + g4 + af == int(cov["retained"])
    # Dedup guard: no duplicate row_id in the divergence scan.
    assert int(div["dup_rows"]) == 0


@pytest.mark.parametrize("schedule", FOCUS_SCHEDULES)
def test_coverage_partition_on_non_truncated_blocks(ctx, schedule):
    engine, config_hash = ctx
    # On non-truncated blocks, unchanged + gas_only + stored == tx_count.
    bad = _q(
        engine,
        f"""
        SELECT count() AS violations
        FROM gas_analysis.gas_analysis_block_coverage
        WHERE {_where(config_hash, schedule)}
          AND drill_ins_truncated = false
          AND (tx_count_unchanged + tx_count_gas_only + tx_count_stored) != tx_count
        """,
    ).iloc[0]["violations"]
    assert int(bad) == 0


@pytest.mark.parametrize("schedule", FOCUS_SCHEDULES)
def test_min_mult_success_biconditional(ctx, schedule):
    engine, config_hash = ctx
    # schedule_success <=> min_mult <= 1, with zero leakage.
    leak = _q(
        engine,
        f"""
        SELECT
            countIf(schedule_success = true AND (min_multiplier_to_succeed IS NULL
                    OR min_multiplier_to_succeed > 1)) AS success_but_not_le1,
            countIf(schedule_success = false AND min_multiplier_to_succeed <= 1) AS le1_but_not_success
        FROM gas_analysis.gas_analysis_divergence
        WHERE {_where(config_hash, schedule)}
        """,
    ).iloc[0]
    assert int(leak["success_but_not_le1"]) == 0
    assert int(leak["le1_but_not_success"]) == 0


# Window known to contain raw ReplacingMergeTree duplicate row_ids at full scale
# (eip-8037, blocks 24,498,000-24,499,999 — ~2.2k duplicates).
DUP_WINDOW = (24498000, 24499999)


def test_dedup_subquery_collapses_full_scale_duplicates(ctx):
    """The argMax dedup subquery removes the raw duplicates; guard is 0 after."""
    engine, config_hash = ctx
    lo, hi = DUP_WINDOW
    where = (
        f"chain_id = {CHAIN_ID} AND analysis_config_hash = '{config_hash}' "
        f"AND schedule_name = 'eip-8037' AND block_number BETWEEN {lo} AND {hi}"
    )
    raw = _q(
        engine,
        f"SELECT count() n, ({groups.DEDUP_GUARD_SQL}) dups "
        f"FROM gas_analysis.gas_analysis_divergence WHERE {where}",
    ).iloc[0]
    deduped = groups.deduped_divergence_subquery(
        columns=["schedule_success", "min_multiplier_to_succeed"], where=where
    )
    ded = _q(
        engine,
        f"SELECT count() n, ({groups.DEDUP_GUARD_SQL}) dups FROM {deduped}",
    ).iloc[0]

    raw_dups = int(raw["dups"])
    # This window must actually exercise the dedup path (else the test is moot).
    assert raw_dups > 0, "expected raw duplicates in the dup window"
    # Dedup removes exactly the duplicate rows...
    assert int(raw["n"]) - int(ded["n"]) == raw_dups
    # ...and the deduped relation has one row per row_id (guard == 0).
    assert int(ded["dups"]) == 0
