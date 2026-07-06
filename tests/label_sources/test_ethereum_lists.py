"""Offline tests for the ethereum-lists label fetcher (expansion plan §2, phase 3).

These exercise the pure filesystem-walking ``parse_contracts`` / ``parse_tokens``
core against a synthetic checkout tree in a tmp dir, plus a parquet round-trip —
no network and no ``git clone`` (that lives in ``clone_repos`` / ``refresh``,
which the tests never call).

The fixtures mirror the real live-repo JSON shape (verified 2026-07):

- ``contracts/1/<checksummed-addr>.json`` -> ``{"project", "name", "source"}``;
  the ``project`` slug is the attribution used as label + owner_project, with a
  ``name`` fallback for older files that lack a slug.
- ``tokens/eth/<checksummed-addr>.json`` -> ``{"symbol", "address", "decimals",
  "name", "type"?, ...}``; label is ``name`` (``symbol`` fallback), ``type`` maps
  to ``erc_type``, and there is no project attribution (owner_project stays None).

Filenames in both repos are checksummed (mixed-case) and must be lowercased.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from repricing_impact.label_sources import ethereum_lists
from repricing_impact.label_sources.schema import (
    Category,
    Confidence,
    Source,
    read_contract_parquet,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_fake_checkout(root: Path) -> tuple[Path, Path]:
    """Create a tiny ``contracts`` + ``tokens`` checkout under ``root``.

    Returns ``(contracts_repo, tokens_repo)`` mirroring what ``clone_repos``
    would produce.
    """
    contracts_repo = root / "contracts"
    tokens_repo = root / "tokens"

    contracts_dir = contracts_repo / "contracts" / "1"
    # Real shape: {"project", "name", "source"}. Checksummed (mixed-case)
    # filename -> address must be lowercased. `project` slug is the label.
    _write_json(
        contracts_dir / "0xAbC0000000000000000000000000000000000123.json",
        {"project": "uniswap", "name": "UniswapV2Router02", "source": "dune"},
    )
    # No `project` (older file); falls back to `name`.
    _write_json(
        contracts_dir / "0x0000000000000000000000000000000000000abc.json",
        {"name": "SomeProtocol"},
    )
    # No usable name -> skipped.
    _write_json(
        contracts_dir / "0x0000000000000000000000000000000000000dead.json",
        {"source": "dune"},
    )

    tokens_dir = tokens_repo / "tokens" / "eth"
    # Real shape: {"symbol", "address", "decimals", "name", "type"?, ...}.
    _write_json(
        tokens_dir / "0xDeF0000000000000000000000000000000000456.json",
        {
            "symbol": "DAI",
            "address": "0xDeF0000000000000000000000000000000000456",
            "decimals": 18,
            "name": "Dai Stablecoin",
            "type": "ERC20",
        },
    )
    # No `name`; falls back to `symbol`. No `type` -> erc_type stays None.
    _write_json(
        tokens_dir / "0x0000000000000000000000000000000000000fee.json",
        {"symbol": "FEE", "decimals": 6},
    )
    return contracts_repo, tokens_repo


def test_parse_contracts_uses_project_as_label_and_owner(tmp_path):
    contracts_repo, _ = _build_fake_checkout(tmp_path)
    records = ethereum_lists.parse_contracts(contracts_repo)

    # The name-less file is dropped; two usable records remain.
    assert len(records) == 2
    by_addr = {r.address: r for r in records}

    # Checksummed filename lowercased; `project` slug used as label + owner.
    uni = by_addr["0xabc0000000000000000000000000000000000123"]
    assert uni.label == "uniswap"
    assert uni.owner_project == "uniswap"
    # Project-level only: category left unknown for a higher source to fill.
    assert uni.category == Category.UNKNOWN.value
    assert uni.source == Source.ETHLISTS.value
    assert uni.confidence == Confidence.MEDIUM.value

    # `project` absent -> `name` fallback.
    other = by_addr["0x0000000000000000000000000000000000000abc"]
    assert other.label == "SomeProtocol"
    assert other.owner_project == "SomeProtocol"


def test_parse_tokens_are_token_category(tmp_path):
    _, tokens_repo = _build_fake_checkout(tmp_path)
    records = ethereum_lists.parse_tokens(tokens_repo)

    assert len(records) == 2
    by_addr = {r.address: r for r in records}

    dai = by_addr["0xdef0000000000000000000000000000000000456"]
    assert dai.label == "Dai Stablecoin"
    assert dai.category == Category.TOKEN.value
    assert dai.source == Source.ETHLISTS.value
    assert dai.confidence == Confidence.MEDIUM.value
    # `type` -> erc_type; tokens repo has no project attribution.
    assert dai.erc_type == "ERC20"
    assert dai.owner_project is None

    # `name` absent -> `symbol` fallback; no `type` -> erc_type None.
    fee = by_addr["0x0000000000000000000000000000000000000fee"]
    assert fee.label == "FEE"
    assert fee.category == Category.TOKEN.value
    assert fee.erc_type is None


def test_parse_missing_dirs_returns_empty(tmp_path):
    # Pointing at a bare dir (no contracts/1 or tokens/eth subtree) is safe.
    assert ethereum_lists.parse_contracts(tmp_path) == []
    assert ethereum_lists.parse_tokens(tmp_path) == []


def test_parquet_round_trip_merges_both(tmp_path):
    contracts_repo, tokens_repo = _build_fake_checkout(tmp_path)
    records = ethereum_lists.parse_contracts(
        contracts_repo
    ) + ethereum_lists.parse_tokens(tokens_repo)

    from repricing_impact.label_sources.schema import write_contract_parquet

    out = tmp_path / ethereum_lists.CACHE_FILENAME
    write_contract_parquet(records, out)

    loaded = read_contract_parquet(out)
    assert len(loaded) == len(records) == 4

    orig = {r.address: r for r in records}
    for rec in loaded:
        src = orig[rec.address]
        assert rec.label == src.label
        assert rec.category == src.category
        # A None owner_project round-trips through parquet as pandas NaN; treat
        # them as equivalent (the parquet layer, not this fetcher, owns that).
        loaded_owner = None if pd.isna(rec.owner_project) else rec.owner_project
        assert loaded_owner == src.owner_project
        assert rec.source == Source.ETHLISTS.value
        assert rec.confidence == Confidence.MEDIUM.value
