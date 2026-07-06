"""Merge per-source label caches into one resolved ``contract_labels.parquet``.

Reads whichever per-source parquet files exist under ``label_cache/`` (missing
sources are simply skipped), layers the curated manual overrides from
:data:`repricing_impact.labels.ADDRESS_PROJECT_LABELS` on top, resolves conflicts
on the same address by the fixed precedence in :data:`schema.SOURCE_PRECEDENCE`
(plan §4.3), applies the MEV *overlay* (behavioural, not a category replacement),
and writes the merged file.

Refresh cadence is manual (plan §4.2): run the source fetchers, then

    python -m repricing_impact.label_sources.build

before ``scripts/precompute.py``. With no source caches present this still
produces a valid merged file containing just the manual overrides, so a fresh
checkout / CI keeps working.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Optional

from ..config import LABEL_CACHE
from .schema import (
    Category,
    Confidence,
    LabelRecord,
    SOURCE_CONFIDENCE,
    SOURCE_PRECEDENCE,
    Source,
    read_contract_parquet,
    write_contract_parquet,
)

#: Per-source cache filenames merged into the resolved file, in the order the
#: fetchers write them. Only files that exist are read.
SOURCE_FILES: Dict[str, str] = {
    Source.OLI.value: "oli.parquet",
    Source.DUNE.value: "dune.parquet",
    Source.ETHLISTS.value: "ethlists.parquet",
    Source.MEV.value: "mev_bots.parquet",
    Source.HEURISTIC.value: "heuristic.parquet",
    # Warehouse-native structural/upgradability source. Its records carry
    # ``source="heuristic"`` (same low-confidence, last-resort tier); this is a
    # distinct *file* key only — ``load_source_records`` reads every value here.
    "xatu_structural": "xatu_structural.parquet",
}

MERGED_FILENAME = "contract_labels.parquet"

# Precedence rank: lower wins. Sources not listed sort last.
_RANK = {src: i for i, src in enumerate(SOURCE_PRECEDENCE)}


def _rank(source: str) -> int:
    return _RANK.get(source, len(_RANK))


def load_source_records(cache_dir: Path | str = LABEL_CACHE) -> List[LabelRecord]:
    """Load every per-source cache that exists under ``cache_dir``."""
    cache_dir = Path(cache_dir)
    records: List[LabelRecord] = []
    for filename in SOURCE_FILES.values():
        path = cache_dir / filename
        if path.exists():
            records.extend(read_contract_parquet(path))
    return records


def _compose(records: List[LabelRecord]) -> LabelRecord:
    """Fold same-address records into one by **field-level** precedence.

    ``label`` is additive with the name (plan §1): the curated manual layer wins
    the *display name* even when it carries no category, while ``category`` /
    ``owner_project`` / structural tags fill from the highest-precedence source
    that actually provides them. This way a manual name and a Dune/OLI category
    coexist instead of the name-only manual record blanking the taxonomy.

    ``source`` / ``confidence`` reflect the record that won the display name, so
    the dashboard can style a heuristic name differently from an attested one.
    """
    ordered = sorted(records, key=lambda r: _rank(r.source))
    addr = ordered[0].address

    def first(pred):
        for r in ordered:
            v = pred(r)
            if v is not None:
                return v
        return None

    name_winner = next((r for r in ordered if r.label and r.label != addr), ordered[0])
    category = first(
        lambda r: r.category if r.category != Category.UNKNOWN.value else None
    )
    return LabelRecord(
        address=addr,
        label=name_winner.label or addr,
        category=category or Category.UNKNOWN.value,
        owner_project=first(lambda r: r.owner_project or None),
        source=name_winner.source,
        confidence=name_winner.confidence,
        is_proxy=first(lambda r: r.is_proxy),
        is_factory=first(lambda r: r.is_factory),
        is_safe=first(lambda r: r.is_safe),
        erc_type=first(lambda r: r.erc_type or None),
        is_upgradable=first(lambda r: r.is_upgradable),
        upgrade_mechanism=first(lambda r: r.upgrade_mechanism or None),
        upgrade_admin=first(lambda r: r.upgrade_admin or None),
    )


def merge_records(
    source_records: List[LabelRecord],
    manual_records: Optional[List[LabelRecord]] = None,
) -> List[LabelRecord]:
    """Resolve many per-source records into one per address (plan §4.3).

    Precedence: manual > OLI > Dune > ethereum-lists > heuristic > unknown,
    applied field-by-field (see :func:`_compose`). MEV records never win the
    ``category`` slot outright — they fold in as a behavioural overlay
    (``is_mev_bot`` / ``mev_role``); the taxonomy category only becomes
    ``mev_bot`` when nothing else classified the address.
    """
    if manual_records is None:
        # Imported lazily to avoid a labels -> build import cycle at module load.
        from ..labels import manual_label_records

        manual_records = manual_label_records()

    # Split behavioural MEV overlay from the taxonomy-owning sources.
    mev_by_addr: Dict[str, LabelRecord] = {}
    by_addr: Dict[str, List[LabelRecord]] = {}
    for rec in list(manual_records) + list(source_records):
        if rec.source == Source.MEV.value:
            mev_by_addr.setdefault(rec.address, rec)  # first-seen MEV flag
            continue
        by_addr.setdefault(rec.address, []).append(rec)

    winners: Dict[str, LabelRecord] = {
        addr: _compose(recs) for addr, recs in by_addr.items()
    }

    # Fold the MEV overlay onto the composed record (or synthesise one).
    for addr, mev in mev_by_addr.items():
        winner = winners.get(addr)
        if winner is None:
            winner = LabelRecord(
                address=addr,
                label=mev.label or addr,
                category=Category.MEV_BOT.value,
                owner_project=mev.owner_project,
                source=Source.MEV.value,
                confidence=SOURCE_CONFIDENCE[Source.MEV.value],
            )
            winners[addr] = winner
        winner.is_mev_bot = True
        winner.mev_role = mev.mev_role
        # Behavioural class only claims the taxonomy slot when nothing else did.
        if winner.category == Category.UNKNOWN.value:
            winner.category = Category.MEV_BOT.value

    return sorted(winners.values(), key=lambda r: r.address)


def build(
    cache_dir: Path | str = LABEL_CACHE,
    out_path: Optional[Path | str] = None,
) -> Path:
    """Merge all source caches + manual overrides → ``contract_labels.parquet``."""
    cache_dir = Path(cache_dir)
    out_path = Path(out_path) if out_path else cache_dir / MERGED_FILENAME
    merged = merge_records(load_source_records(cache_dir))
    write_contract_parquet(merged, out_path)
    return out_path


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=LABEL_CACHE,
        help="directory holding per-source parquet caches (default: label_cache/)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="merged output path (default: <cache-dir>/contract_labels.parquet)",
    )
    args = parser.parse_args(argv)
    out = build(cache_dir=args.cache_dir, out_path=args.out)
    records = read_contract_parquet(out)
    print(f"merged {len(records)} labels -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
