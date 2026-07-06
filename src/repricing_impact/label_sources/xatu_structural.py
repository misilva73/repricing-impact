"""Xatu-backed structural / upgradability label source (expansion plan §4.1).

A **warehouse-native** alternative to the JSON-RPC :mod:`heuristics` probe. Both
derive the same structural descriptor (proxy / factory / ERC type) *and* the
upgradability verdict (``is_upgradable`` / ``upgrade_mechanism`` /
``upgrade_admin``) — the only difference is where the raw bytecode and storage
come from. Instead of ``eth_getCode`` / ``eth_getStorageAt`` against an external
node, this reads them out of Xatu, the **same ClickHouse host** the rest of the
pipeline already uses:

- **EIP-1967 slots** — ``canonical_execution_storage_reads`` /
  ``canonical_execution_storage_diffs`` carry ``(contract_address, slot, value)``.
  A proxy ``SLOAD``\\s its implementation slot on *every* delegated call, so any
  active proxy in the window has its impl-slot value captured; the admin slot is
  usually only *written* (at deploy/upgrade), so we union reads with diffs.
- **Deployed bytecode** — ``canonical_execution_contracts.code`` holds the
  runtime code of contracts deployed *within the queried range*, which feeds the
  EIP-1167 clone match and the UUPS / diamond selector scan.

The read is the **sanctioned cross-source exception** (see AGENTS.md /
``docs/warehouse.md``): bounded to the pinned block range **and** the specific
addresses, read-only, off the request path — never a full scan. It reuses
:func:`heuristics.classify`, so the detectors (and their tests) are the single
source of truth; this module is only an *input adapter*.

Coverage notes (honest limits):

- ``code`` exists only for contracts **deployed inside** ``[block_start,
  block_end]``. An older contract keeps ``code_hex=None`` and is classified from
  its storage slots alone — which is exactly the signal that covers long-lived
  proxies (they're detected when *called*, not when deployed).
- ``supportsInterface`` results are not obtainable from Xatu, so diamonds are
  detected via the ``diamondCut`` selector embedded in ``code`` rather than the
  DiamondLoupe ERC-165 id. A diamond with no in-range ``code`` row is missed.
- EIP-1167 clones deployed *before* the window are missed (no ``code`` row, and
  clones use no EIP-1967 slot) — they fall through to unknown rather than being
  mislabeled upgradable, so the important invariant still holds.

Only :func:`fetch_structural_inputs` (and :func:`refresh`, which drives it)
touches the warehouse; :func:`classify_inputs` is a pure mapping the offline unit
tests exercise. ``run_query`` is imported lazily so importing this module never
requires the DB driver / credentials.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import LABEL_CACHE
from .heuristics import EIP1967_SLOTS, classify
from .schema import LabelRecord, read_contract_parquet, write_contract_parquet

#: Cache filename this fetcher writes (merged by ``build.py``).
CACHE_FILENAME = "xatu_structural.parquet"

#: Xatu tables read (all on the ``default`` database, the pipeline's host).
CONTRACTS_TABLE = "default.canonical_execution_contracts"
STORAGE_READS_TABLE = "default.canonical_execution_storage_reads"
STORAGE_DIFFS_TABLE = "default.canonical_execution_storage_diffs"

#: Xatu network selector — the same cohort as ``chain_id = 1`` on gas_analysis.
NETWORK_NAME = "mainnet"


def _sql_str_list(values: Iterable[str]) -> str:
    """Render an iterable of strings as a SQL ``IN (...)`` literal list."""
    return ", ".join("'" + str(v) + "'" for v in values)


def classify_inputs(inputs: Dict[str, Dict[str, Any]]) -> List[LabelRecord]:
    """Classify prefetched per-address structural inputs (pure; offline-testable).

    ``inputs`` maps a lowercased address to ``{"code_hex": str|None, "storage":
    {slot: value}}``. Delegates to :func:`heuristics.classify`, so the structural
    tags and the upgradability verdict come from the exact same detectors as the
    RPC path — this module only changes where the inputs originate.
    ``supportsInterface`` is unavailable from Xatu, so it is passed as ``None``
    (diamonds are then detected via the ``diamondCut`` selector in ``code_hex``).
    """
    records: List[LabelRecord] = []
    for address, raw in inputs.items():
        records.append(
            classify(
                address=address,
                code_hex=raw.get("code_hex"),
                storage=raw.get("storage") or {},
                supports_interface_results=None,
            )
        )
    return records


def fetch_structural_inputs(
    addresses: List[str],
    block_start: int,
    block_end: int,
    engine: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Read EIP-1967 slot values + deployed bytecode for ``addresses`` from Xatu.

    Bounded, read-only cross-source read (sanctioned exception): filtered to the
    pinned ``[block_start, block_end]`` range **and** the given addresses, never a
    full scan. Returns ``{lowercased addr: {"code_hex", "storage"}}`` for every
    input address (fields empty when the warehouse observed nothing). This is the
    only warehouse-touching function; ``run_query`` is imported lazily.
    """
    if not addresses:
        return {}
    from ..clickhouse import run_query  # lazy — only needed to refresh the cache

    addrs = sorted({a.lower() for a in addresses})
    addr_list = _sql_str_list(addrs)
    slot_list = _sql_str_list(EIP1967_SLOTS.values())
    inputs: Dict[str, Dict[str, Any]] = {
        a: {"code_hex": None, "storage": {}} for a in addrs
    }

    # 1) EIP-1967 slot values — latest observed per (address, slot). Reads catch
    #    the impl/beacon slots a live proxy SLOADs on every call; diffs catch the
    #    admin slot, which is usually only written (deploy/upgrade). Union both.
    slot_query = f"""
        SELECT addr, slot, argMax(value, bn) AS value FROM (
            SELECT lower(contract_address) AS addr, slot, value, block_number AS bn
            FROM {STORAGE_READS_TABLE}
            WHERE meta_network_name = '{NETWORK_NAME}'
              AND block_number BETWEEN {block_start} AND {block_end}
              AND lower(contract_address) IN ({addr_list})
              AND slot IN ({slot_list})
            UNION ALL
            SELECT lower(address) AS addr, slot, to_value AS value, block_number AS bn
            FROM {STORAGE_DIFFS_TABLE}
            WHERE meta_network_name = '{NETWORK_NAME}'
              AND block_number BETWEEN {block_start} AND {block_end}
              AND lower(address) IN ({addr_list})
              AND slot IN ({slot_list})
        )
        GROUP BY addr, slot
    """
    for _, row in run_query(slot_query, engine).iterrows():
        addr = str(row["addr"])
        if addr in inputs:
            inputs[addr]["storage"][str(row["slot"])] = str(row["value"])

    # 2) Deployed runtime bytecode — for the EIP-1167 clone match and the UUPS /
    #    diamond selector scan. Only contracts deployed in-range have a row; older
    #    ones keep code_hex=None and rely on the slot signal above.
    code_query = f"""
        SELECT lower(contract_address) AS addr, argMax(code, block_number) AS code
        FROM {CONTRACTS_TABLE}
        WHERE meta_network_name = '{NETWORK_NAME}'
          AND block_number BETWEEN {block_start} AND {block_end}
          AND lower(contract_address) IN ({addr_list})
        GROUP BY addr
    """
    for _, row in run_query(code_query, engine).iterrows():
        addr = str(row["addr"])
        code = row["code"]
        if addr in inputs and isinstance(code, str) and code:
            inputs[addr]["code_hex"] = code

    return inputs


def refresh(
    addresses: List[str],
    block_start: int,
    block_end: int,
    cache_dir: Path | str = LABEL_CACHE,
    engine: Optional[Any] = None,
) -> Path:
    """Fetch + classify + write ``xatu_structural.parquet``.

    Warehouse-touching (drives :func:`fetch_structural_inputs`); not exercised by
    the offline tests. Returns the written cache path.
    """
    inputs = fetch_structural_inputs(addresses, block_start, block_end, engine=engine)
    records = classify_inputs(inputs)
    return write_contract_parquet(records, Path(cache_dir) / CACHE_FILENAME)


def _resolve_block_range(
    block_start: Optional[int], block_end: Optional[int]
) -> tuple[int, int]:
    """Return the block range, falling back to the pinned config's common range."""
    if block_start is not None and block_end is not None:
        return block_start, block_end
    from ..config import resolve_config_hash

    rng = resolve_config_hash().common_block_range
    if not rng:
        raise ValueError(
            "no --block-start/--block-end given and the pinned config exposes no "
            "common block range to fall back to"
        )
    return int(rng[0]), int(rng[1])


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addresses",
        nargs="+",
        default=None,
        help="contract addresses (0x…) to classify",
    )
    parser.add_argument(
        "--addresses-file",
        type=Path,
        default=None,
        help="file of 0x addresses (one per line) to classify",
    )
    parser.add_argument("--block-start", type=int, default=None)
    parser.add_argument("--block-end", type=int, default=None)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=LABEL_CACHE,
        help="directory to write xatu_structural.parquet into (default: label_cache/)",
    )
    args = parser.parse_args(argv)

    addresses: List[str] = list(args.addresses or [])
    if args.addresses_file is not None:
        addresses += [
            line.strip()
            for line in args.addresses_file.read_text().splitlines()
            if line.strip()
        ]
    if not addresses:
        parser.error("provide --addresses and/or --addresses-file")

    block_start, block_end = _resolve_block_range(args.block_start, args.block_end)
    out = refresh(
        addresses=addresses,
        block_start=block_start,
        block_end=block_end,
        cache_dir=args.cache_dir,
    )
    records = read_contract_parquet(out)
    upgradable = sum(1 for r in records if r.is_upgradable)
    print(
        f"xatu_structural: classified {len(records)} addrs "
        f"({upgradable} upgradable) over blocks {block_start}–{block_end} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
