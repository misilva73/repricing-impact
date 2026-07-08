#!/usr/bin/env python3
"""Precompute pipeline — ClickHouse aggregations -> build-time DuckDB -> JSON.

Re-runnable CLI that, for each focus schedule, runs **chunked, server-side**
ClickHouse ``GROUP BY`` aggregations (never pulling the ~10^7 slim rows to the
client), stages them in a build-time **DuckDB** file (never published), and
emits the **published JSON files per schedule** under ``site/data/{schedule}/``
exactly matching ``site/data/SCHEMA.md``:

    meta.json, overview_series.json, gas_delta_hist.json,
    group_categories.json, oog_forensics.json, nonoog_forensics.json,
    contract_failures.json, examples.json

plus the **sharded affected-contracts** output under ``site/data/{schedule}/affected/``:

    affected/index.json                 (small init file; name-searchable contracts)
    affected/{lowercase_addr}.json      (one per-contract record shard, fetched on lookup)
    affected/deploy_oog.json            (aggregate of the collapsed freshly-deployed
                                         self-OOG long tail — one file, no shards)

The group partition is derived entirely from
:mod:`repricing_impact.groups` (the single source of truth). All scans are
bounded by ``block_number`` range and chunked; the heavy ``_divergence`` work is
expressed as aggregates returning small results.

Usage examples
--------------

    # Small validation window (default-ish), both focus schedules:
    .venv/bin/python scripts/precompute.py --block-start 24320000 --limit-blocks 2000

    # Explicit range, one schedule, custom out-dir:
    .venv/bin/python scripts/precompute.py \
        --schedules eip-8038 --block-start 24320000 --block-end 24322000 \
        --out-dir /tmp/out

If no block window is supplied the run defaults to a **small** validation window
at the start of the pinned config's range (never a silent full scan).
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import pandas as pd

from repricing_impact import groups
from repricing_impact.clickhouse import get_engine, run_query
from repricing_impact.config import (
    CHAIN_ID,
    FOCUS_SCHEDULES,
    SITE_DATA,
    resolve_config_hash,
)
from repricing_impact.labels import classify_address, label_address
from repricing_impact.opcodes import opcode_name, parse_arr

# --- Defaults --------------------------------------------------------------

# A small, cheap validation window if the caller specifies no bounds. Sized so a
# both-schedules run finishes in well under a minute and never silently scans 1M
# blocks.
DEFAULT_VALIDATION_BLOCKS = 2000

# Per-query block chunk for the expensive _divergence / coverage scans.
DEFAULT_CHUNK_BLOCKS = 50000

# Top-N failing recipients to publish in contract_failures.json.
TOP_N_CONTRACTS = 40

# OOG entry-contract (recipient) leaderboards in oog_forensics.json. We build a
# broad candidate pool ranked by halt count, fetch each one's mainnet total-tx
# denominator once, then publish the top-N of that pool BY HALT COUNT and, from
# the same pool, the top-N BY FAILURE RATE. The rate ranking is gated on a
# minimum total-tx floor so a handful of tiny-denominator contracts (1 tx / 1
# halt = 100%) can't dominate it. The pool is bounded (never a full scan) which
# means a genuinely high-rate contract outside the top-OOG_RECIPIENT_POOL by
# halt count could be missed — acceptable given the min-volume floor makes such
# contracts high-halt-count anyway.
OOG_RECIPIENT_TOP_N = 12
OOG_RECIPIENT_POOL = 200
OOG_RATE_MIN_TOTAL_TX = 100

# Cap on examples.json.
EXAMPLES_CAP = 40

GROUP_LABELS = {
    "g1": "No change",
    "g2": "Succeeds with changes",
    "g3": "Fixable with gas-limit increase",
    "g4": "Potentially broken",
    "af": "Already failing",
    "g5": "Unknown",
}

# eip-8037 carries the per-tx state reservoir -> "state" flavour; eip-8038 is a
# state access/write repricing (non-uniform: storage write 2800->10000, cold access
# ->3000, account write ->8000) -> "opcode" flavour (a legacy key name; see SCHEMA.md).
SCHEDULE_FLAVOUR = {"eip-8037": "state", "eip-8038": "opcode"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    """Flushed progress line so long runs are observable under block buffering."""
    print(msg, flush=True)


def _chunks(start: int, end: int, size: int):
    """Yield inclusive [lo, hi] block sub-ranges covering [start, end]."""
    lo = start
    while lo <= end:
        hi = min(lo + size - 1, end)
        yield lo, hi
        lo = hi + 1


def _chunk_list(start: int, end: int, size: int):
    return list(_chunks(start, end, size))


# Slim deduped per-tx _divergence projection columns materialized into DuckDB.
# Excludes trace_payload (and all other heavy/unused columns). Every downstream
# per-tx aggregate (gas_delta_hist, group_categories, contract_failures,
# examples) reads from this local table — so the ONLY _divergence scans against
# ClickHouse are the chunked block_groups pass and this chunked materialization.
DIVERGENCE_TX_COLUMNS = [
    "block_number",
    "tx_hash",
    "recipient",
    "schedule_success",
    "baseline_success",
    "min_multiplier_to_succeed",
    "gas_delta",
    "baseline_gas_used",
    "is_create",
    "tx_type",
    "has_authorization",
    "input_zero_bytes",
    "input_nonzero_bytes",
    "status_changed",
    "event_logs_changed",
    "output_changed",
    "logs_bloom_changed",
    "oog_pattern",
    "oog_call_depth",
    "replay_halt_oog",
    "oog_bottleneck_kind",
    "state_gas_category",
    "divergence_opcode",
    "divergence_contract",
    "divergence_call_depth",
    "oog_contract",
    "reservoir_exhausted",
    "runtime_state_gas_spillover",
    "schedule_initial_reservoir",
    # OOG halt-site forensics (populated only for OOG halts; NULL otherwise).
    "oog_opcode",
    "oog_gas_remaining",
    "oog_bottleneck_depth",
    # Failure detail (F1/F2/F6) — the "why" behind non-OOG reverts.
    "failure_reason",
    "revert_decoded",
    # Function-selector columns (verified live 2026-07-07): the function called
    # on the entry contract, the function the failing frame lands in (fall back to
    # entry_selector when NULL, ~53% populated over G4), and the selector call-path
    # to the failure (string-repr array; parsed with opcodes.parse_arr, context).
    "entry_selector",
    "tier1_failing_selector",
    "failure_selector_path",
    # Causal repricing-driver counts (the "why" — the repriced state line items).
    # surcharge_at_oog is populated only on OOG halts (~33% over G4); the cold /
    # access-list counts are ~100% populated.
    "surcharge_at_oog",
    "cold_account_access_count",
    "sload_cold_count",
    "sstore_cold_count",
    "access_list_address_count",
    "access_list_storage_key_count",
]


# --- Build-time DuckDB intermediate ---------------------------------------


@dataclass
class RunContext:
    config_hash: str
    schedule: str
    block_start: int
    block_end: int
    chunk_blocks: int
    con: duckdb.DuckDBPyConnection
    engine: object
    truncation: Dict[str, object] = field(default_factory=dict)
    raw_divergence_dups: int = 0

    def base_where(self, table_alias: str = "") -> str:
        p = f"{table_alias}." if table_alias else ""
        return (
            f"{p}chain_id = {CHAIN_ID} "
            f"AND {p}analysis_config_hash = '{self.config_hash}' "
            f"AND {p}schedule_name = '{self.schedule}'"
        )

    def window_where(self) -> str:
        """``base_where`` plus the full block-range bound for the whole run."""
        return (
            f"{self.base_where()} "
            f"AND block_number BETWEEN {self.block_start} AND {self.block_end}"
        )


def _ch(query: str, engine) -> pd.DataFrame:
    return run_query(query, engine=engine)


# ---- Stage 1: per-block coverage + drill-in group counts -> block_groups ----


def stage_block_groups(ctx: RunContext) -> None:
    """Build the ``block_groups`` DuckDB table: per-block groups + rescues + trunc.

    Coverage counts come from ``block_coverage`` (one row/block) and the drill-in
    group counts (G2/G3/G4 + Already failing) from ``_divergence`` aggregated
    server-side per block. Both are chunked by block range; we dedup-guard the
    divergence scan.
    """
    cov_frames: List[pd.DataFrame] = []
    div_frames: List[pd.DataFrame] = []
    raw_dup_total = 0  # informational: raw ReplacingMergeTree duplicate row_ids

    chunks = _chunk_list(ctx.block_start, ctx.block_end, ctx.chunk_blocks)
    for i, (lo, hi) in enumerate(chunks, 1):
        t0 = time.monotonic()
        cov_sql = f"""
            SELECT
                block_number,
                toDate(block_timestamp)      AS day,
                tx_count,
                tx_count_unchanged,
                tx_count_gas_only,
                tx_count_stored,
                retained_drill_in_count,
                toUInt8(drill_ins_truncated) AS truncated
            FROM gas_analysis.gas_analysis_block_coverage
            WHERE {ctx.base_where()}
              AND block_number BETWEEN {lo} AND {hi}
        """
        cov_frames.append(_ch(cov_sql, ctx.engine))

        # Read group counts off a row_id-DEDUPED relation (handles full-scale
        # ReplacingMergeTree duplicates without FINAL / trace_payload).
        deduped = groups.deduped_divergence_subquery(
            columns=[
                "block_number",
                "schedule_success",
                "baseline_success",
                "min_multiplier_to_succeed",
            ],
            where=f"{ctx.base_where()} AND block_number BETWEEN {lo} AND {hi}",
        )
        div_sql = f"""
            SELECT
                block_number,
                {groups.divergence_group_counts_sql()}
            FROM {deduped}
            GROUP BY block_number
        """
        d = _ch(div_sql, ctx.engine)
        # Post-dedup invariant: on the deduped relation one row per row_id, so the
        # guard MUST be 0. Only raise if dedup itself failed.
        post = int(d["dup_rows"].sum()) if not d.empty else 0
        if post != 0:
            raise RuntimeError(
                f"dedup invariant violated for {ctx.schedule} blocks {lo}-{hi}: "
                f"{post} residual duplicate row_ids AFTER argMax dedup."
            )
        # Track raw duplicates (informational) with a cheap guard on the raw table.
        rawg = _ch(
            f"SELECT ({groups.DEDUP_GUARD_SQL}) AS raw_dups "
            f"FROM gas_analysis.gas_analysis_divergence "
            f"WHERE {ctx.base_where()} AND block_number BETWEEN {lo} AND {hi}",
            ctx.engine,
        )
        raw_dup_total += int(rawg.iloc[0]["raw_dups"]) if not rawg.empty else 0
        div_frames.append(d)
        n_drillin = (
            int(d[["g2_drillin", "g3", "g4", "af"]].sum().sum()) if not d.empty else 0
        )
        _log(
            f"[{ctx.schedule}] block_groups chunk {i}/{len(chunks)} "
            f"blocks {lo:,}..{hi:,} -> {n_drillin:,} drill-ins "
            f"({time.monotonic() - t0:.1f}s)"
        )

    cov = pd.concat(cov_frames, ignore_index=True)
    div = (
        pd.concat(div_frames, ignore_index=True)
        if div_frames
        else pd.DataFrame(
            columns=["block_number", "g2_drillin", "g3", "g4", "af", "rescues"]
        )
    )

    # block_coverage is ReplacingMergeTree too, but verified to carry ZERO
    # duplicate row_ids even at full scale (one row per block). Guard cheaply: a
    # duplicate block_number here would inflate every G1/G2 count via the merge.
    if cov["block_number"].duplicated().any():
        raise RuntimeError(
            f"block_coverage returned duplicate block_number rows for "
            f"{ctx.schedule} — dedup coverage before merging (unexpected on this config)."
        )

    if raw_dup_total:
        print(
            f"[{ctx.schedule}] note: {raw_dup_total} raw ReplacingMergeTree "
            f"duplicate row_ids collapsed by argMax dedup (no double-counting)."
        )
    ctx.raw_divergence_dups = raw_dup_total

    merged = cov.merge(
        div[["block_number", "g2_drillin", "g3", "g4", "af", "rescues"]],
        on="block_number",
        how="left",
    )
    for c in ["g2_drillin", "g3", "g4", "af", "rescues"]:
        merged[c] = merged[c].fillna(0).astype("int64")

    merged["g1"] = merged["tx_count_unchanged"].astype("int64")
    merged["g2"] = (merged["tx_count_gas_only"] + merged["g2_drillin"]).astype("int64")
    # g3, g4, af already set
    merged["g5"] = (
        merged["tx_count"]
        - (merged["g1"] + merged["g2"] + merged["g3"] + merged["g4"] + merged["af"])
    ).astype("int64")

    ctx.con.execute("DROP TABLE IF EXISTS block_groups")
    ctx.con.register("block_groups_df", merged)
    ctx.con.execute("CREATE TABLE block_groups AS SELECT * FROM block_groups_df")
    ctx.con.unregister("block_groups_df")

    total_blocks = int(len(merged))
    trunc_blocks = int(merged["truncated"].sum())
    ctx.truncation = {
        "drill_ins_truncated_blocks": trunc_blocks,
        "total_blocks": total_blocks,
        "truncated_share": (
            round(trunc_blocks / total_blocks, 4) if total_blocks else 0.0
        ),
        "note": (
            "Truncated blocks drop their drill-in rows at the 1024 cap, inflating "
            "the Unknown group. Treat Unknown as a coverage gap, not a true partition."
            + (
                f" ({raw_dup_total} raw ReplacingMergeTree duplicate row_ids were "
                "collapsed by argMax dedup; no double-counting.)"
                if raw_dup_total
                else ""
            )
        ),
    }


# ---- Stage 2: slim deduped divergence_tx projection -> DuckDB (CHUNKED) ----

# DuckDB column types for the slim projection (CH Bool -> BOOLEAN via _to_bool;
# nullable numerics stay nullable; addresses/strings VARCHAR).
_DIVTX_DDL = """
CREATE TABLE divergence_tx (
    block_number BIGINT,
    tx_hash VARCHAR,
    recipient VARCHAR,
    schedule_success BOOLEAN,
    baseline_success BOOLEAN,
    min_multiplier_to_succeed DOUBLE,
    gas_delta BIGINT,
    baseline_gas_used BIGINT,
    is_create BOOLEAN,
    tx_type SMALLINT,
    has_authorization SMALLINT,
    input_zero_bytes BIGINT,
    input_nonzero_bytes BIGINT,
    status_changed BOOLEAN,
    event_logs_changed BOOLEAN,
    output_changed BOOLEAN,
    logs_bloom_changed BOOLEAN,
    oog_pattern VARCHAR,
    oog_call_depth INTEGER,
    replay_halt_oog BOOLEAN,
    oog_bottleneck_kind VARCHAR,
    state_gas_category VARCHAR,
    divergence_opcode VARCHAR,
    divergence_contract VARCHAR,
    divergence_call_depth INTEGER,
    oog_contract VARCHAR,
    reservoir_exhausted BOOLEAN,
    runtime_state_gas_spillover UBIGINT,
    schedule_initial_reservoir UBIGINT,
    oog_opcode VARCHAR,
    oog_gas_remaining BIGINT,
    oog_bottleneck_depth INTEGER,
    failure_reason VARCHAR,
    revert_decoded VARCHAR,
    entry_selector VARCHAR,
    tier1_failing_selector VARCHAR,
    failure_selector_path VARCHAR,
    surcharge_at_oog BIGINT,
    cold_account_access_count BIGINT,
    sload_cold_count BIGINT,
    sstore_cold_count BIGINT,
    access_list_address_count BIGINT,
    access_list_storage_key_count BIGINT
)
"""

_DIVTX_BOOL_COLS = (
    "schedule_success",
    "baseline_success",
    "is_create",
    "status_changed",
    "event_logs_changed",
    "output_changed",
    "logs_bloom_changed",
    "replay_halt_oog",
    "reservoir_exhausted",
)

# UInt64 reservoir columns. The warehouse encodes "no/unlimited reservoir" as a
# near-``2^64`` sentinel; over the HTTP driver it arrives as a float that both
# overflows DuckDB's UBIGINT range and would poison ``avg()`` — so we null any
# value at/above ``2**63`` (real reservoirs are many orders of magnitude smaller).
_DIVTX_UINT64_COLS = (
    "runtime_state_gas_spillover",
    "schedule_initial_reservoir",
)
_UINT64_SENTINEL_FLOOR = 2**63

# Nullable small-int / byte-count columns (tx shape, F5). Over the HTTP driver
# they arrive as strings/floats with NaN for NULL, which cannot cast into an
# integer DuckDB column — coerce to a nullable Python int so NULL stays NULL.
_DIVTX_INT_NULLABLE_COLS = (
    "tx_type",
    "has_authorization",
    "input_zero_bytes",
    "input_nonzero_bytes",
    # OOG halt-site numerics: gas left at the halt and the proven-bottleneck depth.
    "oog_gas_remaining",
    "oog_bottleneck_depth",
    # Non-OOG revert call depth (first-divergence frame; NULL otherwise).
    "divergence_call_depth",
    # Causal repricing-driver counts (§1b). surcharge_at_oog is Int64 (populated
    # only on OOG halts); the rest are UInt64 line-item counts far below the
    # reservoir sentinel, so they coerce cleanly through _to_int_nullable.
    "surcharge_at_oog",
    "cold_account_access_count",
    "sload_cold_count",
    "sstore_cold_count",
    "access_list_address_count",
    "access_list_storage_key_count",
)

# Nullable selector (hex string) columns. Over the HTTP driver a NULL VARCHAR can
# arrive as NaN / '' / '\n'; normalize those to SQL NULL so a missing selector
# never becomes a spurious cluster key. Selectors are hex — case is preserved.
_DIVTX_SELECTOR_COLS = (
    "entry_selector",
    "tier1_failing_selector",
    "failure_selector_path",
)


def stage_divergence_tx(ctx: RunContext) -> int:
    """Materialize the slim, row_id-DEDUPED per-tx ``_divergence`` projection.

    One **chunked** ClickHouse pass (no ``trace_payload``, argMax dedup per
    ``row_id``) lands the slim columns into the DuckDB ``divergence_tx`` table.
    All downstream per-tx aggregates then run **locally in DuckDB** — so no
    further ``_divergence`` scan hits the shared warehouse, and no single CH
    query covers the whole range. Returns the total retained row count.
    """
    ctx.con.execute("DROP TABLE IF EXISTS divergence_tx")
    ctx.con.execute(_DIVTX_DDL)

    chunks = _chunk_list(ctx.block_start, ctx.block_end, ctx.chunk_blocks)
    total = 0
    for i, (lo, hi) in enumerate(chunks, 1):
        t0 = time.monotonic()
        deduped = groups.deduped_divergence_subquery(
            columns=DIVERGENCE_TX_COLUMNS,
            where=f"{ctx.base_where()} AND block_number BETWEEN {lo} AND {hi}",
        )
        # Decode opcode byte -> mnemonic server-side is not available; pull the
        # raw byte and map in pandas. Bools come back as 'true'/'false' strings.
        df = _ch(
            f"SELECT {', '.join(DIVERGENCE_TX_COLUMNS)} FROM {deduped}",
            ctx.engine,
        )
        if not df.empty:
            for c in _DIVTX_BOOL_COLS:
                df[c] = df[c].map(_to_bool_nullable)
            # UInt64 reservoir columns: the warehouse uses a near-``2^64`` sentinel
            # for "no/unlimited reservoir", which (a) overflows DuckDB's UBIGINT
            # cast when the HTTP driver hands it back as a float and (b) would
            # corrupt the reservoir averages. Null those out.
            for c in _DIVTX_UINT64_COLS:
                df[c] = df[c].map(_to_uint64_nullable)
            for c in _DIVTX_INT_NULLABLE_COLS:
                df[c] = df[c].map(_to_int_nullable)
            # Selector VARCHARs: normalize HTTP-driver NULLs (NaN/''/'\n') to None
            # so an absent selector never seeds a spurious cluster key.
            for c in _DIVTX_SELECTOR_COLS:
                df[c] = df[c].map(_to_str_nullable)
            # opcode byte -> mnemonic (kept as string; NULL stays NULL)
            for oc in ("divergence_opcode", "oog_opcode"):
                df[oc] = df[oc].map(lambda b: opcode_name(b) if pd.notna(b) else None)
            # normalize address case for stable joins/labels
            for c in ("recipient", "divergence_contract", "oog_contract"):
                df[c] = df[c].map(lambda v: v.lower() if isinstance(v, str) else None)
            ctx.con.register("divtx_chunk", df)
            ctx.con.execute(
                "INSERT INTO divergence_tx SELECT "
                + ", ".join(DIVERGENCE_TX_COLUMNS)
                + " FROM divtx_chunk"
            )
            ctx.con.unregister("divtx_chunk")
        total += len(df)
        _log(
            f"[{ctx.schedule}] divergence_tx chunk {i}/{len(chunks)} "
            f"blocks {lo:,}..{hi:,} -> {len(df):,} rows "
            f"({time.monotonic() - t0:.1f}s)"
        )

    # Post-dedup invariant: divergence_tx must equal the retained drill-in count
    # (one row per retained drill-in tx). block_groups already has retained.
    retained = int(
        ctx.con.execute(
            "SELECT SUM(retained_drill_in_count) FROM block_groups"
        ).fetchone()[0]
        or 0
    )
    if total != retained:
        raise RuntimeError(
            f"divergence_tx row count {total} != retained_drill_in_count "
            f"{retained} for {ctx.schedule} (dedup/materialization mismatch)."
        )
    _log(f"[{ctx.schedule}] divergence_tx complete: {total:,} deduped per-tx rows")
    return total


# ---- meta.json + overview_series.json (from block_groups) ----


def emit_overview_series(ctx: RunContext) -> dict:
    """Per-day-bucketed group composition + totals + rescues (SCHEMA §2)."""
    buckets = ctx.con.execute("""
        SELECT
            CAST(day AS VARCHAR)        AS date,
            MIN(block_number)           AS block_start,
            MAX(block_number)           AS block_end,
            SUM(tx_count)               AS tx_count,
            SUM(g1) AS g1, SUM(g2) AS g2, SUM(g3) AS g3, SUM(g4) AS g4,
            SUM(af) AS af, SUM(g5) AS g5,
            SUM(rescues)                AS rescues,
            SUM(CAST(truncated AS INTEGER)) AS drill_ins_truncated_blocks
        FROM block_groups
        GROUP BY day
        ORDER BY day
        """).df()

    bucket_list = [
        {
            "date": r["date"],
            "block_start": int(r["block_start"]),
            "block_end": int(r["block_end"]),
            "tx_count": int(r["tx_count"]),
            "g1": int(r["g1"]),
            "g2": int(r["g2"]),
            "g3": int(r["g3"]),
            "g4": int(r["g4"]),
            "af": int(r["af"]),
            "g5": int(r["g5"]),
            "rescues": int(r["rescues"]),
            "drill_ins_truncated_blocks": int(r["drill_ins_truncated_blocks"]),
        }
        for _, r in buckets.iterrows()
    ]

    totals = _totals(ctx)
    return {
        "schedule": ctx.schedule,
        "bucket_by": "day",
        "buckets": bucket_list,
        "totals": totals,
        "group_labels": GROUP_LABELS,
    }


def _totals(ctx: RunContext) -> dict:
    r = ctx.con.execute("""
        SELECT SUM(tx_count) tx, SUM(g1) g1, SUM(g2) g2, SUM(g3) g3, SUM(g4) g4,
               SUM(af) af, SUM(g5) g5, SUM(rescues) rescues,
               SUM(CAST(truncated AS INTEGER)) trunc
        FROM block_groups
        """).fetchone()
    return {
        "tx_count": int(r[0] or 0),
        "g1": int(r[1] or 0),
        "g2": int(r[2] or 0),
        "g3": int(r[3] or 0),
        "g4": int(r[4] or 0),
        "af": int(r[5] or 0),
        "g5": int(r[6] or 0),
        "rescues": int(r[7] or 0),
        "drill_ins_truncated_blocks": int(r[8] or 0),
    }


def emit_meta(ctx: RunContext, schedules_available: List[str], cfg_source: str) -> dict:
    totals = _totals(ctx)
    rng = ctx.con.execute(
        "SELECT MIN(block_number), MAX(block_number), MIN(day), MAX(day) FROM block_groups"
    ).fetchone()
    b_start, b_end, d_min, d_max = rng
    overview_totals = {
        k: totals[k]
        for k in ("tx_count", "g1", "g2", "g3", "g4", "af", "g5", "rescues")
    }
    return {
        "schedule": ctx.schedule,
        "schedules_available": schedules_available,
        "analysis_config_hash": ctx.config_hash,
        "chain_id": CHAIN_ID,
        "generated_at": _now_iso(),
        "block_range": {
            "start": int(b_start),
            "end": int(b_end),
            "count": int(b_end) - int(b_start) + 1,
        },
        "date_range": {"start": str(d_min), "end": str(d_max)},
        "totals": overview_totals,
        "group_labels": GROUP_LABELS,
        "truncation": ctx.truncation,
        "manifest": {
            "source": "ClickHouse gas_analysis warehouse (per-block/per-tx replays)",
            "schedule_name": ctx.schedule,
            "config_selected_by": cfg_source,
        },
        "pinned_config_note": (
            "Pinned to chain_id=1 and a single analysis_config_hash; all aggregates "
            "derive the group partition once in groups.py."
        ),
    }


# ---- gas_delta_hist.json ----


def _log2_hist_to_bins(hist: List[int]) -> List[dict]:
    """Map a length-N ``gas_delta_log2_hist`` array to SCHEMA bins (sign=1)."""
    out = []
    for i, c in enumerate(hist):
        if c:
            out.append({"bin_log2": int(i), "sign": 1, "count": int(c)})
    return out


# --- block_summary-compatible log2 binning (the producer's ``log2_bin``) --------
#
# ``gas_analysis_block_summary.gas_delta_log2_hist`` is a 12-element array whose
# index is the producer's ``block_aggregator.rs::log2_bin``:
#   bin 0  = exact zero delta
#   bin k  (1..10) = |delta| in [2^(k-1), 2^k)   (i.e. bits = 1 + floor(log2))
#   bin 11 = catch-all, |delta| >= 1024
# The G2 gas histogram combines this aggregate cohort with per-tx drill-in
# members, so the drill-in members MUST be binned with the SAME definition (the
# previous ``floor(log2)`` binning was off-by-one and unclamped, mixing two
# incompatible histograms). This SQL fragment reproduces ``log2_bin`` in DuckDB.
GAS_LOG2_BIN_SQL = (
    "CASE WHEN gas_delta = 0 THEN 0 "
    "ELSE least(CAST(floor(log2(abs(gas_delta))) AS INTEGER) + 1, 11) END"
)

# Real gas-unit ranges for bins 1..11 (bin 0 = exact zero is not a "gas change").
# ``hi`` is exclusive; ``None`` marks the >= 1024 catch-all.
_GAS_BIN_RANGES = {i: (2 ** (i - 1), 2**i) for i in range(1, 11)}
_GAS_BIN_RANGES[11] = (1024, None)


# --- Percent gas-diff histogram (G3/G4 drill-in members) --------------------
#
# Signed bin edges for ``100 * gas_delta / baseline_gas_used`` per tx, taken from
# docs/producer-data-recommendations.md (Recommendation 2). Left-closed,
# right-open. ``pct`` is bounded below at -100% (``schedule_gas_used >= 0``), so
# the lowest bin is ``[-100, -50)``; the costlier tail runs far past +100%, so it
# is split rather than capped, with ``[500, +inf)`` as the catch-all.
#
# NOTE: computable here ONLY for the G3/G4 drill-in cohorts, which carry per-tx
# ``baseline_gas_used``. The G2 ``gas_only`` cohort is collapsed to per-block
# aggregates with no per-tx pairing and no class-grain baseline denominator, so
# its percentage distribution needs a producer-side ``gas_diff_pct_hist`` column
# (see the doc) and stays absolute here.
GAS_PCT_BIN_EDGES = [-100, -50, -25, -10, -1, 0, 1, 10, 25, 50, 100, 200, 500]


def _pct_bin_case_sql(expr: str) -> str:
    """SQL CASE mapping a percent ``expr`` to a :data:`GAS_PCT_BIN_EDGES` index.

    Left-closed/right-open: index ``i`` is ``[edges[i], edges[i+1])``; the final
    index is the ``[edges[-1], +inf)`` catch-all. Arithmetic floor guarantees
    ``expr >= -100``, so nothing lands below index 0.
    """
    parts = ["CASE"]
    for i, edge in enumerate(GAS_PCT_BIN_EDGES[1:]):
        parts.append(f"WHEN {expr} < {edge} THEN {i}")
    parts.append(f"ELSE {len(GAS_PCT_BIN_EDGES) - 1} END")
    return " ".join(parts)


def _pct_bins(counts: Dict[int, int]) -> List[dict]:
    """``[{lo, hi, count}]`` over :data:`GAS_PCT_BIN_EDGES` (hi excl, None=catch-all)."""
    edges = GAS_PCT_BIN_EDGES
    out = []
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        out.append({"lo": lo, "hi": hi, "count": int(counts.get(i, 0))})
    return out


def _gas_bins(count_gas_only: List[int], count_drillin: List[int]) -> List[dict]:
    """Real-gas-unit bins 1..11 combining the two G2 cohorts (bin 0 omitted).

    ``count_gas_only`` / ``count_drillin`` are 12-element arrays indexed by the
    producer ``log2_bin``. Bin 0 (exact-zero delta) is dropped — this histogram is
    "txs with a gas change" (``gas_delta != 0``).
    """
    out = []
    for i in range(1, 12):
        lo, hi = _GAS_BIN_RANGES[i]
        go = int(count_gas_only[i]) if i < len(count_gas_only) else 0
        dr = int(count_drillin[i]) if i < len(count_drillin) else 0
        out.append(
            {
                "lo": lo,
                "hi": hi,
                "count_gas_only": go,
                "count_drillin": dr,
                "count": go + dr,
            }
        )
    return out


def _percentiles_from_log2_hist(hist: List[int]) -> dict:
    """Approximate percentiles from a log2 magnitude histogram (bin midpoints)."""
    total = sum(hist)
    pcts = {
        "p01": 0.01,
        "p10": 0.10,
        "p25": 0.25,
        "p50": 0.50,
        "p75": 0.75,
        "p90": 0.90,
        "p99": 0.99,
    }
    out: Dict[str, int] = {}
    if total == 0:
        return {k: 0 for k in pcts}
    cum = 0
    targets = {k: v * total for k, v in pcts.items()}
    # midpoint of bin i = 1.5 * 2^i (between 2^i and 2^(i+1))
    bin_mid = [int(1.5 * (2**i)) for i in range(len(hist))]
    remaining = dict(targets)
    for i, c in enumerate(hist):
        cum += c
        for k, t in list(remaining.items()):
            if cum >= t:
                out[k] = bin_mid[i]
                del remaining[k]
    # any unfilled (shouldn't happen) -> last populated bin
    last = max((i for i, c in enumerate(hist) if c), default=0)
    for k in pcts:
        out.setdefault(k, bin_mid[last])
    return out


def emit_gas_delta_hist(ctx: RunContext) -> dict:
    """G2 real-gas-unit histogram + G3/G4 signed log2 histograms.

    G2 covers **txs with a gas change** (``gas_delta != 0``): the ``gas_only``
    aggregate cohort (from ``block_summary.gas_delta_log2_hist``) combined with the
    per-tx drill-in members whose gas changed. Both cohorts are binned with the
    producer's ``log2_bin`` (see :data:`GAS_LOG2_BIN_SQL`) so the drill-in members
    land in the same buckets as the aggregate hist, then reported as real gas-unit
    bins (:func:`_gas_bins`). G3/G4: signed exact per-tx from ``_divergence``.
    """
    groups_out: Dict[str, dict] = {}

    # --- G2 aggregate (gas_only) cohort: sum the block_summary log2 hist ---
    # block_summary is chunked by block_number (no dups at full scale; one row
    # per (block,class)). This is the only remaining CH scan in this stage. The
    # 12-bin array is indexed by the producer log2_bin (bin 0 = exact zero, empty
    # for gas_only since it is gas_delta != 0 by definition).
    g2_go_hist = [0] * 12
    go_sum = go_min = go_max = 0
    go_count = 0
    have_go = False
    bs_chunks = _chunk_list(ctx.block_start, ctx.block_end, ctx.chunk_blocks)
    for i, (lo, hi) in enumerate(bs_chunks, 1):
        t0 = time.monotonic()
        r = _ch(
            f"""
            SELECT
                sumForEach(gas_delta_log2_hist) AS h,
                sum(gas_delta_sum)              AS s,
                min(gas_delta_min)              AS mn,
                max(gas_delta_max)              AS mx,
                sum(tx_count)                   AS n
            FROM gas_analysis.gas_analysis_block_summary
            WHERE {ctx.base_where()} AND class = 'gas_only'
              AND block_number BETWEEN {lo} AND {hi}
            """,
            ctx.engine,
        )
        if r.empty or r.iloc[0]["n"] is None:
            _log(
                f"[{ctx.schedule}] gas_hist/g2 block_summary chunk {i}/{len(bs_chunks)} "
                f"blocks {lo:,}..{hi:,} -> 0 gas_only ({time.monotonic() - t0:.1f}s)"
            )
            continue
        h = parse_arr(r.iloc[0]["h"])
        for j, v in enumerate(h):
            if j < len(g2_go_hist):
                g2_go_hist[j] += int(v)
        go_sum += int(r.iloc[0]["s"] or 0)
        go_min = (
            min(go_min, int(r.iloc[0]["mn"] or 0))
            if have_go
            else int(r.iloc[0]["mn"] or 0)
        )
        go_max = (
            max(go_max, int(r.iloc[0]["mx"] or 0))
            if have_go
            else int(r.iloc[0]["mx"] or 0)
        )
        go_count += int(r.iloc[0]["n"] or 0)
        have_go = True
        _log(
            f"[{ctx.schedule}] gas_hist/g2 block_summary chunk {i}/{len(bs_chunks)} "
            f"blocks {lo:,}..{hi:,} -> {int(r.iloc[0]['n'] or 0):,} gas_only tx "
            f"({time.monotonic() - t0:.1f}s)"
        )

    # --- G2 drill-in members with a gas change: same log2_bin, LOCAL DuckDB ---
    g2_dr_hist = [0] * 12
    r = ctx.con.execute(f"""
        SELECT {GAS_LOG2_BIN_SQL} AS b, count(*) AS c, sum(gas_delta) AS s,
               min(gas_delta) AS mn, max(gas_delta) AS mx
        FROM divergence_tx
        WHERE ({groups.G2_DRILLIN_PREDICATE}) AND gas_delta != 0
        GROUP BY b
        """).df()
    dr_sum = dr_count = 0
    dr_min = dr_max = None
    for _, row in r.iterrows():
        idx = int(row["b"])
        if 0 <= idx < len(g2_dr_hist):
            g2_dr_hist[idx] += int(row["c"])
        dr_sum += int(row["s"] or 0)
        dr_count += int(row["c"])
        mn, mx = row["mn"], row["mx"]
        dr_min = int(mn) if dr_min is None else min(dr_min, int(mn))
        dr_max = int(mx) if dr_max is None else max(dr_max, int(mx))

    g2_count = go_count + dr_count
    g2_sum = go_sum + dr_sum
    mins = [v for v in (go_min if have_go else None, dr_min) if v is not None]
    maxs = [v for v in (go_max if have_go else None, dr_max) if v is not None]
    groups_out["2"] = {
        "label": GROUP_LABELS["g2"],
        "signed": False,
        "note": (
            "Txs with a gas change (gas_delta != 0): block_summary gas_only cohort "
            "+ Succeeds-with-changes drill-in members, binned by the producer log2 "
            "bucket. The >=1024-gas bin is a catch-all — the aggregate cohort has no "
            "finer resolution above 1024 gas."
        ),
        "count": int(g2_count),
        "gas_bins": _gas_bins(g2_go_hist, g2_dr_hist),
        "sum_gas_delta": int(g2_sum),
        "min_gas_delta": int(min(mins)) if mins else 0,
        "max_gas_delta": int(max(maxs)) if maxs else 0,
    }

    # --- G3 / G4: signed exact per-tx histograms + percentiles ---
    for gnum, pred in (("3", groups.G3_PREDICATE), ("4", groups.G4_PREDICATE)):
        groups_out[gnum] = _signed_per_tx_hist(ctx, pred, GROUP_LABELS[f"g{gnum}"])

    return {"schedule": ctx.schedule, "groups": groups_out}


def _signed_per_tx_hist(ctx: RunContext, predicate: str, label: str) -> dict:
    """Signed log2 histogram + percentiles + sum/min/max for a drill-in predicate.

    Computed **locally in DuckDB** over the materialized ``divergence_tx`` table
    (no further ClickHouse scan). ``predicate`` is a ``groups`` G3/G4 SQL string
    valid in both ClickHouse and DuckDB.
    """
    r = ctx.con.execute(f"""
        SELECT
            CASE WHEN gas_delta < 0 THEN -1 ELSE 1 END AS sign,
            CAST(CASE WHEN abs(gas_delta) < 1 THEN 0
                      ELSE floor(log2(abs(gas_delta))) END AS INTEGER) AS bin_log2,
            count(*) AS c
        FROM divergence_tx
        WHERE ({predicate})
        GROUP BY sign, bin_log2
        """).df()
    agg = ctx.con.execute(f"""
        SELECT count(*) c, sum(gas_delta) s, min(gas_delta) mn, max(gas_delta) mx
        FROM divergence_tx
        WHERE ({predicate})
        """).fetchone()

    bins: Dict[tuple, int] = {}
    abs_hist = [0] * 64
    for _, row in r.iterrows():
        sign = int(row["sign"])
        b = int(row["bin_log2"])
        bins[(sign, b)] = bins.get((sign, b), 0) + int(row["c"])
        if 0 <= b < len(abs_hist):
            abs_hist[b] += int(row["c"])

    count = int(agg[0] or 0)
    total_sum = int(agg[1] or 0)
    gmin = int(agg[2]) if agg[2] is not None else 0
    gmax = int(agg[3]) if agg[3] is not None else 0

    bin_list = [
        {"bin_log2": b, "sign": s, "count": c}
        for (s, b), c in sorted(bins.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]

    # Percent histogram: per-tx 100*gas_delta/baseline_gas_used over the fixed
    # signed edges. Rows lacking a usable denominator (NULL / <= 0 baseline) can't
    # form a ratio and are excluded from the pct hist (reported via pct_excluded).
    pr = ctx.con.execute(f"""
        SELECT {_pct_bin_case_sql("100.0 * gas_delta / baseline_gas_used")} AS b,
               count(*) AS c
        FROM divergence_tx
        WHERE ({predicate})
          AND baseline_gas_used IS NOT NULL AND baseline_gas_used > 0
        GROUP BY b
        """).df()
    pct_counts = {int(row["b"]): int(row["c"]) for _, row in pr.iterrows()}
    pct_covered = sum(pct_counts.values())

    return {
        "label": label,
        "signed": True,
        "note": "Signed exact per-tx gas_delta (negative = schedule cheaper).",
        "count": count,
        "bins": bin_list,
        "percentiles": _percentiles_from_log2_hist(abs_hist),
        "sum_gas_delta": total_sum,
        "min_gas_delta": gmin,
        "max_gas_delta": gmax,
        "pct_bins": _pct_bins(pct_counts),
        "pct_covered_count": int(pct_covered),
        "pct_note": (
            "Per-tx 100*gas_delta/baseline_gas_used (share of baseline gas used; "
            "negative = schedule cheaper), fixed signed bins. Bounded below at "
            "-100%; the >=500% bin is a catch-all."
            + (
                f" Excludes {count - pct_covered:,} txs with no usable baseline gas."
                if count - pct_covered
                else ""
            )
        ),
    }


# ---- group_categories.json ----


def _mix(rows, key_col="k", count_col="n") -> List[dict]:
    return [
        {
            "key": (str(r[key_col]) if pd.notna(r[key_col]) else "none"),
            "count": int(r[count_col]),
        }
        for _, r in rows.iterrows()
    ]


def emit_group_categories(ctx: RunContext) -> dict:
    flavour = SCHEDULE_FLAVOUR.get(ctx.schedule, "opcode")
    out = {"schedule": ctx.schedule, "flavour": flavour}

    out["g2"] = _g2_categories(ctx)
    out["g3"] = _g3_categories(ctx, flavour)
    out["g4"] = _g4_categories(ctx, flavour)
    return out


def _g2_categories(ctx: RunContext) -> dict:
    # State-driver mix + gas_only tx_count from block_summary (gas_only cohort).
    # The opcode gas-shift leaderboard was dropped — it is no longer surfaced on
    # the transaction-failures page, and dropping it removes an expensive
    # per-opcode GROUP BY.
    sd_ns = sd_rs = sd_cr = sd_au = 0
    gas_only_count = 0
    bs_chunks = _chunk_list(ctx.block_start, ctx.block_end, ctx.chunk_blocks)
    for ci, (lo, hi) in enumerate(bs_chunks, 1):
        t0 = time.monotonic()
        sd = _ch(
            f"""
            SELECT sum(tx_count_no_state) ns, sum(tx_count_runtime_state) rs,
                   sum(tx_count_creation) cr, sum(tx_count_authorization) au,
                   sum(tx_count) n
            FROM gas_analysis.gas_analysis_block_summary
            WHERE {ctx.base_where()} AND class = 'gas_only'
              AND block_number BETWEEN {lo} AND {hi}
            """,
            ctx.engine,
        ).iloc[0]
        sd_ns += int(sd["ns"] or 0)
        sd_rs += int(sd["rs"] or 0)
        sd_cr += int(sd["cr"] or 0)
        sd_au += int(sd["au"] or 0)
        gas_only_count += int(sd["n"] or 0)
        _log(
            f"[{ctx.schedule}] group_cat/g2 block_summary chunk {ci}/{len(bs_chunks)} "
            f"blocks {lo:,}..{hi:,} ({time.monotonic() - t0:.1f}s)"
        )

    # state-driver mix from block_summary gas_only cohort (accumulated chunked).
    state_driver_mix = [
        {"key": "no_state", "count": sd_ns},
        {"key": "runtime_state", "count": sd_rs},
        {"key": "creation", "count": sd_cr},
        {"key": "authorization", "count": sd_au},
    ]

    # G2 drill-in members — LOCAL DuckDB. status_changed is definitionally false
    # here (both replays succeed), so it is not a change type. The change-type
    # flags are OVERLAPPING properties (a tx can have several) — see the note.
    pred = groups.G2_DRILLIN_PREDICATE
    dr = ctx.con.execute(f"""
        SELECT
            count(*)                                             AS n_drillin,
            count(*) FILTER (WHERE gas_delta != 0)               AS n_gas_changed,
            count(*) FILTER (WHERE event_logs_changed)           AS n_event_logs,
            count(*) FILTER (WHERE output_changed)               AS n_output,
            count(*) FILTER (WHERE logs_bloom_changed)           AS n_logs_bloom,
            count(*) FILTER (WHERE NOT event_logs_changed
                             AND NOT output_changed
                             AND NOT logs_bloom_changed)         AS n_trace_only
        FROM divergence_tx
        WHERE ({pred})
        """).fetchone()
    drillin_count = int(dr[0] or 0)

    # "gas_changed" spans BOTH cohorts: every gas_only tx has gas_delta != 0, plus
    # the drill-in members whose gas changed. It is NOT the gas_only class count.
    change_type_mix = [
        {"key": "gas_changed", "count": gas_only_count + int(dr[1] or 0)},
        {"key": "event_logs_changed", "count": int(dr[2] or 0)},
        {"key": "output_changed", "count": int(dr[3] or 0)},
        {"key": "logs_bloom_changed", "count": int(dr[4] or 0)},
        {"key": "trace_only", "count": int(dr[5] or 0)},
    ]

    # State-gas driver for the drill-in subset, from the per-tx state_gas_category
    # enum (access_list / authorization / contract_creation / transfer_new_account),
    # with unclassified rows folded into 'none' so the shares cover the WHOLE
    # subset (not just the categorised rows). Different taxonomy from the gas_only
    # cohort's driver counts above — the two subsets are surfaced side by side,
    # each in its native categories.
    sdd = ctx.con.execute(f"""
        SELECT coalesce(state_gas_category, 'none') AS k, count(*) AS n
        FROM divergence_tx WHERE ({pred})
        GROUP BY k ORDER BY n DESC
        """).df()
    state_driver_mix_drillin = [
        {"key": str(row["k"]), "count": int(row["n"])} for _, row in sdd.iterrows()
    ]

    return {
        "label": GROUP_LABELS["g2"],
        "count": int(_totals(ctx)["g2"]),
        "gas_only_count": int(gas_only_count),
        "drillin_count": drillin_count,
        "state_driver_mix": state_driver_mix,
        "state_driver_mix_drillin": state_driver_mix_drillin,
        "change_type_mix": change_type_mix,
        "change_type_note": (
            "Change types are non-exclusive: a tx can be counted under several "
            "(e.g. a gas change plus a logs-bloom change), so they do not sum to "
            "the group total. gas_changed spans the gas_only cohort and drill-in "
            "members with gas_delta != 0; the other flags exist only on drill-in "
            "members."
        ),
    }


def _ddb_mix(ctx: RunContext, col: str, where: str) -> List[dict]:
    """``[{key,count}]`` for a column over divergence_tx (NULL -> 'none')."""
    r = ctx.con.execute(
        f"SELECT {col} AS k, count(*) AS n FROM divergence_tx "
        f"WHERE {where} AND {col} IS NOT NULL GROUP BY k ORDER BY n DESC"
    ).df()
    return [{"key": str(row["k"]), "count": int(row["n"])} for _, row in r.iterrows()]


# --- Transaction-shape / EIP-2718 type taxonomies (drill-in cohorts) ----------
#
# Per-tx transaction *shape* and EIP-2718 *type*, from the F5 tx-shape facts on
# ``_divergence`` (``is_create``, ``has_authorization``, ``input_*_bytes``,
# ``tx_type``). These cover the WHOLE of G3/G4 (both are entirely drill-in rows),
# unlike ``state_gas_category`` which is NULL for most txs and describes the
# state-op driver rather than tx shape. NOT emitted for G2: its bulk ``gas_only``
# cohort has no per-tx rows, and the G2 drill-in slice is unrepresentative (see
# docs/producer-data-recommendations.md, Recommendation 1).

# Mutually exclusive shape, in precedence order (a set-code tx with calldata is an
# authorization, not a contract_call). Every tx lands in exactly one bucket.
TX_SHAPE_CASE = (
    "CASE WHEN is_create THEN 'contract_creation' "
    "WHEN coalesce(has_authorization, 0) > 0 THEN 'authorization' "
    "WHEN coalesce(input_zero_bytes, 0) + coalesce(input_nonzero_bytes, 0) = 0 "
    "AND recipient IS NOT NULL THEN 'simple_transfer' "
    "ELSE 'contract_call' END"
)
TX_SHAPE_ORDER = [
    "simple_transfer",
    "contract_call",
    "contract_creation",
    "authorization",
]

# EIP-2718 tx_type byte -> label. NULL / unrecognised -> 'unknown'.
TX_TYPE_CASE = (
    "CASE tx_type WHEN 0 THEN 'legacy' WHEN 1 THEN 'access_list' "
    "WHEN 2 THEN 'dynamic_fee' WHEN 3 THEN 'blob' WHEN 4 THEN 'set_code' "
    "ELSE 'unknown' END"
)
TX_TYPE_ORDER = ["legacy", "access_list", "dynamic_fee", "blob", "set_code", "unknown"]


def _case_mix(
    ctx: RunContext, case_sql: str, predicate: str, order: List[str]
) -> List[dict]:
    """``[{key,count}]`` for a CASE-derived taxonomy over divergence_tx.

    Returns every key in ``order`` (zero-filled) so the categories are stable
    across schedules/groups regardless of which appear in a given window.
    """
    r = ctx.con.execute(
        f"SELECT {case_sql} AS k, count(*) AS n FROM divergence_tx "
        f"WHERE ({predicate}) GROUP BY k"
    ).df()
    counts = {str(row["k"]): int(row["n"]) for _, row in r.iterrows()}
    return [{"key": k, "count": counts.get(k, 0)} for k in order]


def _g3_categories(ctx: RunContext, flavour: str) -> dict:
    """G3 categorization — LOCAL DuckDB over the materialized divergence_tx."""
    pred = groups.G3_PREDICATE
    # Bins span the real (1, 10] sweep range (min_mult = schedule_gas_used /
    # tx_gas_limit, measured at the 10x ceiling; empirical max 9.9979). The top
    # bin is left open-ended (> 8) so the histogram always totals the whole G3
    # cohort, but it is effectively (8, 10] since nothing exceeds the ceiling.
    mh = ctx.con.execute(f"""SELECT
            count(*) FILTER (WHERE min_multiplier_to_succeed > 1 AND min_multiplier_to_succeed <= 2) m2,
            count(*) FILTER (WHERE min_multiplier_to_succeed > 2 AND min_multiplier_to_succeed <= 4) m4,
            count(*) FILTER (WHERE min_multiplier_to_succeed > 4 AND min_multiplier_to_succeed <= 6) m6,
            count(*) FILTER (WHERE min_multiplier_to_succeed > 6 AND min_multiplier_to_succeed <= 8) m8,
            count(*) FILTER (WHERE min_multiplier_to_succeed > 8) m10
        FROM divergence_tx WHERE ({pred})""").fetchone()
    multiplier_histogram = [
        {"multiplier": 2, "count": int(mh[0] or 0)},
        {"multiplier": 4, "count": int(mh[1] or 0)},
        {"multiplier": 6, "count": int(mh[2] or 0)},
        {"multiplier": 8, "count": int(mh[3] or 0)},
        {"multiplier": 10, "count": int(mh[4] or 0)},
    ]
    isc = ctx.con.execute(f"""SELECT count(*) FILTER (WHERE is_create) t,
                   count(*) FILTER (WHERE NOT is_create) f
            FROM divergence_tx WHERE ({pred})""").fetchone()
    out = {
        "label": GROUP_LABELS["g3"],
        "count": int(_totals(ctx)["g3"]),
        "multiplier_histogram": multiplier_histogram,
        "state_gas_category": _ddb_mix(ctx, "state_gas_category", f"({pred})"),
        "tx_shape_mix": _case_mix(ctx, TX_SHAPE_CASE, pred, TX_SHAPE_ORDER),
        "tx_type_mix": _case_mix(ctx, TX_TYPE_CASE, pred, TX_TYPE_ORDER),
        "is_create": {"true": int(isc[0] or 0), "false": int(isc[1] or 0)},
        "oog_pattern": _ddb_mix(ctx, "oog_pattern", f"({pred})"),
    }
    if flavour == "state":
        out["reservoir"] = _reservoir(ctx, pred, include_initial=False)
    return out


def _g4_categories(ctx: RunContext, flavour: str) -> dict:
    """G4 categorization — LOCAL DuckDB over the materialized divergence_tx."""
    pred = groups.G4_PREDICATE
    # fixability: the AUTHORITATIVE "does more gas help" split, keyed on the
    # top-tier (10x) halt kind (replay_halt_oog). not_gas_fixable = non-gas halt
    # at 10x (no limit rescues it -> genuinely broken; ~99.98% of G4);
    # still_oog_at_ceiling = still OOG at 10x (needs >10x or unbounded loop ->
    # unknown); unknown = the ~0 remainder where replay_halt_oog is NULL.
    fx = ctx.con.execute(f"""SELECT
            count(*) FILTER (WHERE replay_halt_oog = false) AS not_gas_fixable,
            count(*) FILTER (WHERE replay_halt_oog = true)  AS still_oog_at_ceiling,
            count(*) FILTER (WHERE replay_halt_oog IS NULL) AS unknown
        FROM divergence_tx WHERE ({pred})""").fetchone()
    fixability = [
        {"key": "not_gas_fixable", "count": int(fx[0] or 0)},
        {"key": "still_oog_at_ceiling", "count": int(fx[1] or 0)},
        {"key": "unknown", "count": int(fx[2] or 0)},
    ]
    # break_reason: a DIFFERENT question — did the tx hit an OOG wall at the
    # ORIGINAL limit (oog_* halt-site forensics present) vs flip to failure with
    # no OOG halt captured. This is a halt-site descriptor, NOT a fixability
    # signal (a row can OOG at 1x yet be not_gas_fixable, halting non-gas at 10x).
    oog_sig = (
        "(oog_pattern IS NOT NULL OR oog_call_depth IS NOT NULL "
        "OR replay_halt_oog = true)"
    )
    br = ctx.con.execute(f"""SELECT
            count(*) FILTER (WHERE {oog_sig}) AS oog,
            count(*) FILTER (WHERE NOT {oog_sig} AND status_changed = true) AS non_oog_revert,
            count(*) FILTER (WHERE NOT {oog_sig} AND status_changed = false) AS other
        FROM divergence_tx WHERE ({pred})""").fetchone()
    break_reason = [
        {"key": "oog", "count": int(br[0] or 0)},
        {"key": "non_oog_revert", "count": int(br[1] or 0)},
        {"key": "other", "count": int(br[2] or 0)},
    ]
    sf = ctx.con.execute(f"""SELECT
            count(*) FILTER (WHERE baseline_success AND NOT schedule_success)     success_to_fail,
            count(*) FILTER (WHERE NOT baseline_success AND NOT schedule_success)  fail_to_fail,
            count(*) FILTER (WHERE NOT baseline_success AND schedule_success)      fail_to_success
        FROM divergence_tx WHERE ({pred})""").fetchone()
    status_flip = [
        {"key": "success_to_fail", "count": int(sf[0] or 0)},
        {"key": "fail_to_fail", "count": int(sf[1] or 0)},
        {"key": "fail_to_success", "count": int(sf[2] or 0)},
    ]
    out = {
        "label": GROUP_LABELS["g4"],
        "count": int(_totals(ctx)["g4"]),
        "fixability": fixability,
        "break_reason": break_reason,
        "oog_bottleneck_kind": _ddb_mix(ctx, "oog_bottleneck_kind", f"({pred})"),
        "status_flip": status_flip,
        "state_gas_category": _ddb_mix(ctx, "state_gas_category", f"({pred})"),
        "tx_shape_mix": _case_mix(ctx, TX_SHAPE_CASE, pred, TX_SHAPE_ORDER),
        "tx_type_mix": _case_mix(ctx, TX_TYPE_CASE, pred, TX_TYPE_ORDER),
    }
    if flavour == "state":
        out["reservoir"] = _reservoir(ctx, pred, include_initial=True)
    return out


def _reservoir(ctx: RunContext, predicate: str, include_initial: bool) -> dict:
    """Reservoir stats for a group predicate — LOCAL DuckDB over divergence_tx."""
    r = ctx.con.execute(f"""SELECT
            count(*) FILTER (WHERE reservoir_exhausted = true)  AS rex_t,
            count(*) FILTER (WHERE reservoir_exhausted = false) AS rex_f,
            avg(runtime_state_gas_spillover)                    AS avg_spill,
            avg(schedule_initial_reservoir)                     AS avg_init
        FROM divergence_tx WHERE ({predicate})""").fetchone()
    out = {
        "reservoir_exhausted": {"true": int(r[0] or 0), "false": int(r[1] or 0)},
        "avg_runtime_state_gas_spillover": (
            int(round(r[2])) if r[2] is not None else 0
        ),
    }
    if include_initial:
        out["avg_initial_reservoir"] = int(round(r[3])) if r[3] is not None else 0
    return out


# ---- oog_forensics.json ----

# A G4 row carries out-of-gas halt-site forensics iff any halt-site signal is
# present. This describes WHERE the tx hit the wall at the ORIGINAL limit — it is
# NOT the fixability split (see group_categories.g4.fixability, keyed on the 10x
# ceiling outcome). Same definition as the G4 break_reason "oog" bucket.
_OOG_SIGNAL = (
    "(oog_pattern IS NOT NULL OR oog_call_depth IS NOT NULL "
    "OR replay_halt_oog = true)"
)
# Call-depth histogram: explicit 1..N bins + a "N+" overflow so the whole OOG
# cohort totals (halts typically run several calls deep — see the design note).
_OOG_DEPTH_TOP = 8


def _addr_category(addr) -> Optional[str]:
    """Taxonomy category for an address, or ``None`` when unclassified.

    Additive enrichment (expansion plan §4.5): reads the merged label cache via
    :func:`repricing_impact.labels.classify_address`. Returns ``None`` for the
    ``unknown`` category (serialised as JSON ``null``) so the field carries no
    noise, and so the value is uniformly ``null`` when no cache is present —
    leaving the pre-expansion ``label`` behaviour untouched.
    """
    rec = classify_address(addr if isinstance(addr, str) else None)
    return rec.category if rec.category != "unknown" else None


def _addr_owner_project(addr) -> Optional[str]:
    """Owner project/entity for an address, or ``None`` (plan §4.5, additive)."""
    rec = classify_address(addr if isinstance(addr, str) else None)
    return rec.owner_project or None


def _labeled_leaderboard(
    ctx: RunContext, col: str, where: str, limit: int = 12
) -> List[dict]:
    """Top-``limit`` values of an address column with human labels + counts.

    ``col`` is a trusted identifier (``oog_contract`` / ``recipient``); addresses
    in ``divergence_tx`` are already lowercased.
    """
    r = ctx.con.execute(
        f"SELECT {col} AS k, count(*) AS n FROM divergence_tx "
        f"WHERE {where} AND {col} IS NOT NULL GROUP BY k "
        f"ORDER BY n DESC LIMIT {limit}"
    ).df()
    return [
        {
            "addr": str(row["k"]),
            "label": label_address(str(row["k"])),
            "category": _addr_category(str(row["k"])),
            "count": int(row["n"]),
        }
        for _, row in r.iterrows()
    ]


def _recipient_structural(ctx: RunContext, addrs: List[str]) -> Dict[str, object]:
    """Fresh on-chain structural + upgradability tags for ``addrs``, read from Xatu.

    Runs the warehouse-native probe (:mod:`repricing_impact.label_sources.xatu_structural`)
    over the pinned block range for exactly the leaderboard addresses — the same
    bounded, off-request-path cross-source pattern as :func:`_recipient_failure_rates`
    (deployed bytecode + EIP-1967 slot reads, never a full scan). Returns
    ``{lowercased addr: LabelRecord}``. Degrades to ``{}`` if the read fails, so the
    structural/upgradability tags simply fall back to the merged label cache and the
    build never breaks on the probe.
    """
    if not addrs:
        return {}
    try:
        from repricing_impact.label_sources.xatu_structural import (
            classify_inputs,
            fetch_structural_inputs,
        )

        inputs = fetch_structural_inputs(
            addrs, ctx.block_start, ctx.block_end, engine=ctx.engine
        )
        return {rec.address: rec for rec in classify_inputs(inputs)}
    except Exception as exc:  # noqa: BLE001 — never break the build on the probe
        _log(
            f"[{ctx.schedule}] structural/upgradability probe failed "
            f"({type(exc).__name__}: {exc}); tags fall back to the label cache."
        )
        return {}


def _labeled_leaderboard_rich(
    ctx: RunContext, col: str, where: str, limit: int = 12
) -> List[dict]:
    """Top-``limit`` addresses with the FULL resolved label record + ``count``.

    Like :func:`_labeled_leaderboard`, but surfaces every *populated* field of the
    merged :class:`LabelRecord` (``owner_project`` / ``source`` / ``confidence`` /
    the MEV overlay / the structural tags). Unset or default fields are dropped so
    the JSON stays lean — mirroring the "null/absent when unknown" convention of
    :func:`_addr_category`. The count is emitted under the generic key ``count``;
    the caller renames it (e.g. to ``halt_count``) and joins any extra columns.
    """
    r = ctx.con.execute(
        f"SELECT {col} AS k, count(*) AS n FROM divergence_tx "
        f"WHERE {where} AND {col} IS NOT NULL GROUP BY k "
        f"ORDER BY n DESC LIMIT {limit}"
    ).df()
    # One bounded Xatu probe for all candidate addresses (see _recipient_structural).
    # The fresh on-chain read is authoritative for the structural/upgradability
    # tags; the merged label cache fills any field the probe didn't observe.
    structural = _recipient_structural(ctx, [str(row["k"]) for _, row in r.iterrows()])
    out: List[dict] = []
    for _, row in r.iterrows():
        addr = str(row["k"])
        rec = classify_address(addr)
        probe = structural.get(addr)

        def _tag(field):
            """Fresh probe wins; fall back to the cache record for that field."""
            val = getattr(probe, field, None) if probe is not None else None
            return val if val else getattr(rec, field)

        entry: Dict[str, object] = {"addr": addr, "label": rec.label}
        if rec.category and rec.category != "unknown":
            entry["category"] = rec.category
        if rec.owner_project:
            entry["owner_project"] = rec.owner_project
        if rec.source and rec.source != "unknown":
            entry["source"] = rec.source
            entry["confidence"] = rec.confidence
        if rec.is_mev_bot:
            entry["is_mev_bot"] = True
            if rec.mev_role:
                entry["mev_role"] = rec.mev_role
        for tag in ("is_proxy", "is_factory", "is_safe"):
            if _tag(tag):
                entry[tag] = True
        if _tag("erc_type"):
            entry["erc_type"] = _tag("erc_type")
        # Upgradability triplet (is_upgradable / mechanism / admin) is taken from ONE
        # record so the three stay consistent: the fresh probe when it reached a
        # definite verdict (any mechanism other than the ambiguous `none` — which may
        # just mean the probe never observed the contract in-window), else the cache.
        # `minimal_proxy_immutable` is a definite NOT-upgradable verdict; a bare
        # `none` is elided like the other defaults.
        up = (
            probe
            if probe is not None
            and getattr(probe, "upgrade_mechanism", "none") != "none"
            else rec
        )
        if up.is_upgradable:
            entry["is_upgradable"] = True
        if up.upgrade_mechanism and up.upgrade_mechanism != "none":
            entry["upgrade_mechanism"] = up.upgrade_mechanism
        if up.upgrade_admin:
            entry["upgrade_admin"] = up.upgrade_admin
        entry["count"] = int(row["n"])
        out.append(entry)
    return out


def _recipient_failure_rates(ctx: RunContext, addrs: List[str]) -> Dict[str, int]:
    """Total mainnet txs per recipient over the pinned window (Xatu EL table).

    Cross-source read of ``default.canonical_execution_transaction`` — the
    denominator for OOG halt rate. Bounded to the pinned block range AND to the
    specific ``addrs`` from the halt leaderboard (never a full scan). Sanctioned
    departure from the gas_analysis-only rule (see AGENTS.md / warehouse.md):
    read-only, off the request path. ``chain_id = 1`` (gas_analysis) is the same
    cohort as ``meta_network_name = 'mainnet'`` (Xatu).

    Returns ``{lowercased addr: total_tx}``. Degrades to ``{}`` (leaving the rate
    ``null``) if the read fails, so the dashboard build never breaks on it.
    """
    if not addrs:
        return {}
    addr_list = ", ".join(f"'{a}'" for a in addrs)
    query = f"""
        SELECT lower(to_address) AS recipient, count(*) AS total_tx
        FROM default.canonical_execution_transaction
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {ctx.block_start} AND {ctx.block_end}
          AND lower(to_address) IN ({addr_list})
        GROUP BY recipient
    """
    try:
        df = _ch(query, ctx.engine)
    except Exception as exc:  # noqa: BLE001 — never break the build on the join
        _log(
            f"[{ctx.schedule}] recipient denominator query failed "
            f"({type(exc).__name__}: {exc}); halt_rate will be null."
        )
        return {}
    return {str(r["recipient"]): int(r["total_tx"]) for _, r in df.iterrows()}


def _entry_flow_sankey(
    ctx: RunContext,
    where: str,
    target_col: str,
    target_side: str,
    top: int = 10,
) -> dict:
    """Bipartite entry(``recipient``) -> ``target_col`` flow for a row cohort.

    Used for both the OOG halt flow (``target_col='oog_contract'``,
    ``target_side='halt'``) and the non-OOG revert flow
    (``target_col='divergence_contract'``, ``target_side='revert'``). Keeps the
    top-``top`` entries and top-``top`` target contracts by cohort count;
    everything else collapses into an "Other" node on each side so the diagram
    stays legible while every row is still accounted for. Entries (left) and
    targets (right) are distinct node sets even when an address appears on both.
    """
    flows = ctx.con.execute(f"""
        WITH rows AS (
            SELECT recipient, {target_col} AS tgt FROM divergence_tx
            WHERE {where} AND recipient IS NOT NULL AND {target_col} IS NOT NULL
        ),
        top_e AS (SELECT recipient FROM rows GROUP BY 1 ORDER BY count(*) DESC LIMIT {top}),
        top_t AS (SELECT tgt       FROM rows GROUP BY 1 ORDER BY count(*) DESC LIMIT {top}),
        b AS (
            SELECT
                CASE WHEN recipient IN (SELECT recipient FROM top_e)
                     THEN recipient ELSE '__other__' END AS e,
                CASE WHEN tgt IN (SELECT tgt FROM top_t)
                     THEN tgt ELSE '__other__' END AS t
            FROM rows
        )
        SELECT e, t, count(*) AS n FROM b GROUP BY 1, 2 ORDER BY n DESC
    """).df()

    nodes: List[dict] = []
    entry_ix: Dict[str, int] = {}
    target_ix: Dict[str, int] = {}

    def _idx(addr: str, side: str, store: Dict[str, int]) -> int:
        if addr not in store:
            other_lbl = "Other entries" if side == "entry" else f"Other {side}s"
            store[addr] = len(nodes)
            nodes.append(
                {
                    "label": other_lbl if addr == "__other__" else label_address(addr),
                    "addr": None if addr == "__other__" else addr,
                    "side": side,
                }
            )
        return store[addr]

    links = []
    for _, row in flows.iterrows():
        s = _idx(str(row["e"]), "entry", entry_ix)
        t = _idx(str(row["t"]), target_side, target_ix)
        links.append({"source": s, "target": t, "value": int(row["n"])})
    return {"nodes": nodes, "links": links}


def _percentiles(ctx: RunContext, col: str, where: str) -> dict:
    """p50/p90/p99/max of a numeric column over the given predicate."""
    r = ctx.con.execute(f"""
        SELECT quantile_cont({col}, 0.5)  AS p50,
               quantile_cont({col}, 0.9)  AS p90,
               quantile_cont({col}, 0.99) AS p99,
               max({col})                 AS mx
        FROM divergence_tx WHERE {where} AND {col} IS NOT NULL
    """).fetchone()
    return {
        "p50": _round_or_none(r[0]),
        "p90": _round_or_none(r[1]),
        "p99": _round_or_none(r[2]),
        "max": _round_or_none(r[3]),
    }


def _call_depth_hist(ctx: RunContext, col: str, where: str) -> List[dict]:
    """Call-depth histogram: explicit 1..N bins + an "N+" overflow bin.

    ``col`` is a nullable depth column (``oog_call_depth`` / ``divergence_call_depth``);
    the overflow bin keeps the whole cohort totalling even for deep frames.
    """
    depth = ctx.con.execute(f"""
        SELECT least({col}, {_OOG_DEPTH_TOP + 1}) AS d, count(*) AS n
        FROM divergence_tx WHERE {where} AND {col} IS NOT NULL
        GROUP BY d ORDER BY d
    """).df()
    dmap = {int(r["d"]): int(r["n"]) for _, r in depth.iterrows()}
    hist = [
        {"depth": str(d), "count": dmap.get(d, 0)} for d in range(1, _OOG_DEPTH_TOP + 1)
    ]
    hist.append(
        {"depth": f"{_OOG_DEPTH_TOP + 1}+", "count": dmap.get(_OOG_DEPTH_TOP + 1, 0)}
    )
    return hist


# Ordered gas-remaining magnitude buckets for the "gas left at halt" distribution.
# (lower_inclusive, upper_inclusive|None, label) — a heavy mass sits at exactly 0
# (halted with nothing left), so 0 gets its own bucket.
_GAS_REMAINING_BUCKETS = [
    (0, 0, "0"),
    (1, 999, "1–1K"),
    (1_000, 9_999, "1K–10K"),
    (10_000, 99_999, "10K–100K"),
    (100_000, 999_999, "100K–1M"),
    (1_000_000, None, "1M+"),
]


def _gas_remaining_hist(ctx: RunContext, col: str, where: str) -> List[dict]:
    """Distribution of gas remaining at the halt over ordered magnitude buckets.

    ``col`` is a nullable gas column (``oog_gas_remaining``); every non-null row
    lands in exactly one bucket so the whole OOG cohort totals.
    """
    filters = ", ".join(
        f"count(*) FILTER (WHERE {col} >= {lo}"
        + (f" AND {col} <= {hi}" if hi is not None else "")
        + ") AS b{}".format(i)
        for i, (lo, hi, _) in enumerate(_GAS_REMAINING_BUCKETS)
    )
    row = ctx.con.execute(
        f"SELECT {filters} FROM divergence_tx WHERE {where} AND {col} IS NOT NULL"
    ).fetchone()
    return [
        {"bucket": label, "count": int(row[i])}
        for i, (_, _, label) in enumerate(_GAS_REMAINING_BUCKETS)
    ]


def _recipient_leaderboards(
    ctx: RunContext, where: str, count_key: str, rate_key: str
) -> tuple:
    """Entry-contract (``recipient``) leaderboards for a G4 row cohort.

    Builds a broad candidate pool ranked by row count, fetches each one's mainnet
    total-tx denominator in a single bounded cross-source Xatu read (see
    :func:`_recipient_failure_rates`), then derives two rankings: BY COUNT
    (top-N of the pool) and BY FAILURE RATE (top-N among contracts clearing the
    ``OOG_RATE_MIN_TOTAL_TX`` floor, so tiny-denominator contracts can't
    dominate). ``count_key`` / ``rate_key`` name the per-row count/rate fields
    (``halt_count`` / ``halt_rate`` for OOG halts, ``revert_count`` /
    ``revert_rate`` for non-OOG reverts). Shared by both forensics emitters.
    """
    candidates = _labeled_leaderboard_rich(
        ctx, "recipient", where, limit=OOG_RECIPIENT_POOL
    )
    total_tx_by_addr = _recipient_failure_rates(
        ctx, [row["addr"] for row in candidates]
    )
    for row in candidates:
        count = int(row.pop("count"))
        total_tx = total_tx_by_addr.get(row["addr"])
        row[count_key] = count
        row["total_tx"] = total_tx
        row[rate_key] = round(count / total_tx, 6) if total_tx else None

    by_count = candidates[:OOG_RECIPIENT_TOP_N]
    by_rate = sorted(
        (
            row
            for row in candidates
            if row[rate_key] is not None and row["total_tx"] >= OOG_RATE_MIN_TOTAL_TX
        ),
        key=lambda row: row[rate_key],
        reverse=True,
    )[:OOG_RECIPIENT_TOP_N]
    return by_count, by_rate


def emit_oog_forensics(ctx: RunContext) -> dict:
    """OOG halt-site forensics for the Potentially-broken (G4) cohort (SCHEMA §7).

    Scoped to G4 rows carrying an OOG halt-site signal — this is the
    ORIGINAL-limit halt site (WHERE the tx first hit the wall), *not* a fixability
    verdict: most of these are ``not_gas_fixable`` (a non-gas halt at the 10x
    ceiling — see ``group_categories.g4.fixability``). Answers *what* opcode ran
    out, *where* (halt contract + call depth), *why* (pattern / bottleneck kind),
    and the entry->halt contract flow (Sankey).
    """
    where = f"({groups.G4_PREDICATE}) AND {_OOG_SIGNAL}"
    g4_total = int(_totals(ctx)["g4"])
    oog_total = int(
        ctx.con.execute(f"SELECT count(*) FROM divergence_tx WHERE {where}").fetchone()[
            0
        ]
    )
    call_depth_hist = _call_depth_hist(ctx, "oog_call_depth", where)

    # Distinct entry contracts (recipients) that recorded at least one OOG halt —
    # the breadth of the impact, complementing oog_total (the volume).
    distinct_oog_recipients = int(
        ctx.con.execute(
            f"SELECT count(DISTINCT recipient) FROM divergence_tx "
            f"WHERE {where} AND recipient IS NOT NULL"
        ).fetchone()[0]
    )

    # Distinct halt-site contracts (oog_contract) — WHERE the halt landed, distinct
    # from distinct_oog_recipients (WHO was called). A halt deep in a shared
    # library/router shows here but not under recipient, so the two diverge.
    distinct_oog_contracts = int(
        ctx.con.execute(
            f"SELECT count(DISTINCT oog_contract) FROM divergence_tx "
            f"WHERE {where} AND oog_contract IS NOT NULL"
        ).fetchone()[0]
    )

    # Entry-contract (recipient) leaderboards with rich labels + failure rate.
    # Keyed on `recipient` (WHO was called), distinct from oog_contract_leaderboard
    # (WHERE the halt landed). Ranked by OOG halt count and, from the same pool,
    # by failure rate.
    recipient_leaderboard, recipient_rate_leaderboard = _recipient_leaderboards(
        ctx, where, "halt_count", "halt_rate"
    )

    return {
        "schedule": ctx.schedule,
        "g4_total": g4_total,
        "oog_total": oog_total,
        "oog_share_of_g4": round(oog_total / g4_total, 4) if g4_total else 0.0,
        # HOW MANY distinct entry contracts recorded an OOG halt (impact breadth)
        "distinct_oog_recipients": distinct_oog_recipients,
        # HOW MANY distinct halt-site contracts (oog_contract) — WHERE the halt landed
        "distinct_oog_contracts": distinct_oog_contracts,
        # WHY it ran out
        "oog_pattern": _ddb_mix(ctx, "oog_pattern", where),
        # HOW MUCH gas was left at the halt (distribution)
        "gas_remaining_hist": _gas_remaining_hist(ctx, "oog_gas_remaining", where),
        # WHAT ran out (opcode at the halt)
        "oog_opcode": _ddb_mix(ctx, "oog_opcode", where),
        # WHERE it ran out
        "call_depth_hist": call_depth_hist,
        "call_depth_percentiles": _percentiles(ctx, "oog_call_depth", where),
        # WHERE (halt site): top oog_contract addresses, where the halt landed
        "oog_contract_leaderboard": _labeled_leaderboard(ctx, "oog_contract", where),
        # WHERE (entry): top recipient addresses with rich labels + real failure rate
        "oog_recipient_leaderboard": recipient_leaderboard,
        # WHERE (entry, by rate): same shape, ranked by failure rate among
        # recipients clearing the OOG_RATE_MIN_TOTAL_TX total-tx floor.
        "oog_recipient_rate_leaderboard": recipient_rate_leaderboard,
        # FLOW: entry contract -> halt contract
        "sankey": _entry_flow_sankey(ctx, where, "oog_contract", "halt"),
    }


# ---- nonoog_forensics.json ----

# A non-OOG revert is a G4 row whose status flipped (baseline succeeded, schedule
# failed — always true for G4) WITHOUT any out-of-gas signal. Mirrors the G4
# break_reason "non_oog_revert" bucket in _g4_categories.
_NONOOG_SIGNAL = f"NOT {_OOG_SIGNAL} AND status_changed = true"

# ABI-decoded string reverts arrive as ``Error(string): <msg>`` in revert_decoded.
_ERROR_STRING_PREFIX = "Error(string):"


def _revert_error_mix(ctx: RunContext, where: str, top: int = 5) -> List[dict]:
    """Top-``top`` ``Error(string): <msg>`` revert messages + an "Others" bucket.

    Only rows whose ``revert_decoded`` is an ABI string revert are counted; the
    ``Error(string):`` prefix is stripped so the chart shows the bare message.
    Aggregating server-side keeps the JSON small (there can be thousands of
    distinct messages) while still accounting for the whole string-revert cohort.
    """
    prefix_len = len(_ERROR_STRING_PREFIX)
    r = ctx.con.execute(f"""
        SELECT trim(substr(revert_decoded, {prefix_len + 1})) AS msg, count(*) AS n
        FROM divergence_tx
        WHERE {where} AND revert_decoded LIKE '{_ERROR_STRING_PREFIX}%'
        GROUP BY msg ORDER BY n DESC
    """).df()
    rows = [{"key": str(row["msg"]), "count": int(row["n"])} for _, row in r.iterrows()]
    if len(rows) <= top:
        return rows
    head = rows[:top]
    rest = sum(row["count"] for row in rows[top:])
    if rest > 0:
        head.append({"key": "Others", "count": rest})
    return head


def emit_nonoog_forensics(ctx: RunContext) -> dict:
    """Non-OOG revert forensics for the Potentially-broken (G4) cohort.

    Scoped to G4 rows that flipped status without an OOG signal — the status
    flipped under the schedule but no out-of-gas halt was recorded. Answers *why*
    it reverted (``failure_reason`` + decoded ``Error(string)`` messages), *what*
    opcode diverged (``divergence_opcode``), *where* (call depth), and the
    entry->revert contract flow (Sankey).
    """
    where = f"({groups.G4_PREDICATE}) AND {_NONOOG_SIGNAL}"
    g4_total = int(_totals(ctx)["g4"])
    nonoog_total = int(
        ctx.con.execute(f"SELECT count(*) FROM divergence_tx WHERE {where}").fetchone()[
            0
        ]
    )

    # Distinct entry contracts (recipients) with >=1 non-OOG revert — the breadth
    # of the impact, complementing nonoog_total (the volume). Mirrors the OOG side.
    distinct_nonoog_recipients = int(
        ctx.con.execute(
            f"SELECT count(DISTINCT recipient) FROM divergence_tx "
            f"WHERE {where} AND recipient IS NOT NULL"
        ).fetchone()[0]
    )

    # Distinct revert-site contracts (divergence_contract) — WHERE the revert
    # landed, distinct from distinct_nonoog_recipients (WHO was called). Mirrors
    # the OOG side.
    distinct_nonoog_contracts = int(
        ctx.con.execute(
            f"SELECT count(DISTINCT divergence_contract) FROM divergence_tx "
            f"WHERE {where} AND divergence_contract IS NOT NULL"
        ).fetchone()[0]
    )

    # Entry-contract (recipient) leaderboards, ranked by non-OOG revert count and,
    # from the same pool, by failure rate. Mirrors the OOG recipient leaderboards.
    recipient_leaderboard, recipient_rate_leaderboard = _recipient_leaderboards(
        ctx, where, "revert_count", "revert_rate"
    )

    return {
        "schedule": ctx.schedule,
        "g4_total": g4_total,
        "nonoog_total": nonoog_total,
        "nonoog_share_of_g4": round(nonoog_total / g4_total, 4) if g4_total else 0.0,
        # HOW MANY distinct entry contracts reverted without OOG (impact breadth)
        "distinct_nonoog_recipients": distinct_nonoog_recipients,
        # HOW MANY distinct revert-site contracts (divergence_contract) — WHERE it landed
        "distinct_nonoog_contracts": distinct_nonoog_contracts,
        # WHY it reverted
        "failure_reason": _ddb_mix(ctx, "failure_reason", where),
        "revert_error_mix": _revert_error_mix(ctx, where),
        # WHAT diverged (opcode at the revert)
        "divergence_opcode": _ddb_mix(ctx, "divergence_opcode", where),
        # WHERE it reverted (call-depth histogram + median for the headline card)
        "call_depth_hist": _call_depth_hist(ctx, "divergence_call_depth", where),
        "call_depth_percentiles": _percentiles(ctx, "divergence_call_depth", where),
        # WHERE (entry): top recipient addresses with rich labels + real failure rate
        "nonoog_recipient_leaderboard": recipient_leaderboard,
        # WHERE (entry, by rate): same shape, ranked by failure rate among
        # recipients clearing the OOG_RATE_MIN_TOTAL_TX total-tx floor.
        "nonoog_recipient_rate_leaderboard": recipient_rate_leaderboard,
        # FLOW: entry contract -> revert contract
        "sankey": _entry_flow_sankey(ctx, where, "divergence_contract", "revert"),
    }


# ---- contract_failures.json ----


def emit_contract_failures(ctx: RunContext) -> dict:
    """Top-N failing recipients ranked by G4 tx count (SCHEMA §5)."""
    totals = _totals(ctx)
    # Per-recipient aggregate — LOCAL DuckDB over the materialized divergence_tx.
    g4 = groups.G4_PREDICATE
    g3 = groups.G3_PREDICATE
    g2d = groups.G2_DRILLIN_PREDICATE
    agg = ctx.con.execute(f"""
        SELECT
            recipient,
            count(*) FILTER (WHERE {g4}) AS g4_tx_count,
            count(*) FILTER (WHERE {g3}) AS g3_tx_count,
            count(*) FILTER (WHERE {g2d}) AS g2_drillin_tx_count,
            count(*) FILTER (WHERE baseline_success AND NOT schedule_success) AS status_flips,
            avg(gas_delta) AS avg_gas_delta,
            sum(gas_delta) AS sum_gas_delta,
            min(block_number) AS block_span_start,
            max(block_number) AS block_span_end,
            count(DISTINCT CASE WHEN {g4} THEN block_number END) AS distinct_blocks_with_g4,
            quantile_cont(min_multiplier_to_succeed, 0.5) FILTER (WHERE min_multiplier_to_succeed IS NOT NULL) AS mm_p50,
            quantile_cont(min_multiplier_to_succeed, 0.9) FILTER (WHERE min_multiplier_to_succeed IS NOT NULL) AS mm_p90,
            quantile_cont(min_multiplier_to_succeed, 0.99) FILTER (WHERE min_multiplier_to_succeed IS NOT NULL) AS mm_p99
        FROM divergence_tx
        WHERE recipient IS NOT NULL
        GROUP BY recipient
        HAVING count(*) FILTER (WHERE {g4}) > 0
        ORDER BY g4_tx_count DESC
        LIMIT {TOP_N_CONTRACTS}
        """).df()

    g4_total = totals["g4"]
    contracts = []
    cum = 0
    for _, r in agg.iterrows():
        recipient = r["recipient"]
        g4 = int(r["g4_tx_count"])
        g3 = int(r["g3_tx_count"])
        g2d = int(r["g2_drillin_tx_count"])
        cum += g4
        drillin_cohort = g2d + g3 + g4
        # per-recipient divergence/oog contract mix (top few)
        div_mix = _contract_mix(ctx, recipient, "divergence_contract")
        oog_mix = _contract_mix(ctx, recipient, "oog_contract")
        contracts.append(
            {
                "recipient": recipient,
                "label": label_address(recipient),
                "category": _addr_category(recipient),
                "owner_project": _addr_owner_project(recipient),
                "g4_tx_count": g4,
                "g3_tx_count": g3,
                "g2_drillin_tx_count": g2d,
                "status_flips": int(r["status_flips"]),
                "avg_gas_delta": (
                    int(round(r["avg_gas_delta"]))
                    if pd.notna(r["avg_gas_delta"])
                    else 0
                ),
                "sum_gas_delta": int(r["sum_gas_delta"] or 0),
                "min_mult_percentiles": {
                    "p50": _round_or_none(r["mm_p50"]),
                    "p90": _round_or_none(r["mm_p90"]),
                    "p99": _round_or_none(r["mm_p99"]),
                },
                "block_span_start": int(r["block_span_start"]),
                "block_span_end": int(r["block_span_end"]),
                "distinct_blocks_with_g4": int(r["distinct_blocks_with_g4"]),
                "avg_g4_per_block": (
                    round(g4 / int(r["distinct_blocks_with_g4"]), 3)
                    if r["distinct_blocks_with_g4"]
                    else 0.0
                ),
                "g4_vs_other_ratio": (
                    round(g4 / drillin_cohort, 4) if drillin_cohort else 0.0
                ),
                "divergence_contract_mix": div_mix,
                "oog_contract_mix": oog_mix,
                "cumulative_share": round(cum / g4_total, 4) if g4_total else 0.0,
            }
        )

    return {
        "schedule": ctx.schedule,
        "g4_total": int(g4_total),
        "g3_total": int(totals["g3"]),
        "note": (
            "Per-recipient ratios are over the drill-in cohort (Fixable + "
            "Potentially broken + Succeeds-with-changes drill-in members) only — "
            "the warehouse has no per-recipient total-tx-per-block count. The "
            "No-change and Succeeds-with-changes aggregate cohorts have no recipient."
        ),
        "contracts": contracts,
    }


# ---- affected_contracts.json ----

# Per-contract cap on the emitted failure-cluster spine (top-N by count).
AFFECTED_CLUSTER_TOP_N = 8
# Cap on supporting breakdowns (functions / counterpart-contract mixes).
AFFECTED_BREAKDOWN_TOP_N = 8
# Examples emitted per cluster.
AFFECTED_EXAMPLES_PER_CLUSTER = 2
# Upper bound on the SANCTIONED cross-source Xatu probe set (structural tags +
# failure-rate denominator). At full scale the affected set is tens of thousands
# of contracts; both reads must stay bounded to top-N (AGENTS.md). Contracts
# outside this set fall back to cache-based tags + a null failure_rate.
AFFECTED_PROBE_TOP_N = 1000

_AFFECTED_NOTE = (
    "Affected = appears in any Potentially-broken (G4) tx as entry/halt/revert "
    "site. Clusters are distinct failure modes ranked by tx count; drivers are "
    "the repriced state line items behind each. Failure rates cover only "
    "contracts with a Xatu denominator."
)

_DEPLOY_OOG_EXPLAINER = (
    "Freshly-deployed contract accounts (mostly ERC-4337 smart-account wallets "
    "created via CREATE2 inside EntryPoint handleOps) that run out of gas during "
    "their own construction under the state-creation repricing: the deployment's "
    "constructor / code-deposit exceeds the transaction gas limit, halting the "
    "create. Each is a single-tx, self-halt, unlabeled address whose per-contract "
    "shard would be near-identical to every other, so they are collapsed into this "
    "one aggregate file instead of one shard apiece."
)


def _q(series, q):
    """Percentile of a pandas Series over non-null values (int, or None)."""
    s = series.dropna()
    if s.empty:
        return None
    return int(round(float(s.quantile(q))))


def _pop_share(series) -> bool:
    """True if a Series has at least one non-null value (drives driver inclusion)."""
    return bool(series.notna().any())


def _cluster_drivers(rows: pd.DataFrame) -> dict:
    """Aggregate the causal repricing drivers within one failure cluster.

    Emits only the driver keys whose source columns are populated for the
    cluster (per §1b/§1d); an all-NULL column is omitted rather than reported as
    zero. ``access_list_entries`` sums the address + storage-key counts per row.
    """
    drivers: Dict[str, object] = {}

    # state_gas_category: [{key,count}] — only when populated in the cluster.
    sgc = rows["state_gas_category"].dropna()
    sgc = sgc[sgc.astype(str).str.len() > 0]
    if not sgc.empty:
        counts = sgc.astype(str).value_counts()
        drivers["state_gas_category"] = [
            {"key": str(k), "count": int(v)} for k, v in counts.items()
        ]

    for key, col in (
        ("cold_account", "cold_account_access_count"),
        ("sload", "sload_cold_count"),
        ("sstore", "sstore_cold_count"),
    ):
        if _pop_share(rows[col]):
            drivers[key] = {"p50": _q(rows[col], 0.5), "p90": _q(rows[col], 0.9)}

    ale = rows["access_list_address_count"].fillna(0) + rows[
        "access_list_storage_key_count"
    ].fillna(0)
    if _pop_share(rows["access_list_address_count"]) or _pop_share(
        rows["access_list_storage_key_count"]
    ):
        drivers["access_list_entries"] = {"p50": _q(ale, 0.5), "p90": _q(ale, 0.9)}

    if _pop_share(rows["surcharge_at_oog"]):
        s = rows["surcharge_at_oog"].dropna()
        drivers["surcharge_at_oog"] = {
            "p50": _q(rows["surcharge_at_oog"], 0.5),
            "sum": int(s.sum()),
        }

    if _pop_share(rows["oog_gas_remaining"]):
        drivers["gas_remaining_at_oog"] = {
            "p50": _q(rows["oog_gas_remaining"], 0.5),
            "p90": _q(rows["oog_gas_remaining"], 0.9),
        }

    n = len(rows)
    if n and _pop_share(rows["reservoir_exhausted"]):
        drivers["reservoir_exhausted_share"] = round(
            int((rows["reservoir_exhausted"] == True).sum()) / n, 4  # noqa: E712
        )
    if n and _pop_share(rows["runtime_state_gas_spillover"]):
        drivers["spillover_share"] = round(
            int((rows["runtime_state_gas_spillover"].fillna(0) > 0).sum()) / n, 4
        )
    return drivers


def _affected_context(
    entry_rows: pd.DataFrame,
    site_rows: pd.DataFrame,
    selector_map: dict,
    category: Optional[str],
    failure_rate: Optional[dict],
    broader: tuple,
) -> dict:
    """The compact per-contract context strip (§1e).

    ``entry_rows`` are G4 rows where this contract is the entry (``recipient``);
    ``site_rows`` are G4 rows where it is the OOG/revert site. All breakdowns are
    capped at :data:`AFFECTED_BREAKDOWN_TOP_N`. ``broader`` is the precomputed
    ``(g3, g2_drillin, af, status_flips)`` count tuple over ALL of this contract's
    rows-as-recipient (the emit_contract_failures pattern), not just the G4 slice —
    computed ONCE for every recipient in a single grouped scan before the loop, not
    per-contract (at full scale a per-contract ``WHERE recipient = ?`` was a full
    table scan repeated for every affected contract).
    """
    gd = entry_rows["gas_delta"].dropna() if not entry_rows.empty else pd.Series([])
    gas_delta = {
        "avg": int(round(float(gd.mean()))) if not gd.empty else 0,
        "sum": int(gd.sum()) if not gd.empty else 0,
        "p50": _q(gd, 0.5) if not gd.empty else None,
        "p90": _q(gd, 0.9) if not gd.empty else None,
    }

    span = entry_rows if not entry_rows.empty else site_rows
    if not span.empty:
        block_span_start = int(span["block_number"].min())
        block_span_end = int(span["block_number"].max())
        distinct_blocks = int(span["block_number"].nunique())
    else:
        block_span_start = block_span_end = distinct_blocks = 0

    def _fn_breakdown(series: pd.Series) -> List[dict]:
        s = series.dropna()
        if s.empty:
            return []
        counts = s.astype(str).value_counts().head(AFFECTED_BREAKDOWN_TOP_N)
        return [
            {
                "selector": str(sel),
                "signature": _decode_sel(str(sel), category, selector_map),
                "count": int(cnt),
            }
            for sel, cnt in counts.items()
        ]

    entry_functions = _fn_breakdown(entry_rows["entry_selector"])
    failing_functions = _fn_breakdown(
        entry_rows["tier1_failing_selector"].fillna(entry_rows["entry_selector"])
        if not entry_rows.empty
        else pd.Series([], dtype=object)
    )

    def _mix(rows: pd.DataFrame, col: str) -> List[dict]:
        if rows.empty:
            return []
        s = rows[col].dropna()
        if s.empty:
            return []
        counts = s.astype(str).value_counts().head(AFFECTED_BREAKDOWN_TOP_N)
        return [
            {
                "contract": str(c),
                "label": label_address(str(c)),
                "category": _addr_category(str(c)),
                "count": int(cnt),
            }
            for c, cnt in counts.items()
        ]

    return {
        "g3_tx_count": int(broader[0] or 0),
        "g2_drillin_tx_count": int(broader[1] or 0),
        "af_tx_count": int(broader[2] or 0),
        "status_flips": int(broader[3] or 0),
        "gas_delta": gas_delta,
        "block_span_start": block_span_start,
        "block_span_end": block_span_end,
        "distinct_blocks": distinct_blocks,
        "failure_rate": failure_rate,
        "entry_functions": entry_functions,
        "failing_functions": failing_functions,
        # Counterpart mixes: where the entry contract's txs halted / reverted, and
        # (for site roles) which entry contracts called this one.
        "halt_contracts": _mix(entry_rows, "oog_contract"),
        "revert_contracts": _mix(entry_rows, "divergence_contract"),
        "entry_contracts": _mix(site_rows, "recipient"),
    }


def _build_affected_contracts(ctx: RunContext) -> tuple:
    """Build the sharded affected-contracts payload — ``(index_dict, records)``.

    Runs entirely LOCALLY over the materialized ``divergence_tx`` table — no new
    ClickHouse ``_divergence`` scan. A contract is *affected* iff it appears in
    any G4 row as the entry (``recipient``), the OOG halt site (``oog_contract``),
    or the non-OOG revert site (``divergence_contract``). For each affected
    contract we build an identity header, per-role headline counts, the top-N
    failure-cluster spine (distinct failure modes, each annotated with the causal
    repricing drivers), and a compact context strip.

    Returns a ``(index_dict, {lowercase_addr: record})`` tuple. ``records`` holds
    the per-contract shard payloads (one file each); ``index_dict`` is the small
    init file — it lists ONLY name-searchable contracts (a real ``label`` name or a
    set ``owner_project``), sorted by total G4 footprint descending, with light
    role footprint counts to drive the search pick-list. Unlabeled contracts are
    still emitted as shard files (direct-address lookup) but omitted from the index
    to keep it tiny. :func:`write_affected_contracts` writes the files.
    """
    totals = _totals(ctx)
    g4_total = int(totals["g4"])

    # Selector decoder (display only): load once, degrade to raw hex on miss so a
    # missing selectors.parquet cache never crashes the build or splits a cluster.
    try:
        from repricing_impact.label_sources.selectors import load_selector_map

        selector_map = load_selector_map()
    except Exception as exc:  # noqa: BLE001 — degrade to raw hex, never crash
        _log(
            f"[{ctx.schedule}] selector map load failed "
            f"({type(exc).__name__}: {exc}); signatures fall back to raw hex."
        )
        selector_map = {}

    # Pull the whole G4 cohort into pandas ONCE (G4 is small — ~hundreds of
    # contracts / ~10^5 txs). All clustering + driver stats are pandas over this.
    g4 = ctx.con.execute(f"""
        SELECT
            tx_hash, block_number, recipient, gas_delta,
            oog_contract, divergence_contract,
            entry_selector, tier1_failing_selector,
            oog_opcode, oog_pattern, oog_bottleneck_kind, oog_call_depth,
            oog_gas_remaining, replay_halt_oog,
            divergence_opcode, divergence_call_depth, failure_reason, revert_decoded,
            state_gas_category, reservoir_exhausted, runtime_state_gas_spillover,
            surcharge_at_oog, cold_account_access_count,
            sload_cold_count, sstore_cold_count,
            access_list_address_count, access_list_storage_key_count
        FROM divergence_tx
        WHERE ({groups.G4_PREDICATE})
        """).df()

    block_range = {"start": int(ctx.block_start), "end": int(ctx.block_end)}

    def _index(records: Dict[str, dict], deploy_count: int = 0) -> dict:
        """Assemble the small init index from the built per-contract records.

        Includes only name-searchable contracts (a real ``label`` name — not a bare
        ``0x…`` fallback — OR a set ``owner_project``), sorted by total G4 footprint
        (entry + halt + revert counts) descending. Light role counts are omitted per
        role when absent, mirroring ``roles_summary``.

        ``affected_count`` reflects the FULL affected total — the union over the
        three role columns — INCLUDING the deploy-OOG accounts collapsed out of the
        per-contract shards into ``affected/deploy_oog.json`` (``deploy_count``).
        Those accounts are unlabeled and were already absent from the searchable
        ``contracts`` list.
        """
        entries = []
        for addr, rec in records.items():
            label = rec.get("label")
            owner = rec.get("owner_project")
            named = (label and label != addr) or bool(owner)
            if not named:
                continue
            rs = rec.get("roles_summary", {})
            entry_c = rs.get("entry", {}).get("g4_tx_count")
            halt_c = rs.get("oog_site", {}).get("halt_count")
            revert_c = rs.get("revert_site", {}).get("revert_count")
            footprint = (entry_c or 0) + (halt_c or 0) + (revert_c or 0)
            e: Dict[str, object] = {"address": addr, "label": label}
            if owner:
                e["owner_project"] = owner
            if rec.get("category"):
                e["category"] = rec["category"]
            if entry_c:
                e["entry_g4_tx_count"] = entry_c
            if halt_c:
                e["halt_count"] = halt_c
            if revert_c:
                e["revert_count"] = revert_c
            entries.append((footprint, e))
        entries.sort(key=lambda fe: fe[0], reverse=True)
        return {
            "schedule": ctx.schedule,
            "block_range": block_range,
            "g4_total": g4_total,
            "affected_count": len(records) + int(deploy_count),
            "deploy_oog": {"count": int(deploy_count), "file": "deploy_oog.json"},
            "note": _AFFECTED_NOTE,
            "contracts": [e for _, e in entries],
        }

    def _empty_deploy_oog() -> dict:
        """A benign, valid deploy_oog object with no accounts (e.g. eip-8038)."""
        return {
            "schedule": ctx.schedule,
            "class": "deploy_oog",
            "block_range": block_range,
            "g4_total": g4_total,
            "count": 0,
            "explainer": _DEPLOY_OOG_EXPLAINER,
            "aggregate": {
                "halt_opcode_split": [],
                "initcode_families": [],
                "top_entry_contracts": [],
                "revert_reasons": [],
                "gas_delta": {"p50": None, "p90": None, "min": None, "max": None},
                "drivers": {},
            },
            "accounts": {},
        }

    if g4.empty:
        return _index({}), {}, _empty_deploy_oog()

    # The affected-address set = union over the three role columns.
    affected: set = set()
    for col in ("recipient", "oog_contract", "divergence_contract"):
        affected.update(a for a in g4[col].dropna().astype(str) if a)
    affected_list = sorted(affected)

    # Per-address role footprints (used to rank the bounded probe set and, later,
    # reused inside the loop via the per-contract masks). counts over the whole G4
    # frame once — cheap, and independent of the per-contract loop.
    entry_counts = g4["recipient"].dropna().astype(str).value_counts()
    halt_counts = g4["oog_contract"].dropna().astype(str).value_counts()
    revert_counts = g4["divergence_contract"].dropna().astype(str).value_counts()

    def _footprint(a: str) -> int:
        return (
            int(entry_counts.get(a, 0))
            + int(halt_counts.get(a, 0))
            + int(revert_counts.get(a, 0))
        )

    # Bound the two SANCTIONED cross-source Xatu reads to a top-N probe set so the
    # full run stays within the "bounded to top-N addresses" rule (AGENTS.md) — at
    # full scale ``affected`` is tens of thousands of contracts, and an unbounded
    # bytecode/storage probe + IN-clause denominator scan over 1M blocks would be
    # enormous. ``classify_address`` (cache-only, no network) still runs for EVERY
    # affected contract below; long-tail contracts outside the probe set simply get
    # cache-based structural tags + a null failure_rate (the existing graceful
    # fallback in _labeled_leaderboard_rich / the null the doc already allows).
    #
    # probe set = {top-N by G4 footprint} ∪ {every name-searchable affected addr}
    # (name-searchable = a real label OR owner_project — the same rule the index
    # uses) so the contracts users actually open are always covered.
    def _name_searchable(a: str) -> bool:
        rec = classify_address(a)
        return (rec.label and rec.label != a) or bool(rec.owner_project)

    top_by_footprint = sorted(affected_list, key=_footprint, reverse=True)[
        :AFFECTED_PROBE_TOP_N
    ]
    probe_addrs = set(top_by_footprint) | {
        a for a in affected_list if _name_searchable(a)
    }
    # Failure rate is only meaningful for entry contracts (denominator = total txs
    # sent to the recipient), so restrict the denominator read to probed entries.
    entry_probe_addrs = sorted(
        a for a in probe_addrs if int(entry_counts.get(a, 0)) > 0
    )
    _log(
        f"[{ctx.schedule}] affected={len(affected_list)}, structural/rate probe "
        f"bounded to {len(probe_addrs)} (entry-rate probe {len(entry_probe_addrs)})"
    )

    # ONE bounded Xatu structural probe over the top-N probe set (never the full
    # affected set) — same off-request-path pattern as _labeled_leaderboard_rich.
    structural = _recipient_structural(ctx, sorted(probe_addrs))
    # ONE bounded Xatu failure-rate denominator read over the probed entries.
    total_tx_by_addr = _recipient_failure_rates(ctx, entry_probe_addrs)

    # Precompute the broader per-recipient counts (g3 / g2-drillin / af / status
    # flips over ALL of a recipient's rows, not just the G4 slice) in ONE grouped
    # scan, then look them up per contract. The previous per-contract
    # ``WHERE recipient = ?`` query was a full scan of the multi-million-row
    # divergence_tx table repeated once per affected contract (100k+ scans) and did
    # not terminate in practical time.
    broader_rows = ctx.con.execute(f"""
        SELECT
            recipient,
            count(*) FILTER (WHERE {groups.G3_PREDICATE})         AS g3,
            count(*) FILTER (WHERE {groups.G2_DRILLIN_PREDICATE})  AS g2d,
            count(*) FILTER (WHERE {groups.AF_PREDICATE})         AS af,
            count(*) FILTER (WHERE baseline_success AND NOT schedule_success) AS flips
        FROM divergence_tx
        WHERE recipient IS NOT NULL
        GROUP BY recipient
        """).fetchall()
    broader_by_recipient = {
        r[0]: (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), int(r[4] or 0))
        for r in broader_rows
    }

    # Group the G4 frame ONCE by each role column so per-contract row lookup is an
    # O(1) hash-slice (``get_group``) rather than a full-frame boolean mask. At full
    # scale (100k+ affected contracts over a multi-million-row G4 frame) the prior
    # per-contract ``g4[col] == addr`` masking (plus a full-length ``pd.Series`` per
    # contract) was O(contracts × rows) and never finished; grouping makes the loop
    # O(rows + contracts).
    _EMPTY = g4.iloc[0:0]
    entry_groups = g4.groupby("recipient")
    oog_groups = g4.groupby("oog_contract")
    rev_groups = g4.groupby("divergence_contract")

    def _grp(gb, a):
        try:
            return gb.get_group(a)
        except KeyError:
            return _EMPTY

    # Deploy-OOG accounts (rule owned by groups.py — see DEPLOY_OOG_RULE_DOC): the
    # freshly-deployed-and-self-OOG long tail (~102k of ~118k affected under
    # eip-8037). Collapsed OUT of the per-contract shards into one aggregate file
    # (affected/deploy_oog.json) instead of one near-identical shard apiece. An
    # affected addr qualifies iff (a) not name-searchable, (b) never a tx recipient,
    # (c) an OOG halt site >=1 time, and (d) EVERY oog-halt row lands in init code.
    def _is_deploy_oog(addr: str) -> bool:
        if _name_searchable(addr):  # (a)
            return False
        if int(entry_counts.get(addr, 0)) != 0:  # (b)
            return False
        oog_rows = _grp(oog_groups, addr)  # (c)
        if oog_rows.empty:
            return False
        sels = oog_rows["tier1_failing_selector"].fillna(oog_rows["entry_selector"])
        return bool(sels.map(groups.is_initcode_selector).all())  # (d)

    deploy_set = {addr for addr in affected_list if _is_deploy_oog(addr)}

    contracts_out: Dict[str, dict] = {}
    for addr in affected_list:
        # Deploy-OOG accounts are collapsed into affected/deploy_oog.json — no shard.
        if addr in deploy_set:
            continue
        entry_rows = _grp(entry_groups, addr)
        oog_rows = _grp(oog_groups, addr)
        rev_rows = _grp(rev_groups, addr)

        n_entry = len(entry_rows)
        n_halt = len(oog_rows)
        n_rev = len(rev_rows)

        # site_rows = union of OOG-halt and non-OOG-revert rows (a row can be both;
        # keep it once), used by the context strip below.
        if n_halt and n_rev:
            site_rows = pd.concat([oog_rows, rev_rows])
            site_rows = site_rows[~site_rows.index.duplicated()]
        elif n_halt:
            site_rows = oog_rows
        elif n_rev:
            site_rows = rev_rows
        else:
            site_rows = _EMPTY

        # roles_summary — omit a role key when its count is 0.
        roles_summary: Dict[str, dict] = {}
        if n_entry:
            n_oog = int(entry_rows["oog_contract"].notna().sum())
            roles_summary["entry"] = {
                "g4_tx_count": n_entry,
                "g4_oog_count": n_oog,
                "g4_nonoog_count": n_entry - n_oog,
            }
        if n_halt:
            roles_summary["oog_site"] = {"halt_count": n_halt}
        if n_rev:
            roles_summary["revert_site"] = {"revert_count": n_rev}

        # --- Build the tagged cluster rows: one (row, role) per role the row
        # plays for THIS contract (entry + self-halt are both kept). ---
        role_totals = {"entry": n_entry, "oog_site": n_halt, "revert_site": n_rev}
        tagged = []
        if n_entry:
            df = entry_rows.copy()
            df["_role"] = "entry"
            # entry kind follows the row's own OOG membership.
            df["_kind"] = (
                df["oog_contract"].notna().map(lambda b: "oog" if b else "non_oog")
            )
            tagged.append(df)
        if n_halt:
            df = oog_rows.copy()
            df["_role"] = "oog_site"
            df["_kind"] = "oog"  # an oog_site row is always an OOG halt
            tagged.append(df)
        if n_rev:
            df = rev_rows.copy()
            df["_role"] = "revert_site"
            df["_kind"] = "non_oog"  # a revert_site row is always non-OOG
            tagged.append(df)
        tagged_df = pd.concat(tagged, ignore_index=True)

        # failing_selector = coalesce(tier1_failing_selector, entry_selector).
        tagged_df["_failing_selector"] = tagged_df["tier1_failing_selector"].fillna(
            tagged_df["entry_selector"]
        )

        # Cluster key columns (§1d): the raw selector keys the cluster; decoding
        # is display-only. OOG rows key on oog_* site fields; non_oog on
        # divergence_* / failure_reason / revert_decoded.
        def _wc(r):
            return (
                r["oog_contract"] if r["_kind"] == "oog" else r["divergence_contract"]
            )

        def _op(r):
            return r["oog_opcode"] if r["_kind"] == "oog" else r["divergence_opcode"]

        def _pr(r):
            return r["oog_pattern"] if r["_kind"] == "oog" else r["failure_reason"]

        def _cd(r):
            return (
                r["oog_call_depth"]
                if r["_kind"] == "oog"
                else r["divergence_call_depth"]
            )

        tagged_df["_where"] = tagged_df.apply(_wc, axis=1)
        tagged_df["_opcode"] = tagged_df.apply(_op, axis=1)
        tagged_df["_pattern_or_reason"] = tagged_df.apply(_pr, axis=1)
        tagged_df["_call_depth"] = tagged_df.apply(_cd, axis=1)
        # oog_bottleneck_kind / revert_decoded only carry on their respective kinds.
        tagged_df["_bottleneck"] = tagged_df.apply(
            lambda r: r["oog_bottleneck_kind"] if r["_kind"] == "oog" else None, axis=1
        )
        tagged_df["_revert_decoded"] = tagged_df.apply(
            lambda r: r["revert_decoded"] if r["_kind"] == "non_oog" else None, axis=1
        )

        key_cols = [
            "_role",
            "_kind",
            "_failing_selector",
            "_where",
            "_opcode",
            "_pattern_or_reason",
            "_bottleneck",
            "_call_depth",
            "_revert_decoded",
        ]
        # NULL-safe grouping: fillna a sentinel so NULL keys group together, then
        # restore None on emit.
        _SENT = "\x00__na__"
        grp_keys = {
            c: tagged_df[c].where(tagged_df[c].notna(), _SENT) for c in key_cols
        }
        grouped = tagged_df.groupby(list(grp_keys.values()), sort=False)

        contract_total = len(tagged_df)
        clusters = []
        for _, cluster_rows in grouped:
            first = cluster_rows.iloc[0]

            def _val(col):
                v = first[col]
                if pd.isna(v):
                    return None
                return v

            role = str(first["_role"])
            kind = str(first["_kind"])
            failing_sel = _val("_failing_selector")
            entry_sel = _val("entry_selector")
            where_contract = _val("_where")
            opcode = _val("_opcode")
            pattern_or_reason = _val("_pattern_or_reason")
            bottleneck = _val("_bottleneck")
            call_depth = _val("_call_depth")
            revert_decoded = _val("_revert_decoded")

            count = int(len(cluster_rows))
            role_denom = role_totals.get(role, 0)
            gd = cluster_rows["gas_delta"].dropna()
            examples = [
                {
                    "tx_hash": str(er["tx_hash"]),
                    "block_number": int(er["block_number"]),
                    "gas_delta": (
                        int(er["gas_delta"]) if pd.notna(er["gas_delta"]) else None
                    ),
                }
                for _, er in cluster_rows.head(AFFECTED_EXAMPLES_PER_CLUSTER).iterrows()
            ]
            clusters.append(
                {
                    "role": role,
                    "kind": kind,
                    "selector": str(failing_sel) if failing_sel else None,
                    "selector_signature": _decode_sel(
                        failing_sel, _addr_category(addr), selector_map
                    ),
                    "entry_selector": str(entry_sel) if entry_sel else None,
                    "entry_signature": _decode_sel(
                        entry_sel, _addr_category(addr), selector_map
                    ),
                    "where_contract": (
                        str(where_contract).lower() if where_contract else None
                    ),
                    "where_label": (
                        label_address(str(where_contract)) if where_contract else None
                    ),
                    "where_category": (
                        _addr_category(str(where_contract)) if where_contract else None
                    ),
                    "opcode": str(opcode) if opcode is not None else None,
                    "pattern_or_reason": (
                        str(pattern_or_reason)
                        if pattern_or_reason is not None
                        else None
                    ),
                    "oog_bottleneck_kind": (
                        str(bottleneck) if bottleneck is not None else None
                    ),
                    "call_depth": int(call_depth) if call_depth is not None else None,
                    "revert_decoded": (
                        str(revert_decoded) if revert_decoded is not None else None
                    ),
                    "count": count,
                    "share_of_role": (
                        round(count / role_denom, 4) if role_denom else None
                    ),
                    "share_of_contract": (
                        round(count / contract_total, 4) if contract_total else None
                    ),
                    "gas_delta": {
                        "avg": int(round(float(gd.mean()))) if not gd.empty else 0,
                        "p50": _q(gd, 0.5) if not gd.empty else None,
                        "p90": _q(gd, 0.9) if not gd.empty else None,
                    },
                    "drivers": _cluster_drivers(cluster_rows),
                    "examples": examples,
                }
            )

        clusters.sort(key=lambda c: c["count"], reverse=True)
        distinct_cluster_count = len(clusters)
        shown = clusters[:AFFECTED_CLUSTER_TOP_N]
        shown_count = sum(c["count"] for c in shown)
        clusters_shown_share = (
            round(shown_count / contract_total, 4) if contract_total else 0.0
        )

        # --- identity header (§1f): fresh probe wins, cache fills the rest. ---
        rec = classify_address(addr)
        probe = structural.get(addr)

        def _tag(field):
            val = getattr(probe, field, None) if probe is not None else None
            return val if val else getattr(rec, field)

        up = (
            probe
            if probe is not None
            and getattr(probe, "upgrade_mechanism", "none") != "none"
            else rec
        )

        # failure_rate denominator (null when no Xatu total for this contract).
        total_tx = total_tx_by_addr.get(addr)
        if total_tx:
            failure_rate = {
                "total_tx": int(total_tx),
                "halt_rate": round(n_halt / total_tx, 6) if n_halt else 0.0,
                "revert_rate": round(n_rev / total_tx, 6) if n_rev else 0.0,
            }
        else:
            failure_rate = None

        context = _affected_context(
            entry_rows,
            site_rows,
            selector_map,
            _addr_category(addr),
            failure_rate,
            broader_by_recipient.get(addr, (0, 0, 0, 0)),
        )

        contracts_out[addr] = {
            "address": addr,
            "label": rec.label,
            "category": rec.category if rec.category != "unknown" else None,
            "owner_project": rec.owner_project or None,
            "source": rec.source if rec.source != "unknown" else None,
            "confidence": rec.confidence,
            "is_proxy": _tag("is_proxy"),
            "is_factory": _tag("is_factory"),
            "is_safe": _tag("is_safe"),
            "erc_type": _tag("erc_type"),
            "is_upgradable": up.is_upgradable,
            "upgrade_mechanism": (
                up.upgrade_mechanism
                if up.upgrade_mechanism and up.upgrade_mechanism != "none"
                else None
            ),
            "upgrade_admin": up.upgrade_admin or None,
            "is_mev_bot": bool(rec.is_mev_bot),
            "mev_role": rec.mev_role or None,
            "roles_summary": roles_summary,
            "distinct_cluster_count": distinct_cluster_count,
            "clusters_shown_share": clusters_shown_share,
            "failure_clusters": shown,
            "context": context,
        }

    # --- Build the collapsed deploy-OOG aggregate (VECTORIZED over the g4 frame
    # via deploy_set membership — never per-account concatenation). ---
    if not deploy_set:
        deploy_oog = _empty_deploy_oog()
    else:
        deploy_oog_rows = g4[g4["oog_contract"].isin(deploy_set)]
        deploy_rev_rows = g4[g4["divergence_contract"].isin(deploy_set)]

        def _kv(counts) -> List[dict]:
            return [{"key": str(k), "count": int(v)} for k, v in counts.items()]

        # halt_opcode_split: value_counts of oog_opcode (desc).
        halt_opcode_split = _kv(
            deploy_oog_rows["oog_opcode"].dropna().astype(str).value_counts()
        )

        # initcode_families: 6-char prefix of coalesce(tier1_failing_selector,
        # entry_selector) (desc). "0x60"/"0x61" init-code prefixes.
        _sel = deploy_oog_rows["tier1_failing_selector"].fillna(
            deploy_oog_rows["entry_selector"]
        )
        _prefix = _sel.dropna().astype(str).str.slice(0, 6)
        initcode_families = _kv(_prefix.value_counts())

        # top_entry_contracts: top-8 recipients (the EntryPoint / factory the create
        # ran inside), labeled + categorized.
        top_entry_contracts = [
            {
                "contract": str(c).lower(),
                "label": label_address(str(c)),
                "category": _addr_category(str(c)),
                "count": int(n),
            }
            for c, n in deploy_oog_rows["recipient"]
            .dropna()
            .astype(str)
            .value_counts()
            .head(8)
            .items()
        ]

        # revert_reasons: top-8 decoded reverts on the bubbled-up revert rows.
        revert_reasons = _kv(
            deploy_rev_rows["revert_decoded"]
            .dropna()
            .astype(str)
            .value_counts()
            .head(8)
        )

        gd = deploy_oog_rows["gas_delta"].dropna()
        gas_delta = {
            "p50": _q(deploy_oog_rows["gas_delta"], 0.5),
            "p90": _q(deploy_oog_rows["gas_delta"], 0.9),
            "min": int(gd.min()) if not gd.empty else None,
            "max": int(gd.max()) if not gd.empty else None,
        }

        # accounts: one entry per deploy account (FIRST self-halt row).
        accounts: Dict[str, dict] = {}
        for acct, rows in deploy_oog_rows.groupby("oog_contract"):
            first = rows.iloc[0]
            sel = first["tier1_failing_selector"]
            if pd.isna(sel):
                sel = first["entry_selector"]
            entry = first["recipient"]
            accounts[str(acct).lower()] = {
                "tx": str(first["tx_hash"]),
                "block": int(first["block_number"]),
                "gas_delta": (
                    int(first["gas_delta"]) if pd.notna(first["gas_delta"]) else None
                ),
                "opcode": (
                    str(first["oog_opcode"]) if pd.notna(first["oog_opcode"]) else None
                ),
                "selector": str(sel) if pd.notna(sel) else None,
                "entry": str(entry).lower() if pd.notna(entry) else None,
            }

        deploy_oog = {
            "schedule": ctx.schedule,
            "class": "deploy_oog",
            "block_range": block_range,
            "g4_total": g4_total,
            "count": len(deploy_set),
            "explainer": _DEPLOY_OOG_EXPLAINER,
            "aggregate": {
                "halt_opcode_split": halt_opcode_split,
                "initcode_families": initcode_families,
                "top_entry_contracts": top_entry_contracts,
                "revert_reasons": revert_reasons,
                "gas_delta": gas_delta,
                "drivers": _cluster_drivers(deploy_oog_rows),
            },
            "accounts": accounts,
        }

    return _index(contracts_out, len(deploy_set)), contracts_out, deploy_oog


def write_affected_contracts(ctx: RunContext, sched_dir: Path) -> dict:
    """Write the sharded affected-contracts output under ``sched_dir/affected/``.

    Emits ``affected/index.json`` (COMPACT — the small init file the page loads on
    load) plus one ``affected/{lowercase_addr}.json`` per affected contract (each
    the per-contract record built by :func:`_build_affected_contracts`; shards are
    small, so ``indent=2`` is fine) plus one aggregate ``affected/deploy_oog.json``
    collapsing the freshly-deployed-and-self-OOG long tail (written COMPACT — it can
    be large). Returns a size summary
    ``{shard_count, index_bytes, deploy_oog_bytes, total_bytes}``.
    """
    index, records, deploy_oog = _build_affected_contracts(ctx)

    affected_dir = sched_dir / "affected"
    affected_dir.mkdir(parents=True, exist_ok=True)

    # Clear stale outputs first so a re-run is idempotent: contracts that drop out
    # of the affected set (and, above all, the ~100k deploy-OOG accounts now
    # collapsed into deploy_oog.json instead of one shard apiece) would otherwise
    # linger as orphaned {addr}.json files, defeating the collapse. index.json and
    # deploy_oog.json are rewritten below regardless.
    for stale in affected_dir.glob("*.json"):
        stale.unlink()

    index_path = affected_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    index_bytes = index_path.stat().st_size
    total_bytes = index_bytes

    # The collapsed deploy-OOG aggregate (one file for the whole long tail).
    deploy_oog_path = affected_dir / "deploy_oog.json"
    with open(deploy_oog_path, "w") as f:
        json.dump(deploy_oog, f, separators=(",", ":"))
    deploy_oog_bytes = deploy_oog_path.stat().st_size
    total_bytes += deploy_oog_bytes

    for addr, record in records.items():
        shard_path = affected_dir / f"{addr}.json"
        with open(shard_path, "w") as f:
            json.dump(record, f, indent=2)
        total_bytes += shard_path.stat().st_size

    return {
        "shard_count": len(records),
        "index_bytes": index_bytes,
        "deploy_oog_bytes": deploy_oog_bytes,
        "total_bytes": total_bytes,
    }


def _decode_sel(selector, category, selector_map):
    """Decode a 4-byte selector to a signature (display only), or None.

    Wraps :func:`label_sources.selectors.decode_selector`; a cache miss / import
    failure degrades to ``None`` (the frontend shows raw hex) and never splits a
    cluster (clustering keys on the raw selector, not the decoded signature).
    """
    if not selector:
        return None
    try:
        from repricing_impact.label_sources.selectors import decode_selector

        return decode_selector(str(selector), category, selector_map)
    except Exception:  # noqa: BLE001 — display-only, degrade to raw hex
        return None


def _round_or_none(v):
    return int(round(v)) if v is not None and pd.notna(v) else None


def _to_bool(v) -> bool:
    """Coerce a ClickHouse ``Bool`` cell to a Python bool.

    The clickhouse-http driver returns ``Bool`` columns as the **strings**
    ``"true"`` / ``"false"`` (not Python bools), so ``bool(v)`` on ``"false"``
    would wrongly be ``True``. Handle string, numeric, and native-bool forms.
    """
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def _to_bool_nullable(v):
    """Like :func:`_to_bool` but preserves NULL (NaN/None -> None).

    Used for ``Nullable(Bool)`` columns (e.g. ``replay_halt_oog``,
    ``reservoir_exhausted``) so a missing value stays NULL in DuckDB rather than
    collapsing to ``False``.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "nan", "none", "\\n"):
            return None
        return s == "true"
    return bool(v)


def _to_uint64_nullable(v):
    """Coerce a warehouse ``UInt64`` reservoir cell to a bounded int (or None).

    NULL/NaN stays None, and the near-``2^64`` "no reservoir" sentinel (which
    overflows DuckDB's UBIGINT and would poison averages) is nulled. Everything
    below :data:`_UINT64_SENTINEL_FLOOR` passes through as a Python int.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("", "nan", "none", "\\n"):
            return None
        v = float(s) if ("." in s or "e" in s.lower()) else int(s)
    iv = int(v)
    return None if iv >= _UINT64_SENTINEL_FLOOR or iv < 0 else iv


def _to_int_nullable(v):
    """Coerce a nullable small-int / byte-count cell to a Python int (or None).

    NULL/NaN/empty stays None; numeric strings and floats become ints. Used for
    ``tx_type`` / ``has_authorization`` (UInt8) and the ``input_*_bytes`` (UInt64)
    tx-shape columns, whose values are small and never hit the reservoir sentinel.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("", "nan", "none", "\\n"):
            return None
        v = float(s) if ("." in s or "e" in s.lower()) else int(s)
    return int(v)


def _to_str_nullable(v):
    """Coerce a nullable VARCHAR (selector) cell to a Python str (or None).

    Mirrors :func:`_to_int_nullable`'s string-NULL handling but for strings: the
    HTTP driver hands back NULL VARCHARs as NaN / ``''`` / ``'\\n'`` / ``'none'``,
    which would otherwise become a spurious non-NULL cluster key. Selectors are
    hex, so the value is passed through verbatim (no lowercasing).
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if not isinstance(v, str):
        return str(v)
    s = v.strip()
    if s.lower() in ("", "nan", "none", "\\n"):
        return None
    return s


def _contract_mix(
    ctx: RunContext, recipient: str, col: str, limit: int = 3
) -> List[dict]:
    """Top divergence/oog contracts for a recipient's G4 cohort — LOCAL DuckDB.

    ``col`` is a fixed identifier from a trusted set (``divergence_contract`` /
    ``oog_contract``); ``recipient`` is parameterized. Addresses in
    ``divergence_tx`` are already lowercased.
    """
    if col not in ("divergence_contract", "oog_contract"):
        raise ValueError(f"unexpected contract-mix column: {col}")
    r = ctx.con.execute(
        f"""
        SELECT {col} AS c, count(*) AS n
        FROM divergence_tx
        WHERE ({groups.G4_PREDICATE})
          AND recipient = ?
          AND {col} IS NOT NULL
        GROUP BY c ORDER BY n DESC LIMIT {limit}
        """,
        [recipient],
    ).df()
    return [
        {
            "contract": row["c"],
            "label": label_address(row["c"]),
            "category": _addr_category(row["c"]),
            "count": int(row["n"]),
        }
        for _, row in r.iterrows()
    ]


# ---- examples.json ----


def emit_examples(ctx: RunContext) -> dict:
    """Capped LIMITed pull of representative G4 (and G3) example txs (SCHEMA §6)."""
    half = EXAMPLES_CAP // 2
    rows = []
    # LOCAL DuckDB over divergence_tx (bools already decoded, opcode already a
    # mnemonic, addresses already lowercased).
    for gnum, pred, lim in (
        ("4", groups.G4_PREDICATE, EXAMPLES_CAP - half),
        ("3", groups.G3_PREDICATE, half),
    ):
        r = ctx.con.execute(f"""
            SELECT tx_hash, block_number, gas_delta, min_multiplier_to_succeed,
                   baseline_success, schedule_success, oog_pattern,
                   divergence_opcode, recipient, divergence_contract, state_gas_category
            FROM divergence_tx
            WHERE ({pred})
            ORDER BY abs(gas_delta) DESC
            LIMIT {lim}
            """).df()
        for _, row in r.iterrows():
            rows.append(
                {
                    "tx_hash": str(row["tx_hash"]),
                    "block_number": int(row["block_number"]),
                    "group": int(gnum),
                    "recipient": (
                        row["recipient"] if pd.notna(row["recipient"]) else None
                    ),
                    "recipient_label": label_address(
                        row["recipient"] if pd.notna(row["recipient"]) else None
                    ),
                    "recipient_category": _addr_category(
                        row["recipient"] if pd.notna(row["recipient"]) else None
                    ),
                    "gas_delta": int(row["gas_delta"]),
                    "min_multiplier_to_succeed": _round_or_none(
                        row["min_multiplier_to_succeed"]
                    ),
                    "baseline_success": bool(row["baseline_success"]),
                    "schedule_success": bool(row["schedule_success"]),
                    "oog_pattern": (
                        row["oog_pattern"] if pd.notna(row["oog_pattern"]) else None
                    ),
                    "divergence_opcode": (
                        row["divergence_opcode"]
                        if pd.notna(row["divergence_opcode"])
                        else None
                    ),
                    "divergence_contract": (
                        row["divergence_contract"]
                        if pd.notna(row["divergence_contract"])
                        else None
                    ),
                    "state_gas_category": (
                        row["state_gas_category"]
                        if pd.notna(row["state_gas_category"])
                        else None
                    ),
                }
            )
    return {"schedule": ctx.schedule, "capped_at": EXAMPLES_CAP, "examples": rows}


# --- Orchestration ---------------------------------------------------------


def run_schedule(
    schedule: str,
    config_hash: str,
    block_start: int,
    block_end: int,
    chunk_blocks: int,
    out_dir: Path,
    schedules_available: List[str],
    cfg_source: str,
    duckdb_path: Path,
) -> Dict[str, int]:
    engine = get_engine()
    con = duckdb.connect(str(duckdb_path))
    ctx = RunContext(
        config_hash=config_hash,
        schedule=schedule,
        block_start=block_start,
        block_end=block_end,
        chunk_blocks=chunk_blocks,
        con=con,
        engine=engine,
    )

    print(f"[{schedule}] stage block_groups ({block_start}..{block_end}) ...")
    stage_block_groups(ctx)

    # Materialize the slim deduped per-tx projection into DuckDB before any
    # emitter reads it (gas_delta_hist / group_categories / contract_failures /
    # examples all query the divergence_tx table locally).
    print(f"[{schedule}] stage divergence_tx ({block_start}..{block_end}) ...")
    stage_divergence_tx(ctx)

    sched_dir = out_dir / schedule
    sched_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "meta.json": emit_meta(ctx, schedules_available, cfg_source),
        "overview_series.json": emit_overview_series(ctx),
        "gas_delta_hist.json": emit_gas_delta_hist(ctx),
        "group_categories.json": emit_group_categories(ctx),
        "oog_forensics.json": emit_oog_forensics(ctx),
        "nonoog_forensics.json": emit_nonoog_forensics(ctx),
        "contract_failures.json": emit_contract_failures(ctx),
        "examples.json": emit_examples(ctx),
    }
    sizes = {}
    for name, payload in files.items():
        path = sched_dir / name
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        sizes[name] = path.stat().st_size
        print(f"[{schedule}] wrote {path} ({sizes[name]:,} bytes)")

    # Sharded affected-contracts output: affected/index.json (compact) + one
    # affected/{addr}.json shard per affected contract, written directly to disk
    # (the single nested file was too big at full scale).
    affected = write_affected_contracts(ctx, sched_dir)
    sizes["affected/"] = affected["total_bytes"]
    print(
        f"[{schedule}] wrote {sched_dir / 'affected'}/ "
        f"({affected['shard_count']:,} shards, "
        f"index {affected['index_bytes']:,} bytes, "
        f"total {affected['total_bytes']:,} bytes)"
    )
    con.close()
    return sizes


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schedules", nargs="+", default=FOCUS_SCHEDULES)
    ap.add_argument("--block-start", type=int, default=None)
    ap.add_argument("--block-end", type=int, default=None)
    ap.add_argument(
        "--limit-blocks",
        type=int,
        default=None,
        help="Number of blocks from --block-start (alternative to --block-end).",
    )
    ap.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS)
    ap.add_argument("--out-dir", type=Path, default=SITE_DATA)
    ap.add_argument(
        "--config-hash",
        type=str,
        default=None,
        help="Override the pinned analysis_config_hash.",
    )
    ap.add_argument(
        "--duckdb-path",
        type=Path,
        default=None,
        help="Build-time DuckDB intermediate (default: <out-dir>/.build/precompute.duckdb).",
    )
    ap.add_argument(
        "--full-range",
        action="store_true",
        help="Use the full pinned-config block range (DELIBERATE; large scan).",
    )
    args = ap.parse_args(argv)

    res = resolve_config_hash()
    config_hash = args.config_hash or res.single_hash
    if config_hash is None:
        raise SystemExit("No single config covers both schedules; pass --config-hash.")
    cfg_source = (
        "override"
        if args.config_hash
        else ("most blocks + most recent updated_at covering both schedules")
    )
    cfg_lo, cfg_hi = res.common_block_range

    # Resolve window.
    if args.full_range:
        block_start, block_end = cfg_lo, cfg_hi
    elif args.block_start is not None:
        block_start = args.block_start
        if args.block_end is not None:
            block_end = args.block_end
        elif args.limit_blocks is not None:
            block_end = block_start + args.limit_blocks - 1
        else:
            block_end = block_start + DEFAULT_VALIDATION_BLOCKS - 1
    else:
        # Default: small validation window at the start of the config range.
        block_start = cfg_lo
        nblocks = args.limit_blocks or DEFAULT_VALIDATION_BLOCKS
        block_end = block_start + nblocks - 1
        print(
            f"No window specified — defaulting to a SMALL validation window: "
            f"{block_start}..{block_end} ({nblocks} blocks)."
        )

    block_start = max(block_start, cfg_lo)
    block_end = min(block_end, cfg_hi)

    duckdb_path = args.duckdb_path or (args.out_dir / ".build" / "precompute.duckdb")
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"config_hash = {config_hash}")
    print(
        f"window      = {block_start}..{block_end}  ({block_end - block_start + 1} blocks)"
    )
    print(f"schedules   = {args.schedules}")
    print(f"out-dir     = {args.out_dir}")
    print(f"duckdb      = {duckdb_path}")

    all_sizes = {}
    for schedule in args.schedules:
        sizes = run_schedule(
            schedule=schedule,
            config_hash=config_hash,
            block_start=block_start,
            block_end=block_end,
            chunk_blocks=args.chunk_blocks,
            out_dir=args.out_dir,
            schedules_available=list(args.schedules),
            cfg_source=cfg_source,
            duckdb_path=duckdb_path,
        )
        all_sizes[schedule] = sizes

    print("\nDone. Files written:")
    for sched, sizes in all_sizes.items():
        for name, sz in sizes.items():
            print(f"  site/data/{sched}/{name}  {sz:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
