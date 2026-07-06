"""4-byte function-selector decoding source (expansion plan §6, phase 5).

A **selector** is the first four bytes of transaction calldata — e.g.
``0x38ed1739`` decodes to
``swapExactTokensForTokens(uint256,uint256,address[],address,uint256)``. Mapping
selectors back to human-readable signatures lets the failure tables say *what*
call a failing tx was making, not just which contract it hit.

The 4-byte space is tiny (2^32) and heavily oversubscribed: >1.2M distinct text
signatures share it, so **collisions are real**. This module therefore stores
**every** candidate signature for a selector (one parquet row per
``(selector, text_signature)`` pair) and never silently picks one at write time;
disambiguation is a *read-time* heuristic in :func:`decode_selector`.

Selectors use a **separate** parquet schema from contract labels — the
:data:`schema.SELECTOR_COLUMNS` (``selector, text_signature, source``) cache at
:data:`config.SELECTORS_PATH` — not :class:`schema.LabelRecord`.

Sources (plan §6):

- **Bulk:** the Sourcify signature DB (a BigQuery mirror of the openchain /
  4byte corpus). Bulk import is out of scope for this module.
- **API fallback / on-demand:** :func:`fetch_signatures` looks up a *bounded*
  list of selectors against the openchain-compatible
  :data:`config.FOURBYTE_SOURCIFY_URL` REST API (``api.4byte.sourcify.dev``) —
  intended for the handful of selectors that dominate our failure tables. Live
  calls use ``requests``, imported lazily inside the fetch path so this module
  (and its offline tests) import without touching the network. :func:`normalize`
  and :func:`decode_selector` are pure and network-free.

.. note::

   **Phase-5 / calldata-gated (plan §6, §10).** Whether we have selectors *at
   all* depends on transaction calldata being available in the warehouse — and
   the raw ``trace_payload`` blob is explicitly off-limits per the warehouse
   rules. This module provides the *decoding capability* only; wiring selectors
   into precompute (and the source of the selector bytes) is gated separately
   and is not enabled here.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..config import FOURBYTE_SOURCIFY_URL, LABEL_CACHE, SELECTORS_PATH
from .schema import read_selector_parquet, write_selector_parquet

#: Provenance tag written into the ``source`` column for API-fetched signatures.
SOURCE_4BYTE = "4byte"

#: Endpoint for the openchain signature lookup API. Openchain accepts a
#: comma-separated ``function`` query and returns, per selector, a list of
#: ``{name, filtered, hasVerifiedContract}`` candidates under
#: ``result.function.<selector>``. Verified live 2026-07-03.
_LOOKUP_PATH = "/signature-database/v1/lookup"


def _canonical_selector(value: object) -> Optional[str]:
    """Coerce ``value`` to a canonical ``0x``+8-hex selector, or ``None``.

    Accepts values with or without the ``0x`` prefix and in any case; anything
    that is not exactly four bytes of hex is rejected (returns ``None``).
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 8 or not all(c in "0123456789abcdef" for c in s):
        return None
    return "0x" + s


def fetch_signatures(selectors: List[str]) -> List[dict]:
    """Look up a bounded list of selectors via the 4byte/Sourcify REST API.

    Intended for the *handful* of selectors that dominate our failure tables, not
    a bulk crawl (use the Sourcify signature DB for that — plan §6). Returns raw
    rows ``{selector, text_signature, source}`` with ``source="4byte"``, one row
    per candidate signature (so a colliding selector yields several rows).

    ``requests`` is imported lazily here so the module imports offline; this path
    is not exercised by the unit tests. Selectors that are malformed or missing
    from the API simply contribute no rows.
    """
    # Lazy import: network is only needed on this path (plan §6). Openchain
    # accepts GET /signature-database/v1/lookup?function=<comma-separated
    # selectors>&filter=false and replies
    #   {"ok": true, "result": {"function": {"0x38ed1739": [{"name": "...(...)",
    #     "filtered": bool, "hasVerifiedContract": bool}]}}}
    # (verified live 2026-07-03). ``filter=false`` is essential: with the default
    # ``filter=true`` openchain collapses each selector to a single "best"
    # candidate, hiding the collisions this cache exists to record (plan §6).
    import requests

    canon = [s for s in (_canonical_selector(x) for x in selectors) if s]
    if not canon:
        return []

    url = FOURBYTE_SOURCIFY_URL.rstrip("/") + _LOOKUP_PATH
    resp = requests.get(
        url,
        params={"function": ",".join(canon), "filter": "false"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    by_selector = (payload.get("result") or {}).get("function") or {}
    raw_rows: List[dict] = []
    for selector, candidates in by_selector.items():
        for candidate in candidates or []:
            name = candidate.get("name") if isinstance(candidate, dict) else None
            if not name:
                continue
            raw_rows.append(
                {
                    "selector": selector,
                    "text_signature": name,
                    "source": SOURCE_4BYTE,
                }
            )
    return raw_rows


def normalize(raw_rows: List[dict]) -> List[dict]:
    """Normalise raw selector rows; keep every candidate, dedup exact pairs.

    - The ``selector`` is canonicalised to lowercase ``0x``+8-hex
      (:func:`_canonical_selector`); rows with an unparseable selector or blank
      signature are dropped.
    - **All** candidate signatures per selector are kept (collisions are real —
      plan §6); only *identical* ``(selector, text_signature)`` pairs are
      deduped, preserving first-seen order.

    Pure and network-free. ``source`` defaults to ``"4byte"`` when absent.
    """
    seen: set[tuple[str, str]] = set()
    out: List[dict] = []
    for row in raw_rows:
        selector = _canonical_selector(row.get("selector"))
        if selector is None:
            continue
        signature = row.get("text_signature")
        if not isinstance(signature, str) or not signature.strip():
            continue
        signature = signature.strip()
        key = (selector, signature)
        if key in seen:
            continue
        seen.add(key)
        source = row.get("source")
        out.append(
            {
                "selector": selector,
                "text_signature": signature,
                "source": (
                    source if isinstance(source, str) and source else SOURCE_4BYTE
                ),
            }
        )
    return out


def refresh(selectors: List[str], cache_dir: Path | str = LABEL_CACHE) -> Path:
    """Fetch + normalise + write the ``selectors.parquet`` cache; return its path.

    Looks up ``selectors`` via :func:`fetch_signatures`, normalises, and writes
    the selector cache (:data:`schema.SELECTOR_COLUMNS`). Refresh cadence is
    manual (plan §4.2): run this for the dominant failure-table selectors, commit
    the regenerated slice.
    """
    raw_rows = fetch_signatures(selectors)
    rows = normalize(raw_rows)
    path = Path(cache_dir) / "selectors.parquet"
    write_selector_parquet(rows, path)
    return path


def load_selector_map(path: Path | str = SELECTORS_PATH) -> Dict[str, List[str]]:
    """Read the selector cache into ``{selector: [candidate signatures]}``.

    Returns an empty dict when the cache is absent or unreadable, so callers
    degrade gracefully exactly like the contract-label resolver
    (:func:`repricing_impact.labels._load_merged_cache`). Candidate order follows
    the parquet row order.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        df = read_selector_parquet(path)
    except Exception:
        # A malformed / partial cache must never break the dashboard build.
        return {}

    mapping: Dict[str, List[str]] = {}
    for selector, signature in zip(df["selector"], df["text_signature"]):
        canon = _canonical_selector(selector)
        if canon is None or not isinstance(signature, str) or not signature:
            continue
        mapping.setdefault(canon, []).append(signature)
    return mapping


def _signature_name(signature: str) -> str:
    """The function name of a text signature (before the ``(`` args)."""
    return signature.split("(", 1)[0]


def _category_tokens(category: str) -> List[str]:
    """Lowercase word tokens of a category, e.g. ``swap_dex`` -> ``[swap, dex]``."""
    return [t for t in re.split(r"[_\s]+", category.lower()) if t]


def decode_selector(
    selector: str,
    category: Optional[str] = None,
    selector_map: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    """Return the best candidate signature for ``selector``, or ``None``.

    Disambiguation heuristic (plan §6): when a selector has several colliding
    candidates and a contract ``category`` is supplied, prefer a candidate whose
    **function name** mentions a category token — e.g. a ``swap_dex`` contract's
    ``0x38ed1739`` is Uniswap's ``swapExactTokensForTokens``, not a colliding junk
    signature. With no category (or no match), fall back to a deterministic
    plausible candidate: the shortest signature (tie broken lexicographically),
    which favours the concise real-world ABI entry over long junk collisions.

    ``selector_map`` defaults to :func:`load_selector_map` (the on-disk cache).
    Returns ``None`` when the selector is unknown / has no candidates.
    """
    canon = _canonical_selector(selector)
    if canon is None:
        return None
    if selector_map is None:
        selector_map = load_selector_map()

    candidates = selector_map.get(canon)
    if not candidates:
        return None

    if category:
        tokens = _category_tokens(category)
        for candidate in candidates:
            name = _signature_name(candidate).lower()
            if any(tok in name for tok in tokens):
                return candidate

    # Deterministic fallback: shortest signature, then lexicographic.
    return min(candidates, key=lambda s: (len(s), s))


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the 4-byte selector cache by looking up a bounded list of "
            "selectors via the 4byte/Sourcify API (plan §6, calldata-gated)."
        )
    )
    parser.add_argument(
        "--selectors",
        nargs="+",
        required=True,
        metavar="0xXXXXXXXX",
        help="Selectors to look up, e.g. --selectors 0x38ed1739 0xa9059cbb",
    )
    args = parser.parse_args()

    path = refresh(args.selectors)
    mapping = load_selector_map(path)
    total = sum(len(v) for v in mapping.values())
    print(
        f"wrote {total} candidate signature(s) for {len(mapping)} selector(s) to {path}"
    )
    for selector, sigs in mapping.items():
        for sig in sigs:
            print(f"  {selector}  {sig}")


if __name__ == "__main__":
    _main()
