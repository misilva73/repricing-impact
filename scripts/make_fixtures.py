#!/usr/bin/env python3
"""Generate realistic FIXTURE JSON for the static dashboard.

These fixtures match — key-for-key, type-for-type — the schema that the real
precompute step (`scripts/precompute.py`) must emit, documented in
`site/data/SCHEMA.md` and at the top of `site/assets/app.js`.

Wiring real data later is a drop-in: replace these files with the precompute
output and the static site renders unchanged.

IMPORTANT: site/data/ now holds REAL precompute output. This script writes to
fixtures_scratch/ (or $FIXTURE_OUT_DIR) so it can NEVER overwrite real data.

Run:  python scripts/make_fixtures.py
      FIXTURE_OUT_DIR=/tmp/fix python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# NOTE: fixtures write to a scratch dir, NOT site/data/. site/data/ now holds
# REAL precompute output and must not be overwritten. Override with
# FIXTURE_OUT_DIR if you want them elsewhere for offline dev.
import os

DATA = Path(os.environ.get("FIXTURE_OUT_DIR", ROOT / "fixtures_scratch"))

# Pinned analysis window: Jan 1 2026 .. Jun 28 2026 (~150 daily buckets).
START = date(2026, 1, 1)
N_DAYS = 150
# Mainnet ~7150 blocks/day; pin a realistic contiguous block range.
BLOCK_START = 21_500_000
BLOCKS_PER_DAY = 7150

# Schedule-specific shape. eip-8037 = state-gas/reservoir flavour;
# eip-8038 = state access/write repricing (non-uniform; storage write 2800->10000,
# cold access ->3000, account write ->8000). Tagged "opcode" flavour for legacy reasons.
SCHEDULES = {
    "eip-8038": {
        "config_hash": "0xa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "seed": 8038,
        "txs_per_day": 1_150_000,
        # group base fractions (G1 dominant)
        "frac": {"g1": 0.9760, "g2": 0.0205, "g3": 0.0021, "g4": 0.0009, "g5": 0.0005},
        "rescue_rate": 0.00010,
        "truncated_block_frac": 0.018,
        "flavour": "opcode",
    },
    "eip-8037": {
        "config_hash": "0xf0e1d2c3b4a5968778695a4b3c2d1e0ff0e1d2c3b4a5968778695a4b3c2d1e0f",
        "seed": 8037,
        "txs_per_day": 1_120_000,
        "frac": {"g1": 0.9710, "g2": 0.0240, "g3": 0.0028, "g4": 0.0014, "g5": 0.0008},
        "rescue_rate": 0.00016,
        "truncated_block_frac": 0.024,
        "flavour": "state",
    },
}

GROUP_LABELS = {
    "g1": "No change",
    "g2": "Succeeds with changes",
    "g3": "Fixable with gas-limit increase",
    "g4": "Potentially broken",
    "g5": "Unknown",
}

# Human-labelled recipients (mirrors ported labels.py).
RECIPIENTS = [
    ("0xdac17f958d2ee523a2206206994597c13d831ec7", "Tether USDT"),
    ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "Circle USDC"),
    ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH"),
    ("0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad", "Uniswap Universal Router"),
    ("0x66a9893cc07d91d95644aedd05d03f95e1dba8af", "Uniswap V4 Universal Router"),
    ("0x000000000004444c5dc75cb358380d2e3de08a90", "Uniswap V4 PoolManager"),
    ("0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "Uniswap V2 Router"),
    ("0xe592427a0aece92de3edee1f18e0157c05861564", "Uniswap V3 Router"),
    ("0x000000000022d473030f116ddee9f6b43ac78ba3", "Uniswap Permit2"),
    ("0x0000000071727de22e5e9d8baf0edac6f37da032", "ERC-4337 EntryPoint v0.7"),
    ("0x4337084d9e255ff0702461cf8895ce9e3b5ff108", "ERC-4337 EntryPoint v0.8"),
    ("0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789", "ERC-4337 EntryPoint v0.6"),
    ("0x111111125421ca6dc452d289314280a0f8842a65", "1inch Aggregation Router"),
    ("0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae", "LI.FI / Socket Bridge"),
    ("0x881d40237659c251811cec9c364ef91dc08d300c", "Metamask Swap Router"),
    ("0x6b175474e89094c44da98b954eedeac495271d0f", "Maker DAI"),
    ("0x00005ea00ac477b1030ce78506496e8c2de24bf5", "OpenSea SeaDrop"),
    ("0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb", "0x Settler / Aggregation"),
]


def jitter(rng: random.Random, base: float, rel: float) -> float:
    return base * (1.0 + rng.uniform(-rel, rel))


def build_overview_series(rng: random.Random, cfg: dict) -> dict:
    """Per-day-bucketed G1-G5 composition + totals + rescues."""
    buckets = []
    totals = {k: 0 for k in ("g1", "g2", "g3", "g4", "g5")}
    rescues_total = 0
    truncated_blocks_total = 0
    f = cfg["frac"]
    for d in range(N_DAYS):
        day = START + timedelta(days=d)
        # weekly + slow trend seasonality on volume
        season = (
            1.0
            + 0.18 * math.sin(2 * math.pi * d / 7)
            + 0.05 * math.sin(2 * math.pi * d / 30)
        )
        tx_count = int(jitter(rng, cfg["txs_per_day"] * season, 0.04))

        g1 = int(tx_count * jitter(rng, f["g1"], 0.004))
        g2 = int(tx_count * jitter(rng, f["g2"], 0.10))
        g3 = int(tx_count * jitter(rng, f["g3"], 0.18))
        g4 = int(tx_count * jitter(rng, f["g4"], 0.22))
        g5 = max(0, tx_count - (g1 + g2 + g3 + g4))

        rescues = int(tx_count * jitter(rng, cfg["rescue_rate"], 0.25))
        block_start = BLOCK_START + d * BLOCKS_PER_DAY
        blocks = int(jitter(rng, BLOCKS_PER_DAY, 0.03))
        truncated = int(blocks * jitter(rng, cfg["truncated_block_frac"], 0.3))

        for k, v in (("g1", g1), ("g2", g2), ("g3", g3), ("g4", g4), ("g5", g5)):
            totals[k] += v
        rescues_total += rescues
        truncated_blocks_total += truncated

        buckets.append(
            {
                "date": day.isoformat(),
                "block_start": block_start,
                "block_end": block_start + blocks - 1,
                "tx_count": tx_count,
                "g1": g1,
                "g2": g2,
                "g3": g3,
                "g4": g4,
                "g5": g5,
                "rescues": rescues,
                "drill_ins_truncated_blocks": truncated,
            }
        )

    return {
        "schedule": cfg["_name"],
        "bucket_by": "day",
        "buckets": buckets,
        "totals": {
            "tx_count": sum(totals.values()),
            **totals,
            "rescues": rescues_total,
            "drill_ins_truncated_blocks": truncated_blocks_total,
        },
        "group_labels": GROUP_LABELS,
    }


def log2_hist(
    rng: random.Random,
    center: float,
    spread: float,
    total: int,
    signed: bool,
    lo: int = -28,
    hi: int = 28,
) -> list[dict]:
    """Build a log2-binned histogram. Bins are integer log2 exponents.

    For signed data, a bin's `bin_log2` is the exponent and `sign` is -1/+1.
    For magnitude-only (G2 source), all bins are positive magnitude.
    """
    bins = []
    for e in range(lo, hi + 1):
        # gaussian-ish mass around center exponent
        w = math.exp(-0.5 * ((e - center) / spread) ** 2)
        count = int(total * w / (spread * 2.5066))
        if count <= 0:
            continue
        if signed:
            # mostly positive (schedule costs more), some negative (cheaper)
            pos = int(count * 0.82)
            neg = count - pos
            if pos > 0:
                bins.append({"bin_log2": e, "sign": 1, "count": pos})
            if neg > 0 and e <= center + 2:
                bins.append({"bin_log2": e, "sign": -1, "count": neg})
        else:
            bins.append({"bin_log2": e, "sign": 1, "count": count})
    return bins


def percentiles(rng: random.Random, base: float, signed: bool) -> dict:
    s = -1 if signed and rng.random() < 0.15 else 1
    p = {
        "p01": int(s * base * 0.05),
        "p10": int(s * base * 0.2),
        "p25": int(base * 0.45),
        "p50": int(base * 1.0),
        "p75": int(base * 2.1),
        "p90": int(base * 4.0),
        "p99": int(base * 9.5),
    }
    return p


def build_gas_delta_hist(rng: random.Random, cfg: dict, series: dict) -> dict:
    t = series["totals"]
    groups = {}

    # G2: magnitude-only (abs gas_delta), combines block_summary log2 hist +
    # per-tx drill-in members that pass at 1x.
    groups["2"] = {
        "label": GROUP_LABELS["g2"],
        "signed": False,
        "note": "Magnitude only (abs gas_delta): G2 source histogram is unsigned.",
        "count": t["g2"],
        "bins": log2_hist(rng, center=14.0, spread=3.0, total=t["g2"], signed=False),
        "percentiles": percentiles(rng, base=18000, signed=False),
        "sum_gas_delta": int(t["g2"] * jitter(rng, 22000, 0.1)),
        "min_gas_delta": 12,
        "max_gas_delta": int(jitter(rng, 4_500_000, 0.2)),
    }
    # G3: signed exact per-tx (needs gas bump -> usually positive delta).
    groups["3"] = {
        "label": GROUP_LABELS["g3"],
        "signed": True,
        "note": "Signed per-tx gas_delta (negative = schedule cheaper).",
        "count": t["g3"],
        "bins": log2_hist(rng, center=16.5, spread=3.2, total=t["g3"], signed=True),
        "percentiles": percentiles(rng, base=96000, signed=True),
        "sum_gas_delta": int(t["g3"] * jitter(rng, 110000, 0.1)),
        "min_gas_delta": int(-jitter(rng, 8000, 0.2)),
        "max_gas_delta": int(jitter(rng, 12_000_000, 0.2)),
    }
    # G4: signed exact per-tx.
    groups["4"] = {
        "label": GROUP_LABELS["g4"],
        "signed": True,
        "note": "Signed per-tx gas_delta (negative = schedule cheaper).",
        "count": t["g4"],
        "bins": log2_hist(rng, center=17.5, spread=3.5, total=t["g4"], signed=True),
        "percentiles": percentiles(rng, base=180000, signed=True),
        "sum_gas_delta": int(t["g4"] * jitter(rng, 240000, 0.1)),
        "min_gas_delta": int(-jitter(rng, 15000, 0.2)),
        "max_gas_delta": int(jitter(rng, 28_000_000, 0.2)),
    }
    return {"schedule": cfg["_name"], "groups": groups}


def counts_dict(
    rng: random.Random, total: int, keys_weights: list[tuple]
) -> list[dict]:
    out = []
    remaining = total
    n = len(keys_weights)
    for i, (k, w) in enumerate(keys_weights):
        if i == n - 1:
            c = remaining
        else:
            c = int(total * jitter(rng, w, 0.08))
            remaining -= c
        out.append({"key": k, "count": max(0, c)})
    return out


def build_group_categories(rng: random.Random, cfg: dict, series: dict) -> dict:
    t = series["totals"]
    flavour = cfg["flavour"]

    # G2 categorisation: gas-only opcode shift leaderboard + state-driver mix
    # + drill-in divergence reasons + rescues.
    if flavour == "opcode":
        opcode_leaderboard = [
            {"opcode": "SLOAD", "tx_count": int(t["g2"] * 0.34), "avg_gas_shift": 1700},
            {
                "opcode": "SSTORE",
                "tx_count": int(t["g2"] * 0.22),
                "avg_gas_shift": 3400,
            },
            {"opcode": "CALL", "tx_count": int(t["g2"] * 0.16), "avg_gas_shift": 1300},
            {
                "opcode": "KECCAK256",
                "tx_count": int(t["g2"] * 0.11),
                "avg_gas_shift": 60,
            },
            {"opcode": "LOG3", "tx_count": int(t["g2"] * 0.08), "avg_gas_shift": 750},
            {
                "opcode": "EXTCODESIZE",
                "tx_count": int(t["g2"] * 0.05),
                "avg_gas_shift": 1400,
            },
        ]
    else:
        opcode_leaderboard = [
            {
                "opcode": "SSTORE",
                "tx_count": int(t["g2"] * 0.40),
                "avg_gas_shift": 4100,
            },
            {"opcode": "SLOAD", "tx_count": int(t["g2"] * 0.25), "avg_gas_shift": 2100},
            {"opcode": "CALL", "tx_count": int(t["g2"] * 0.14), "avg_gas_shift": 900},
            {
                "opcode": "BALANCE",
                "tx_count": int(t["g2"] * 0.07),
                "avg_gas_shift": 1600,
            },
            {
                "opcode": "EXTCODEHASH",
                "tx_count": int(t["g2"] * 0.05),
                "avg_gas_shift": 1400,
            },
        ]

    state_driver_mix = counts_dict(
        rng,
        t["g2"],
        [
            ("no_state", 0.55),
            ("runtime_state", 0.30),
            ("creation", 0.10),
            ("authorization", 0.05),
        ],
    )
    divergence_reasons = counts_dict(
        rng,
        max(1, int(t["g2"] * 0.04)),
        [
            ("trace_changed", 0.5),
            ("event_logs_changed", 0.3),
            ("output_changed", 0.2),
        ],
    )

    # G3: multiplier hist, state-category, is_create, OOG pattern.
    mult_hist = [
        {"multiplier": 2, "count": int(t["g3"] * 0.58)},
        {"multiplier": 4, "count": int(t["g3"] * 0.27)},
        {"multiplier": 8, "count": int(t["g3"] * 0.14)},
        {"multiplier": ">8", "count": int(t["g3"] * 0.01)},
    ]
    g3_state_category = counts_dict(
        rng,
        t["g3"],
        [
            ("execution_bound", 0.45 if flavour == "opcode" else 0.20),
            ("state_bound", 0.30 if flavour == "opcode" else 0.55),
            ("mixed", 0.18),
            ("unknown", 0.07),
        ],
    )
    g3_oog_pattern = counts_dict(
        rng,
        t["g3"],
        [
            ("call_oog", 0.6),
            ("top_level_oog", 0.3),
            ("none", 0.1),
        ],
    )

    # G4: break-reason, oog pattern/bottleneck, status-flip direction, state-category.
    g4_break_reason = counts_dict(
        rng,
        t["g4"],
        [
            ("oog", 0.62),
            ("non_oog_revert", 0.28),
            ("other", 0.10),
        ],
    )
    g4_oog_bottleneck = counts_dict(
        rng,
        max(1, int(t["g4"] * 0.62)),
        [
            ("call_depth", 0.4),
            ("single_frame", 0.45),
            ("unknown", 0.15),
        ],
    )
    g4_status_flip = counts_dict(
        rng,
        t["g4"],
        [
            ("success_to_fail", 0.78),
            ("fail_to_fail", 0.18),
            ("fail_to_success", 0.04),
        ],
    )
    g4_state_category = counts_dict(
        rng,
        t["g4"],
        [
            ("execution_bound", 0.40 if flavour == "opcode" else 0.18),
            ("state_bound", 0.32 if flavour == "opcode" else 0.60),
            ("mixed", 0.20),
            ("unknown", 0.08),
        ],
    )

    out = {
        "schedule": cfg["_name"],
        "flavour": flavour,
        "g2": {
            "label": GROUP_LABELS["g2"],
            "count": t["g2"],
            "opcode_gas_shift_leaderboard": opcode_leaderboard,
            "state_driver_mix": state_driver_mix,
            "drill_in_divergence_reasons": divergence_reasons,
            "rescues": t["rescues"],
        },
        "g3": {
            "label": GROUP_LABELS["g3"],
            "count": t["g3"],
            "multiplier_histogram": mult_hist,
            "state_gas_category": g3_state_category,
            "is_create": {"true": int(t["g3"] * 0.06), "false": int(t["g3"] * 0.94)},
            "oog_pattern": g3_oog_pattern,
        },
        "g4": {
            "label": GROUP_LABELS["g4"],
            "count": t["g4"],
            "break_reason": g4_break_reason,
            "oog_bottleneck_kind": g4_oog_bottleneck,
            "status_flip": g4_status_flip,
            "state_gas_category": g4_state_category,
        },
    }

    # eip-8037: surface reservoir/spillover as genuine signal.
    if flavour == "state":
        out["g4"]["reservoir"] = {
            "reservoir_exhausted": {
                "true": int(t["g4"] * 0.66),
                "false": int(t["g4"] * 0.34),
            },
            "avg_initial_reservoir": 380000,
            "avg_runtime_state_gas_spillover": int(jitter(rng, 142000, 0.1)),
        }
        out["g3"]["reservoir"] = {
            "reservoir_exhausted": {
                "true": int(t["g3"] * 0.48),
                "false": int(t["g3"] * 0.52),
            },
            "avg_runtime_state_gas_spillover": int(jitter(rng, 88000, 0.1)),
        }
    return out


def build_contract_failures(rng: random.Random, cfg: dict, series: dict) -> dict:
    t = series["totals"]
    g4_total = t["g4"]
    g3_total = t["g3"]
    # distribute G4 across recipients with a heavy head (concentration).
    weights = [1.0 / (i + 1) ** 1.15 for i in range(len(RECIPIENTS))]
    wsum = sum(weights)
    contracts = []
    cum = 0
    for i, (addr, label) in enumerate(RECIPIENTS):
        share = weights[i] / wsum
        g4 = max(1, int(g4_total * share * jitter(rng, 1.0, 0.1)))
        g3 = max(0, int(g3_total * share * jitter(rng, 1.0, 0.15)))
        g2_drillin = int((g3 + g4) * jitter(rng, 1.4, 0.2))
        status_flips = int(g4 * jitter(rng, 0.78, 0.1))
        blocks_with_g4 = max(1, int(g4 * jitter(rng, 0.6, 0.15)))
        avg_g4_per_block = round(g4 / blocks_with_g4, 3)
        denom = g2_drillin + g3 + g4
        g4_vs_other_ratio = round(g4 / denom, 4) if denom else 0.0
        block_span_start = BLOCK_START + int(rng.uniform(0, 1000))
        block_span_end = (
            BLOCK_START + N_DAYS * BLOCKS_PER_DAY - int(rng.uniform(0, 2000))
        )
        contracts.append(
            {
                "recipient": addr,
                "label": label,
                "g4_tx_count": g4,
                "g3_tx_count": g3,
                "g2_drillin_tx_count": g2_drillin,
                "status_flips": status_flips,
                "avg_gas_delta": int(jitter(rng, 210000, 0.2)),
                "sum_gas_delta": int(g4 * jitter(rng, 210000, 0.2)),
                "min_mult_percentiles": {
                    "p50": rng.choice([None, 2, 4]),
                    "p90": rng.choice([None, 4, 8]),
                    "p99": None,
                },
                "block_span_start": block_span_start,
                "block_span_end": block_span_end,
                "distinct_blocks_with_g4": blocks_with_g4,
                "avg_g4_per_block": avg_g4_per_block,
                "g4_vs_other_ratio": g4_vs_other_ratio,
                "divergence_contract_mix": [
                    {"contract": addr, "label": label, "count": int(g4 * 0.7)},
                    {
                        "contract": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                        "label": "WETH",
                        "count": int(g4 * 0.18),
                    },
                ],
                "oog_contract_mix": [
                    {"contract": addr, "label": label, "count": int(g4 * 0.55)},
                ],
            }
        )
    # sort by g4 desc and compute cumulative share
    contracts.sort(key=lambda c: c["g4_tx_count"], reverse=True)
    total_g4_in_list = sum(c["g4_tx_count"] for c in contracts)
    for c in contracts:
        cum += c["g4_tx_count"]
        c["cumulative_share"] = (
            round(cum / total_g4_in_list, 4) if total_g4_in_list else 0.0
        )

    return {
        "schedule": cfg["_name"],
        "g4_total": g4_total,
        "g3_total": g3_total,
        "note": (
            "Per-recipient ratios are over the drill-in cohort (G3/G4 + "
            "G2 drill-in members) only — the warehouse has no per-recipient "
            "total-tx-per-block count. G1/G2 aggregate cohorts have no recipient."
        ),
        "contracts": contracts,
    }


def build_examples(rng: random.Random, cfg: dict, series: dict) -> dict:
    examples = []
    n = 40
    for i in range(n):
        addr, label = rng.choice(RECIPIENTS)
        group = 4 if i % 3 != 0 else 3
        block = BLOCK_START + int(rng.uniform(0, N_DAYS * BLOCKS_PER_DAY))
        tx_hash = "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(64))
        examples.append(
            {
                "tx_hash": tx_hash,
                "block_number": block,
                "group": group,
                "recipient": addr,
                "recipient_label": label,
                "gas_delta": int(jitter(rng, 320000 if group == 4 else 110000, 0.4)),
                "min_multiplier_to_succeed": (
                    None if group == 4 else rng.choice([2, 4, 8])
                ),
                "baseline_success": True,
                "schedule_success": group == 3,
                "oog_pattern": rng.choice(["call_oog", "top_level_oog", None]),
                "divergence_opcode": rng.choice(
                    ["SSTORE", "CALL", "SLOAD", "DELEGATECALL"]
                ),
                "divergence_contract": addr,
                "state_gas_category": rng.choice(
                    ["state_bound", "execution_bound", "mixed"]
                ),
            }
        )
    examples.sort(key=lambda e: e["gas_delta"], reverse=True)
    return {"schedule": cfg["_name"], "capped_at": n, "examples": examples}


def build_meta(cfg: dict, series: dict) -> dict:
    t = series["totals"]
    first = series["buckets"][0]
    last = series["buckets"][-1]
    total_blocks = last["block_end"] - first["block_start"] + 1
    truncated = t["drill_ins_truncated_blocks"]
    return {
        "schedule": cfg["_name"],
        "schedules_available": ["eip-8038", "eip-8037"],
        "analysis_config_hash": cfg["config_hash"],
        "chain_id": 1,
        "generated_at": "2026-06-30T12:00:00Z",
        "block_range": {
            "start": first["block_start"],
            "end": last["block_end"],
            "count": total_blocks,
        },
        "date_range": {"start": first["date"], "end": last["date"]},
        "totals": {
            "tx_count": t["tx_count"],
            "g1": t["g1"],
            "g2": t["g2"],
            "g3": t["g3"],
            "g4": t["g4"],
            "g5": t["g5"],
            "rescues": t["rescues"],
        },
        "group_labels": GROUP_LABELS,
        "truncation": {
            "drill_ins_truncated_blocks": truncated,
            "total_blocks": total_blocks,
            "truncated_share": round(truncated / total_blocks, 4),
            "note": (
                "Truncated blocks drop their drill-in rows, inflating G5 "
                "(Unknown). Treat G5 as a coverage gap, not a true partition."
            ),
        },
        "manifest": {
            "source": "ClickHouse gas_analysis warehouse (per-block/per-tx replays)",
            "schedule_name": cfg["_name"],
            "config_selected_by": "most blocks + most recent updated_at covering both schedules",
        },
        "pinned_config_note": (
            "Pinned to chain_id=1 and a single analysis_config_hash; "
            "all aggregates derive the transaction partition once in groups.py."
        ),
    }


def main() -> None:
    for name, cfg in SCHEDULES.items():
        cfg = dict(cfg)
        cfg["_name"] = name
        rng = random.Random(cfg["seed"])
        out_dir = DATA / name
        out_dir.mkdir(parents=True, exist_ok=True)

        series = build_overview_series(rng, cfg)
        files = {
            "meta.json": build_meta(cfg, series),
            "overview_series.json": series,
            "gas_delta_hist.json": build_gas_delta_hist(rng, cfg, series),
            "group_categories.json": build_group_categories(rng, cfg, series),
            "contract_failures.json": build_contract_failures(rng, cfg, series),
            "examples.json": build_examples(rng, cfg, series),
        }
        for fname, obj in files.items():
            (out_dir / fname).write_text(json.dumps(obj, indent=2) + "\n")
        print(f"wrote {len(files)} files for {name} -> {out_dir}")


if __name__ == "__main__":
    main()
