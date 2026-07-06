"""Schema-contract tests: record validation, taxonomy maps, parquet I/O."""

import pytest

from repricing_impact.label_sources import schema as s


def test_address_is_lowercased():
    rec = s.LabelRecord(address="0xDAC17F958D2ee523a2206206994597C13D831ec7", label="x")
    assert rec.address == "0xdac17f958d2ee523a2206206994597c13d831ec7"


def test_invalid_category_raises():
    with pytest.raises(ValueError):
        s.LabelRecord(address="0xabc", label="x", category="not_a_category")


def test_map_oli_category():
    assert s.map_oli_category("lending") == s.Category.DEFI_COMPLEX.value
    assert s.map_oli_category("dex") == s.Category.SWAP_DEX.value
    assert s.map_oli_category("erc4337") == s.Category.ACCOUNT_ABSTRACTION.value
    # Unmapped-but-present folds to `other`; empty/None to `unknown`.
    assert s.map_oli_category("gaming") == s.Category.OTHER.value
    assert s.map_oli_category(None) == s.Category.UNKNOWN.value
    assert s.map_oli_category("") == s.Category.UNKNOWN.value


def test_map_dune_category():
    assert s.map_dune_category("cex") == s.Category.CEX.value
    assert s.map_dune_category("BRIDGE") == s.Category.BRIDGE.value  # case-insensitive
    assert s.map_dune_category("weird") == s.Category.OTHER.value
    assert s.map_dune_category(None) == s.Category.UNKNOWN.value


def test_is_precompile():
    assert s.is_precompile("0x0000000000000000000000000000000000000001")
    assert s.is_precompile("0x000000000000000000000000000000000000000A")  # 0x0a, upper
    assert not s.is_precompile("0x000000000000000000000000000000000000000b")
    assert not s.is_precompile("0xdac17f958d2ee523a2206206994597c13d831ec7")
    assert not s.is_precompile(None)


def test_parquet_round_trip(tmp_path):
    recs = [
        s.LabelRecord(
            address="0xAbc",
            label="Foo",
            category="swap_dex",
            owner_project="foo",
            source="oli",
            confidence="high",
            is_proxy=True,
            erc_type="erc20",
        ),
        s.LabelRecord(
            address="0xdef",
            label="Bot",
            category="mev_bot",
            source="mev",
            confidence="medium",
            is_mev_bot=True,
            mev_role="arb",
        ),
    ]
    path = tmp_path / "x.parquet"
    s.write_contract_parquet(recs, path)
    back = {r.address: r for r in s.read_contract_parquet(path)}
    assert back["0xabc"].category == "swap_dex"
    assert back["0xabc"].is_proxy is True
    assert back["0xabc"].erc_type == "erc20"
    assert back["0xdef"].is_mev_bot is True
    assert back["0xdef"].mev_role == "arb"


def test_read_tolerates_older_schema_missing_columns(tmp_path):
    """A cache written before a field was added must still read (field -> None).

    Regression: adding columns to LabelRecord changed CONTRACT_COLUMNS, and the
    reader used to `SELECT` every column — raising a duckdb binder error on any
    pre-existing parquet and silently collapsing the whole label cache. The reader
    now backfills absent canonical columns as NULL.
    """
    import duckdb
    import pandas as pd

    # Simulate an old cache: the canonical columns MINUS the three upgrade fields.
    old_cols = [
        c
        for c in s.CONTRACT_COLUMNS
        if c not in ("is_upgradable", "upgrade_mechanism", "upgrade_admin")
    ]
    df = pd.DataFrame(
        [
            {
                **{c: None for c in old_cols},
                "address": "0xabc",
                "label": "Foo",
                "category": "swap_dex",
                "source": "oli",
                "confidence": "high",
                "is_proxy": True,
            }
        ],
        columns=old_cols,
    )
    path = tmp_path / "old.parquet"
    con = duckdb.connect()
    try:
        con.register("df", df)
        con.execute(
            f"COPY (SELECT {', '.join(old_cols)} FROM df) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()

    back = {r.address: r for r in s.read_contract_parquet(path)}
    rec = back["0xabc"]
    # Old fields preserved…
    assert rec.category == "swap_dex"
    assert rec.is_proxy is True
    # …and the newly-added columns backfill cleanly as None (never raising).
    assert rec.is_upgradable is None
    assert rec.upgrade_mechanism is None
    assert rec.upgrade_admin is None


def test_empty_parquet_round_trips(tmp_path):
    path = tmp_path / "empty.parquet"
    s.write_contract_parquet([], path)
    assert s.read_contract_parquet(path) == []


def test_selector_parquet_round_trip(tmp_path):
    rows = [
        {"selector": "0x38ed1739", "text_signature": "swap(...)", "source": "4byte"},
        {"selector": "0x38ed1739", "text_signature": "junk(bytes)", "source": "4byte"},
    ]
    path = tmp_path / "sel.parquet"
    s.write_selector_parquet(rows, path)
    df = s.read_selector_parquet(path)
    assert list(df.columns) == s.SELECTOR_COLUMNS
    assert len(df) == 2
