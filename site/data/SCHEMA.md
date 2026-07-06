# Published static JSON contract — `site/data/{schedule}/*.json`

This is the **authoritative contract** between the precompute step
(`scripts/precompute.py`, built by the data-pipeline agent) and the static
frontend (`site/*.html` + `site/assets/app.js`). The frontend renders against
exactly these keys/types. The committed fixtures in
`site/data/eip-8038/` and `site/data/eip-8037/` are valid instances of this
contract; wiring real data later is a **drop-in replacement** of these files.

A mirror of this contract lives in the header comment of `site/assets/app.js`.
Keep both in sync.

## Conventions

- One directory per schedule: `site/data/eip-8038/` and `site/data/eip-8037/`.
- Seven files per schedule: `meta`, `overview_series`, `gas_delta_hist`,
  `group_categories`, `oog_forensics`, `contract_failures`, `examples` (all `.json`).
- Pages select a schedule via query param: `overview.html?schedule=eip-8038`
  and fetch `data/{schedule}/{file}.json` (relative to the page).
- All counts are **integers** (already deduped for ReplacingMergeTree — see plan).
- Gas deltas are integers in gas units. **G2 is magnitude-only** (`abs(gas_delta)`);
  **G3/G4 are signed** (negative = schedule cheaper). The `signed` flag states which.
- The groups: `g1` No change · `g2` Succeeds with changes · `g3` Fixable with
  gas-limit increase · `g4` Potentially broken · `af` Already failing ·
  `g5` Unknown. The three "changed" groups (`g2`/`g3`/`g4`) require a **working
  baseline** (`baseline_success = true`); every retained drill-in whose baseline
  replay already failed is `af` Already failing, so `g4` Potentially broken
  **excludes** transactions that failed in the baseline.
- `flavour` is `"opcode"` for eip-8038 (state access/write repricing — storage
  write 2,800→10,000, cold access →3,000, account write →8,000; non-uniform) and
  `"state"` for eip-8037 (per-tx state reservoir). Reservoir blocks are only
  present when `flavour == "state"`. (The `"opcode"` tag is a legacy key name, not
  a claim that eip-8038 reprices opcode execution gas.)

---

## 1. `meta.json`

Pinned config, block/date range, totals, truncation stats.

```jsonc
{
  "schedule": "eip-8038",
  "schedules_available": ["eip-8038", "eip-8037"],
  "analysis_config_hash": "0xa1b2…f90",        // string
  "chain_id": 1,                                // int
  "generated_at": "2026-06-30T12:00:00Z",       // ISO-8601 string
  "block_range": { "start": 21500000, "end": 22572449, "count": 1072450 },
  "date_range": { "start": "2026-01-01", "end": "2026-05-30" },
  "totals": {                                   // all int
    "tx_count": 172710049,
    "g1": 168425768, "g2": 3570791, "g3": 359646, "g4": 157185,
    "af": 74321, "g5": 196659,
    "rescues": 16838
  },
  "group_labels": { "g1": "No change", "g2": "Succeeds with changes",
    "g3": "Fixable with gas-limit increase", "g4": "Potentially broken",
    "af": "Already failing", "g5": "Unknown" },
  "truncation": {
    "drill_ins_truncated_blocks": 19340,        // int
    "total_blocks": 1072450,                    // int
    "truncated_share": 0.018,                   // float 0..1
    "note": "…why Unknown is a coverage gap…"     // string
  },
  "manifest": {
    "source": "ClickHouse gas_analysis warehouse (per-block/per-tx replays)",
    "schedule_name": "eip-8038",
    "config_selected_by": "most blocks + most recent updated_at covering both schedules"
  },
  "pinned_config_note": "…"                      // string
}
```

---

## 2. `overview_series.json`

Per-day-bucketed (≈150 buckets) group composition + totals + rescues. Never the
raw ~1M points. Fall back to `bucket_by: "block"` if timestamps are sparse.

```jsonc
{
  "schedule": "eip-8038",
  "bucket_by": "day",                           // "day" | "block"
  "buckets": [
    {
      "date": "2026-01-01",                      // "YYYY-MM-DD" (or "" if block-bucketed)
      "block_start": 21500000, "block_end": 21507243,
      "tx_count": 1104116,
      "g1": 1073306, "g2": 21032, "g3": 1906, "g4": 1187, "af": 500, "g5": 6685,
      "rescues": 129,
      "drill_ins_truncated_blocks": 152
    }
    // … ~150 buckets
  ],
  "totals": {                                    // sum over buckets, all int
    "tx_count": 172710049,
    "g1": …, "g2": …, "g3": …, "g4": …, "af": …, "g5": …,
    "rescues": 16838, "drill_ins_truncated_blocks": 19340
  },
  "group_labels": { "g1": "…", …, "af": "…", "g5": "…" }
}
```

The frontend computes percent mode client-side as `100 * g_k / tx_count` per
bucket — emit raw counts, not percentages.

---

## 3. `gas_delta_hist.json`

G2: real gas-unit histogram of **txs with a gas change**. G3/G4: signed log2
per-tx histogram + percentiles + sum/min/max.

- **G2** (`signed: false`): covers `gas_delta != 0` only — the `gas_only` aggregate
  cohort (from `block_summary.gas_delta_log2_hist`) **+** the Succeeds-with-changes
  drill-in members whose gas changed, binned by the **producer `log2_bin`** so both
  cohorts land in the same buckets. Emitted as **`gas_bins`** (real gas units): each
  entry is `{ lo, hi, count_gas_only, count_drillin, count }`; `hi` is **exclusive**,
  and `hi: null` marks the `≥1024`-gas **catch-all** (the aggregate cohort has no
  finer resolution above 1024 gas). Bins cover the producer indices 1..11 — the
  exact-zero bin 0 is omitted (not a gas change).
- **G3/G4** (`signed: true`): signed exact per-tx log2 magnitude bins — emit both
  positive and negative bins (`sign: -1` for schedule-cheaper). `bin_log2` is the
  integer exponent; the frontend plots `sign * bin_log2` on the x axis. They also
  carry a **`pct_bins`** percentage histogram of per-tx
  `100 * gas_delta / baseline_gas_used` (share of baseline gas used) over the fixed
  signed edges `[-100,-50,-25,-10,-1,0,1,10,25,50,100,200,500]` (see
  `docs/producer-data-recommendations.md`): each entry is `{ lo, hi, count }` with
  `hi` **exclusive** and `hi: null` the `≥500%` **catch-all**. `pct_covered_count`
  is the tx count with a usable (`> 0`) baseline denominator; rows without one are
  excluded from `pct_bins`. This percentage view is computable **only** for the
  G3/G4 drill-in cohorts (per-tx `baseline_gas_used`); the G2 `gas_only` cohort
  cannot form a per-tx ratio, so it has **no** `pct_bins` and stays absolute.

```jsonc
{
  "schedule": "eip-8038",
  "groups": {
    "2": {
      "label": "Succeeds with changes",
      "signed": false,
      "note": "Txs with a gas change (gas_delta != 0)… >=1024-gas bin is a catch-all…",
      "count": 3570791,
      "gas_bins": [                                   // producer bins 1..11, hi exclusive
        { "lo": 1,    "hi": 2,    "count_gas_only": 12, "count_drillin": 3, "count": 15 },
        { "lo": 2,    "hi": 4,    "count_gas_only": …,  "count_drillin": …, "count": … },
        // …
        { "lo": 512,  "hi": 1024, "count_gas_only": …,  "count_drillin": …, "count": … },
        { "lo": 1024, "hi": null, "count_gas_only": …,  "count_drillin": …, "count": … } // ≥1024 catch-all
      ],
      "sum_gas_delta": 78557402000, "min_gas_delta": 12, "max_gas_delta": 4500000
    },
    "3": { "label": "…", "signed": true, "count": …,
           "bins": [ { "bin_log2": 16, "sign": 1, "count": … },
                     { "bin_log2": 16, "sign": -1, "count": … } ],
           "percentiles": { "p01": …, …, "p99": … },   // all int
           "sum_gas_delta": …, "min_gas_delta": …, "max_gas_delta": …,
           "pct_bins": [ { "lo": -100, "hi": -50, "count": … },  // hi excl
                         // … [-1,0) [0,1) [1,10) … [200,500)
                         { "lo": 500,  "hi": null, "count": … } ], // ≥500% catch-all
           "pct_covered_count": …, "pct_note": "…" },
    "4": { … "signed": true, "pct_bins": [ … ], "pct_covered_count": …, "pct_note": "…" … }
  }
}
```

Keys `"2" "3" "4"` are **strings** (JSON object keys).

---

## 4. `group_categories.json`

Per-group categorization counts. `flavour` switches which drivers dominate; the
`reservoir` sub-objects appear **only when `flavour == "state"`** (eip-8037).

All `*_mix` / pattern / category / break-reason lists are arrays of
`{ "key": string, "count": int }`. Leaderboards add fields as noted.

```jsonc
{
  "schedule": "eip-8038",
  "flavour": "opcode",                           // "opcode" (8038) | "state" (8037)
  "g2": {
    "label": "Succeeds with changes",
    "count": 3570791,                              // full group (gas_only + drill-in)
    "gas_only_count": 3512044,                     // aggregate cohort (no per-tx rows)
    "drillin_count": 58747,                        // per-tx drill-in members
    "state_driver_mix": [ { "key": "no_state", "count": … }, … ],   // gas_only cohort native driver counts: no_state|runtime_state|creation|authorization
    "state_driver_mix_drillin": [ { "key": "authorization", "count": … }, … ], // drill-in subset: state_gas_category (access_list|authorization|contract_creation|transfer_new_account|none)
    "change_type_mix": [ { "key": "gas_changed", "count": … }, … ], // OVERLAPPING: gas_changed|event_logs_changed|output_changed|logs_bloom_changed|trace_only
    "change_type_note": "Change types are non-exclusive…"
  },
  "g3": {
    "label": "Fixable with gas-limit increase",
    "count": 359646,
    "multiplier_histogram": [ { "multiplier": 2, "count": … }, { "multiplier": 4, … }, { "multiplier": 6, … }, { "multiplier": 8, … }, { "multiplier": 10, … } ], // measured min_mult (= schedule_gas_used/tx_gas_limit) binned over the real (1,10] sweep: (1,2](2,4](4,6](6,8](8,10]; top bin open-ended so bins total the whole G3 cohort
    "state_gas_category": [ { "key": "execution_bound", "count": … }, … ], // execution_bound|state_bound|mixed|unknown
    "tx_shape_mix": [ { "key": "simple_transfer", "count": … }, … ],  // simple_transfer|contract_call|contract_creation|authorization (mutually exclusive, zero-filled)
    "tx_type_mix": [ { "key": "legacy", "count": … }, … ],            // EIP-2718: legacy|access_list|dynamic_fee|blob|set_code|unknown (zero-filled)
    "is_create": { "true": 21578, "false": 338068 },
    "oog_pattern": [ { "key": "call_oog", "count": … }, … ],         // call_oog|top_level_oog|none
    "reservoir": {                               // 8037 only
      "reservoir_exhausted": { "true": …, "false": … },
      "avg_runtime_state_gas_spillover": 88000
    }
  },
  "g4": {
    "label": "Potentially broken",
    "count": 157185,
    "fixability": [ { "key": "not_gas_fixable", "count": … }, … ], // AUTHORITATIVE gas-fixability split, keyed on replay_halt_oog (10x-ceiling halt): not_gas_fixable (non-gas halt at 10x, ~99.98%) | still_oog_at_ceiling (needs >10x/loop) | unknown (~0)
    "break_reason": [ { "key": "oog", "count": … }, … ],            // ORIGINAL-limit halt site (not fixability): oog|non_oog_revert|other
    "oog_bottleneck_kind": [ { "key": "call_depth", "count": … }, … ], // call_depth|single_frame|unknown
    "status_flip": [ { "key": "success_to_fail", "count": … }, … ], // success_to_fail|fail_to_fail|fail_to_success
    "state_gas_category": [ { "key": "execution_bound", "count": … }, … ],
    "tx_shape_mix": [ { "key": "simple_transfer", "count": … }, … ],  // same taxonomy as g3 (zero-filled)
    "tx_type_mix": [ { "key": "legacy", "count": … }, … ],            // same taxonomy as g3 (zero-filled)
    "reservoir": {                               // 8037 only
      "reservoir_exhausted": { "true": …, "false": … },
      "avg_initial_reservoir": 380000,
      "avg_runtime_state_gas_spillover": 142000
    }
  }
}
```

---

## 4b. `oog_forensics.json`

Out-of-gas halt-site forensics for the Potentially-broken (`g4`) cohort, scoped
to rows carrying an OOG signal (`oog_pattern`/`oog_call_depth`/`replay_halt_oog`).
This is the **original-limit** halt site (WHERE the tx hit the wall), **not** a
fixability verdict — most of these rows are `not_gas_fixable` (a non-gas halt at
the 10× ceiling; see `group_categories.g4.fixability`). G3 (Fixable) failures
almost never carry halt-site forensics, so OOG halt sites are a broken-cohort
phenomenon. Powers the **Out-of-gas failures** section of the transaction-failures
page (what/where/why + an entry→halt Sankey).

All `*_pattern` / `*_kind` / `*_opcode` lists are `[{ "key": string, "count": int }]`
ordered by count desc. Percentile objects are `{ p50, p90, p99, max }`, each
`int | null` (null when no rows have a value). `oog_opcode` keys are decoded EVM
mnemonics (e.g. `SLOAD`, `CALL`, `DELEGATECALL`), so the frontend renders them
**without** `humanizeKey`.

```jsonc
{
  "schedule": "eip-8038",
  "g4_total": 1113223,                           // whole Potentially-broken cohort
  "oog_total": 248390,                           // g4 rows with an OOG signal
  "oog_share_of_g4": 0.2231,                     // float 0..1 = oog_total / g4_total
  "distinct_oog_recipients": 3182,               // distinct entry contracts (recipient) with >=1 OOG halt
  // WHY it ran out of gas
  "oog_pattern": [ { "key": "storage_heavy", "count": … }, … ], // storage_heavy|call_chain|loop|memory_expansion|unknown
  // HOW MUCH gas was left at the halt (distribution over ordered magnitude buckets)
  "gas_remaining_hist": [ { "bucket": "0", "count": … }, { "bucket": "1–1K", "count": … }, …, { "bucket": "1M+", "count": … } ], // buckets: 0 | 1–1K | 1K–10K | 10K–100K | 100K–1M | 1M+; totals the cohort
  // WHAT ran out — opcode executing at the halt (decoded mnemonic)
  "oog_opcode": [ { "key": "SLOAD", "count": … }, { "key": "CALL", "count": … }, … ],
  // WHERE it ran out
  "call_depth_hist": [ { "depth": "1", "count": … }, …, { "depth": "9+", "count": … } ], // bins 1..8 + "9+" overflow; totals the cohort
  "call_depth_percentiles": { "p50": 4, "p90": 7, "p99": 10, "max": 18 }, // depth of the halt frame
  "oog_contract_leaderboard": [                  // top-12 halt contracts (oog_contract) — WHERE the halt landed
    { "addr": "0x…", "label": "…", "category": "swap_dex", "count": … }, …  // category: taxonomy tag or null (§7)
  ],
  "oog_recipient_leaderboard": [                 // top-N entry contracts (recipient) — WHO was called
    { "addr": "0x…", "label": "Uniswap V4 PoolManager",
      "category": "swap_dex",        // OPTIONAL — absent when unknown (taxonomy tag, §7)
      "owner_project": "Uniswap",    // OPTIONAL — absent when unset
      "source": "oli",               // OPTIONAL — absent when unknown; label source (oli|dune|ethlists|manual|heuristic|mev|…)
      "confidence": "high",          // OPTIONAL — present iff source present (high|medium|low)
      "is_mev_bot": true,            // OPTIONAL — present only when true
      "mev_role": "arb",             // OPTIONAL — arb|sandwich|liquidation
      "is_proxy": true, "is_factory": true, "is_safe": true, // OPTIONAL — present only when true
      "erc_type": "erc20",           // OPTIONAL — structural tag
      "is_upgradable": true,         // OPTIONAL — present only when true (clones are NOT upgradable)
      "upgrade_mechanism": "eip1967_transparent", // OPTIONAL — eip1967_transparent|uups|beacon|diamond|minimal_proxy_immutable (bare "none" elided)
      "upgrade_admin": "0x…",        // OPTIONAL — EIP-1967 admin address, when the admin slot is set
      "halt_count": 41771,           // NUMERATOR — OOG halts whose entry recipient is this addr
      "total_tx": 1250000,           // DENOMINATOR — all mainnet txs to this recipient over the pinned block range; int | null (null = unavailable)
      "halt_rate": 0.033417 },       // ratio halt_count/total_tx (6 dp); null when total_tx is null or 0
    … // top-N by halt_count desc
  ],
  "oog_recipient_rate_leaderboard": [            // SAME row shape as above, ranked by halt_rate desc
    { … }, …                                     // top-N by failure rate, gated on total_tx >= 100
  ],
  // FLOW: entry contract (recipient) -> halt contract (oog_contract)
  "sankey": {
    "nodes": [ { "label": "ERC-4337 EntryPoint v0.7", "addr": "0x…", "side": "entry" },
               { "label": "Other halts", "addr": null, "side": "halt" }, … ],
    "links": [ { "source": 0, "target": 1, "value": … }, … ]  // indices into nodes
  }
}
```

The Sankey is **bipartite**: `side: "entry"` nodes (top-10 `recipient`s plus an
"Other entries" bucket) on the left, `side: "halt"` nodes (top-10 `oog_contract`s
plus "Other halts") on the right. Every OOG row is accounted for; `addr` is
`null` for the "Other" buckets. `label` falls back to the raw address when unknown.

**The two leaderboards answer different questions and are both kept.**
`oog_contract_leaderboard` is keyed on **`oog_contract`** — *where* the halt
landed, which for nested calls is a contract deep inside the call tree, not the
one the user invoked. `oog_recipient_leaderboard` is keyed on the tx
**`recipient`** — *who* was called, i.e. the entry contract at the top of the
call tree. A halt that lands in a shared library or router shows up under
`oog_contract`, while the DEX/aggregator the user actually called shows up under
`recipient`; the two rankings therefore diverge and neither subsumes the other.

Each `oog_recipient_leaderboard` row is the full resolved label record for the
recipient (via `repricing_impact.labels.classify_address`) plus a real failure
rate. The label fields follow the **"absent when unknown/default"** convention
(they are *dropped*, not emitted as `null`, when unset — see §7 for the shared
enrichment fields; `source`/`confidence`/`is_mev_bot`/`mev_role`/`is_proxy`/
`is_factory`/`is_safe`/`erc_type` extend the same optional-when-unknown rule).
`confidence` is present **iff** `source` is present. The rate is:

- **`halt_count`** (numerator) — OOG halts whose entry `recipient` is this
  address, from the same deduped `_divergence` OOG cohort as the rest of §4b.
- **`total_tx`** (denominator) — *all* mainnet txs sent to this recipient over
  the pinned block range, from a **cross-source read of the Xatu EL table**
  `default.canonical_execution_transaction` (matched on `to_address`,
  `block_number` within `RunContext.block_start..block_end` = 24,319,986 →
  25,319,985, and `meta_network_name = 'mainnet'`). The query is bounded to the
  pinned block range **and to only the top-N leaderboard addresses** — never a
  full scan. `chain_id = 1` (gas_analysis) and `meta_network_name = 'mainnet'`
  (Xatu) are the same cohort. See [`../../docs/warehouse.md`](../../docs/warehouse.md)
  for the sanctioned cross-source exception. `total_tx` is **`null`** when that
  denominator query fails or is unavailable.
- **`halt_rate`** — `halt_count / total_tx` rounded to 6 dp, or **`null`** when
  `total_tx` is `null` or `0`.

**Caveat (biases `halt_rate` low):** the `_divergence` drill-in cap
(`max_divergences_per_block = 1024`) can under-report halts for busy recipients
on truncated blocks, so `halt_count` — and therefore `halt_rate` — is a lower
bound for high-traffic entry contracts. See `meta.truncation.truncated_share`
for the truncated-block share.

**`oog_recipient_rate_leaderboard`** is the *same rows* re-ranked by `halt_rate`
descending instead of `halt_count`. It surfaces the entry contracts where a halt
is most *likely* per call, rather than most *numerous*. To keep the ranking off
tiny-denominator noise (a contract with 1 tx / 1 halt is 100% but meaningless),
a row qualifies only if `total_tx >= 100`. Both leaderboards are drawn from the
same bounded candidate pool (top-`OOG_RECIPIENT_POOL` recipients by halt count,
whose denominators are fetched in one Xatu read), so a genuinely high-rate
contract *outside* that pool by halt count is not guaranteed to appear — an
accepted limit of the never-full-scan rule; the min-volume floor makes such a
contract high-halt-count in practice anyway. The transaction-failures page shows
one table with a **Rank by** toggle between the two orderings.

---

## 4c. `nonoog_forensics.json`

Non-OOG revert forensics for the Potentially-broken (`g4`) cohort, scoped to rows
that flipped status (`status_changed`) **without** any OOG signal — the mirror of
the `oog_forensics` cohort within `g4`, and the same rows as the
`non_oog_revert` bucket in `group_categories.g4.break_reason`. Powers the
**Non-OOG reverts** section of the transaction-failures page (why/what/where +
an entry→revert Sankey).

`failure_reason` / `divergence_opcode` lists are `[{ "key": string, "count": int }]`
ordered by count desc. `divergence_opcode` keys are decoded EVM mnemonics, so the
frontend renders them **without** `humanizeKey` (and applies its own top-5+Others
collapse). `revert_error_mix` is already collapsed **server-side** to the top-5
`Error(string)` messages plus an `"Others"` bucket (the `Error(string):` prefix
is stripped; free-text messages are rendered verbatim).

```jsonc
{
  "schedule": "eip-8038",
  "g4_total": 1113223,                           // whole Potentially-broken cohort
  "nonoog_total": 864833,                        // g4 rows that reverted without OOG
  "nonoog_share_of_g4": 0.7769,                  // float 0..1 = nonoog_total / g4_total
  "distinct_nonoog_recipients": 2517,            // distinct entry contracts (recipient) with >=1 non-OOG revert
  // WHY it reverted
  "failure_reason": [ { "key": "…", "count": … }, … ],       // producer failure_reason enum
  "revert_error_mix": [ { "key": "STF", "count": … }, …, { "key": "Others", "count": … } ], // top-5 Error(string) msgs + Others
  // WHAT diverged — opcode at the first-divergence frame (decoded mnemonic)
  "divergence_opcode": [ { "key": "JUMPDEST", "count": … }, { "key": "EXTCODESIZE", "count": … }, … ],
  // WHERE it reverted
  "call_depth_hist": [ { "depth": "1", "count": … }, …, { "depth": "9+", "count": … } ], // bins 1..8 + "9+" overflow; totals the cohort
  "call_depth_percentiles": { "p50": 6, "p90": 10, "p99": 11, "max": 12 }, // divergence_call_depth; p50 = the "Median call depth" card
  // WHERE (entry): top-N entry contracts (recipient) — WHO was called
  "nonoog_recipient_leaderboard": [             // ranked by revert_count desc; row shape below
    {
      "addr": "0x…", "label": "…", "category": "…", "source": "…", // resolved label record (absent when unknown)
      "revert_count": 41771,          // NUMERATOR — non-OOG reverts whose entry recipient is this addr
      "total_tx": 1250000,            // DENOMINATOR — all mainnet txs to this recipient over the pinned range; int | null
      "revert_rate": 0.033417         // float | null = revert_count / total_tx
    }, …
  ],
  "nonoog_recipient_rate_leaderboard": [        // SAME row shape, ranked by revert_rate desc (total_tx >= 100 gate)
    { "addr": "0x…", "revert_count": …, "total_tx": …, "revert_rate": … }, …
  ],
  // FLOW: entry contract (recipient) -> revert contract (divergence_contract)
  "sankey": {
    "nodes": [ { "label": "…", "addr": "0x…", "side": "entry" },
               { "label": "Other reverts", "addr": null, "side": "revert" }, … ],
    "links": [ { "source": 0, "target": 1, "value": … }, … ]  // indices into nodes
  }
}
```

The Sankey is **bipartite**: `side: "entry"` nodes (top-10 `recipient`s plus an
"Other entries" bucket) on the left, `side: "revert"` nodes (top-10
`divergence_contract`s plus "Other reverts") on the right. Same builder as the OOG
Sankey (`_entry_flow_sankey`), just targeting `divergence_contract`.

`distinct_nonoog_recipients` and the two `nonoog_recipient_*_leaderboard`s mirror
the OOG side (§4b) exactly — same bounded candidate pool, same rich label record,
same bounded Xatu total-tx denominator, and the same `total_tx >= 100` floor on
the rate ranking — only keyed on the non-OOG-revert cohort and named
`revert_count` / `revert_rate` in place of `halt_count` / `halt_rate`.

---

## 5. `contract_failures.json`

Top-N failing `recipient`s, ranked by Potentially-broken (`g4`) tx count, with
cumulative share for the Pareto/concentration chart. Ratios are over the
**drill-in cohort only** (see `note`); the No-change / Succeeds-with-changes
aggregate cohorts have no recipient. `g4` here already excludes baseline
failures (those are Already failing).

```jsonc
{
  "schedule": "eip-8038",
  "g4_total": 157185,
  "g3_total": 359646,
  "note": "Per-recipient ratios are over the drill-in cohort (G3/G4 + G2 drill-in members) only…",
  "contracts": [                                 // sorted by g4_tx_count desc
    {
      "recipient": "0xdac17f9…ec7",              // lowercase hex string
      "label": "Tether USDT",                    // human label or raw addr fallback
      "category": "stablecoin",                  // taxonomy tag or null (§7)
      "owner_project": "tether",                 // project/entity or null (§7)
      "g4_tx_count": 51654, "g3_tx_count": 132373, "g2_drillin_tx_count": 280464,
      "status_flips": 37716,
      "avg_gas_delta": 170352, "sum_gas_delta": 10532175813,
      "min_mult_percentiles": { "p50": 2, "p90": null, "p99": null }, // int | null
      "block_span_start": 21500136, "block_span_end": 22572437,
      "distinct_blocks_with_g4": 27104,
      "avg_g4_per_block": 1.906,                  // float = g4_tx_count / distinct_blocks_with_g4
      "g4_vs_other_ratio": 0.1112,               // float = g4 / (g2_drillin + g3 + g4)
      "cumulative_share": 0.3293,                // float, running share of total G4
      "divergence_contract_mix": [ { "contract": "0x…", "label": "…", "category": "swap_dex", "count": … }, … ], // category: tag or null (§7)
      "oog_contract_mix": [ { "contract": "0x…", "label": "…", "category": "swap_dex", "count": … }, … ]
    }
    // … top-N
  ]
}
```

---

## 6. `examples.json`

A small `LIMIT`ed list of representative Potentially-broken (`g4`) and Fixable
(`g3`) example txs.

```jsonc
{
  "schedule": "eip-8038",
  "capped_at": 40,                               // int
  "examples": [
    {
      "tx_hash": "0x…",                           // 0x + 64 hex
      "block_number": 21554540,
      "group": 4,                                 // 3 | 4
      "recipient": "0x…", "recipient_label": "Tether USDT",
      "recipient_category": "stablecoin",         // taxonomy tag or null (§7)
      "gas_delta": 447498,                        // signed int
      "min_multiplier_to_succeed": null,          // int | null (null ⟺ Potentially broken/G4; any value >1 ⟺ Fixable/G3)
      "baseline_success": true, "schedule_success": false,
      "oog_pattern": null,                        // string | null
      "divergence_opcode": "CALL",
      "divergence_contract": "0x…",
      "state_gas_category": "mixed"
    }
    // … up to capped_at
  ]
}
```

---

## 7. Contract label enrichment (`category` / `owner_project`)

The address-bearing records above carry two **additive** enrichment fields
alongside the existing `label` (contract/selector labeling expansion,
`docs/labeling-expansion-plan.md`):

- **`category`** — a taxonomy tag from the fixed enum: `precompile`,
  `stablecoin`, `token`, `swap_dex`, `defi_complex`, `bridge`,
  `account_abstraction`, `wallet_safe`, `mev_bot`, `nft`, `oracle`, `infra`,
  `cex`, `other`. **`null`** when the address is unclassified.
- **`owner_project`** — the owning project/entity string (leaderboard entries
  only), or `null`.

These are populated by `repricing_impact.labels.classify_address`, which reads a
build-time merged label cache (`label_cache/contract_labels.parquet`, produced by
`python -m repricing_impact.label_sources.build`). **When no cache is present the
fields are uniformly `null`** and `label` keeps its exact pre-enrichment
behaviour — so the frontend must treat both as optional/nullable. The values are
a superset over time (new sources add coverage) but the `category` enum is
closed; the frontend humanizes the tag for display and must tolerate `null`.

Fields carrying `category`: `oog_contract_leaderboard[]` and
`oog_recipient_leaderboard[]` / `nonoog_recipient_leaderboard[]` and the
`divergence_contract_leaderboard`/top lists (§4b/4c), `divergence_contract_mix[]`
/ `oog_contract_mix[]` and the recipient entry (§5), and `recipient_category`
(§6). Note `oog_recipient_leaderboard[]` follows the "absent when unknown"
convention — `category` is *omitted* there rather than emitted as `null` (§4b) —
and carries the wider label record (`owner_project`/`source`/`confidence`/
`is_mev_bot`/`mev_role`/`is_proxy`/`is_factory`/`is_safe`/`erc_type`/
`is_upgradable`/`upgrade_mechanism`/`upgrade_admin`), all optional under the same
rule. **Upgradability is narrower than proxy-ness:** an EIP-1167 minimal-proxy
clone has `is_proxy: true` but **not** `is_upgradable` (it forwards to a
bytecode-baked implementation that can never change) — it surfaces only as
`upgrade_mechanism: "minimal_proxy_immutable"`. `is_upgradable` is emitted only
when `true`; `upgrade_mechanism` carries the full verdict (a bare `none` is
elided); `upgrade_admin` is the EIP-1967 admin address when the admin slot is
set. These come from the on-chain heuristics source (`source="heuristic"`,
`confidence="low"`) — see `repricing_impact.label_sources.heuristics`. Selector
decoding (4-byte → signature) is **not yet emitted** — it is gated on calldata
availability in the warehouse (plan §6/§10).

## Regenerating fixtures

`python scripts/make_fixtures.py` deterministically regenerates 12 seeded
fixture files into `fixtures_scratch/` (NOT `site/data/`, which holds real
precompute output). Set `$FIXTURE_OUT_DIR` to redirect. Use only for offline
frontend dev when the real JSON is unavailable.

## Note on percentile approximation

`gas_delta_hist` percentiles are **log2 bin-midpoint approximations**, not exact
order statistics. Each group object carries a `note` field describing its source
(magnitude-only for G2, signed per-tx for G3/G4); the overview page surfaces
these notes beside the percentile table.

## Note on categorical enum values

The `*_mix` / `state_gas_category` / `oog_pattern` / `oog_bottleneck_kind` /
`break_reason` / `status_flip` lists carry **whatever enum values the warehouse
emits** (e.g. `access_list`, `contract_creation`, `authorization`, `none`,
`storage_heavy`, `call_chain`, `FixedGas`, `FractionalGas`, `Stipend2300`). The
frontend never hardcodes the value set — it renders every key that arrives and
humanizes the string for display (`humanizeKey` in app.js). The shape is always
`[{ "key": string, "count": int }]`.
