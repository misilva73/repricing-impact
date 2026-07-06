"""Pure-Python tests of the group predicate boundary logic (no DB needed).

These exercise :mod:`repricing_impact.groups` on synthetic drill-in rows so CI
without warehouse access still verifies the load-bearing ``<=1 / >1 / NULL``
boundary, the ``baseline_success`` gate that carves out the Already-failing
group, and the block-level partition assembly.

All drill-in rows below use ``baseline_success=True`` unless the test is about
the Already-failing carve-out.
"""

from repricing_impact import groups

# --- classify_drill_in: the G2/G3/G4 boundary (working baseline) --------------


def test_g2_success_at_original_limit():
    # schedule_success True -> G2 regardless of min_mult value.
    assert groups.classify_drill_in(True, 0.5, baseline_success=True) == "g2"
    assert groups.classify_drill_in(True, 1.0, baseline_success=True) == "g2"
    # defensive: success keyed on flag
    assert groups.classify_drill_in(True, None, baseline_success=True) == "g2"


def test_g3_needs_gas_bump():
    # Not success, any min_mult > 1 -> G3 (real values span (1, 10], the sweep
    # ceiling; the classifier is purely >1-keyed so it is robust to any value).
    assert groups.classify_drill_in(False, 1.0001, baseline_success=True) == "g3"
    assert groups.classify_drill_in(False, 2.0, baseline_success=True) == "g3"
    assert groups.classify_drill_in(False, 4.0, baseline_success=True) == "g3"
    assert groups.classify_drill_in(False, 8.0, baseline_success=True) == "g3"
    assert groups.classify_drill_in(False, 9.9979, baseline_success=True) == "g3"
    assert groups.classify_drill_in(False, 100.0, baseline_success=True) == "g3"


def test_g4_broken_is_null_only():
    # Not success and NULL (not rescued at any multiplier) -> G4.
    assert groups.classify_drill_in(False, None, baseline_success=True) == "g4"


def test_g3_at_and_above_ceiling_is_still_g3():
    # The 10x ceiling and anything above it are both G3 (classifier is >1-keyed).
    assert (
        groups.classify_drill_in(False, groups.TOP_MULTIPLIER, baseline_success=True)
        == "g3"
    )
    assert (
        groups.classify_drill_in(
            False, groups.TOP_MULTIPLIER + 1e-9, baseline_success=True
        )
        == "g3"
    )


def test_g2_g3_boundary_at_one():
    # min_mult exactly 1 with failure should not happen (biconditional), but the
    # predicate must still route a failing min_mult<=1 row deterministically: it
    # falls to G4 (not G3, since not >1), consistent with "not rescued by a bump".
    assert groups.classify_drill_in(False, 1.0, baseline_success=True) == "g4"
    assert groups.classify_drill_in(False, 0.5, baseline_success=True) == "g4"


def test_baseline_failures_are_already_failing():
    # A failed baseline replay -> Already failing, regardless of schedule outcome
    # or min_mult. This is what carves baseline failures out of Potentially broken.
    for ss in (True, False):
        for mm in (None, 0.1, 1.0, 1.5, 8.0, 9.0):
            assert groups.classify_drill_in(ss, mm, baseline_success=False) == "af"


def test_every_drill_in_is_af_g2_g3_or_g4():
    for bs in (True, False):
        for ss in (True, False):
            for mm in (None, 0.1, 1.0, 1.5, 8.0, 9.0):
                assert groups.classify_drill_in(ss, mm, baseline_success=bs) in (
                    "af",
                    "g2",
                    "g3",
                    "g4",
                )


# --- block_group_counts: per-block partition assembly + identity --------------


def test_block_group_counts_non_truncated_closes_to_tx_count():
    # Example mirrors verification-findings block 24320002 with 2 baseline-fail
    # drill-ins split off into Already failing:
    # 256 + 166(g2) + 122(g3) + 3(g4) + 2(af) = 549 == tx_count, G5 == 0.
    out = groups.block_group_counts(
        tx_count=549,
        tx_count_unchanged=256,
        tx_count_gas_only=165,
        g2_drillin=1,
        g3=122,
        g4=3,
        af=2,
    )
    assert out["g1"] == 256
    assert out["g2"] == 166  # 165 + 1
    assert out["g3"] == 122
    assert out["g4"] == 3
    assert out["af"] == 2
    assert out["g5"] == 0
    assert sum(out[k] for k in ("g1", "g2", "g3", "g4", "af", "g5")) == out["tx_count"]


def test_block_group_counts_truncated_g5_positive():
    # A truncated block: retained drill-ins account for fewer than stored, so the
    # dropped drill-ins show up as G5 (Unknown).
    out = groups.block_group_counts(
        tx_count=1000,
        tx_count_unchanged=600,
        tx_count_gas_only=19,
        g2_drillin=100,
        g3=150,
        g4=45,
        af=10,
    )
    # g1+g2+g3+g4+af = 600 + 119 + 150 + 45 + 10 = 924; g5 = 76
    assert out["g5"] == 76
    assert out["g5"] > 0
    assert sum(out[k] for k in ("g1", "g2", "g3", "g4", "af", "g5")) == out["tx_count"]


def test_partition_identity_always_holds():
    # For any inputs, g1+g2+g3+g4+af+g5 == tx_count by construction.
    out = groups.block_group_counts(
        tx_count=12345,
        tx_count_unchanged=10000,
        tx_count_gas_only=1000,
        g2_drillin=200,
        g3=300,
        g4=400,
        af=100,
    )
    assert sum(out[k] for k in ("g1", "g2", "g3", "g4", "af", "g5")) == 12345


# --- SQL fragment sanity (string predicates exist + clamp encoded) ------------


def test_sql_fragments_present_and_clamped():
    # The changed groups are all gated on a working baseline.
    assert "baseline_success = true" in groups.G2_DRILLIN_PREDICATE
    assert "schedule_success = true" in groups.G2_DRILLIN_PREDICATE
    assert "baseline_success = true" in groups.G3_PREDICATE
    # G3 is open-ended above 1 — no upper clamp on min_mult.
    assert "min_multiplier_to_succeed > 1" in groups.G3_PREDICATE
    assert f"<= {groups.TOP_MULTIPLIER}" not in groups.G3_PREDICATE
    # G4 is exactly the not-rescued-at-ceiling cohort (min_mult IS NULL), no clamp.
    assert "baseline_success = true" in groups.G4_PREDICATE
    assert "min_multiplier_to_succeed IS NULL" in groups.G4_PREDICATE
    assert f"> {groups.TOP_MULTIPLIER}" not in groups.G4_PREDICATE


def test_top_multiplier_is_ten():
    # The real sweep ceiling is 10x (the manifest's [1,2,4,8] is wrong).
    assert groups.TOP_MULTIPLIER == 10


def test_g4_fixability_subpredicates():
    # The G4 fixability split keys on replay_halt_oog (the 10x-ceiling halt kind),
    # the authoritative "does more gas help" signal — not the oog_* halt-site
    # columns. Both sub-predicates narrow G4 (min_mult IS NULL) and are disjoint.
    for p in (groups.G4_NOT_GAS_FIXABLE_PREDICATE, groups.G4_STILL_OOG_PREDICATE):
        assert "min_multiplier_to_succeed IS NULL" in p
        assert "baseline_success = true" in p
    assert "replay_halt_oog = false" in groups.G4_NOT_GAS_FIXABLE_PREDICATE
    assert "replay_halt_oog = true" in groups.G4_STILL_OOG_PREDICATE
    # Already failing is exactly the baseline-failure carve-out.
    assert groups.AF_PREDICATE == "baseline_success = false"
    # divergence count fragment exposes the five count columns + dedup guard.
    frag = groups.divergence_group_counts_sql()
    for col in ("g2_drillin", "g3", "g4", "af", "rescues", "dup_rows"):
        assert col in frag
