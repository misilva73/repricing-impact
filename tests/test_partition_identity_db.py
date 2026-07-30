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


# Window that carried raw ReplacingMergeTree duplicate row_ids at full scale on
# the producer-v10 run (eip-8037, blocks 24,498,000-24,499,999 — ~2.2k dups).
#
# RE-MEASURED 2026-07-30 against the v11 run (config 0x6617c5db…6ed3): **zero**
# duplicate row_ids, not only in this window but across the ENTIRE 1,000,000-block
# range for BOTH focus schedules (chunked `count() - uniqExact(row_id)`, all
# chunks 0) — that run's parts are fully merged. Whether any duplicates are
# visible is a property of background merge timing, so it is NOT something a test
# may pin: the live test below asserts the dedup *identity* on real rows (which
# holds either way) and skips only the "duplicates were actually present" leg.
# The collapse itself is covered unconditionally by the synthetic test that
# follows, so removing the v10-era `assert raw_dups > 0` costs no coverage.
DUP_WINDOW = (24498000, 24499999)


def test_dedup_subquery_preserves_live_rows_and_collapses_any_duplicates(ctx):
    """On live rows: dedup drops exactly the duplicates and leaves guard == 0."""
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
    # Non-vacuity: the window must return rows, or the identities below are moot.
    assert int(ded["n"]) > 0, "dup window returned no rows at all"
    # Dedup removes exactly the duplicate rows — a no-op when there are none...
    assert int(raw["n"]) - int(ded["n"]) == raw_dups
    # ...and the deduped relation has one row per row_id (guard == 0).
    assert int(ded["dups"]) == 0
    if raw_dups == 0:
        pytest.skip(
            "the current run has no pre-merge duplicate row_ids in DUP_WINDOW "
            "(v11 is fully merged), so the live rows exercise only the no-op "
            "leg; the collapse is covered by the synthetic-duplicates test"
        )


# Synthetic relation standing in for a pre-merge ``_divergence`` slice: row_id
# 'a' three times, 'b' twice, 'c' once, with out-of-order ``updated_at`` so a
# naive "last row wins" would pick the wrong value. Latest-version values are
# a -> 3.5, b -> 8.0, c -> 0.5.
_SYNTHETIC_DUPLICATE_ROWS = (
    "(SELECT * FROM values("
    "'row_id String, updated_at DateTime, min_multiplier_to_succeed Float64', "
    "('a', toDateTime('2026-01-01 00:00:00'), 1.5), "
    "('a', toDateTime('2026-01-03 00:00:00'), 3.5), "
    "('a', toDateTime('2026-01-02 00:00:00'), 2.5), "
    "('b', toDateTime('2026-01-02 00:00:00'), 8.0), "
    "('b', toDateTime('2026-01-01 00:00:00'), 9.0), "
    "('c', toDateTime('2026-01-01 00:00:00'), 0.5)))"
)


def test_dedup_subquery_collapses_synthetic_duplicates(ctx):
    """The argMax dedup collapses to one row per row_id, keeping the latest.

    Exercises the real SQL emitted by :func:`groups.deduped_divergence_subquery`
    against a hand-built duplicated relation, so the collapse is covered even
    when the warehouse's parts happen to be fully merged (as the v11 run is).
    Substituting the table is deliberate and test-only — it fails loudly if the
    helper ever stops reading :data:`groups.DIVERGENCE_TABLE`.
    """
    engine, _ = ctx
    sql = groups.deduped_divergence_subquery(
        columns=["min_multiplier_to_succeed"], where="1 = 1"
    )
    assert groups.DIVERGENCE_TABLE in sql, "dedup helper no longer reads the table"
    deduped = sql.replace(groups.DIVERGENCE_TABLE, _SYNTHETIC_DUPLICATE_ROWS)

    raw = _q(
        engine,
        f"SELECT count() n, ({groups.DEDUP_GUARD_SQL}) dups "
        f"FROM {_SYNTHETIC_DUPLICATE_ROWS}",
    ).iloc[0]
    # The fixture really does carry duplicates (6 rows, 3 distinct row_ids).
    assert int(raw["n"]) == 6
    assert int(raw["dups"]) == 3

    got = _q(
        engine,
        f"SELECT row_id, min_multiplier_to_succeed AS m FROM {deduped} "
        f"ORDER BY row_id",
    )
    # Collapsed to one row per row_id, each holding the max-updated_at value.
    assert list(got["row_id"]) == ["a", "b", "c"]
    assert [float(v) for v in got["m"]] == [3.5, 8.0, 0.5]

    ded = _q(
        engine,
        f"SELECT count() n, ({groups.DEDUP_GUARD_SQL}) dups FROM {deduped}",
    ).iloc[0]
    assert int(ded["n"]) == 3
    assert int(ded["dups"]) == 0
