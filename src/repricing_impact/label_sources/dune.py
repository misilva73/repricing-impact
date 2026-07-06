"""Dune labels fetcher (expansion plan §5, §2).

Pulls Ethereum contract labels from Dune's community-maintained ``labels``
spellbook and normalises them to the shared
:class:`~repricing_impact.label_sources.schema.LabelRecord` contract, writing a
``dune.parquet`` per-source cache under ``label_cache/`` that
:mod:`repricing_impact.label_sources.build` merges by the precedence in plan
§4.3. Dune sits just below OLI in that precedence; its default confidence is
``high`` (:data:`schema.SOURCE_CONFIDENCE`).

**Table (verified live 2026-07):** the legacy ``dune.labels`` spellbook is gone;
the current source is the ``labels.addresses`` table (columns
``address, name, category, contributor, source`` — exactly the shape we want,
plus ``blockchain`` for filtering). We exclude the purely *behavioural* per-EOA
activity categories (``dex``, ``cex users``) — those are trader-activity tags,
not contract identity, and drown the result (111M ``dex`` rows chain-wide). What
remains is contract-identity + structural labels (``contracts``,
``infrastructure``, ``safe``, ``bridge``, ``nft``, ``social`` …). The sibling
``labels.contracts`` table is cleaner but stamps every row ``category =
'contracts'`` (useless for our taxonomy) and covers fewer addresses, so we use
``labels.addresses`` with the behavioural filter.

Two cost-control modes (plan §5):

- **Targeted enrichment** (default, cheap): pass a bounded ``addresses`` list;
  the query inlines those addresses as an ``IN`` list so we pay for a small
  result.
- **Bulk pull**: no ``addresses``; the query returns the full (filtered)
  Ethereum label set — run this infrequently as an off-peak refresh.

Live pulls use the official ``dune-client`` SDK, **lazily imported inside the
fetch path** so this module (and its offline tests) import without the optional
dependency installed. By default :func:`fetch` runs ad-hoc SQL via the API
(``DuneClient.run_sql`` — the ``/sql/execute`` endpoint, requires a paid Plus
plan) so no saved query has to exist; pass ``query_id`` to run a pre-saved query
instead (which supports a ``{{addresses}}`` text parameter). The API key comes
from ``secrets.json`` (key ``dune_api_key``, via
:func:`repricing_impact.clickhouse.load_secrets`) and is only required when
actually fetching. :func:`normalize` is the pure, testable core and touches
neither the network nor the SDK.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from ..config import LABEL_CACHE
from .schema import (
    Confidence,
    LabelRecord,
    Source,
    map_dune_category,
    write_contract_parquet,
)

#: Per-source cache filename (mirrors ``build.SOURCE_FILES[Source.DUNE]``).
CACHE_FILENAME = "dune.parquet"

#: Raw result columns we ask Dune for / expect back from the query.
RAW_COLUMNS = ["address", "name", "category", "contributor", "source"]

#: The live Dune table we read (legacy ``dune.labels`` was retired).
DUNE_LABELS_TABLE = "labels.addresses"

#: Purely-behavioural per-EOA activity categories excluded from the pull — these
#: are trader-activity tags (e.g. "DEX Trader", "$2k-$5k avg. DEX trade value"),
#: not contract identity, and ``dex`` alone is ~111M rows chain-wide.
BEHAVIORAL_CATEGORIES = ("dex", "cex users")


def _load_api_key() -> str:
    """Return the Dune API key from ``secrets.json`` or raise a clear error."""
    from ..clickhouse import load_secrets

    key = load_secrets().get("dune_api_key")
    if not key:
        raise RuntimeError(
            "dune_api_key missing from secrets.json — add it before running a "
            "live Dune fetch (see plan §5)."
        )
    return key


def _clean_addresses(addresses: List[str]) -> List[str]:
    """Lowercase, strip, and de-duplicate a target address list (order-preserving)."""
    seen: dict[str, None] = {}
    for a in addresses:
        if isinstance(a, str) and a.strip():
            seen.setdefault(a.strip().lower(), None)
    return list(seen)


def build_labels_sql(addresses: Optional[List[str]] = None) -> str:
    """Return the ``labels.addresses`` SELECT run by :func:`fetch` (Option A path).

    Selects ``address, name, category, contributor, source`` filtered to
    ``blockchain = 'ethereum'`` with the behavioural categories
    (:data:`BEHAVIORAL_CATEGORIES`) excluded. When ``addresses`` is given they are
    inlined as a lowercased ``IN`` list (targeted enrichment); otherwise the full
    filtered Ethereum set is returned (bulk pull). ``run_sql`` does not support
    bind parameters, so the bounded, self-supplied address literals are inlined
    directly.
    """
    not_in = ", ".join("'" + c + "'" for c in BEHAVIORAL_CATEGORIES)
    where = [
        "blockchain = 'ethereum'",
        f"lower(category) NOT IN ({not_in})",
    ]
    if addresses:
        in_list = ", ".join("'" + a + "'" for a in _clean_addresses(addresses))
        where.append(f"lower(cast(address AS varchar)) IN ({in_list})")
    return (
        "SELECT cast(address AS varchar) AS address, name, category, "
        "contributor, source\n"
        f"FROM {DUNE_LABELS_TABLE}\n"
        "WHERE " + "\n  AND ".join(where)
    )


def fetch(
    query_id: Optional[int] = None,
    addresses: Optional[List[str]] = None,
) -> List[dict]:
    """Live-pull Ethereum labels from Dune's ``labels`` spellbook via ``dune-client``.

    Selects ``address, name, category, contributor, source`` from
    :data:`DUNE_LABELS_TABLE` filtered ``blockchain = 'ethereum'`` (behavioural
    categories excluded). Two modes (plan §5):

    - **Targeted enrichment** (default): pass ``addresses`` (a bounded list); they
      are inlined into the query's ``IN`` list, keeping the credit cost small.
    - **Bulk pull**: omit ``addresses`` to return the full filtered label set.

    Two API paths:

    - **Ad-hoc SQL** (default, ``query_id is None``): runs :meth:`DuneClient.run_sql`
      (the ``/sql/execute`` endpoint — requires a paid Plus plan). No saved query
      needs to exist; the SQL from :func:`build_labels_sql` is executed directly.
    - **Saved query** (``query_id`` given): runs that query via
      :meth:`DuneClient.run_query`, passing ``addresses`` as a comma-separated
      ``addresses`` text parameter the saved query is expected to filter on
      (its SQL should ``SELECT`` the :data:`RAW_COLUMNS` from the labels table).

    ``dune-client`` and the API key are only touched here — the SDK is imported
    lazily so the module imports without it, and the key is read on demand.

    Returns raw rows as dicts with keys :data:`RAW_COLUMNS`.
    """
    # Lazy import: the SDK is an optional, fetch-time-only dependency (plan §5).
    from dune_client.client import DuneClient

    api_key = _load_api_key()
    client = DuneClient(api_key)

    if query_id is None:
        # Option A: ad-hoc SQL via /sql/execute (no saved query required).
        response = client.run_sql(query_sql=build_labels_sql(addresses))
    else:
        # Option B: run a pre-saved query, handing it the bounded address list.
        from dune_client.query import QueryBase
        from dune_client.types import QueryParameter

        params = []
        if addresses:
            params.append(
                QueryParameter.text_type(
                    name="addresses",
                    value=",".join(_clean_addresses(addresses)),
                )
            )
        response = client.run_query(QueryBase(query_id=query_id, params=params))

    rows = response.result.rows if response.result is not None else []
    return [{col: row.get(col) for col in RAW_COLUMNS} for row in rows]


def _owner_project(row: dict) -> Optional[str]:
    """Derive ``owner_project`` from a raw Dune row, or ``None`` if not sensible.

    Dune label ``name``s encode the owning project as a ``"Project: Contract"``
    prefix (e.g. ``"Uniswap_v2: Router02"``, ``"Circle: USDC"``), so we take the
    prefix before the first colon as the project slug. Freeform/behavioural names
    with no colon (e.g. ``"Aave v3 Flashloan User"``) yield ``None``. We do **not**
    use ``contributor`` — that is the Dune dashboard author, not the owner.
    """
    name = row.get("name")
    if isinstance(name, str) and ":" in name:
        prefix = name.split(":", 1)[0].strip()
        if prefix:
            return prefix.lower()
    return None


def normalize(raw_rows: List[dict]) -> List[LabelRecord]:
    """Map raw ``dune.labels`` rows to :class:`LabelRecord`s (plan §5).

    Each record gets ``source="dune"``, ``confidence="high"``, ``label`` from the
    Dune ``name``, ``category`` via :func:`schema.map_dune_category`, and
    ``owner_project`` from the contributor/name (:func:`_owner_project`). Rows with
    no address are skipped — the address is the join key and a labelless-address
    row is useless downstream. This is the pure, network-free core.
    """
    records: List[LabelRecord] = []
    for row in raw_rows:
        address = row.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        name = row.get("name")
        records.append(
            LabelRecord(
                address=address.strip(),
                label=name if isinstance(name, str) and name.strip() else address,
                category=map_dune_category(row.get("category")),
                owner_project=_owner_project(row),
                source=Source.DUNE.value,
                confidence=Confidence.HIGH.value,
            )
        )
    return records


def refresh(cache_dir: Path | str = LABEL_CACHE, **fetch_kwargs) -> Path:
    """Fetch + normalise + write the ``dune.parquet`` cache; return its path.

    ``**fetch_kwargs`` are forwarded to :func:`fetch` (``query_id``,
    ``addresses``). Refresh cadence is manual (plan §4.2): run this, then
    ``python -m repricing_impact.label_sources.build`` before precompute.
    """
    raw_rows = fetch(**fetch_kwargs)
    records = normalize(raw_rows)
    path = Path(cache_dir) / CACHE_FILENAME
    write_contract_parquet(records, path)
    return path


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the Dune (dune.labels) per-source label cache (plan §5)."
    )
    parser.add_argument(
        "--query-id",
        type=int,
        default=None,
        help=(
            "Optional saved Dune query id (Option B). Omit to run ad-hoc SQL "
            "against labels.addresses via /sql/execute (Option A, needs Plus)."
        ),
    )
    parser.add_argument(
        "--bulk",
        action="store_true",
        help="Bulk pull the full Ethereum label set (default is targeted enrichment).",
    )
    parser.add_argument(
        "--addresses",
        default=None,
        help=(
            "Comma-separated addresses for targeted enrichment (ignored with "
            "--bulk). If omitted without --bulk, the query runs with no address "
            "filter."
        ),
    )
    args = parser.parse_args()

    addresses = None
    if not args.bulk and args.addresses:
        addresses = [a.strip() for a in args.addresses.split(",") if a.strip()]

    path = refresh(query_id=args.query_id, addresses=addresses)

    from .schema import read_contract_parquet

    count = len(read_contract_parquet(path))
    print(f"wrote {count} Dune label(s) to {path}")


if __name__ == "__main__":
    _main()
