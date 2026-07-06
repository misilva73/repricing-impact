"""Project constants, paths, and the pinned-config resolver.

Everything downstream (groups, precompute) pins to ``chain_id = 1`` and a single
``analysis_config_hash`` so runs are never mixed. This module owns:

- ``CHAIN_ID`` and the schedule list (focus: ``eip-8037`` / ``eip-8038``);
- repo paths (notably ``SITE_DATA`` = ``site/data`` where precompute writes JSON);
- :func:`resolve_config_hash`, which auto-picks the config that covers **both**
  focus schedules with the most blocks / most recent ``updated_at``, with a
  per-schedule fallback when no single config covers both. Overridable via the
  ``REPRICING_CONFIG_HASH`` env var.

Warehouse rules respected here (docs/warehouse.md): filter ``chain_id``; query the
distributed tables; dedup ReplacingMergeTree (the ``run`` table is deduped with
``argMax(..., updated_at)`` per hash, the coverage block counts with
``count(DISTINCT block_number)`` so pre-merge duplicate rows cannot inflate
them). The resolver query is cheap: it touches the tiny ``run`` table and runs a
single chain-filtered ``GROUP BY`` over ``block_coverage`` (~3M rows).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from sqlalchemy.engine import Engine

from .clickhouse import get_engine, run_query

# --- Core constants -------------------------------------------------------

CHAIN_ID = 1  # mainnet — only chain present in the warehouse

# Focus schedules for the dashboard. A pinned config must cover BOTH of these.
FOCUS_SCHEDULES = ["eip-8037", "eip-8038"]

# All schedules present in the warehouse (for reference; 7904 is out of focus).
ALL_SCHEDULES = ["7904-prelim", "eip-8037", "eip-8038"]

# Env var override: if set, the resolver returns this hash without querying.
CONFIG_HASH_ENV_VAR = "REPRICING_CONFIG_HASH"

# --- Paths ----------------------------------------------------------------

# Repo root = two levels up from this file (src/repricing_impact/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SITE_DIR = REPO_ROOT / "site"
SITE_DATA = SITE_DIR / "data"  # precompute writes published JSON here
SECRETS_PATH = REPO_ROOT / "secrets.json"

# Build-time contract/selector label caches (expansion plan §4.2). Gitignored;
# per-source parquet pulls + the merged ``contract_labels.parquet`` live here.
# Only the tiny slice of labels that appears in the published JSON is committed
# (inlined by precompute into ``site/data/**``) — never the raw dumps.
LABEL_CACHE = REPO_ROOT / "label_cache"
MERGED_LABELS_PATH = LABEL_CACHE / "contract_labels.parquet"
SELECTORS_PATH = LABEL_CACHE / "selectors.parquet"

# Per-source endpoints / config for the label fetchers (plan §2, §5, §6, §11).
# Read-only, public endpoints; secrets (e.g. dune_api_key) live in secrets.json.
# OLI's keyless read pool is a FastAPI REST service (the /graphql path 404s); the
# label read path is GET {OLI_API_URL}/attestations (verified 2026-07-03).
OLI_API_URL = "https://api.openlabelsinitiative.org"
ETHEREUM_LISTS_CONTRACTS_REPO = "https://github.com/ethereum-lists/contracts.git"
ETHEREUM_LISTS_TOKENS_REPO = "https://github.com/ethereum-lists/tokens.git"
# openchain (the successor of the 4byte/Sourcify signature DB) exposes the
# canonical openchain-compatible lookup surface. The old api.4byte.sourcify.dev
# host does not serve the /signature-database lookup path, so we point at the
# live openchain endpoint that does (verified 2026-07-03).
FOURBYTE_SOURCIFY_URL = "https://api.openchain.xyz"
ZEROMEV_API_URL = "https://data.zeromev.org/v1"


# --- Resolver result types ------------------------------------------------


@dataclass
class ScheduleCoverage:
    """Per-schedule coverage for one config: block count and block range."""

    schedule_name: str
    analysis_config_hash: str
    block_count: int
    min_block: int
    max_block: int
    updated_at: Optional[pd.Timestamp] = None


@dataclass
class ConfigResolution:
    """Resolved pinned config(s).

    ``single_hash`` is set when one config covers both focus schedules.
    Otherwise ``per_schedule`` maps each focus schedule to its own hash
    (fallback) and ``single_hash`` is ``None``. ``coverage`` records the chosen
    schedule coverage (block counts + ranges). ``common_block_range`` is the
    overlapping ``[min, max]`` of the focus schedules for like-for-like compares.
    """

    single_hash: Optional[str]
    per_schedule: Dict[str, str]
    coverage: Dict[str, ScheduleCoverage] = field(default_factory=dict)
    common_block_range: Optional[tuple] = None
    source: str = "resolved"  # "env_override" | "resolved" | "resolved_per_schedule"

    @property
    def is_single_config(self) -> bool:
        return self.single_hash is not None


# --- Resolver -------------------------------------------------------------


def _coverage_by_config_schedule(engine: Engine) -> pd.DataFrame:
    """Per (config, schedule) block coverage over the focus schedules.

    One cheap chain-filtered GROUP BY over ``block_coverage``. ReplacingMergeTree
    duplicates are neutralised with ``count(DISTINCT block_number)`` (a block is
    counted once regardless of how many pre-merge row versions exist).
    """
    schedules_sql = ", ".join(f"'{s}'" for s in FOCUS_SCHEDULES)
    query = f"""
        SELECT
            analysis_config_hash,
            schedule_name,
            count(DISTINCT block_number) AS block_count,
            min(block_number)            AS min_block,
            max(block_number)            AS max_block,
            max(updated_at)              AS updated_at
        FROM gas_analysis.gas_analysis_block_coverage
        WHERE chain_id = {CHAIN_ID}
          AND schedule_name IN ({schedules_sql})
        GROUP BY analysis_config_hash, schedule_name
    """
    return run_query(query, engine=engine)


def _runs(engine: Engine) -> pd.DataFrame:
    """Deduped ``gas_analysis_run`` rows (one per ``analysis_config_hash``).

    The run table is ReplacingMergeTree on ``updated_at``; collapse to one row
    per hash with ``max(updated_at)`` + ``argMax(col, updated_at)`` so a
    re-exported config does not appear twice.
    """
    query = f"""
        SELECT
            analysis_config_hash,
            max(updated_at)                         AS latest_updated_at,
            argMax(producer_git_commit, updated_at) AS producer_git_commit,
            argMax(manifest_json, updated_at)       AS manifest_json
        FROM gas_analysis.gas_analysis_run
        WHERE chain_id = {CHAIN_ID}
        GROUP BY analysis_config_hash
    """
    return run_query(query, engine=engine)


def resolve_config_hash(engine: Optional[Engine] = None) -> ConfigResolution:
    """Resolve the pinned ``analysis_config_hash`` for the focus schedules.

    Selection (plan "Config selection"):

    1. If ``REPRICING_CONFIG_HASH`` is set, use it verbatim (still records
       coverage if it can be queried).
    2. Otherwise pick the config that covers **both** focus schedules, ranked by
       total focus-schedule block count (desc) then most recent ``updated_at``.
    3. Fallback: if no single config covers both, pin one config **per
       schedule** (each schedule's best config by block count / recency) and
       record both, plus the common/overlapping block range.
    """
    if engine is None:
        engine = get_engine()

    cov = _coverage_by_config_schedule(engine)
    runs = _runs(engine)
    run_updated = dict(zip(runs["analysis_config_hash"], runs["latest_updated_at"]))

    def _coverage_for(config_hash: str) -> Dict[str, ScheduleCoverage]:
        out: Dict[str, ScheduleCoverage] = {}
        sub = cov[cov["analysis_config_hash"] == config_hash]
        for _, r in sub.iterrows():
            out[r["schedule_name"]] = ScheduleCoverage(
                schedule_name=r["schedule_name"],
                analysis_config_hash=config_hash,
                block_count=int(r["block_count"]),
                min_block=int(r["min_block"]),
                max_block=int(r["max_block"]),
                updated_at=run_updated.get(config_hash),
            )
        return out

    # --- Env override ---
    override = os.environ.get(CONFIG_HASH_ENV_VAR)
    if override:
        coverage = _coverage_for(override)
        return ConfigResolution(
            single_hash=override,
            per_schedule={s: override for s in FOCUS_SCHEDULES},
            coverage=coverage,
            common_block_range=_common_range(coverage),
            source="env_override",
        )

    # --- Configs covering BOTH focus schedules ---
    by_config = cov.groupby("analysis_config_hash")["schedule_name"].agg(set)
    both = [h for h, s in by_config.items() if set(FOCUS_SCHEDULES).issubset(s)]

    if both:
        # Rank by total focus-schedule block count (desc), then updated_at (desc).
        totals = (
            cov[cov["analysis_config_hash"].isin(both)]
            .groupby("analysis_config_hash")["block_count"]
            .sum()
        )
        ranked = sorted(
            both,
            key=lambda h: (int(totals.get(h, 0)), _ts_key(run_updated.get(h))),
            reverse=True,
        )
        chosen = ranked[0]
        coverage = _coverage_for(chosen)
        return ConfigResolution(
            single_hash=chosen,
            per_schedule={s: chosen for s in FOCUS_SCHEDULES},
            coverage=coverage,
            common_block_range=_common_range(coverage),
            source="resolved",
        )

    # --- Fallback: pin one config per schedule ---
    per_schedule: Dict[str, str] = {}
    coverage: Dict[str, ScheduleCoverage] = {}
    for schedule in FOCUS_SCHEDULES:
        sub = cov[cov["schedule_name"] == schedule]
        if sub.empty:
            continue
        sub = sub.assign(
            _ts=sub["analysis_config_hash"].map(lambda h: _ts_key(run_updated.get(h)))
        )
        best = sub.sort_values(["block_count", "_ts"], ascending=False).iloc[0]
        h = best["analysis_config_hash"]
        per_schedule[schedule] = h
        coverage[schedule] = ScheduleCoverage(
            schedule_name=schedule,
            analysis_config_hash=h,
            block_count=int(best["block_count"]),
            min_block=int(best["min_block"]),
            max_block=int(best["max_block"]),
            updated_at=run_updated.get(h),
        )

    return ConfigResolution(
        single_hash=None,
        per_schedule=per_schedule,
        coverage=coverage,
        common_block_range=_common_range(coverage),
        source="resolved_per_schedule",
    )


def _ts_key(ts) -> float:
    """Sort key for an updated_at timestamp; missing sorts oldest."""
    if ts is None or pd.isna(ts):
        return float("-inf")
    return pd.Timestamp(ts).timestamp()


def _common_range(coverage: Dict[str, ScheduleCoverage]) -> Optional[tuple]:
    """Overlapping [min, max] block range across the focus schedules' coverage."""
    focus = [coverage[s] for s in FOCUS_SCHEDULES if s in coverage]
    if not focus:
        return None
    lo = max(c.min_block for c in focus)
    hi = min(c.max_block for c in focus)
    return (lo, hi)


if __name__ == "__main__":
    # CLI smoke test: resolve and print the chosen config + per-schedule coverage.
    res = resolve_config_hash()
    print(f"source: {res.source}")
    if res.is_single_config:
        print(f"pinned analysis_config_hash (both schedules): {res.single_hash}")
    else:
        print("no single config covers both focus schedules — per-schedule fallback:")
        for sched, h in res.per_schedule.items():
            print(f"  {sched}: {h}")
    print("\nper-schedule block coverage:")
    for sched in FOCUS_SCHEDULES:
        c = res.coverage.get(sched)
        if c is None:
            print(f"  {sched}: (no coverage found)")
            continue
        print(
            f"  {sched}: {c.block_count:,} blocks "
            f"[{c.min_block:,} .. {c.max_block:,}]  updated_at={c.updated_at}"
        )
    print(f"\ncommon (overlapping) block range: {res.common_block_range}")
