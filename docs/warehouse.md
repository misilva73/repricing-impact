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

- `bin/reth-research/clickhouse/migrations/002_gas_analysis_v10.up.sql` — current
  schema (producer schema **v10**) with per-column `COMMENT`s.
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
| `gas_analysis_block_coverage` | one row per (config, schedule, block) | ~3M | cohort tx-counts per block; drill-in completeness; block gas used/limit |
| `gas_analysis_block_summary` | one row per (config, schedule, block, **class**) | ~6M | aggregated **gas impact** (gas-delta stats, per-opcode totals, state-gas) for the cheap cohorts |
| `gas_analysis_divergence` | one row per (config, schedule, block, tx) drill-in | ~113M | per-tx **failure forensics** (status flips, OOG, recoverability, traces) |

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
  (wallet-fixable / contract-broken / aa-reestimation) were removed in v10 and
  are **re-derived downstream** from raw facts — that is exactly this repo's job
  (see [`../src/repricing_impact/groups.py`](../src/repricing_impact/groups.py)).
- **`replay_semantics = canonical_pre_tx_state`** (only value present) — a
  hardcoded provenance tag: each tx is replayed against canonical pre-tx state.
- **Schedules present** (`schedule_name`): `7904-prelim` (EIP-7904 preliminary
  execution-gas repricing — out of focus), `eip-8037` (native state-creation gas
  + reservoir), `eip-8038` (native state access/write repricing — non-uniform;
  storage write 2,800→10,000, cold access →3,000, account write →8,000).
- **Coverage:** mainnet (`chain_id = 1`) only. ⚠️ The manifest reports
  `gas_limit_multipliers = [1,2,4,8]` but this is **wrong** (verified 2026-07-03):
  the producer re-runs each failing tx at exactly **two** limits, `1×` and `10×`,
  and `min_multiplier_to_succeed` is the measured ratio `schedule_gas_used /
  tx_gas_limit` from the completing run (see below); drill-in cap
  `max_divergences_per_block` (1024 in the pinned config) → `drill_ins_truncated
  = true` ⇒ `_divergence` is **incomplete** for that block. The timestamp column
  is **`block_timestamp`** (not `block_time`/`time`).
- **Shared provenance columns** on every table: `updated_at` (ReplacingMergeTree
  version), `row_id` (deterministic keccak identity), `analysis_config_hash`
  (whole-run identity), `schedule_config_hash`, `producer_schema_version` (10),
  `producer_git_commit`.
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
  pre-v10 schema — we reuse its chart ideas, not its architecture or classifier.
</content>
