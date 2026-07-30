# Producer data recommendations — "Succeeds with changes" deep dive

> **Status: SHIPPED (producer schema v11, verified live 2026-07-29).**
> Recommendation 1 (tx-type taxonomy) and Recommendation 2 (percentage histogram +
> class-grain denominator) **both landed**, as eleven additive
> `gas_analysis_block_summary` columns — see
> [`warehouse.md`](warehouse.md#v11-block_summary-columns-producer-schema-11) for
> the shipped column semantics and
> [`verification-findings.md`](verification-findings.md#addendum--producer-schema-v11-2026-07-29)
> for the verification pass. **This doc is retained as the dated record of what was
> proposed**; the original text below is left intact and annotated in place, so the
> proposal and the outcome can be compared.
>
> **Naming / shape deltas vs what this doc proposed:**
>
> - proposed `gas_diff_pct_hist` shipped as **`gas_delta_pct_hist`** — the 13 bin
>   edges are **byte-identical** to what was proposed here (and to
>   `GAS_PCT_BIN_EDGES` in `scripts/precompute.py`);
> - proposed `tx_count_contract_creation` **did NOT ship** as a new column —
>   instead **`tx_count_creation` was REDEFINED** to tx-level creations
>   (`to IS NULL`). That is a **breaking** change to the v10 state-op meaning this
>   doc describes below, and it is what makes the tx-shape partition close;
> - bonus, unproposed: **`tx_count_type_other`**, which makes the six tx-type
>   counts sum **exactly** to `tx_count`;
> - bonus, unproposed name: the class-grain denominator shipped as
>   **`baseline_gas_used_sum`** — this resolves the "no baseline denominator at the
>   class grain" blocker in Recommendation 2 outright;
> - also shipped: **`tx_count_simple_transfer`**, **`tx_count_contract_call`**,
>   exactly as proposed.
>
> **⚠️ This doc's own "Caveat — drill-ins skew more extreme than the bulk cohort"
> was empirically REFUTED.** It predicted the `≥100%` tail would be *thinner* in
> the bulk `gas_only` cohort than in the ~2.3% drill-in slice. Measured over the
> **full 1,000,000-block window** (2026-07-30), for **eip-8037 the tail is far
> FATTER: 25.17% of the cohort sits at `≥100%`** — an order of magnitude above the
> ~2.7% the caveat predicted it would fall below — with `[100,200)` alone at
> **23.56%** and a ratio-of-sums gas increase of **+38.88%**. For eip-8038 the
> caveat held: mass concentrates in `[25,50)` (44.79%), the `≥100%` tail is
> **0.0011%**, nothing above 200%, ratio-of-sums **+13.50%**. Consumer-side
> percentage distributions must therefore **not** be assumed to be a milder version
> of the drill-in slice — the skew is **schedule-dependent in direction**.
> The caveat's two **structural** claims both stood: the `−100%` floor is
> arithmetic, and the tail genuinely had to be split rather than lumped into one
> `[100, +inf)` bin — for eip-8037 that split matters far more than the caveat
> argued, since `[100,200)` is its second-largest bin. Where the caveat was
> *partly* right is low-bin mass: eip-8037's **modal** bin at full scale is
> `[1,10)` (29.26%), so mass did pile into the low bins **as well as** the tail.
> Full per-bin figures are in the annotation on the caveat itself, at the end of
> Recommendation 2 below.

## Recommendation 1 — a transaction-type taxonomy for the `gas_only` cohort

**Problem.** From first principles, the natural way to read "who pays the repricing
tax" is by transaction *type*: simple ETH transfers vs contract calls vs contract
deployments vs EIP-7702 authorizations, and by EIP-2718 `tx_type`
(legacy / access-list / dynamic-fee / blob / set-code). None of these are available for
the `gas_only` cohort — only the four state-op driver counts, which describe *state gas*,
not transaction shape. A simple ETH transfer and an ERC-20 transfer can both land in
`no_state`, and `tx_count_creation` counts a *state-op* creation, not the tx-level
`is_create`.

> **SHIPPED (v11).** "None of these are available" is no longer true — the whole
> taxonomy now exists at the class grain. And the last clause is now **inverted**:
> `tx_count_creation` was **redefined** to the tx-level creation
> (`to IS NULL`), so it no longer counts a state-op creation at all. One knock-on:
> the four state-op driver counts (`no_state` / `runtime_state` / `creation` /
> `authorization`) are consequently **not** a partition under v11 — only
> `no_state + runtime_state` closes to `tx_count`, with `creation` and
> `authorization` as overlapping overlays.

**Proposed `block_summary` columns** (per `(block, class)`, alongside the existing
`tx_count_*` driver counts), populated from the same per-tx facts already visited when
building the aggregate:

| Column | Meaning |
| --- | --- |
| `tx_count_type_legacy / _access_list / _dynamic_fee / _blob / _set_code` | EIP-2718 `tx_type` histogram for the cohort |
| `tx_count_simple_transfer` | `is_create = false` **and** recipient set **and** empty calldata (`input_zero_bytes + input_nonzero_bytes = 0`) |
| `tx_count_contract_call` | `is_create = false` **and** non-empty calldata |
| `tx_count_contract_creation` | tx-level `is_create = true` (distinct from the state-op `tx_count_creation`) |

> **What actually shipped (v11).** The five `tx_count_type_*` columns landed as
> proposed, **plus an unproposed sixth**, `tx_count_type_other`, which makes the six
> sum exactly to `tx_count`. `tx_count_simple_transfer` and
> `tx_count_contract_call` landed as proposed (the producer's comment for
> `simple_transfer` notes "destination may still be a contract").
> `tx_count_contract_creation` **did not ship**: instead the existing
> `tx_count_creation` was **redefined** to the tx-level meaning, which is why
> `creation + simple_transfer + contract_call == tx_count` now holds exactly. Note
> the producer's 3-way shape partition gives EIP-7702 authorizations **no slot** —
> they fall in `contract_call` (or `simple_transfer` when calldata is empty) — so
> `tx_count_authorization` is an overlay on top of it, not a fourth category.

With these, the dashboard could show a tx-type breakdown covering **all** of G2. Today it
shows none, because the split is only computable per-tx (on the drill-in subset), which is
an unrepresentative slice of the group — so the categories would have to be derived by the
producer while aggregating the `gas_only` cohort.

> **Now stale.** "Today it shows none" was true when written. As of v11 the
> dashboard publishes G2 `tx_shape_mix` and `tx_type_mix` from these class-grain
> columns, covering the whole `gas_only` cohort — see
> [`../site/data/SCHEMA.md`](../site/data/SCHEMA.md) §4.

## Recommendation 2 — a gas-diff **percentage** histogram for the `gas_only` cohort

**Problem.** The only distribution `block_summary` carries for the `gas_only` cohort is
`gas_delta_log2_hist` — a histogram of **absolute** gas delta (`abs(gas_delta)`, in gas
units). Absolute gas is hard to interpret across a heterogeneous mix of transactions: a
+5,000-gas delta is negligible on a 2M-gas contract call but a large relative hit on a
21,000-gas transfer. The question we actually want to answer for "who pays the repricing
tax" is the **relative** change — `gas_delta / baseline_gas_used` per transaction — and
its distribution across the cohort.

That percentage is **not computable by the consumer** from `block_summary`, for two
independent reasons:

- **No per-tx pairing.** The `gas_only` cohort is collapsed into per-block aggregates
  with no per-transaction rows, so a per-tx `(gas_delta, baseline_gas_used)` pair can
  never be recovered to form a ratio.
- **No baseline denominator at the class grain.** `block_summary` has `gas_delta_sum`
  but **no `baseline_gas_used` aggregate** for the class, so even a crude block-level
  `Σgas_delta / Σbaseline_gas` is impossible (and a ratio-of-sums would be one number
  per block, not a distribution — and ≠ the mean of per-tx ratios). `opcode_gas_baseline`
  doesn't help: it's per-opcode *execution* gas only (no intrinsic / calldata / tx-level
  gas, no refunds), so it isn't the tx's baseline gas.

So a percentage distribution is resolvable today only from the drill-in members (which
carry per-tx `baseline_gas_used` + `gas_delta`) — a small, unrepresentative minority of
G2.

> **RESOLVED (v11).** Both blockers above are gone, by different means. The
> **"no per-tx pairing"** problem was solved the way this section asked — the
> producer forms the ratio while it still has the per-tx pair and emits only the
> binned result (`gas_delta_pct_hist`); the consumer still cannot recover per-tx
> pairs, and does not need to. The **"no baseline denominator at the class grain"**
> problem was solved outright by the unproposed `baseline_gas_used_sum`
> (`Nullable(UInt64)`, producer comment: "ratio-of-sums denominator for
> `gas_delta_sum`"). The parenthetical warning that a ratio-of-sums is *one number,
> not a distribution*, and **≠ the mean of per-tx ratios**, remains exactly right —
> so the dashboard publishes **both**: `pct_bins` for the distribution and
> `gas_delta_pct_of_baseline` as the gas-weighted headline, labelled as a ratio of
> sums. The `opcode_gas_baseline` dead end is also still correctly diagnosed;
> `baseline_gas_used_sum` is the real denominator.

**Proposed change.** Emit a **percentage** histogram alongside the existing absolute one,
computed by the producer while aggregating the cohort (where per-tx `baseline_gas_used`
and `gas_delta` are both in hand):

| Column | Meaning |
| --- | --- |
| `gas_diff_pct_hist` | Histogram of per-tx `100 * gas_delta / baseline_gas_used` over the class, using the fixed signed bin edges below |

> **Shipped under a different name:** **`gas_delta_pct_hist`** (`Array(Int32)`),
> semantics exactly as proposed and bin edges **byte-identical** to the set below.
> The producer's comment documents it as closed-left with `bin sum equal to
> tx_count`, and an **empty** array means the row was written pre-v11 — consumers
> must pad rather than filter, or the sibling aggregates in the same query get
> corrupted.

**Bin edges (percent), empirically chosen** — closed on the left, open on the right:

```text
[-100, -50)  [-50, -25)  [-25, -10)  [-10, -1)  [-1, 0)   <- schedule cheaper
[0, 1)  [1, 10)  [10, 25)  [25, 50)  [50, 100)             <- schedule costlier, bulk
[100, 200)  [200, 500)  [500, +inf)                        <- costlier tail (catch-all)
```

Two properties are load-bearing and were **verified against the G2 drill-in set** (the
only slice carrying per-tx `baseline_gas_used` + `gas_delta`; ~1.46M rows for eip-8037,
~2.93M for eip-8038, over the pinned config's full block range):

- **Bounded below at −100%.** `schedule_gas_used ≥ 0` forces `pct ≥ −100%`, so the lowest
  bin is `[-100, -50)`, not an open-ended `(-∞, …]`. Measured minimum was −93.9%
  (eip-8038) / −91.7% (eip-8037) — nothing below −100%, as expected.
- **The costlier tail runs far past +100%,** so it must be split, not lumped into one
  `[100, +inf)` bin. eip-8037 reached **+650%** (p99 = 147%, p99.9 = 222%): ~2.3% of its
  drill-ins fall in `[100, 200)` and ~0.4% above 200%. Hence the `[100, 200)`,
  `[200, 500)`, `[500, +inf)` split, with `[500, +inf)` as the real catch-all (a handful
  of rows). eip-8038 is milder (max +278%) but still crosses 100%.

Mass concentrates in `[10, 50)` (≈51% of eip-8037, ≈69% of eip-8038 drill-ins) and the
near-zero bins are sparse, so the fine `[1,10) / [10,25) / [25,50)` resolution is where it
pays off. The exact-zero case needs no bin — the `gas_only` cohort is `gas_delta != 0` by
definition.

**Caveat — drill-ins skew more extreme than the bulk cohort.** These edges were tuned on
the drill-in slice, which is *not* representative of the `gas_only` cohort the histogram
would actually cover: drill-ins are retained precisely because something diverged, so they
over-sample large relative changes. When the producer aggregates the full cohort, expect
more mass to pile into the low bins (`[-1,0) / [0,1) / [1,10)`) and the `≥100%` tail to be
thinner than the ~2.7% seen here. The two structural facts still hold regardless — the
−100% floor is arithmetic, and the tail must be split rather than capped at `[100, +inf)`
since deltas above 100% exist at all — so the edge set stands; only the per-bin occupancy
will shift toward the low end.

> **⚠️ REFUTED for eip-8037 — full-scale figures (measured 2026-07-30 over the
> complete 1,000,000-block v11 window; these SUPERSEDE the 50,000-block probe
> figures this annotation previously carried).** The prediction was directionally
> wrong, not just imprecise: the bulk `gas_only` cohort's `≥100%` tail is
> **fatter**, not thinner — **25.17%** of eip-8037's cohort versus the ~2.7% the
> caveat said it would fall below. Per-bin share of the `gas_only` cohort:
>
> | bin | eip-8037 | eip-8038 |
> | --- | --- | --- |
> | `[-100,-50)` | 0.0079% | 0.0001% |
> | `[-50,-25)` | 0.0745% | 1.4345% |
> | `[-25,-10)` | 2.2070% | 10.7607% |
> | `[-10,-1)` | 5.5083% | 7.9429% |
> | `[-1,0)` | 0.9689% | 0.3888% |
> | `[0,1)` | 1.0588% | 0.9377% |
> | `[1,10)` | **29.2585%** | 17.3429% |
> | `[10,25)` | 8.6373% | 10.3251% |
> | `[25,50)` | 16.4028% | **44.7865%** |
> | `[50,100)` | 10.7083% | 6.0796% |
> | `[100,200)` | 23.5645% | 0.0011% |
> | `[200,500)` | 1.5803% | 0 |
> | `[500,∞)` | 0.0230% | 0 |
> | **cohort n** | 38,414,804 | 119,031,666 |
> | **ratio-of-sums** | **+38.88%** | **+13.50%** |
> | **`≥100%` tail** | **25.17%** | 0.0011% |
> | **negative (cheaper)** | 8.77% | 20.53% |
>
> Two corrections to what the probe-scale figures suggested. First, eip-8037's
> **modal bin is `[1,10)` (29.26%), not `[100,200)`** — at probe scale `[100,200)`
> looked modal at 31.0%; at full scale it is the clear second at 23.56%. Second,
> and consequently, the earlier flat claim that mass did **not** "pile into the low
> bins" is **wrong at full scale**: `[0,1)` + `[1,10)` is 30.3%, so the caveat was
> *partly* right — the distribution is **bimodal**, piling into both the low bins
> and a heavy `≥100%` tail. What survives unchanged is the refutation that matters:
> the tail is an order of magnitude **fatter**, not thinner. For **eip-8038** the
> caveat held throughout: mass concentrates in `[25,50)` (44.79%), the `≥100%` tail
> is 0.0011%, nothing exceeds 200%. Lesson: the drill-in slice is unrepresentative
> in a **schedule-dependent direction**, so it cannot predict the bulk cohort's
> shape either way. The two **structural** claims stood — the `−100%` floor held
> (bounded by arithmetic; both schedules leave `[-100,-50)` nearly empty) and the
> tail did have to be split, which for eip-8037 matters far more than argued here.
> Nothing downstream hardcodes these figures; they are re-derived by every
> precompute run.

Fixed bin edges keep the array summable across blocks (same semantics as
`gas_delta_log2_hist`). This is purely additive on the producer side and needs no per-tx
drill-in rows on the consumer; the consumer just sums the arrays across the pinned
config/schedule/block range. If a distribution-free encoding is preferred over hand-picked
edges, a signed log2 histogram of `abs(gas_delta) / baseline_gas_used` (mirroring
`gas_delta_log2_hist`, one sign bit) would also capture the −100%-to-+650% range, at the
cost of coarser, non-round buckets.

## Notes

> **v11 outcome for this section.** The first bullet was **borne out**: v11 was
> purely additive on `block_summary` (43 → 54 columns), `_divergence` stayed at 113
> and `_block_coverage` at 22, and the tx partition is untouched — with **one
> exception this bullet did not anticipate**, the breaking `tx_count_creation`
> redefinition, which changed an existing column's meaning rather than adding one.
> The second bullet is **superseded**: the dashboard now shows both a
> transaction-type split and a gas-diff percentage distribution for the `gas_only`
> cohort. The `≥1024`-**gas** catch-all it mentions is the *absolute* histogram and
> is still exactly as described — that bin is unrelated to the percentage view, and
> the absolute histogram is kept alongside it because it is the only view spanning
> **both** the `gas_only` cohort and the drill-in members.

- Both recommendations are additive columns on `block_summary`; they don't change the
  existing partition or the `divergence` drill-in contract.
- Until they land, the dashboard shows: the state-gas driver mix for **both subsets**
  (the `gas_only` aggregate cohort and the drill-in members, each in its native
  taxonomy), the change-type mix, and an **absolute** gas-diff histogram with a
  `≥1024`-gas catch-all bin. It does **not** show a transaction-type split or a
  gas-diff **percentage** distribution, since neither is computable for the bulk of the
  group.
