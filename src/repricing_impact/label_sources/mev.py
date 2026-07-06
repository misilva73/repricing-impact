"""MEV bot behavioural-overlay fetcher (expansion plan §7, §4.3).

Pulls MEV activity (arbitrage / sandwich / liquidation) from the ZeroMEV
``/v1/mevBlock`` API and normalises the **attacker/searcher** addresses into the
shared :class:`~repricing_impact.label_sources.schema.LabelRecord` contract,
writing a ``mev_bots.parquet`` per-source cache under ``label_cache/`` that
:mod:`repricing_impact.label_sources.build` folds in.

MEV is a **behavioural overlay, not a taxonomy category** (plan §4.3). Unlike the
registry sources (OLI / Dune / ethereum-lists), MEV never claims the ``category``
slot outright: every record here sets ``is_mev_bot=True`` with a ``mev_role`` in
``{"arb", "sandwich", "liquidation"}``, ``source="mev"``, ``confidence="medium"``
and ``category="mev_bot"``. ``build.merge_records`` then folds this onto whatever
taxonomy record already classified the address, only taking ``mev_bot`` as the
category when nothing else did. We just emit clean overlay records.

Live pulls use ``requests`` against :data:`config.ZEROMEV_API_URL`.
:func:`fetch_zeromev` is the only network-touching function; :func:`normalize`
is the pure, testable core and touches neither the network nor any credential.

**Real ZeroMEV schema (from its OpenAPI spec at ``data.zeromev.org/docs``).**
``GET /v1/mevBlock?block_number=<n>&count=<1..100>`` returns a **flat JSON array
of per-transaction rows**. Each row is one MEV transaction with (nullable)
``block_number``, ``tx_index``, ``mev_type``, ``protocol``, ``address_from``,
``address_to``, various USD/volume fields and ``imbalance``. The relevant
``mev_type`` values are ``sandwich``, ``backrun``, ``liquid``, ``arb``,
``frontrun``, ``swap``. **There is no dedicated searcher/frontrunner address
field** — the actor for a given transaction is always its ``address_from``.

**Victim-vs-attacker convention (plan §7 — the load-bearing rule).** ZeroMEV
models a sandwich as *separate* transaction rows: a ``frontrun`` row and a
``backrun`` row are the attacker's own txs, and one or more ``sandwich`` rows are
the **victim** transactions bracketed by them. So:

- ``frontrun`` / ``backrun`` -> the MEV bot is that row's ``address_from``
  (``sandwich`` role).
- ``sandwich`` -> the **victim**; its ``address_from`` must never be emitted, so
  we deliberately drop it (not in :data:`MEV_TYPE_TO_ROLE`).
- ``arb`` -> the arbitrageur's ``address_from`` (``arb`` role).
- ``liquid`` -> the liquidator's ``address_from`` (``liquidation`` role).
- ``swap`` -> non-MEV volume rows; dropped.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import List, Optional

from ..config import LABEL_CACHE, ZEROMEV_API_URL
from .schema import (
    Category,
    Confidence,
    LabelRecord,
    Source,
    write_contract_parquet,
)

#: Per-source cache filename (mirrors ``build.SOURCE_FILES[Source.MEV]``).
CACHE_FILENAME = "mev_bots.parquet"

#: ZeroMEV ``mev_type`` -> our ``mev_role`` (plan §7). The actor for each row is
#: its ``address_from``. ``sandwich`` (the victim) and ``swap`` (non-MEV volume)
#: are intentionally absent so their senders are never flagged as bots.
MEV_TYPE_TO_ROLE = {
    "arb": "arb",
    "frontrun": "sandwich",
    "backrun": "sandwich",
    "liquid": "liquidation",
}

#: ZeroMEV caps ``count`` at 100 blocks per ``/mevBlock`` request.
_MAX_COUNT = 100

#: ZeroMEV rate limit is 5 calls/sec; sleep a touch over 1/5s between pages.
_RATE_LIMIT_SLEEP_S = 0.25


def fetch_zeromev(
    block_start: Optional[int] = None,
    block_end: Optional[int] = None,
) -> List[dict]:
    """Live-pull MEV transaction rows from the ZeroMEV API (plan §7).

    Queries ``<ZEROMEV_API_URL>/mevBlock?block_number=<n>&count=<1..100>`` and
    returns the raw per-transaction rows as a ``list[dict]``. ``block_number`` is
    required by the API; ``count`` is the number of blocks to return starting at
    ``block_number`` (max 100). A range wider than 100 blocks is walked a page at
    a time, sleeping between pages to stay under the documented 5-calls/sec rate
    limit. Callers keep the range small — MEV scans are an infrequent, off-peak
    refresh, never a request path.

    When ``block_start`` is ``None`` no sensible query exists (the API requires
    ``block_number``), so an empty list is returned. This network path is
    intentionally *not* exercised by the offline test; only :func:`normalize` is.
    """
    import requests

    if block_start is None:
        return []

    last_block = block_end if block_end is not None else block_start
    if last_block < block_start:
        last_block = block_start

    rows: List[dict] = []
    cursor = block_start
    while cursor <= last_block:
        count = min(_MAX_COUNT, last_block - cursor + 1)
        resp = requests.get(
            f"{ZEROMEV_API_URL}/mevBlock",
            params={"block_number": cursor, "count": count},
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()

        # The documented shape is a bare JSON array; tolerate an envelope too.
        if isinstance(payload, dict):
            page = payload.get("data") or payload.get("rows") or []
        else:
            page = payload
        rows.extend(row for row in page if isinstance(row, dict))

        cursor += count
        if cursor <= last_block:
            time.sleep(_RATE_LIMIT_SLEEP_S)
    return rows


def _role_for(raw_type: Optional[str]) -> Optional[str]:
    """Map a raw ZeroMEV ``mev_type`` to our ``mev_role`` (or ``None`` to skip)."""
    if not isinstance(raw_type, str):
        return None
    return MEV_TYPE_TO_ROLE.get(raw_type.strip().lower())


def _attacker_address(row: dict) -> Optional[str]:
    """The attacker/searcher address for one raw MEV row: its ``address_from``.

    ZeroMEV rows carry the actor as ``address_from``. This is only ever called for
    rows whose ``mev_type`` mapped to a role (``arb`` / ``frontrun`` / ``backrun``
    / ``liquid``) — i.e. never for the ``sandwich`` victim row, whose sender we
    must not emit (plan §7). Returns a trimmed non-empty address, or ``None``.
    """
    addr = row.get("address_from")
    if isinstance(addr, str) and addr.strip():
        return addr.strip()
    return None


def normalize(raw_rows: List[dict]) -> List[LabelRecord]:
    """Map raw MEV rows to overlay :class:`LabelRecord`s (plan §7, §4.3).

    For each row we resolve a ``mev_role`` from ``mev_type`` and take the
    attacker/searcher address from ``address_from``. Critically, ``sandwich``
    rows (the **victim**) map to no role and are dropped, so a victim sender is
    never emitted; the bot is instead captured from the ``frontrun`` / ``backrun``
    rows. Every emitted record is a clean behavioural overlay: ``is_mev_bot=True``,
    ``source="mev"``, ``confidence="medium"``, ``category="mev_bot"`` with a
    synthetic ``"MEV bot (<role>)"`` label. Rows with an unmapped type or no
    ``address_from`` are skipped. This is the pure, network-free core.
    """
    records: List[LabelRecord] = []
    for row in raw_rows:
        role = _role_for(row.get("mev_type"))
        if role is None:
            continue
        address = _attacker_address(row)
        if not address:
            continue

        records.append(
            LabelRecord(
                address=address,
                label=f"MEV bot ({role})",
                category=Category.MEV_BOT.value,
                source=Source.MEV.value,
                confidence=Confidence.MEDIUM.value,
                is_mev_bot=True,
                mev_role=role,
            )
        )
    return records


def refresh(cache_dir: Path | str = LABEL_CACHE, **fetch_kwargs) -> Path:
    """Fetch + normalise + write the ``mev_bots.parquet`` cache; return its path.

    ``**fetch_kwargs`` are forwarded to :func:`fetch_zeromev` (``block_start`` /
    ``block_end``). Refresh cadence is manual (plan §4.2): run this, then
    ``python -m repricing_impact.label_sources.build`` before precompute.
    """
    raw_rows = fetch_zeromev(**fetch_kwargs)
    records = normalize(raw_rows)
    path = Path(cache_dir) / CACHE_FILENAME
    write_contract_parquet(records, path)
    return path


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the MEV-bot behavioural-overlay label cache (plan §7)."
    )
    parser.add_argument(
        "--block-start",
        type=int,
        default=None,
        help="First block to pull MEV rows from (inclusive).",
    )
    parser.add_argument(
        "--block-end",
        type=int,
        default=None,
        help="Last block to pull MEV rows from (inclusive).",
    )
    args = parser.parse_args()

    path = refresh(block_start=args.block_start, block_end=args.block_end)

    from .schema import read_contract_parquet

    count = len(read_contract_parquet(path))
    print(f"wrote {count} MEV-bot overlay record(s) to {path}")


if __name__ == "__main__":
    _main()
