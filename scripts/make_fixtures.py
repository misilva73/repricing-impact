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


def _hex_selector(rng: random.Random) -> str:
    return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(8))


def _tx_hash(rng: random.Random) -> str:
    return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(64))


def _affected_examples(rng: random.Random, n: int) -> list[dict]:
    return [
        {
            "tx_hash": _tx_hash(rng),
            "block_number": BLOCK_START + int(rng.uniform(0, N_DAYS * BLOCKS_PER_DAY)),
            "gas_delta": int(jitter(rng, 180000, 0.4)),
        }
        for _ in range(n)
    ]


def _oog_drivers(rng: random.Random) -> dict:
    """A fully-populated OOG-cluster drivers block (all keys present)."""
    return {
        "state_gas_category": [
            {"key": "access_list", "count": int(rng.uniform(20, 120))},
            {"key": "transfer_new_account", "count": int(rng.uniform(5, 40))},
        ],
        "cold_account": {"p50": int(rng.uniform(2, 6)), "p90": int(rng.uniform(7, 20))},
        "sload": {"p50": int(rng.uniform(8, 20)), "p90": int(rng.uniform(30, 90))},
        "sstore": {"p50": int(rng.uniform(2, 8)), "p90": int(rng.uniform(9, 30))},
        "access_list_entries": {
            "p50": int(rng.uniform(1, 4)),
            "p90": int(rng.uniform(5, 15)),
        },
        # OOG-halt-only keys.
        "surcharge_at_oog": {
            "p50": int(jitter(rng, 21000, 0.3)),
            "sum": int(jitter(rng, 4_200_000, 0.3)),
        },
        "gas_remaining_at_oog": {
            "p50": int(rng.uniform(400, 1500)),
            "p90": int(rng.uniform(3000, 9000)),
        },
        "reservoir_exhausted_share": round(rng.uniform(0.4, 0.9), 4),
        "spillover_share": round(rng.uniform(0.2, 0.7), 4),
    }


def _nonoog_drivers(rng: random.Random) -> dict:
    """A non-OOG-revert cluster drivers block — no OOG-halt-only keys (surcharge /
    gas_remaining omitted, mirroring the emitter's populated-only rule)."""
    return {
        "state_gas_category": [
            {"key": "authorization", "count": int(rng.uniform(5, 50))},
        ],
        "cold_account": {"p50": int(rng.uniform(1, 4)), "p90": int(rng.uniform(5, 12))},
        "sload": {"p50": int(rng.uniform(4, 12)), "p90": int(rng.uniform(15, 40))},
        "sstore": {"p50": int(rng.uniform(1, 5)), "p90": int(rng.uniform(6, 18))},
        "access_list_entries": {
            "p50": int(rng.uniform(0, 2)),
            "p90": int(rng.uniform(3, 8)),
        },
        "reservoir_exhausted_share": round(rng.uniform(0.1, 0.5), 4),
        "spillover_share": round(rng.uniform(0.05, 0.4), 4),
    }


def _affected_cluster(
    rng: random.Random,
    *,
    role: str,
    kind: str,
    count: int,
    role_total: int,
    contract_total: int,
    where_contract: str | None,
    where_label: str | None,
) -> dict:
    """One failure-mode cluster shaped exactly like emit_affected_contracts."""
    sel = _hex_selector(rng)
    entry_sel = _hex_selector(rng)
    is_oog = kind == "oog"
    return {
        "role": role,
        "kind": kind,
        "selector": sel,
        "selector_signature": rng.choice(
            ["swap(address,uint256)", "transfer(address,uint256)", None]
        ),
        "entry_selector": entry_sel,
        "entry_signature": rng.choice(["execute(bytes,bytes[])", None]),
        "where_contract": where_contract,
        "where_label": where_label,
        "where_category": rng.choice(["swap_dex", "token", None]),
        "opcode": "SLOAD" if is_oog else "CALL",
        "pattern_or_reason": "storage_heavy" if is_oog else "execution_reverted",
        "oog_bottleneck_kind": "FractionalGas" if is_oog else None,
        "call_depth": int(rng.uniform(1, 6)),
        "revert_decoded": None if is_oog else rng.choice(["STF", "insufficient", None]),
        "count": count,
        "share_of_role": round(count / role_total, 4) if role_total else None,
        "share_of_contract": (
            round(count / contract_total, 4) if contract_total else None
        ),
        "gas_delta": {
            "avg": int(jitter(rng, 190000, 0.2)),
            "p50": int(jitter(rng, 170000, 0.2)),
            "p90": int(jitter(rng, 260000, 0.2)),
        },
        "drivers": _oog_drivers(rng) if is_oog else _nonoog_drivers(rng),
        "examples": _affected_examples(rng, 2),
    }


def _affected_context(
    rng: random.Random,
    *,
    addr: str,
    label: str,
    n_halt: int,
    n_revert: int,
    with_failure_rate: bool,
) -> dict:
    """The compact context strip (§1e) matching _affected_context in precompute."""
    total_tx = int(jitter(rng, 1_250_000, 0.5))
    failure_rate = (
        {
            "total_tx": total_tx,
            "halt_rate": round(n_halt / total_tx, 6) if n_halt else 0.0,
            "revert_rate": round(n_revert / total_tx, 6) if n_revert else 0.0,
        }
        if with_failure_rate
        else None
    )
    weth = ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH")
    return {
        "g3_tx_count": int(jitter(rng, 4000, 0.4)),
        "g2_drillin_tx_count": int(jitter(rng, 9000, 0.4)),
        "af_tx_count": int(jitter(rng, 60, 0.5)),
        "status_flips": int(jitter(rng, 800, 0.3)),
        "gas_delta": {
            "avg": int(jitter(rng, 190000, 0.2)),
            "sum": int(jitter(rng, 80_000_000, 0.3)),
            "p50": int(jitter(rng, 170000, 0.2)),
            "p90": int(jitter(rng, 260000, 0.2)),
        },
        "block_span_start": BLOCK_START + int(rng.uniform(0, 2000)),
        "block_span_end": BLOCK_START
        + N_DAYS * BLOCKS_PER_DAY
        - int(rng.uniform(0, 2000)),
        "distinct_blocks": int(jitter(rng, 1500, 0.3)),
        "failure_rate": failure_rate,
        "entry_functions": [
            {
                "selector": _hex_selector(rng),
                "signature": "swap(…)",
                "count": int(rng.uniform(50, 400)),
            },
            {
                "selector": _hex_selector(rng),
                "signature": None,
                "count": int(rng.uniform(10, 60)),
            },
        ],
        "failing_functions": [
            {
                "selector": _hex_selector(rng),
                "signature": "transfer(…)",
                "count": int(rng.uniform(40, 300)),
            },
        ],
        "halt_contracts": [
            {
                "contract": weth[0],
                "label": weth[1],
                "category": "token",
                "count": int(rng.uniform(20, 200)),
            },
        ],
        "revert_contracts": [
            {
                "contract": addr,
                "label": label,
                "category": None,
                "count": int(rng.uniform(10, 100)),
            },
        ],
        "entry_contracts": [
            {
                "contract": weth[0],
                "label": weth[1],
                "category": "token",
                "count": int(rng.uniform(5, 80)),
            },
        ],
    }


def _is_name_searchable(rec: dict) -> bool:
    """True iff the record is name-searchable: a real (non-bare-address) label OR
    an owner_project set. Mirrors the index.contracts inclusion rule."""
    label = rec.get("label") or ""
    addr = rec["address"]
    has_real_label = bool(label) and label.lower() != addr.lower()
    return has_real_label or bool(rec.get("owner_project"))


def build_affected_contracts(
    rng: random.Random, cfg: dict, series: dict, out_dir: Path
) -> dict:
    """Seeded SHARDED affected-contracts fixtures, matching emit_affected_contracts.

    Writes one shard file per affected contract to ``{out_dir}/affected/{addr}.json``
    (content = the per-contract record: identity header + roles_summary +
    distinct_cluster_count + clusters_shown_share + failure_clusters + context) and
    a small name-searchable ``{out_dir}/affected/index.json``. Returns the index
    dict (also written here) so the caller can inspect it.

    Synthesizes contracts spanning multiple roles (each with >= 2 distinct clusters
    carrying populated ``drivers``), exercises the role-omission rule (entry-only /
    site-only contracts), and includes one UNLABELED contract that gets a shard but
    is omitted from ``index.contracts`` (the index-filtering rule).
    """
    t = series["totals"]
    g4_total = t["g4"]
    weth = ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "WETH")

    contracts: dict[str, dict] = {}

    def _finish(
        addr: str,
        *,
        header: dict,
        n_entry: int,
        n_entry_oog: int,
        n_halt: int,
        n_revert: int,
        clusters: list[dict],
        with_failure_rate: bool,
        label: str,
        distinct_over_shown: int = 0,
    ) -> None:
        # roles_summary — OMIT a role whose count is 0 (matches the emitter).
        roles_summary: dict[str, dict] = {}
        if n_entry:
            roles_summary["entry"] = {
                "g4_tx_count": n_entry,
                "g4_oog_count": n_entry_oog,
                "g4_nonoog_count": n_entry - n_entry_oog,
            }
        if n_halt:
            roles_summary["oog_site"] = {"halt_count": n_halt}
        if n_revert:
            roles_summary["revert_site"] = {"revert_count": n_revert}

        clusters = sorted(clusters, key=lambda c: c["count"], reverse=True)
        contract_total = n_entry + n_halt + n_revert
        # distinct_cluster_count >= len(shown); simulate extra tail modes beyond
        # the shown set so clusters_shown_share < 1 when distinct_over_shown > 0.
        distinct_cluster_count = len(clusters) + distinct_over_shown
        shown_count = sum(c["count"] for c in clusters)
        clusters_shown_share = (
            round(shown_count / contract_total, 4) if contract_total else 0.0
        )
        contracts[addr] = {
            **header,
            "roles_summary": roles_summary,
            "distinct_cluster_count": distinct_cluster_count,
            "clusters_shown_share": clusters_shown_share,
            "failure_clusters": clusters,
            "context": _affected_context(
                rng,
                addr=addr,
                label=label,
                n_halt=n_halt,
                n_revert=n_revert,
                with_failure_rate=with_failure_rate,
            ),
        }

    # --- Contract A: entry + oog_site + revert_site, 3 clusters, Xatu rate. ---
    a_addr, a_label = RECIPIENTS[0]
    a_entry, a_entry_oog, a_halt, a_revert = 512, 300, 88, 41
    a_total = a_entry + a_halt + a_revert
    a_clusters = [
        _affected_cluster(
            rng,
            role="entry",
            kind="oog",
            count=210,
            role_total=a_entry,
            contract_total=a_total,
            where_contract=weth[0],
            where_label=weth[1],
        ),
        _affected_cluster(
            rng,
            role="entry",
            kind="non_oog",
            count=120,
            role_total=a_entry,
            contract_total=a_total,
            where_contract=None,
            where_label=None,
        ),
        _affected_cluster(
            rng,
            role="oog_site",
            kind="oog",
            count=88,
            role_total=a_halt,
            contract_total=a_total,
            where_contract=a_addr,
            where_label=a_label,
        ),
        _affected_cluster(
            rng,
            role="revert_site",
            kind="non_oog",
            count=41,
            role_total=a_revert,
            contract_total=a_total,
            where_contract=a_addr,
            where_label=a_label,
        ),
    ]
    _finish(
        a_addr,
        header={
            "address": a_addr,
            "label": a_label,
            "category": "stablecoin",
            "owner_project": "tether",
            "source": "manual",
            "confidence": "high",
            "is_proxy": None,
            "is_factory": None,
            "is_safe": None,
            "erc_type": None,
            "is_upgradable": None,
            "upgrade_mechanism": None,
            "upgrade_admin": None,
            "is_mev_bot": False,
            "mev_role": None,
        },
        n_entry=a_entry,
        n_entry_oog=a_entry_oog,
        n_halt=a_halt,
        n_revert=a_revert,
        clusters=a_clusters,
        with_failure_rate=True,
        label=a_label,
        distinct_over_shown=19,  # 23 distinct modes, top-4 shown -> share < 1
    )

    # --- Contract B: ENTRY-ONLY (exercises the role-omission rule: no oog_site /
    # revert_site keys), 2 clusters, upgradable proxy header, has Xatu rate. ---
    b_addr, b_label = RECIPIENTS[3]
    b_entry, b_entry_oog = 140, 90
    b_clusters = [
        _affected_cluster(
            rng,
            role="entry",
            kind="oog",
            count=90,
            role_total=b_entry,
            contract_total=b_entry,
            where_contract=weth[0],
            where_label=weth[1],
        ),
        _affected_cluster(
            rng,
            role="entry",
            kind="non_oog",
            count=50,
            role_total=b_entry,
            contract_total=b_entry,
            where_contract=None,
            where_label=None,
        ),
    ]
    _finish(
        b_addr,
        header={
            "address": b_addr,
            "label": b_label,
            "category": "swap_dex",
            "owner_project": "Uniswap",
            "source": "oli",
            "confidence": "high",
            "is_proxy": True,
            "is_factory": None,
            "is_safe": None,
            "erc_type": None,
            "is_upgradable": True,
            "upgrade_mechanism": "eip1967_transparent",
            "upgrade_admin": "0x1a9c8182c09f50c8318d769245bea52c32be35bc",
            "is_mev_bot": False,
            "mev_role": None,
        },
        n_entry=b_entry,
        n_entry_oog=b_entry_oog,
        n_halt=0,
        n_revert=0,
        clusters=b_clusters,
        with_failure_rate=True,
        label=b_label,
    )  # all modes shown -> clusters_shown_share == 1.0

    # --- Contract C: SITE-ONLY (never an entry -> no "entry" role key), 2
    # clusters, NO Xatu denominator (failure_rate is null). ---
    c_addr, c_label = RECIPIENTS[8]
    c_halt, c_revert = 33, 27
    c_total = c_halt + c_revert
    c_clusters = [
        _affected_cluster(
            rng,
            role="oog_site",
            kind="oog",
            count=33,
            role_total=c_halt,
            contract_total=c_total,
            where_contract=c_addr,
            where_label=c_label,
        ),
        _affected_cluster(
            rng,
            role="revert_site",
            kind="non_oog",
            count=27,
            role_total=c_revert,
            contract_total=c_total,
            where_contract=c_addr,
            where_label=c_label,
        ),
    ]
    _finish(
        c_addr,
        header={
            "address": c_addr,
            "label": c_label,
            "category": "infra",
            "owner_project": "Uniswap",
            "source": "oli",
            "confidence": "medium",
            "is_proxy": None,
            "is_factory": None,
            "is_safe": None,
            "erc_type": None,
            "is_upgradable": None,
            "upgrade_mechanism": None,
            "upgrade_admin": None,
            "is_mev_bot": False,
            "mev_role": None,
        },
        n_entry=0,
        n_entry_oog=0,
        n_halt=c_halt,
        n_revert=c_revert,
        clusters=c_clusters,
        with_failure_rate=False,
        label=c_label,
    )

    # --- Contract D: UNLABELED (bare-address label, no owner_project) — gets a
    # shard file but is OMITTED from index.contracts (index-filtering rule). ---
    d_addr = "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40))
    d_entry, d_entry_oog = 60, 35
    d_clusters = [
        _affected_cluster(
            rng,
            role="entry",
            kind="oog",
            count=35,
            role_total=d_entry,
            contract_total=d_entry,
            where_contract=weth[0],
            where_label=weth[1],
        ),
        _affected_cluster(
            rng,
            role="entry",
            kind="non_oog",
            count=25,
            role_total=d_entry,
            contract_total=d_entry,
            where_contract=None,
            where_label=None,
        ),
    ]
    _finish(
        d_addr,
        header={
            "address": d_addr,
            "label": d_addr,  # bare-address fallback => not name-searchable
            "category": None,
            "owner_project": None,
            "source": None,
            "confidence": "low",
            "is_proxy": None,
            "is_factory": None,
            "is_safe": None,
            "erc_type": None,
            "is_upgradable": None,
            "upgrade_mechanism": None,
            "upgrade_admin": None,
            "is_mev_bot": False,
            "mev_role": None,
        },
        n_entry=d_entry,
        n_entry_oog=d_entry_oog,
        n_halt=0,
        n_revert=0,
        clusters=d_clusters,
        with_failure_rate=False,
        label=d_addr,
    )

    block_range = {
        "start": series["buckets"][0]["block_start"],
        "end": series["buckets"][-1]["block_end"],
    }
    note = (
        "Affected = appears in any Potentially-broken (G4) tx as "
        "entry/halt/revert site. Clusters are distinct failure modes ranked "
        "by tx count; drivers are the repriced state line items behind each. "
        "Failure rates cover only contracts with a Xatu denominator."
    )

    # Write one shard file per affected contract (labeled or not).
    affected_dir = out_dir / "affected"
    affected_dir.mkdir(parents=True, exist_ok=True)
    for addr, rec in contracts.items():
        (affected_dir / f"{addr}.json").write_text(json.dumps(rec, indent=2) + "\n")

    # --- deploy_oog collapsed class ------------------------------------------
    # Freshly-deployed contract accounts (mostly ERC-4337 wallets) that OOG'd
    # during their own construction under eip-8037. A huge long-tail class that
    # is collapsed OUT of individual shards into this one aggregate file — these
    # account addresses deliberately DO NOT get {addr}.json shards.
    entry_point = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
    deploy_accounts = {
        "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40)): spec
        for spec in (
            {
                "block": block_range["start"] + 1234,
                "gas_delta": -300306,
                "opcode": "RETURN",
                "selector": "0x6100abcd",
            },
            {
                "block": block_range["start"] + 45678,
                "gas_delta": -500912,
                "opcode": "RETURN",
                "selector": "0x6100beef",
            },
            {
                "block": block_range["end"] - 9876,
                "gas_delta": -921634,
                "opcode": "SSTORE",
                "selector": "0x6080cafe",
            },
        )
    }
    accounts = {
        addr: {
            "tx": "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(64)),
            "block": spec["block"],
            "gas_delta": spec["gas_delta"],
            "opcode": spec["opcode"],
            "selector": spec["selector"],
            "entry": entry_point,
        }
        for addr, spec in deploy_accounts.items()
    }
    deploy_oog = {
        "schedule": cfg["_name"],
        "class": "deploy_oog",
        "block_range": block_range,
        "g4_total": g4_total,
        "count": len(accounts),
        "explainer": (
            "Freshly-deployed contract accounts (mostly ERC-4337 smart-account "
            "wallets) that run out of gas during their own construction under "
            "the state-creation repricing."
        ),
        "aggregate": {
            "halt_opcode_split": [
                {"key": "RETURN", "count": 2},
                {"key": "SSTORE", "count": 1},
            ],
            "initcode_families": [
                {"key": "0x6100", "count": 2},
                {"key": "0x6080", "count": 1},
            ],
            "top_entry_contracts": [
                {
                    "contract": entry_point,
                    "label": "ERC-4337 EntryPoint v0.6",
                    "category": "account_abstraction",
                    "count": 3,
                }
            ],
            "revert_reasons": [{"key": "custom:0x220266b6", "count": 3}],
            "gas_delta": {
                "p50": -300000,
                "p90": -500000,
                "min": -921634,
                "max": -100000,
            },
            "drivers": {
                "cold_account": {"p50": 3, "p90": 3},
                "sload": {"p50": 1, "p90": 1},
                "sstore": {"p50": 1, "p90": 2},
                "gas_remaining_at_oog": {"p50": 78472, "p90": 120000},
                "reservoir_exhausted_share": 0.0,
                "spillover_share": 0.0,
            },
        },
        "accounts": accounts,
    }
    (affected_dir / "deploy_oog.json").write_text(
        json.dumps(deploy_oog, indent=2) + "\n"
    )

    # index.json — NAME-SEARCHABLE contracts only, sorted by footprint desc.
    def _footprint(rec: dict) -> int:
        rs = rec["roles_summary"]
        return (
            rs.get("entry", {}).get("g4_tx_count", 0)
            + rs.get("oog_site", {}).get("halt_count", 0)
            + rs.get("revert_site", {}).get("revert_count", 0)
        )

    index_contracts = []
    for addr, rec in sorted(
        contracts.items(), key=lambda kv: _footprint(kv[1]), reverse=True
    ):
        if not _is_name_searchable(rec):
            continue
        rs = rec["roles_summary"]
        entry = {"address": addr, "label": rec["label"]}
        if rec.get("owner_project"):
            entry["owner_project"] = rec["owner_project"]
        if rec.get("category"):
            entry["category"] = rec["category"]
        if "entry" in rs:
            entry["entry_g4_tx_count"] = rs["entry"]["g4_tx_count"]
        if "oog_site" in rs:
            entry["halt_count"] = rs["oog_site"]["halt_count"]
        if "revert_site" in rs:
            entry["revert_count"] = rs["revert_site"]["revert_count"]
        index_contracts.append(entry)

    index = {
        "schedule": cfg["_name"],
        "block_range": block_range,
        "g4_total": g4_total,
        # affected_count = shard files (labeled + unlabeled) PLUS the collapsed
        # deploy-OOG accounts; the latter have no {addr}.json shards of their own.
        "affected_count": len(contracts) + len(accounts),
        "note": note,
        "contracts": index_contracts,
        "deploy_oog": {"count": len(accounts), "file": "deploy_oog.json"},
    }
    (affected_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index


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
        # affected-contracts is SHARDED: the builder writes affected/index.json +
        # one affected/{addr}.json shard per contract directly under out_dir.
        affected_index = build_affected_contracts(rng, cfg, series, out_dir)
        n_shards = affected_index["affected_count"]
        print(
            f"wrote {len(files)} files + affected/ ({n_shards} shards + index) "
            f"for {name} -> {out_dir}"
        )


if __name__ == "__main__":
    main()
