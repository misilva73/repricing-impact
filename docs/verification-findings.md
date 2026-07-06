# Verification findings — locking the partition predicate

> **Historical record (2026-06-30), reinterpreted to current logic.** This
> documents the empirical study that locked the partition. The **current**
> predicate lives in
> [`../src/repricing_impact/groups.py`](../src/repricing_impact/groups.py) —
> read that if anything here disagrees. The *measurements* below (counts,
> cross-tabs, distributions) are preserved exactly as taken on 2026-06-30; their
> **interpretation** has been updated for two things learned afterwards:
>
> - **The gas-limit sweep is `{1×, 10×}`, not `[1,2,4,8]`** (corrected
>   2026-07-03). The producer replays each failing tx at just two limits, `1×`
>   (original) and `10×` (ceiling), and `min_multiplier_to_succeed` is the
>   **measured ratio** `schedule_gas_used / tx_gas_limit` from the completing run
>   — not a swept tier. So `TOP_MULTIPLIER = 10`, values run continuously up to
>   `10` (empirical max 9.9979), and the old "top tier 8×" / R3 `min_mult ≤ 8`
>   clamp is **gone**: the whole `1 < min_mult <= 10` range is G3, and G4 is
>   strictly `min_mult IS NULL`. The G4 "does more gas help" verdict comes from
>   `replay_halt_oog` (the `10×`-ceiling halt kind), not the `oog_*` columns.
> - **The partition gained a sixth group, Already failing (`af`).** Any drill-in
>   with `baseline_success = false` is `af` (it was already broken before the
>   repricing), carved out of the changed groups G2/G3/G4. Cross-tab rows below
>   are annotated with their *current* group.
>
> The "no duplicates / `FINAL` not needed" finding (§ de-duplication) was also
> **superseded**: at full scale `_divergence` carries ~2.2–2.4k pre-merge
> duplicate `row_id`s, so precompute always dedups via
> `groups.deduped_divergence_subquery`.

> Empirical investigation against the **live** ClickHouse `gas_analysis` warehouse
> (`clickhouse.xatu.ethpandaops.io`, server `26.2.5.45`), run 2026-06-30.
> Purpose: nail down the facts the `groups.py` partition predicate hinges on,
> so the next agent can lock it correctly. This is investigation only — no dashboard
> code was written.
>
> All queries pinned `chain_id = 1`, the chosen `analysis_config_hash`, a single
> `schedule_name`, and a small `block_number` window. The throwaway probe script lives
> in the session scratchpad (`scratchpad/q.py`).

---

## TL;DR / headline conclusions

1. **Sample config:** `0xc17ac709e44c2100b9ee61cc17b5167643620462fb8a69cc6bad0d61d35f37d2`
   — v10, has all three schedules, **1,000,000 blocks each**, blocks
   **24,319,986 → 25,319,985**, most recent `updated_at`. Use it.
2. **`min_multiplier_to_succeed` is a CONTINUOUS measured ratio** (`Nullable(Float64)`),
   NOT a discrete swept tier. It equals `schedule_gas_used / tx_gas_limit` from the run
   that completes; values run smoothly from ~0.013 up to the `10×` ceiling (`<1` is
   common). NULL = not rescued even at `10×`.
3. **The `≤1 / >1 / NULL` boundary is exactly equivalent to `schedule_success`:**
   `schedule_success = true ⟺ min_mult ≤ 1`, with **zero leakage** over 2.48M drill-in
   rows. So successful-at-1× txs do **NOT** carry NULL — they carry a value `≤ 1`. The
   plan's feared failure mode does not occur in this data.
4. **The changed groups require a working baseline.** Any drill-in with
   `baseline_success = false` is **Already failing (`af`)**, not G2/G3/G4 — it was
   broken before the repricing, so a schedule-side failure isn't "newly broken".
5. **`_divergence` looked fully merged on every sampled window** (no duplicate `row_id`,
   no logical-key collision) — but this was **superseded at full scale**: the 1M-block
   `_divergence` carries ~2.2–2.4k pre-merge duplicate `row_id`s, so dedup is mandatory
   (see § de-duplication).
6. **Partition identity holds 100%:** `tx_count_unchanged + tx_count_gas_only +
   tx_count_stored == tx_count` on every sampled block (incl. truncated). And the full
   per-tx partition `G1 + gas_only + G2_drillin + G3 + G4 + af == tx_count` closes exactly
   (diff = 0) on non-truncated blocks.
7. **All required columns exist in v10** (one naming note: `block_timestamp`, not
   `block_time`/`time`). All enum domains match the handover.

---

## 1. Sample config

`gas_analysis_run.manifest_json` + per-(config, schedule) `block_coverage` counts:

| analysis_config_hash | schedules | blocks/schedule | block range | last updated |
|---|---|---|---|---|
| `0x1daf…ffdc` | 7904/8037/8038 | 794 | 25,319,189–25,319,985 | 2026-06-22 |
| `0x2686…295a` | 7904/8037/8038 | 1,804 | 25,318,180–25,319,985 | 2026-06-22 |
| **`0xc17a…37d2`** | **7904/8037/8038** | **1,000,000** | **24,319,986–25,319,985** | **2026-06-28** |
| `0xcd52…c05e` | 8037/8038 only | 600 | 25,319,386–25,319,985 | 2026-06-23 |

**Chosen: `0xc17ac709e44c2100b9ee61cc17b5167643620462fb8a69cc6bad0d61d35f37d2`** — only config
with all schedules AND a large recent window. Auto-pick logic (most blocks ∧ both
target schedules ∧ most recent `updated_at`) will select it.

Manifest essentials: `producer_schema_version=10`,
`producer_git_commit=df6207d472455db84320a1a4df223972a1fe1f85`,
`replay_semantics=canonical_pre_tx_state`, **`max_divergences_per_block=1024`**.
⚠️ The manifest also reports **`gas_limit_multipliers=[1,2,4,8]`**, but this is
**wrong** (corrected 2026-07-03) — the real sweep is `{1×, 10×}` (see the header note
and § 2). Schedules: `7904-prelim` (ExecutionOnly), `eip-8037` (Both — state-creation
gas + reservoir), `eip-8038` (Both — state access/write repricing, non-uniform).

Per-schedule block range is identical across schedules for this config
(24,319,986–25,319,985), so eip-8037 and eip-8038 compare on like-for-like blocks — no
fallback per-schedule config needed.

---

## 2. `min_multiplier_to_succeed` semantics (the load-bearing question)

Column type: `Nullable(Float64)`.

### Discrete tier or continuous ratio? → **CONTINUOUS**

Finely-bucketed distribution (eip-8037, blocks 24,320,000–24,322,000), `(1,4]`:
every 0.1 bin between 1.0 and 4.0 is populated (e.g. 1.0→7077, 1.4→15559, 2.2→9716,
3.9→195). It is **not** clustered at {2, 4, 8}. Values below 1 are common
(min observed ~0.0137). It is the **measured continuous ratio**
`schedule_gas_used / tx_gas_limit` — "gas the tx actually needed ÷ its original gas
limit" — from the run that completes, with NULL = never made to succeed even at the
`10×` ceiling. (This corrects the original "estimated / interpolated within the swept
budget, top tier 8×" reading.)

Bucketed counts (blocks 24,320,000–24,330,000). The bins here were cut at the
then-assumed tiers {2,4,8}; under the real `{1,10}` sweep the `>8` bucket is simply the
`(8,10]` tail — genuine rescues, not "beyond range":

| bucket | eip-8038 | eip-8037 |
|---|---|---|
| `(0,1)` | 24,730 | 12,746 |
| `==1`..`(1,2]` | 570,822 | 329,910 |
| `(2,4]` | 331 | 154,938 |
| `(4,8]` | 0 | 14,253 |
| `>8` (i.e. `(8,10]`) | 0 | 6 |
| `NULL` | 48,652 | 41,857 |

### Correlation with success flags → **a perfect, clean partition**

Cross-tab `schedule_success × baseline_success × min_mult-bucket`
(blocks 24,320,000–24,330,000), annotated with the **current** group. Note every
`base_ok = false` row is **Already failing (`af`)**, regardless of the schedule
outcome — those cohorts were G3/G4/rescue under the original 5-group reading:

eip-8038:
```
sch_ok base_ok  mm-bucket      n
false  false    (1,8]       4,389      ← AF (baseline already failing — not "newly broken")
false  false    NULL       31,244      ← AF (baseline already failing)
false  true     (1,8]     566,764      ← G3 (working baseline, rescued by more gas, ≤10×)
false  true     NULL       17,408      ← G4 (working baseline, not rescued even at 10×)
true   false    <=1           930      ← AF / rescue (base-fail → sched-success flip)
true   true     <=1        23,800      ← G2 (working baseline, succeeds as-is; gas/trace differs)
```
eip-8037 has the same shape plus `(4,8]` and 6 `(8,10]` rows, all `schedule_success=false`.

### Do successful-at-1× txs carry NULL? → **NO**

Strict check over **2.48M** drill-in rows (both schedules, blocks 24,320,000–24,340,000):

| schedule | `schedule_success ∧ (mm NULL ∨ mm>1)` | `¬schedule_success ∧ mm≤1` |
|---|---|---|
| eip-8038 | **0** | **0** |
| eip-8037 | **0** | **0** |

So the biconditional **`schedule_success ⟺ min_multiplier_to_succeed ≤ 1`** holds with
zero exceptions. The G2/G4 boundary is safe either way; the plan's "switch to
`schedule_success` if 1× txs are NULL" contingency is moot here.

### `outer_limit_only_failure` cross-check

`Nullable(UInt8)` (0/1), **not** Bool. For eip-8037 it cleanly tags the gas-rescuable
cohort: `olf=1` rows are **all** `(1,10]` with `schedule_success=false` (490,998 rows) —
i.e. "failed only at the original/outer limit, succeeds with more gas" = exactly G3's
core. `olf=0` covers G2 (`≤1`), G4 (`NULL`), and a small `(1,10]` residue (8,103) that
is also genuinely gas-rescuable. So `min_mult > 1` is the **broader, authoritative** G3
signal; `olf` is a useful corroborating flag, not a replacement.

### Edge case: `min_mult` near the `10×` ceiling

The largest `min_mult` values (the old `>8` bucket: 6 rows / 10k blocks for eip-8037;
**0** for eip-8038) sit in `(8,10]`. Under the real `{1,10}` sweep these are **genuinely
measured rescues** — the tx completed at the `10×` ceiling consuming up to ~9.9× its
original limit — so they are **G3 (fixable)**, exactly like any other `min_mult > 1`
row. (Under the original mistaken "top tier 8×" reading they were flagged as "beyond the
swept range" and recommendation R3 proposed clamping them out of G3; that clamp was
**dropped** — there is no ceiling below `10×` to clamp against.) The only "not rescued"
cohort is **`min_mult IS NULL`** (G4); within G4, `replay_halt_oog` splits genuinely
non-gas halts (`false`) from still-OOG-at-`10×` (`true`, needs `>10×` or is an unbounded
loop).

---

## 3. Partition identity

`block_coverage` columns confirmed: `tx_count`, `tx_count_unchanged`,
`tx_count_gas_only`, `tx_count_stored`, `expected_drill_in_count`,
`retained_drill_in_count`, `drill_ins_truncated`, `block_number`, `block_timestamp`.

**`tx_count_unchanged + tx_count_gas_only + tx_count_stored == tx_count`** holds for
**100%** of sampled blocks (10,001/10,001 per schedule, blocks 24,320,000–24,330,000),
including the one truncated block. `tx_count_stored` is the **count of txs that have a
drill-in row stored** (= `expected_drill_in_count`), independent of truncation.

**Truncation effect.** On non-truncated blocks:
`expected_drill_in_count == retained_drill_in_count == tx_count_stored`. On the
truncated block, `tx_count_stored == expected_drill_in_count` (e.g. 381) but
`retained_drill_in_count` is smaller (e.g. 305) — the gap is drill-ins the producer
dropped at the 1024 cap. So:

- The partition identity uses **`tx_count_stored`** (the intended, pre-truncation count).
- The number of `_divergence` rows you can actually count is **`retained_drill_in_count`**,
  which **exactly equals** the live `_divergence` row count (verified: 1001/1001 blocks,
  sum 69,496 = 69,496).
- **G5 (Unknown)** = `tx_count − (G1 + gas_only + retained_drill_ins)` =
  `tx_count_stored − retained_drill_in_count` on truncated blocks (dropped drill-ins),
  and 0 on non-truncated blocks.

**Truncation is rare.** Over 100,001 blocks/schedule: **15 truncated blocks (0.015%)**;
dropped drill-ins ≈ 5,160 of 6.52M stored (~0.08%). G5 is tiny but non-zero — surface it.

**End-to-end partition closes exactly.** The retained drill-ins split into
`G2_drillin + G3 + G4 + af` (the sixth group, Already failing), so on non-truncated
blocks `G1 + tx_count_gas_only + G2_drillin + G3 + G4 + af == tx_count` with **diff = 0**.
(The 2026-06-30 spot-check predated the `af` carve-out and closed against the combined
drill-in total; the identity is unchanged since `af` is a subset of the retained
drill-ins, not a new source of rows.)

---

## 4. Column existence (v10 DESCRIBE) — all present

Verified via `system.columns` for `database='gas_analysis'`. **Every** column the plan
relies on exists. Notes/caveats:

- **`_divergence`:** all present — `min_multiplier_to_succeed` `Nullable(Float64)`,
  `schedule_success`/`baseline_success` `Bool`, `replay_halt_oog` `Nullable(Bool)`
  (the top-tier/`10×` halt kind — authoritative G4 fixability signal),
  `outer_limit_only_failure` **`Nullable(UInt8)`** (plan implies Bool — treat as 0/1),
  `gas_delta` `Int64`, `oog_pattern`/`oog_bottleneck_kind`/`state_gas_category`
  `Nullable(String)`, `oog_call_depth` `Nullable(Int32)`,
  `divergence_contract`/`oog_contract`/`recipient` `Nullable(FixedString(42))`,
  `divergence_opcode` `Nullable(UInt8)`,
  `schedule_initial_reservoir`/`runtime_state_gas_spillover` `Nullable(UInt64)`,
  `reservoir_exhausted` `Nullable(Bool)`, `tx_hash` `FixedString(66)`.
- **`block_coverage`:** all present. **Timestamp column is `block_timestamp`**
  (`DateTime`) — there is no `block_time`/`time`. Use `block_timestamp` for the per-day
  bucketing in `overview_series.json`.
- **`block_summary`:** all present — `gas_delta_sum`/`_min`/`_max` (`Nullable(Int64)`),
  `gas_delta_sum_sq` (`Nullable(Float64)`), `gas_delta_log2_hist` & `multiplier_log2_hist`
  (`Array(Int32)`), opcode arrays (`Array(UInt8/UInt64)`), `class`
  (`LowCardinality(String)`), F2/F3/F8 state-driver counts
  `tx_count_creation`/`_authorization`/`_runtime_state`/`_no_state` (`Nullable(UInt32)`).

**Nothing missing or renamed** beyond the `block_timestamp` naming and the
`outer_limit_only_failure` being `UInt8` not `Bool`.

---

## 5. Enum / domain sanity

- **`class` ∈ {`unchanged`, `gas_only`}** only — exactly the two documented values.
  `stored` is **not** a class (it's a coverage count), confirming the handover warning.
- **`oog_pattern`** observed: `storage_heavy`, `call_chain`, `unknown`, `loop`,
  `memory_expansion` (+ NULL; NULL dominates — most drill-ins aren't OOG).
- **`oog_bottleneck_kind`** observed: `FixedGas`, `FractionalGas`, `Stipend2300` (+ NULL).
- **`state_gas_category`** observed: eip-8037 → `authorization`, `contract_creation`,
  `transfer_new_account`; eip-8038 → `access_list`, `contract_creation` (+ NULL).
  (Domain matches handover; which categories appear is schedule-dependent.)

---

## ReplacingMergeTree de-duplication

> **⚠️ Superseded at full scale.** The windows below showed zero duplicates, and this doc
> originally concluded `FINAL` was unnecessary. That conclusion held only for small
> windows: over the full 1M-block range `_divergence` carries **~2.2–2.4k pre-merge
> duplicate `row_id`s** (localized to blocks 24,400,000–24,499,999). Precompute therefore
> **always** dedups every `_divergence` aggregate via
> `groups.deduped_divergence_subquery` (inner `argMax(col, updated_at) GROUP BY row_id`),
> in preference to `FINAL` on the 113M-row distributed table. `block_coverage` /
> `block_summary` remain duplicate-free even at full scale.

Checked on multiple windows/tables for this config (the small-window snapshot):

| table | window | rows | distinct `row_id` | dup rows | logical-key dups |
|---|---|---|---|---|---|
| block_coverage | 24.320.0–322.0k, eip-8038 | 2,001 | 2,001 | **0** | 0 |
| block_summary | same | 3,996 | 3,996 | **0** | — |
| divergence | same | 134,997 | 134,997 | **0** | 0 |
| divergence | 25.000.0–002.0k, eip-8038 | 79,324 | 79,324 | **0** | — |
| divergence | 25.000.0–002.0k, eip-8037 | 202,028 | 202,028 | **0** | — |

No duplicate `row_id` and no logical-key (`block_number[,tx_hash]`) collisions in **these
sampled windows** — but see the superseding note above; the full range is not dupe-free.

---

## RECOMMENDATION for `groups.py`

> This section records the 2026-06-30 recommendation, updated to what
> `groups.py` actually implements today (the `af` sixth group and the `{1,10}`
> sweep). The module docstring is authoritative.

### Group predicate (per retained `_divergence` drill-in row)

The changed groups are all gated on a **working baseline** (`baseline_success = true`);
any `baseline_success = false` drill-in is **Already failing (`af`)**. On the working-
baseline side the empirical biconditional `schedule_success ⟺ min_mult ≤ 1` means the two
readings agree — key the success side on `schedule_success` (robust if a future re-run
leaves `min_mult` NULL on 1× successes) and use `min_mult` only to split the failing side:

```text
Already failing (af) : baseline_success = false          # takes precedence over all below

G2 drill-in member   : baseline_success = true AND schedule_success = true
                       (≡ min_multiplier_to_succeed <= 1)

G3 (needs gas bump)  : baseline_success = true
                       AND schedule_success = false
                       AND min_multiplier_to_succeed > 1   # any value up to the 10× ceiling

G4 (potentially      : baseline_success = true
     broken)           AND schedule_success = false
                       AND min_multiplier_to_succeed IS NULL   # not rescued even at 10×
```

Block-level groups (per schedule, block):

```text
G1 = block_coverage.tx_count_unchanged
G2 = block_coverage.tx_count_gas_only  +  count(drill-ins with G2 predicate)
G3 = count(drill-ins with G3 predicate)
G4 = count(drill-ins with G4 predicate)
af = count(drill-ins with af predicate)
G5 = tx_count - (G1 + G2 + G3 + G4 + af)
   = tx_count_stored - retained_drill_in_count   (0 on non-truncated blocks)
```

Notes:

- **R1.** Equivalent simpler form on the working-baseline side (verified zero-leakage on
  this config) is `min_mult ≤ 1 → G2 / min_mult > 1 → G3 / NULL → G4`. Prefer the
  `schedule_success`-keyed form for defensiveness; assert the biconditional in a unit
  test so a regression in producer behavior is caught.
- **R2.** Surface `outer_limit_only_failure = 1` as a corroborating G3 sub-signal, but
  do **not** use it as the G3 boundary — `min_mult > 1` is broader and catches the
  `olf=0` rescuable residue (~8k rows/10k blocks for eip-8037).
- **R3 (dropped).** The original recommendation clamped G3 at `min_mult ≤ 8`, believing
  8× was the top swept tier. The real ceiling is `10×`, so there is nothing to clamp:
  the whole `1 < min_mult <= 10` range is genuinely rescued (G3), and G4 is strictly
  `min_mult IS NULL`. For the G4 "does more gas help" verdict use `replay_halt_oog`
  (the `10×`-ceiling halt kind): `false` = non-gas halt (genuinely broken, ~99.98% of
  G4), `true` = still OOG at `10×` (needs `>10×` / unbounded loop).
- **R4.** Reservoir columns (`schedule_initial_reservoir`,
  `runtime_state_gas_spillover`, `reservoir_exhausted`, `state_gas_category`) are
  populated and meaningful for eip-8037 — safe to surface on the 8037
  transaction-failures page.

### De-duplication

**`FINAL` is NOT required, but dedup IS** — the original "no `FINAL` needed" advice was
based on dupe-free small windows and was **superseded at full scale** (~2.2–2.4k
duplicate `row_id`s over the 1M-block range). The implemented approach:

1. **Always dedup `_divergence`** via `groups.deduped_divergence_subquery` — an inner
   `argMax(col, updated_at) GROUP BY row_id` relation feeding the outer aggregate. This
   is preferred over `FINAL` on the 113M-row distributed table (expensive) and never
   selects `trace_payload`.
2. **Cheap guard:** on the deduped relation assert `count() - uniqExact(row_id) == 0`
   (holds by construction); precompute also reports the raw duplicate count as an
   informational stat.
3. `block_coverage` / `block_summary` are duplicate-free (one row per block /
   per `(block, class)`) — no dedup needed there.

### Other locked facts for the implementers

- Pinned config hash: `0xc17ac709e44c2100b9ee61cc17b5167643620462fb8a69cc6bad0d61d35f37d2`.
- Block range 24,319,986–25,319,985; identical per schedule (no per-schedule fallback).
- Timestamp column for day-bucketing is **`block_timestamp`**.
- `outer_limit_only_failure` is `UInt8` (0/1), compare `= 1` not truthiness on Bool.
- Partition identity `unchanged + gas_only + stored == tx_count` is verified true
  (100%); G5 = `stored − retained` on truncated blocks (~0.015% of blocks, ~0.08% of
  drill-ins) and 0 otherwise.
