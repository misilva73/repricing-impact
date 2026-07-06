"""Offline unit tests for the OLI label-source fetcher (plan §2, §3).

Exercises the pure :func:`repricing_impact.label_sources.oli.normalize` mapping
(plus the :func:`~repricing_impact.label_sources.oli._collapse` merge) with
synthetic rows and a parquet round-trip. No network access.

The rows below are the *collapsed* shape :func:`oli.fetch` produces from the live
REST pool: one flat dict per address keyed by OLI tag ids (``contract_name``,
``owner_project``, ``usage_category``, ``is_proxy`` / ``is_factory_contract`` /
``is_safe_contract`` / ``is_paymaster`` / ``erc_type``). ``_collapse`` already
flattens the pool's ``tags_json`` objects (and the ``erc_type`` array) into this
shape, so ``normalize`` only sees flat scalars.
"""

from __future__ import annotations

from pathlib import Path

from repricing_impact.label_sources.oli import (
    CACHE_FILENAME,
    _collapse,
    normalize,
    refresh,
)
from repricing_impact.label_sources.schema import (
    Category,
    Confidence,
    Source,
    read_contract_parquet,
)

# Synthetic collapsed OLI rows covering each mapping branch (plan §3).
RAW_ROWS = [
    {
        "address": "0xAAAA000000000000000000000000000000000001",
        "contract_name": "Aave Lending Pool",
        "owner_project": "aave",
        "usage_category": "lending",
    },
    {
        "address": "0xBBBB000000000000000000000000000000000002",
        "contract_name": "Uniswap Pool",
        "owner_project": "uniswap",
        "usage_category": "dex",
    },
    {
        "address": "0xCCCC000000000000000000000000000000000003",
        "contract_name": "My Safe",
        "owner_project": "safe",
        "usage_category": "fungible_tokens",  # overridden by is_safe_contract
        "is_safe_contract": True,
    },
    {
        "address": "0xDDDD000000000000000000000000000000000004",
        "contract_name": "A Paymaster",
        "owner_project": "pimlico",
        "usage_category": "developer_tools",  # overridden by is_paymaster
        "is_paymaster": True,
    },
    {
        "address": "0xEEEE000000000000000000000000000000000005",
        "contract_name": "Some Proxy Token",
        "owner_project": "acme",
        "usage_category": "fungible_tokens",
        "is_proxy": True,
        "erc_type": "erc20",
    },
    {
        "address": "0xFFFF000000000000000000000000000000000006",
        "contract_name": "Weird Thing",
        "owner_project": None,
        "usage_category": "gaming",  # off-taxonomy -> other
    },
    {
        # Missing address -> skipped.
        "address": None,
        "contract_name": "Ghost",
        "usage_category": "dex",
    },
]


def _by_addr(records):
    return {r.address: r for r in records}


def test_normalize_maps_categories_and_skips_missing_address():
    records = normalize(RAW_ROWS)

    # The address-less row is dropped.
    assert len(records) == 6
    by_addr = _by_addr(records)

    lending = by_addr["0xaaaa000000000000000000000000000000000001"]
    assert lending.category == Category.DEFI_COMPLEX.value
    assert lending.label == "Aave Lending Pool"
    assert lending.owner_project == "aave"
    assert lending.source == Source.OLI.value
    assert lending.confidence == Confidence.HIGH.value

    dex = by_addr["0xbbbb000000000000000000000000000000000002"]
    assert dex.category == Category.SWAP_DEX.value

    safe = by_addr["0xcccc000000000000000000000000000000000003"]
    assert safe.category == Category.WALLET_SAFE.value
    assert safe.is_safe is True

    paymaster = by_addr["0xdddd000000000000000000000000000000000004"]
    assert paymaster.category == Category.ACCOUNT_ABSTRACTION.value

    other = by_addr["0xffff000000000000000000000000000000000006"]
    assert other.category == Category.OTHER.value


def test_normalize_preserves_structural_tags():
    by_addr = _by_addr(normalize(RAW_ROWS))
    proxy = by_addr["0xeeee000000000000000000000000000000000005"]
    assert proxy.is_proxy is True
    assert proxy.erc_type == "erc20"
    assert proxy.category == Category.TOKEN.value


def test_never_emits_mev_bot():
    for rec in normalize(RAW_ROWS):
        assert rec.category != Category.MEV_BOT.value


def test_erc4337_usage_category_maps_to_account_abstraction():
    records = normalize(
        [
            {
                "address": "0x1111000000000000000000000000000000000011",
                "name": "EntryPoint",
                "usage_category": "erc4337",
            }
        ]
    )
    assert records[0].category == Category.ACCOUNT_ABSTRACTION.value


def test_parquet_round_trip_preserves_tags(tmp_path: Path):
    out = refresh_from_rows(tmp_path, RAW_ROWS)
    assert out == tmp_path / CACHE_FILENAME
    assert out.exists()

    round_tripped = _by_addr(read_contract_parquet(out))
    proxy = round_tripped["0xeeee000000000000000000000000000000000005"]
    assert proxy.is_proxy is True
    assert proxy.erc_type == "erc20"

    safe = round_tripped["0xcccc000000000000000000000000000000000003"]
    assert safe.is_safe is True
    assert safe.category == Category.WALLET_SAFE.value


def test_collapse_merges_attestation_envelopes():
    """`_collapse` folds the live pool's per-attestation `tags_json` into one row.

    Live attestations are partial and split across attesters; some are revoked.
    Non-revoked attestations win over revoked ones, newer wins per field, and the
    `erc_type` array is flattened to a scalar — the flat shape `normalize` reads.
    """
    recipient = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    attestations = [
        {
            "recipient": recipient,
            "revoked": True,
            "time": "2025-09-19T14:41:21Z",
            "tags_json": {"contract_name": "STALE"},  # revoked -> overwritten
        },
        {
            "recipient": recipient,
            "revoked": False,
            "time": "2025-09-18T13:26:05Z",
            "tags_json": {
                "contract_name": "USDT",
                "owner_project": "tetherto",
                "usage_category": "stablecoin",
                "is_contract": True,  # unmapped tag ignored
            },
        },
        {
            "recipient": recipient,
            "revoked": False,
            "time": "2025-06-15T01:13:24Z",
            "tags_json": {"erc_type": ["erc20"]},  # array -> scalar
        },
    ]

    row = _collapse(recipient, attestations)
    assert row["address"] == recipient
    assert row["contract_name"] == "USDT"  # live beats revoked "STALE"
    assert row["owner_project"] == "tetherto"
    assert row["usage_category"] == "stablecoin"
    assert row["erc_type"] == "erc20"
    assert "is_contract" not in row  # only mapped tag ids carried through

    (record,) = normalize([row])
    assert record.label == "USDT"
    assert record.category == Category.STABLECOIN.value
    assert record.erc_type == "erc20"


def refresh_from_rows(cache_dir: Path, rows) -> Path:
    """Run ``refresh`` with ``fetch`` monkeypatched to synthetic rows (no network)."""
    import repricing_impact.label_sources.oli as oli

    original_fetch = oli.fetch
    try:
        oli.fetch = lambda **kwargs: rows  # type: ignore[assignment]
        return refresh(cache_dir=cache_dir)
    finally:
        oli.fetch = original_fetch
