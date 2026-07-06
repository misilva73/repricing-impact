"""Build-time contract/selector label enrichment (expansion plan §4).

One fetcher per source, each normalising raw source data to the shared
:class:`~repricing_impact.label_sources.schema.LabelRecord` and writing a
per-source parquet cache under ``label_cache/`` (gitignored). ``build.py`` merges
those into ``label_cache/contract_labels.parquet`` by the precedence in plan
§4.3; :mod:`repricing_impact.labels` reads that merged file at precompute time,
degrading to the hardcoded map when the cache is absent.

Fetchers run at **build time only** (like precompute) — never on the dashboard
request path. Live network access and provider credentials are needed only to
*refresh* the caches; importing these modules and running the merge/resolver over
existing caches (or fixtures) requires neither.
"""

from .schema import (  # noqa: F401
    Category,
    Confidence,
    LabelRecord,
    Source,
    CONTRACT_COLUMNS,
    SELECTOR_COLUMNS,
    DUNE_CATEGORY_MAP,
    OLI_CATEGORY_MAP,
    map_dune_category,
    map_oli_category,
    is_precompile,
)
