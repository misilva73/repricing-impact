# Warehouse reference — ClickHouse `gas_analysis`

Reference for the `gas_analysis` warehouse this repo reads. For the mandatory
query rules and project conventions see [`../AGENTS.md`](../AGENTS.md); this doc
is the deeper detail (connection, tables, column semantics) to consult when
writing queries.

## How to connect

Credentials live in `secrets.json` at the repo root (gitignored), keys
`xatu_username` / `xatu_password`. ClickHouse over HTTPS via
`clickhouse-sqlalchemy` — use the shared engine
(`repricing_impact.clickhouse.get_engine` / `run_query`) rather than rebuilding
it. The underlying connection:

```python
import json
from sqlalchemy import create_engine

with open("secrets.json") as f:
    s = json.load(f)
engine = create_engine(
    f"clickhouse+http://{s['xatu_username']}:{s['xatu_password']}"
    "@clickhouse.xatu.ethpandaops.io:443/default?protocol=https"
)
```

**Two hard rules (also in AGENTS.md, restated here):**

- **`gas_analysis` is a ClickHouse _database_, not a table.** Query the four
  *distributed* tables (`gas_analysis.gas_analysis_run`, `_block_coverage`,
  `_block_summary`, `_divergence`). **Ignore the `_local` tables** — per-shard
  backing tables for the distributed engine.
- **`force_primary_key` is enforced.** Every query MUST filter the leading PK
  column **`chain_id`** (`WHERE chain_id = 1`) or it is rejected (`Code 277 …
  INDEX_NOT_USED`). `count()` and `SHOW TABLES` are exempt. Also filtering
  `analysis_config_hash` / `schedule_name` / `block_number` keeps scans cheap
  (full PK: `chain_id, analysis_config_hash, schedule_name, block_number,
  block_hash, …`).

## Where the data comes from

Produced by the **`reth-research` ExEx** in the
[CarlBeek/reth](https://github.com/CarlBeek/reth) fork (`crates/research`,
`bin/reth-research`). For each canonical mainnet block it:

1. loads historical state at `block - 1`,
2. executes each tx once under **baseline** (canonical) gas pricing,
3. re-executes the same tx once per candidate **gas schedule**,
4. classifies each `(tx, schedule)` and writes coverage + per-class summaries +
   per-tx drill-in divergence rows.

`baseline_*` columns = canonical replay; `schedule_*` = candidate replay;
`gas_delta = schedule_gas_used - baseline_gas_used`. Schedules run isolated (each
gets its own per-block state, so a schedule-induced failure can cascade to later
txs in the same block), but state **resets to canonical parent at each new
block** — it is *not* a cross-block forked chain. It replays already-included
canonical txs only: **no mempool admission, replacement, builder behavior, or
re-packing is modeled.** Treat outputs as "what would this repricing have done to
historical traffic", not a full protocol sim.

### Authoritative source files (for column meanings — don't guess)

Every ClickHouse column carries a `COMMENT`; `DESCRIBE TABLE gas_analysis.<t>`
returns the producer's own per-column docs. Deeper ground truth in the fork:

- `bin/reth-research/clickhouse/migrations/…` — the schema migrations with
  per-column `COMMENT`s. `002_gas_analysis_v10.up.sql` is the **v10** schema; the
  warehouse now serves **producer schema v11** (`producer_git_commit =
  e91fe9d368f485fd84cd6e3ec3c58c9124af6d7e`, verified live 2026-07-29) but the
  **v11 migration filename is unconfirmed from this repo** — TBC, needs
  producer-side confirmation. Do not cite a guessed path; `DESCRIBE TABLE` is the
  reliable in-warehouse source for v11 column comments.
- `crates/research/src/export/model.rs` — export row structs; e.g. the `opcode_*`
  parallel-array construction (~L767).
- `crates/research/src/divergence.rs` — `AggregateClass` enum + `DivergenceFacts`
  (the `class` / "store full forensics" logic, ~L603).
- `crates/research/src/opcode.rs` — canonical opcode-byte constants.
- `crates/research/README.md` + `crates/research/docs/storage-redesign.md`.

## The four tables

| Table | Grain | ~Rows | Use for |
| --- | --- | --- | --- |
| `gas_analysis_run` | one row per analysis run/config | tiny | resolve `analysis_config_hash` → `manifest_json` (schedules, gas tiers, drill-in cap, commit) |
| `gas_analysis_block_coverage` | one row per (config, schedule, block) | ~2M | cohort tx-counts per block; drill-in completeness; block gas used/limit |
| `gas_analysis_block_summary` | one row per (config, schedule, block, **class**) | ~4M | aggregated **gas impact** for the cheap cohorts — gas-delta stats, per-opcode totals, state-gas, plus the v11 additions: EIP-2718 tx-type counts, tx-shape counts, the gas-delta **percentage** histogram, and the class-grain baseline-gas denominator (see [v11 columns](#v11-block_summary-columns-producer-schema-11)) |
| `gas_analysis_divergence` | one row per (config, schedule, block, tx) drill-in | ~105M | per-tx **failure forensics** (status flips, OOG, recoverability, traces) |

Row counts measured 2026-07-30 against the pinned v11 config: `_run` **1**,
`_block_coverage` **2,000,000** (2 schedules × 1M blocks), `_block_summary`
**3,996,376**, `_divergence` **104,816,105**. They dropped by roughly a third
versus the v10 figures previously recorded here (~3M / ~6M / ~113M) purely
because the v11 run carries **two** schedules where v10 carried three —
`7904-prelim` is gone. Re-measure after any producer bump.

## Sanctioned cross-source read — Xatu `canonical_execution_transaction`

The default rule is **`gas_analysis`-only** (see AGENTS.md). There is **one
documented exception**, added for the OOG-halt failure-rate denominators in
`oog_forensics.json`'s `oog_recipient_leaderboard`:

- **What:** a single read of the Xatu EL table
  `default.canonical_execution_transaction` (same ClickHouse host,
  `default` database), selecting only `to_address`, `block_number` and filtering
  `meta_network_name = 'mainnet'`.
- **Why:** `gas_analysis` has no per-recipient _total_ mainnet tx count — it only
  carries replayed drill-in rows — so there is no in-warehouse way to form the
  `halt_rate = halt_count / total_tx` denominator. Xatu's canonical EL table has
  every mainnet tx. `chain_id = 1` (gas_analysis) and
  `meta_network_name = 'mainnet'` (Xatu) are the **same cohort**.
- **Bounds:** the read is bounded to the pinned block range
  (`RunContext.block_start..block_end`, 24,319,986 → 25,319,985) **and** to only
  the top-N leaderboard `to_address`es — it is **never a full scan**.
- **Discipline:** **read-only**, aggregate-only (a `GROUP BY to_address`
  count), and — like the divergence scans — run **off any request path**, as part
  of the same off-peak precompute batch. It writes nothing back to Xatu.

If the denominator query fails or is unavailable, `total_tx` (and hence
`halt_rate`) is emitted as `null`; see [`../site/data/SCHEMA.md`](../site/data/SCHEMA.md) §4b.

## Key semantics (the non-obvious stuff)

- **`class` has exactly two values** (`AggregateClass` in `divergence.rs`):
  `unchanged` (`gas_delta == 0`, no trace change) and `gas_only` (`gas_delta !=
  0`, no other change — "the silent-majority repricing tax"). A tx gets a full
  row in `_divergence` (and is counted as `tx_count_stored` in coverage)
  **iff** `!schedule_success || !baseline_success || trace_diverged()`. ⚠️ The
  schema `COMMENT` lists "stored" as a class value — **misleading**; `stored` is
  a coverage count, not a summary class. The old editorial buckets
  (wallet-fixable / contract-broken / aa-reestimation) were removed in v10 (we
  read v11) and are **re-derived downstream** from raw facts — that is exactly this repo's job
  (see [`../src/repricing_impact/groups.py`](../src/repricing_impact/groups.py)).
- **`replay_semantics = canonical_pre_tx_state`** (only value present) — a
  hardcoded provenance tag: each tx is replayed against canonical pre-tx state.
- **Schedules present** (`schedule_name`): only **two** under v11 — `eip-8037`
  (native state-creation gas + reservoir) and `eip-8038` (native state
  access/write repricing — non-uniform; storage write 2,800→10,000, cold access
  →3,000, account write →8,000). `7904-prelim` (the EIP-7904 preliminary
  execution-gas repricing, always out of focus here) is **gone from the
  warehouse** as of the v11 run (verified 2026-07-29).
- **Coverage:** mainnet (`chain_id = 1`) only. ⚠️ The manifest reports
  `gas_limit_multipliers = [1,2,4,8]` but this is **wrong** (verified 2026-07-03,
  re-confirmed against v11 2026-07-30 — measured `min_multiplier_to_succeed` runs
  continuously `0.0031 … 9.9979` over the full 1M-block window, both schedules):
  the producer re-runs each failing tx at exactly
  **two** limits, `1×` and `10×`, and `min_multiplier_to_succeed` is the measured
  ratio `schedule_gas_used / tx_gas_limit` from the completing run (see below);
  drill-in cap `max_divergences_per_block` (8192 in the pinned config; this file and
  [`../AGENTS.md`](../AGENTS.md) Key facts are the **only two** places that state the
  number — every other mention refers to it by name, so **update both together**) →
  `drill_ins_truncated = true` ⇒ `_divergence` is **incomplete** for that block.
  The timestamp column is **`block_timestamp`** (not `block_time`/`time`).
- **Shared provenance columns** on every table: `updated_at` (ReplacingMergeTree
  version), `row_id` (deterministic keccak identity), `analysis_config_hash`
  (whole-run identity), `schedule_config_hash`, `producer_schema_version` (11),
  `producer_git_commit` (`e91fe9d368f485fd84cd6e3ec3c58c9124af6d7e` for v11).
- **F-series labels** (`F1`…`F13`) prefix many divergence/summary column comments
  — forensic feature groups (F1 tier-1 failure, F2 cold-account, F3
  account/value/access-list drivers, F5 tx shape, F7 baseline frame, F8
  SLOAD/SSTORE drivers, F10 first gas divergence, etc.). ⚠️ The **F12 "tax"
  decomposition** (`tax_second_db_read` / `tax_other` / `tax_intrinsic`) is
  **not documented** beyond "tax category" — treat as uncertain until clarified
  with the producer.
- **Observed enum values** (for filtering/grouping): `oog_pattern` ∈
  {storage_heavy, call_chain, loop, memory_expansion, unknown};
  `oog_bottleneck_kind` ∈ {FractionalGas, FixedGas, Stipend2300};
  `state_gas_category` ∈ {access_list, authorization, contract_creation,
  transfer_new_account}. `min_multiplier_to_succeed` is a **continuous measured
  ratio** — exactly `schedule_gas_used / tx_gas_limit` from the completing run
  (`Nullable(Float64)`, `0 < min_mult <= 10`; NULL ⟺ not rescued even at the
  `10×` ceiling), not a discrete swept tier; `outer_limit_only_failure`
  is `Nullable(UInt8)` (0/1), not Bool. See
  [`verification-findings.md`](verification-findings.md) for the full domain study.

### v11 `block_summary` columns (producer schema 11)

Producer **v11** grew `gas_analysis_block_summary` from **43 to 54 columns**
(verified live 2026-07-29 against the pinned config
`0x6617c5db2827a7e77b08473306381258bb98e7eea456c90f18513d9e76e66ed3`). The change
was **purely additive on `block_summary`** — `gas_analysis_divergence` stays at
**113** columns and `gas_analysis_block_coverage` at **22**, so nothing in the
per-tx drill-in contract moved. The eleven additions implement Recommendations 1
and 2 of [`producer-data-recommendations.md`](producer-data-recommendations.md).

All eleven are `Nullable(UInt32)` unless stated, and were measured **100%
populated (zero nulls)** for both schedules and both `class` values over a
50k-block probe.

**EIP-2718 tx-type taxonomy** (Recommendation 1) — the **six** together sum
exactly to `tx_count`:

| Column | Producer meaning |
| --- | --- |
| `tx_count_type_legacy` | EIP-2718 type 0 |
| `tx_count_type_access_list` | type 1 |
| `tx_count_type_dynamic_fee` | type 2 |
| `tx_count_type_blob` | type 3 |
| `tx_count_type_set_code` | type 4 |
| `tx_count_type_other` | any other type — the residual that closes the sum |

**Tx-shape counts** (Recommendation 1):

| Column | Producer meaning |
| --- | --- |
| `tx_count_simple_transfer` | "Non-create txs with empty calldata (destination may still be a contract)" |
| `tx_count_contract_call` | "Non-create txs with non-empty calldata" |

**Percentage histogram + class-grain denominator** (Recommendation 2):

| Column | Type | Producer meaning |
| --- | --- | --- |
| `gas_delta_pct_hist` | `Array(Int32)` | "13-bin closed-left histogram of `100*gas_delta/baseline_gas_used` with edges `-100,-50,-25,-10,-1,0,1,10,25,50,100,200,500,+inf` and bin sum equal to `tx_count`" — **empty ⟺ the row was written pre-v11**. Like the other arrays it arrives as a string repr; parse with `opcodes.parse_arr`. The edges are **byte-identical** to `GAS_PCT_BIN_EDGES` in `scripts/precompute.py`. Note the shipped name is `gas_delta_pct_hist`, **not** the `gas_diff_pct_hist` the recommendation proposed. |
| `baseline_gas_used_sum` | `Nullable(UInt64)` | "Sum of baseline gas used over the aggregated txs of this class (ratio-of-sums denominator for `gas_delta_sum`)" — the class-grain denominator that did not exist in v10. |

⚠️ **BREAKING — `tx_count_creation` was REDEFINED.** Its v11 comment is "Txs in
this class that are tx-level contract creations (`to is null`)". It is **no longer
the v10 state-op creation count**. Consequences, all verified exact (zero
violating block rows over the 50k-block probe):

- **Tx-shape partition:** `tx_count_creation + tx_count_simple_transfer +
  tx_count_contract_call == tx_count`.
- **State-gas partition:** `tx_count_no_state + tx_count_runtime_state ==
  tx_count` — these **two alone** close it.
- Therefore `tx_count_authorization` ("Txs in this class carrying an EIP-7702
  authorization list") and the redefined `tx_count_creation` are **overlapping
  overlays**, members of _neither_ partition. The old v10-era 4-way sum
  `no_state + runtime_state + creation + authorization` does **not** equal
  `tx_count` (it broke on ~26k eip-8037 and ~23k eip-8038 blocks in the probe) —
  do not treat those four as mutually exclusive.

### `_divergence` selector & driver columns (materialized into `divergence_tx`)

A `DESCRIBE gas_analysis.gas_analysis_divergence` (113 cols, verified live
**2026-07-07**, still 113 under producer v11 — re-verified **2026-07-29**)
surfaced function-selector and causal-repricing-driver columns
that earlier docs never named. These power the per-contract **Affected contracts**
page (`affected_contracts.json`, SCHEMA §5b); they are materialized into the slim
build-time `divergence_tx` table (`scripts/precompute.py`, `DIVERGENCE_TX_COLUMNS`
/ `_DIVTX_DDL`) so all clustering runs locally in DuckDB with **no new
`_divergence` scan**. Populated-rate figures are from a 50k-block eip-8038 G4
probe (84,526 rows).

**Function-selector columns** (all `Nullable(String)`, 4-byte hex; decode to
signatures with `label_sources.selectors.decode_selector` — display only):

| Column | Type | G4 populated | Meaning |
| --- | --- | --- | --- |
| `entry_selector` | `Nullable(String)` | ~100% | Top-level function called on the entry contract (`recipient`). |
| `tier1_failing_selector` | `Nullable(String)` | ~53% | Selector of the function at the failing (original-limit) frame — the function the halt/revert lands in. **Fall back to `entry_selector` when NULL.** |
| `failure_selector_path` | `Nullable(String)` | ~100% | String-repr array of the selector call-path to the failure (parse with `opcodes.parse_arr`); optional context. |

**Causal repricing-driver columns** (the "why" — the repriced state line items
behind a G4 failure). Materialized because they are populated for G4:

| Column | Type | G4 populated | Meaning |
| --- | --- | --- | --- |
| `surcharge_at_oog` | `Nullable(Int64)` | ~33% (OOG-halt-only) | Extra gas the repricing charged at the OOG halt site — the direct cost of the repricing at the failure point. |
| `cold_account_access_count` | `Nullable(UInt64)` | ~100% | F2 cold-account access count. |
| `sload_cold_count` | `Nullable(UInt64)` | ~100% | F8 cold `SLOAD` count. |
| `sstore_cold_count` | `Nullable(UInt64)` | ~100% | F8 cold `SSTORE` count. |
| `access_list_address_count` | `Nullable(UInt64)` | ~100% | F3 EIP-2930 access-list address entries. |
| `access_list_storage_key_count` | `Nullable(UInt64)` | ~100% | F3 EIP-2930 access-list storage-key entries. |

**Present in the table but NOT yet materialized** (documented-for-future; the
DESCRIBE surfaced them, but the current `divergence_tx` projection does not carry
them — add to `DIVERGENCE_TX_COLUMNS` if a future aggregate needs them):

- **F1 tier-1 original-limit halt-site family:** `tier1_oog_contract`,
  `tier1_oog_opcode`, `tier1_oog_pc`, `tier1_oog_depth`,
  `tier1_oog_gas_remaining`, `tier1_failure_reason`, `tier1_failing_gas_provided`,
  `tier1_failing_gas_requested` (the failing-frame family; `tier1_failing_selector`
  above is the one member that _is_ materialized).
- **PC / divergence-site:** `divergence_pc`, `oog_pc`.
- **F10 first-gas-divergence family:** `gas_div_contract`, `gas_div_pc`,
  `gas_div_call_depth`, `gas_div_opcode`.
- **Revert / charge detail:** `revert_data`, `additional_gas_charged`.
- **Fuller state-access count family:** `warm_account_access_count`,
  `sload_warm_count`, `sstore_set_count`, `sstore_reset_count`,
  `sstore_clear_count`, `sstore_noop_count`, `sstore_dirty_count`,
  `value_transfer_count`, `create_opcode_count`.

⚠️ **F12 "tax" columns excluded.** `tax_second_db_read` / `tax_other` /
`tax_intrinsic` exist in the table but remain **undocumented / uncertain** (see
the F-series note above) — they are **not** materialized and must not be treated
as usable pending producer clarification.

### Decoding the `opcode_*` arrays (block_summary)

`opcode`, `opcode_count`, `opcode_gas_baseline`, `opcode_gas_schedule` are
**sparse parallel arrays** — element *i* of all four describes the same opcode,
whose EVM byte is `opcode[i]`. Two gotchas, both handled by helpers in
[`../src/repricing_impact/opcodes.py`](../src/repricing_impact/opcodes.py)
(`OPCODES`, `parse_arr`, `opcode_name`, `explode_opcodes`):

- Over the HTTP driver, ClickHouse `Array` columns come back as **string reprs**
  (`"[1,2,3]"`), not Python lists — parse with `json.loads` (`parse_arr`). Same
  for `gas_delta_log2_hist` / `multiplier_log2_hist`.
- No opcode-name library is installed, so `opcodes.py` ships a compact
  byte→mnemonic table (Cancun/Prague set; PUSH/DUP/SWAP/LOG generated;
  `UNKNOWN_0x..` fallback).

`_divergence.trace_payload` is a large versioned-JSON blob of *child components*
(call frames, opcode counts, event logs) — **not** a full EVM trace
(`trace_format = research_drill_in_components_v1`/`v2`). **Never select it.**

## Cross-references

- Sibling repo `evm-gas-repricings`
  (`/Users/maria/Documents/ef/evm-gas-repricings`) has prior
  EIP-7904/8037/8038 analysis to reuse patterns from.
- Sibling repo `repricing-forensics` (deployed at
  `repricing-forensics.carlbeek.com`) is the earlier, slower analysis over the
  pre-v11 schema — we reuse its chart ideas, not its architecture or classifier.
</content>
