"""Open Labels Initiative (OLI) label-source fetcher (expansion plan §2, §3).

OLI is a public, **read-only, no-API-key** attestation pool of contract labels
(https://www.openlabelsinitiative.org/). Each *attestation* is one attester's
claim about one address (``recipient``) and carries a ``tags_json`` object whose
keys are OLI *tag ids* — e.g. ``contract_name``, ``owner_project``,
``usage_category`` (OLI's own taxonomy), the structural tags ``is_proxy`` /
``is_factory_contract`` / ``is_safe_contract`` / ``erc_type``, and the ERC-4337
role tags ``is_paymaster`` / ``is_bundler`` / ``paymaster_category``. This module
maps those onto the shared :class:`~repricing_impact.label_sources.schema.LabelRecord`
contract and writes a per-source parquet cache at ``label_cache/oli.parquet``,
which :mod:`repricing_impact.label_sources.build` then merges (plan §4.3).

**Live API shape (verified 2026-07 against the real pool):** OLI serves a
FastAPI **REST** label pool, *not* GraphQL. The keyless read endpoint is
``GET {OLI_API_URL}/attestations`` returning ``{"count", "attestations": [...]}``.
Filters used here: ``chain_id`` (CAIP-2, ``eip155:1`` for Ethereum mainnet) and
``recipient`` (a single 0x address). There is no offset param; bulk reads
paginate forward on the ``time`` cursor via ``since`` + ``order=asc`` (``limit``
maxes at 1000/request). The ``/labels`` and ``/labels/bulk`` endpoints exist too
but require an API key, so we use the keyless ``/attestations``.

Because an address usually has several attestations (some ``revoked``), each with
only a partial ``tags_json``, :func:`fetch` **collapses** all attestations for a
recipient into one flat raw row (preferring non-revoked, newest-wins per field)
before handing it to :func:`normalize`. That keeps :func:`normalize` a pure,
flat-dict mapping that the offline unit tests can exercise without the network.

Category resolution (plan §3) refines OLI's ``usage_category`` with the
structural tags **before** the generic :func:`~schema.map_oli_category` fallback:

- ``is_safe_contract`` → ``wallet_safe``;
- ``usage_category == "erc4337"`` or ``is_paymaster`` / ``is_bundler`` →
  ``account_abstraction``;
- otherwise the generic OLI taxonomy fold.

OLI has no MEV taxonomy, so this fetcher never emits ``mev_bot`` (that is a
behavioural overlay set by the MEV source, plan §4.3).

Only :func:`fetch` touches the network; :func:`normalize` is a pure mapping and
is what the offline unit tests exercise. ``requests`` is imported lazily inside
:func:`fetch` so importing this module (and running the merge over an existing
cache / fixtures) never requires network libraries to be present.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import LABEL_CACHE, OLI_API_URL
from .schema import (
    Category,
    Confidence,
    LabelRecord,
    Source,
    map_oli_category,
    write_contract_parquet,
)

#: Cache filename under ``LABEL_CACHE`` (mirrors ``build.SOURCE_FILES["oli"]``).
CACHE_FILENAME = "oli.parquet"

# The keyless OLI pool is a FastAPI REST service; the ``/attestations`` read path
# takes no API key. Base URL is ``OLI_API_URL`` (imported from config above).

#: CAIP-2 chain id for Ethereum mainnet — OLI keys addresses by CAIP-2.
ETHEREUM_MAINNET_CHAIN_ID = "eip155:1"

#: Server cap on ``limit`` for a single ``/attestations`` request.
_MAX_PAGE = 1000

# OLI tag-id -> the flat key :func:`normalize` reads. ``erc_type`` is served as an
# array (e.g. ``["erc20"]``); :func:`_collapse` flattens it to a scalar.
_TAG_KEYS = (
    "contract_name",
    "owner_project",
    "usage_category",
    "is_proxy",
    "is_factory_contract",
    "is_safe_contract",
    "is_paymaster",
    "is_bundler",
    "erc_type",
)


def _collapse(recipient: str, attestations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge one recipient's attestations into a single flat raw row.

    Non-revoked attestations take precedence over revoked ones; within each of
    those tiers newer attestations win per field. Only OLI tag ids we map are
    carried through; ``erc_type`` (an array in the pool) is flattened to its
    first element. The result is the flat dict shape :func:`normalize` consumes.
    """
    row: Dict[str, Any] = {"address": recipient}

    # Newest-first within a tier so the first non-null value we see wins; process
    # revoked attestations first so live ones overwrite them.
    def _time(att: Dict[str, Any]) -> str:
        return att.get("time") or ""

    revoked = sorted(
        (a for a in attestations if a.get("revoked")), key=_time, reverse=True
    )
    live = sorted(
        (a for a in attestations if not a.get("revoked")), key=_time, reverse=True
    )

    for att in list(revoked) + list(live):
        tags = att.get("tags_json") or {}
        for tag in _TAG_KEYS:
            if tag not in tags:
                continue
            value = tags[tag]
            if tag == "erc_type" and isinstance(value, list):
                value = value[0] if value else None
            if value is None or value == "":
                continue
            row[tag] = value  # later (newer / live) writes overwrite earlier
    return row


def fetch(
    addresses: Optional[List[str]] = None,
    limit: Optional[int] = None,
    *,
    chain_id: str = ETHEREUM_MAINNET_CHAIN_ID,
    url: str = OLI_API_URL,
    timeout: float = 60.0,
) -> List[Dict[str, Any]]:
    """Read OLI attestations and return one collapsed raw row per address.

    Two modes:

    - **address-filtered** (``addresses`` given): one keyless
      ``GET /attestations?recipient=<addr>&chain_id=<chain_id>`` per address,
      each collapsed to a single flat row (see :func:`_collapse`). Addresses with
      no attestations are skipped.
    - **bulk** (``addresses`` is ``None``): paginate the whole ``chain_id`` pool
      forward on the ``time`` cursor until ``limit`` distinct addresses are seen
      (``limit`` is required here to bound the pull) or the pool is exhausted.

    Returns flat dicts (``address`` + OLI tag ids), *not* the raw attestation
    envelopes — normalisation happens in :func:`normalize`, the offline-testable
    part. ``requests`` is imported lazily so importing this module never needs it.
    """
    import requests  # lazy — optional, only needed to refresh the cache

    session = requests.Session()

    def _get(params: Dict[str, Any]) -> List[Dict[str, Any]]:
        resp = session.get(f"{url}/attestations", params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        return list(payload.get("attestations") or [])

    if addresses is not None:
        rows: List[Dict[str, Any]] = []
        for addr in addresses:
            atts = _get(
                {
                    "chain_id": chain_id,
                    "recipient": addr.lower(),
                    "limit": _MAX_PAGE,
                }
            )
            if not atts:
                continue
            rows.append(_collapse(addr.lower(), atts))
        return rows

    # Bulk: forward-paginate on the time cursor, grouping by recipient. A limit is
    # required so an unbounded pool scan can't run away.
    if limit is None:
        raise ValueError("fetch() bulk mode requires a limit (address cap)")

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []  # preserve first-seen address order, capped at `limit`
    since: Optional[str] = None
    while len(order) < limit:
        params: Dict[str, Any] = {
            "chain_id": chain_id,
            "limit": _MAX_PAGE,
            "order": "asc",
        }
        if since is not None:
            params["since"] = since
        page = _get(params)
        if not page:
            break

        for att in page:
            recipient = (att.get("recipient") or "").lower()
            if not recipient:
                continue
            if recipient not in grouped:
                if len(order) >= limit:
                    continue  # keep collecting more attestations only for known addrs
                grouped[recipient] = []
                order.append(recipient)
            grouped[recipient].append(att)

        last_time = page[-1].get("time")
        if last_time is None or last_time == since:
            break  # no forward progress — stop rather than loop
        since = last_time
        if len(page) < _MAX_PAGE:
            break  # exhausted the pool

    return [_collapse(addr, grouped[addr]) for addr in order[:limit]]


def _refine_category(raw: Dict[str, Any]) -> str:
    """Resolve an OLI row's category, applying structural refinements first.

    Structural tags take precedence over the generic ``usage_category`` fold
    (plan §3): a Safe contract is ``wallet_safe``; an ERC-4337 account/paymaster/
    bundler is ``account_abstraction``. OLI never yields ``mev_bot``.
    """
    if raw.get("is_safe_contract"):
        return Category.WALLET_SAFE.value

    usage_category = raw.get("usage_category")
    is_erc4337 = (
        (
            isinstance(usage_category, str)
            and usage_category.strip().lower() == "erc4337"
        )
        or bool(raw.get("is_paymaster"))
        or bool(raw.get("is_bundler"))
    )
    if is_erc4337:
        return Category.ACCOUNT_ABSTRACTION.value

    return map_oli_category(usage_category)


def normalize(raw_rows: List[Dict[str, Any]]) -> List[LabelRecord]:
    """Map collapsed OLI rows to :class:`LabelRecord`s (pure; offline).

    Consumes the flat rows produced by :func:`fetch`/:func:`_collapse`
    (``address`` + OLI tag ids). Skips rows with no ``address``. Sets
    ``source="oli"`` / ``confidence="high"``, carries the structural tags
    (``is_proxy`` / ``is_factory`` from ``is_factory_contract`` / ``is_safe`` from
    ``is_safe_contract`` / ``erc_type``), and resolves the category via
    :func:`_refine_category`. The display label comes from ``contract_name``.
    """
    records: List[LabelRecord] = []
    for raw in raw_rows:
        address = raw.get("address")
        if not address:
            continue

        records.append(
            LabelRecord(
                address=address,
                label=raw.get("contract_name") or "",
                category=_refine_category(raw),
                owner_project=raw.get("owner_project"),
                source=Source.OLI.value,
                confidence=Confidence.HIGH.value,
                is_proxy=raw.get("is_proxy"),
                is_factory=raw.get("is_factory_contract"),
                is_safe=raw.get("is_safe_contract"),
                erc_type=raw.get("erc_type"),
            )
        )
    return records


def refresh(cache_dir: Path | str = LABEL_CACHE, **fetch_kwargs: Any) -> Path:
    """Fetch + normalise + write OLI labels to ``<cache_dir>/oli.parquet``.

    Returns the written path. ``fetch_kwargs`` are forwarded to :func:`fetch`
    (e.g. ``addresses=`` / ``limit=``). This is the only entry point that hits the
    network.
    """
    cache_dir = Path(cache_dir)
    raw_rows = fetch(**fetch_kwargs)
    records = normalize(raw_rows)
    return write_contract_parquet(records, cache_dir / CACHE_FILENAME)


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=LABEL_CACHE,
        help="directory to write oli.parquet into (default: label_cache/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of distinct OLI addresses fetched (required for "
        "bulk pulls; ignored when --addresses-file is given)",
    )
    parser.add_argument(
        "--addresses-file",
        type=Path,
        default=None,
        help="optional file of 0x addresses (one per line) to enrich instead of "
        "a bulk pull",
    )
    args = parser.parse_args(argv)

    addresses: Optional[List[str]] = None
    if args.addresses_file is not None:
        addresses = [
            line.strip()
            for line in args.addresses_file.read_text().splitlines()
            if line.strip()
        ]

    out = refresh(cache_dir=args.cache_dir, addresses=addresses, limit=args.limit)
    from .schema import read_contract_parquet

    records = read_contract_parquet(out)
    print(f"wrote {len(records)} OLI labels -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
