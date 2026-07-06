"""Merge/resolver tests: field-level precedence + MEV overlay (plan §4.3)."""

from repricing_impact.label_sources import build
from repricing_impact.label_sources import schema as s

MANUAL = [
    s.LabelRecord(
        address="0xaaa", label="Manual Name", source="manual", confidence="high"
    ),
]


def _rec(addr, label, category="unknown", source="dune", **kw):
    return s.LabelRecord(
        address=addr, label=label, category=category, source=source, **kw
    )


def test_manual_name_wins_but_category_fills_from_lower_source():
    # Manual (name-only) beats Dune on the display name, but the additive
    # category comes from Dune since manual left it unknown.
    merged = build.merge_records(
        [_rec("0xaaa", "Dune Name", category="stablecoin", source="dune")],
        manual_records=MANUAL,
    )
    rec = {r.address: r for r in merged}["0xaaa"]
    assert rec.label == "Manual Name"  # manual wins the name
    assert rec.category == "stablecoin"  # dune fills the category
    assert rec.source == "manual"  # provenance of the winning name


def test_oli_beats_dune():
    merged = build.merge_records(
        [
            _rec("0xbbb", "Dune", category="swap_dex", source="dune"),
            _rec("0xbbb", "OLI", category="defi_complex", source="oli"),
        ],
        manual_records=[],
    )
    rec = {r.address: r for r in merged}["0xbbb"]
    assert rec.label == "OLI"
    assert rec.category == "defi_complex"
    assert rec.source == "oli"


def test_full_precedence_order():
    # manual > oli > dune > ethlists > heuristic > unknown — name follows the top.
    recs = [
        _rec("0xccc", "unk", source="unknown"),
        _rec("0xccc", "heur", source="heuristic"),
        _rec("0xccc", "eth", source="ethlists"),
        _rec("0xccc", "dune", source="dune"),
        _rec("0xccc", "oli", source="oli"),
    ]
    merged = build.merge_records(recs, manual_records=[])
    assert {r.address: r for r in merged}["0xccc"].label == "oli"


def test_mev_overlay_keeps_taxonomy_category():
    # A swap_dex contract also flagged as an arb bot keeps swap_dex + gets overlay.
    merged = build.merge_records(
        [
            _rec("0xddd", "Router", category="swap_dex", source="oli"),
            s.LabelRecord(
                address="0xddd",
                label="bot",
                category="mev_bot",
                source="mev",
                confidence="medium",
                is_mev_bot=True,
                mev_role="arb",
            ),
        ],
        manual_records=[],
    )
    rec = {r.address: r for r in merged}["0xddd"]
    assert rec.category == "swap_dex"  # taxonomy preserved
    assert rec.is_mev_bot is True
    assert rec.mev_role == "arb"


def test_mev_claims_category_only_when_no_other():
    merged = build.merge_records(
        [
            s.LabelRecord(
                address="0xeee",
                label="bot",
                category="mev_bot",
                source="mev",
                confidence="medium",
                is_mev_bot=True,
                mev_role="sandwich",
            )
        ],
        manual_records=[],
    )
    rec = {r.address: r for r in merged}["0xeee"]
    assert rec.category == "mev_bot"
    assert rec.is_mev_bot is True


def test_build_end_to_end_reads_source_caches(tmp_path):
    # Write two per-source caches + let build() layer manual overrides on top.
    s.write_contract_parquet(
        [_rec("0xf01", "Dune USDT", category="stablecoin", source="dune")],
        tmp_path / "dune.parquet",
    )
    s.write_contract_parquet(
        [
            s.LabelRecord(
                address="0xf02",
                label="Safe",
                category="wallet_safe",
                source="oli",
                confidence="high",
                is_safe=True,
            )
        ],
        tmp_path / "oli.parquet",
    )
    out = build.build(cache_dir=tmp_path, out_path=tmp_path / "merged.parquet")
    by = {r.address: r for r in s.read_contract_parquet(out)}
    assert by["0xf01"].category == "stablecoin"
    assert by["0xf02"].is_safe is True
    # Manual overrides are always layered in even with no manual arg.
    assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in by  # Tether from manual map


def test_merge_with_no_sources_is_just_manual():
    merged = build.merge_records([], manual_records=MANUAL)
    assert {r.address: r for r in merged}["0xaaa"].label == "Manual Name"
