# Producer data recommendations — "Succeeds with changes" deep dive

## Recommendation 1 — a transaction-type taxonomy for the `gas_only` cohort

**Problem.** From first principles, the natural way to read "who pays the repricing
tax" is by transaction *type*: simple ETH transfers vs contract calls vs contract
deployments vs EIP-7702 authorizations, and by EIP-2718 `tx_type`
(legacy / access-list / dynamic-fee / blob / set-code). None of these are available for
the `gas_only` cohort — only the four state-op driver counts, which describe *state gas*,
not transaction shape. A simple ETH transfer and an ERC-20 transfer can both land in
`no_state`, and `tx_count_creation` counts a *state-op* creation, not the tx-level
`is_create`.

**Proposed `block_summary` columns** (per `(block, class)`, alongside the existing
`tx_count_*` driver counts), populated from the same per-tx facts already visited when
building the aggregate:

| Column | Meaning |
| --- | --- |
| `tx_count_type_legacy / _access_list / _dynamic_fee / _blob / _set_code` | EIP-2718 `tx_type` histogram for the cohort |
| `tx_count_simple_transfer` | `is_create = false` **and** recipient set **and** empty calldata (`input_zero_bytes + input_nonzero_bytes = 0`) |
| `tx_count_contract_call` | `is_create = false` **and** non-empty calldata |
| `tx_count_contract_creation` | tx-level `is_create = true` (distinct from the state-op `tx_count_creation`) |

With these, the dashboard could show a tx-type breakdown covering **all** of G2. Today it
shows none, because the split is only computable per-tx (on the drill-in subset), which is
an unrepresentative slice of the group — so the categories would have to be derived by the
producer while aggregating the `gas_only` cohort.

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

**Proposed change.** Emit a **percentage** histogram alongside the existing absolute one,
computed by the producer while aggregating the cohort (where per-tx `baseline_gas_used`
and `gas_delta` are both in hand):

| Column | Meaning |
| --- | --- |
| `gas_diff_pct_hist` | Histogram of per-tx `100 * gas_delta / baseline_gas_used` over the class, using the fixed signed bin edges below |

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

Fixed bin edges keep the array summable across blocks (same semantics as
`gas_delta_log2_hist`). This is purely additive on the producer side and needs no per-tx
drill-in rows on the consumer; the consumer just sums the arrays across the pinned
config/schedule/block range. If a distribution-free encoding is preferred over hand-picked
edges, a signed log2 histogram of `abs(gas_delta) / baseline_gas_used` (mirroring
`gas_delta_log2_hist`, one sign bit) would also capture the −100%-to-+650% range, at the
cost of coarser, non-round buckets.

## Notes

- Both recommendations are additive columns on `block_summary`; they don't change the
  existing partition or the `divergence` drill-in contract.
- Until they land, the dashboard shows: the state-gas driver mix for **both subsets**
  (the `gas_only` aggregate cohort and the drill-in members, each in its native
  taxonomy), the change-type mix, and an **absolute** gas-diff histogram with a
  `≥1024`-gas catch-all bin. It does **not** show a transaction-type split or a
  gas-diff **percentage** distribution, since neither is computable for the bulk of the
  group.
