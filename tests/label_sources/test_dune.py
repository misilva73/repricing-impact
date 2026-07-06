"""Offline tests for the Dune label fetcher (expansion plan §5).

These exercise the pure :func:`~repricing_impact.label_sources.dune.normalize`
core and a parquet round-trip only — no network and no ``dune-client`` import at
collection time (the SDK is lazily imported inside ``fetch`` and never touched
here).
"""

from __future__ import annotations

from repricing_impact.label_sources import dune
from repricing_impact.label_sources.schema import (
    Category,
    Confidence,
    Source,
    read_contract_parquet,
)

#: Synthetic raw rows mirroring the REAL live ``labels.addresses`` shape (verified
#: 2026-07): columns ``address, name, category, contributor, source``, where
#: ``category`` is the table's own tag domain (``contracts``, ``infrastructure``,
#: ``nft``, ``social`` …) and ``source`` is ``query`` / ``static``. The rows here
#: are a contract-identity label (uppercased address to check lowercasing) whose
#: ``contracts`` category is unmapped and folds to ``other``, a mapped
#: ``infrastructure`` label, an unmapped ``social`` label folding to ``other``
#: with no contributor, and an address-less row that must be skipped.
RAW_ROWS = [
    {
        "address": "0xDAC17F958D2ee523a2206206994597C13D831ec7",
        "name": "Tether: Tether_USD",
        "category": "contracts",  # not in DUNE_CATEGORY_MAP -> other
        "contributor": "soispoke",
        "source": "query",
    },
    {
        "address": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
        "name": "Flashbots User",
        "category": "infrastructure",
        "contributor": "hildobby",
        "source": "query",
    },
    {
        "address": "0x1111111111111111111111111111111111111111",
        "name": "some.eth",
        "category": "social",  # not in DUNE_CATEGORY_MAP -> other
        "contributor": None,
        "source": "query",
    },
    {
        "address": None,  # skipped
        "name": "no address",
        "category": "contracts",
        "contributor": "x",
        "source": "query",
    },
]


def test_normalize_maps_rows_and_skips_addressless():
    records = dune.normalize(RAW_ROWS)

    # The address-less row is dropped.
    assert len(records) == 3

    by_addr = {r.address: r for r in records}

    # Every record is a high-confidence Dune label.
    for rec in records:
        assert rec.source == Source.DUNE.value
        assert rec.confidence == Confidence.HIGH.value

    # Address is lowercased; the unmapped `contracts` category folds to `other`;
    # label taken from name; owner_project from the "Project: Contract" name
    # prefix (NOT the contributor, which is the Dune dashboard author).
    usdt = by_addr["0xdac17f958d2ee523a2206206994597c13d831ec7"]
    assert usdt.category == Category.OTHER.value
    assert usdt.label == "Tether: Tether_USD"
    assert usdt.owner_project == "tether"

    infra = by_addr["0x7a250d5630b4cf539739df2c5dacb4c659f2488d"]
    assert infra.category == Category.INFRA.value
    # "Flashbots User" has no "Project:" prefix -> no owner_project.
    assert infra.owner_project is None

    # Unmapped Dune category folds to `other`; a colon-less name yields no
    # owner_project (contributor is deliberately ignored).
    other = by_addr["0x1111111111111111111111111111111111111111"]
    assert other.category == Category.OTHER.value
    assert other.owner_project is None


def test_normalize_empty():
    assert dune.normalize([]) == []


def test_parquet_round_trip_preserves_fields(tmp_path):
    records = dune.normalize(RAW_ROWS)

    # Write via the same helper the fetcher uses, then read back.
    from repricing_impact.label_sources.schema import write_contract_parquet

    out = tmp_path / dune.CACHE_FILENAME
    write_contract_parquet(records, out)

    loaded = read_contract_parquet(out)
    assert len(loaded) == len(records)

    orig = {r.address: r for r in records}
    for rec in loaded:
        src = orig[rec.address]
        assert rec.label == src.label
        assert rec.category == src.category
        assert rec.owner_project == src.owner_project
        assert rec.source == Source.DUNE.value
        assert rec.confidence == Confidence.HIGH.value
