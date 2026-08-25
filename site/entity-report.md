# Repricing impact outreach report: projects to contact about EIP 8037 and EIP 8038

This report identifies projects that may need to act before the proposed gas
repricings in **EIP 8037** and **EIP 8038**. It explains what could break, why it
could break, and who can fix it. The findings come from the dashboard's
precomputed **G4 "Potentially broken"** data (`site/data/{schedule}/affected/`).

**What G4 means.** A G4 transaction succeeds under current mainnet gas costs but
fails under the proposed schedule. Raising its gas limit does not help
(`min_multiplier_to_succeed IS NULL`). The cause may be a fixed gas stipend, a
gas sensitive branch, a 63/64 call forwarding shortfall, or a profitability or
slippage check. Transactions that only need a higher gas limit are G3 and are
not included here.

**The two EIPs.**

1. **[EIP 8037](https://eips.ethereum.org/EIPS/eip-8037) prices persistent state
   growth.** It replaces several flat charges with a common price of 1,530 gas
   for each byte added to Ethereum's state. Creating a storage slot is treated
   as 64 bytes, creating an account as 120 bytes, and adding an EIP 7702
   delegation as 23 bytes. This raises the state creation portion of a new
   storage slot from 20,000 to 97,920 gas and a new account from 25,000 to
   183,600 gas. Deployed contract code rises from 200 to 1,530 gas per byte.
   These costs apply to `SSTORE`, `CREATE`, `CREATE2`, calls or `SELFDESTRUCT`
   operations that create accounts, and EIP 7702 authorizations.

   EIP 8037 also separates transaction accounting into execution gas and state
   gas. Both are paid by the transaction, but state gas first draws from a
   reservoir above the execution gas budget. If an operation that created state
   is reverted, its state gas is returned. This allows large deployments without
   weakening the transaction cap on execution work, while giving each block a
   separate limit for state growth.

2. **[EIP 8038](https://eips.ethereum.org/EIPS/eip-8038) reprices access to and
   modification of existing state.** It separates the cost of a state operation
   into access, write, and creation components. Cold account access rises from
   2,600 to 3,000 gas, while cold storage access stays at 2,100 and warm access
   stays at 100. A storage write rises from an equivalent 2,800 to 10,000 gas,
   and an account write rises from 6,700 to 9,000. The access and write portion
   of `CREATE` and `CREATE2` rises from 7,000 to 12,000 gas. A value carrying
   `CALL` costs 11,300 gas, including the unchanged 2,300 gas call stipend.

   Access list entries also rise from 2,400 to 2,900 gas per address and from
   1,900 to 2,000 per storage key. `EXTCODESIZE` and `EXTCODECOPY` pay an extra
   warm access charge because they require a second database read. Storage writes
   are net metered across the transaction, so restoring a slot to its original
   value returns the write charge. Clearing a slot gives an 11,616 gas refund,
   subject to the existing refund cap. EIP 8037 supplies the separate creation
   charge when the operation adds a new account, storage slot, or contract code.

## How to read this list (important framing)

The affected contracts fall into three groups:

1. **Protocol failures that teams can address.** These affect account abstraction
   EntryPoints, wallet implementations, bridges, and settlement or aggregation
   routers. These are the main outreach targets.
2. **Immutable tokens where a failure happens to surface.** USDC, USDT, DAI,
   stETH, Aave aTokens, GHO, and XEN often appear because a call fails inside
   their transfer code after another contract calls them. The token is usually
   not the cause and often cannot be changed. Contact the integrator instead.
3. **Expected economic changes.** Many DEX and pool failures occur when an MEV
   or arbitrage bot no longer finds a trade profitable, or when its internal gas
   check fails. Searchers should adjust this traffic themselves. These findings
   belong in the watchlist unless attribution shows a protocol defect.

Two caveats apply. Counts for busy contracts are lower bounds because the
producer limits detailed records per block. Many contract records also lack a
`failure_rate` because no Xatu transaction denominator is available. In those
cases, this report gives absolute G4 counts only.

---

## Prioritized outreach summary

| Priority | Entity | Category | Worst EIP | Headline impact | Who fixes it |
| --- | --- | --- | --- | --- | --- |
| **HIGH** | eth-infinitism (ERC 4337 EntryPoint v0.6/v0.7/v0.8) | Account abstraction infrastructure | v0.6/v0.8: 8037; **v0.7: 8038** | 3,223,831 G4 role hits on v0.7 alone; v0.8 revert rate 68.2% under 8037; about 150,400 wallet deployments OOG under 8037 | Bundlers and SDKs, plus smart account and factory authors |
| **HIGH** | ZeroDev (Kernel + WeightedECDSAValidator) | Account abstraction | 8037 | ~379k combined OOG halts + reverts, all from a first-touch SSTORE inside `handleOps` exceeding `callGasLimit` | ZeroDev; new implementation + account migration |
| **HIGH** | Across (SpokePools) | Bridge | 8037 | 102,653 unrescuable txs across both SpokePools; state-heavy call-chain OOG + shared gas-forwarding revert | Across governance; UUPS upgrade of both proxies |
| **HIGH** | Socket / Bungee (SocketBatcher) | Bridge aggregator | 8037 | ~18.9% of real SocketBatcher traffic unrescuably broken (16,717 txs) vs 9.24% under 8038 (8,173) | Socket; architectural fix (immutable batcher, no admin key) |
| **HIGH** | CoW Protocol (GPv2Settlement) | DEX settlement | 8037 (8038: none) | ~6.71% of real settlement traffic (105,390/1,569,513 tx) OOG on SSTORE inside paid-out tokens | CoW solver/driver batch-composition limits |
| **HIGH** | 0x Protocol (Settler) | DEX aggregator | 8038 | 11,708 unrescuable txs (177x eip-8037's 66), ~32% of affected volume unfixable; access-list/cold-EXTCODESIZE via flashloan-arb routers | 0x; new Settler version + routing/solver recalibration |
| Medium | Alchemy (Modular Account) | Account abstraction | 8038 (volume); 8037 (severity) | 54,004 unrescuable failures via EntryPoint v0.7; SSTORE OOG (8037) / EXTCODECOPY-site revert (8038) | Alchemy; new implementation + migration |

**Outreach priorities.** The actionable targets are eth infinitism, ZeroDev,
Alchemy, Across, Socket, CoW Protocol, and 0x Protocol. Each has an identifiable
owner and a plausible contract, implementation, routing, or operational change.

### Validate before outreach

| Entity | Evidence | What remains unclear |
| --- | --- | --- |
| 1inch Aggregation Router v6 | 40,727 combined G4 rows, mostly at `EXTCODESIZE` | Almost no affected transaction enters through the router directly. Confirm whether the revert belongs to router validation or to caller profitability and slippage logic. |
| Sushi labeled cohort | 82,498 reverts across more than 386 addresses | The dominant signature matches a pattern attributed to searcher bots elsewhere in this report. Confirm bytecode ownership and the failing frame before calling this a Sushi template defect. |

---

## Detailed analyses

### Account abstraction

## eth-infinitism (ERC-4337 EntryPoint)

**What it is.** The canonical ERC-4337 account-abstraction EntryPoint singletons maintained by eth-infinitism. Every smart-account UserOperation on mainnet is bundled through `handleOps` on one of these three contracts (v0.6, v0.7, v0.8). Across the current 4,000,000-block window (2024-12-03 → 2026-06-15), EntryPoint v0.7 alone racks up **3,223,831** G4 role-hits (entry + halt-site + revert-site summed across both EIPs); by a wide margin the single largest-impact entity in the whole dataset.

**Contracts and roles** (all immutable singletons; `is_proxy=false`, `is_upgradable=false`):
- **EntryPoint v0.7** (`0x0000…da032`); 8037: entry 834,298; oog_site 1,894; revert_site 487,638 (footprint 1,323,830). 8038: entry 969,142; oog_site 0; revert_site 930,859 (footprint 1,900,001).
- **EntryPoint v0.6** (`0x5ff1…d2789`); 8037: entry 451,253; oog_site 20,773; revert_site 356,275 (footprint 828,301). 8038: entry 36,913; oog_site 1; revert_site 36,882 (footprint 73,796).
- **EntryPoint v0.8** (`0x4337…ff108`); 8037: entry 307,305; oog_site 13; revert_site 296,308 (footprint 603,626). 8038: entry 14,027; oog_site 0; revert_site 13,975 (footprint 28,002).

The failing selector in every shard is the batch handler `0x765e827f`/`0x1fad948c` (`handleOps`). Almost every failure lands back inside the EntryPoint itself at call_depth 2, `opcode: JUMPDEST` or `PUSH0`, `revert_decoded: custom:0x220266b6`; the EntryPoint's own post-execution "FailedOp"/gas-accounting guard, not a genuine out-of-gas.

**Scale and failure rate** (real `handleOps` denominators from the Xatu cross-check; the denominators themselves grew with the wider window, so compare rates, not raw totals, against the prior edition of this report):
- v0.7; total_tx 2,404,281. 8037: halt_rate 0.079%, revert_rate **20.3%**. 8038: halt_rate 0.0%, revert_rate **38.7%**.
- v0.6; total_tx 1,391,247. 8037: halt_rate 1.5%, revert_rate **25.6%**. 8038: halt_rate ~0.0001%, revert_rate **2.7%**.
- v0.8; total_tx 434,192. 8037: halt_rate 0.003%, revert_rate **68.2%**. 8038: halt_rate 0.0%, revert_rate **3.2%**.

**How it breaks.**
1. **Validation-time FailedOp reverts (bulk, both EIPs, all three contracts).** The dominant cluster everywhere is entry+revert_site, `JUMPDEST`/`PUSH0`, call_depth 2, `custom:0x220266b6`, inside the EntryPoint's own code (e.g. v0.7/8038: 930,859 txs, 100% of the role; v0.7/8037: 437,101 txs, 52.4%; v0.8/8037: 236,043 + 42,692 = 90.7%). `handleOps` compares actual post-execution gas cost against the bundler-supplied verification/prefund figures; any repriced schedule that shifts gas enough; in either direction; trips this sanity check and reverts the whole batched op, even when nothing genuinely ran out of gas. This is a revert, not a halt, so a gas-limit bump can never rescue it (by construction, G4 requires `min_multiplier_to_succeed IS NULL`).
2. **EIP-8037 authorization surcharge (7702); concentrated in v0.8.** Every top v0.8/8037 cluster carries an `authorization` driver tag (11,346 / 42,692 / 6,769 / 11,661; the 42,692-count `PUSH0` cluster remains **100% authorization-tagged**), together accounting for the large majority of v0.8's 68% revert rate. v0.7 shows the same driver but a smaller share (199,844 of 437,101 in the top cluster, plus smaller counts in the Kernel/EntryPoint SSTORE clusters). v0.6 shows none; it predates 7702 support entirely.
3. **EIP-8037 SSTORE OOG halts inside smart accounts (v0.7 and v0.6, not v0.8).** v0.7: halts now land inside three deployed Kernel smart-account instances (`0xbac849bb…`, `0xd6cedde8…`, `0xd830d15d…`, 61,172+55,060+31,242 = 147,474 in the top clusters alone) plus SemiModularAccountBytecode (11,989) and WeightedECDSAValidator (11,739); genuine `FixedGas`/`FractionalGas` OOG on nonce/session-key storage writes during account execution. v0.6: a single unlabeled contract `0x00000110dcdedc9581cb5ecb8467282f2926534d` now absorbs 41,758 of the v0.6 OOG halts, plus a second unlabeled contract `0x000100abaad02f1cfc8bbe32bd5a564817339e72` (18,757) and 12,502+8,162 OOG SSTORE halts inside the EntryPoint itself. v0.8 has essentially none (13 of 307,305 entries, 0.004%); its failures are revert-only.
4. **Fresh deployment OOG: 150,415 accounts under EIP 8037** (up from 128,579). These smart account wallets run out of gas during `CREATE2` construction. Of them, 111,819 halt on the final `RETURN` during code deposit and 38,710 halt on an `SSTORE` in the constructor. Initcode prefixes `0x6100` (68,651 accounts, full constructors) and `0x603d` (63,773 accounts, mostly EIP 1167 minimal proxies) dominate. EntryPoint v0.7 deploys 123,401 of these accounts and v0.6 deploys 20,032. The median gas delta is ‑423,617. EIP 8038 has no equivalent cases.

**Which EIP hurts more.** For v0.6 and v0.8, EIP 8037 has the larger effect. The v0.6 footprint is 828,301 under 8037 and 73,796 under 8038. The v0.8 footprint is 603,626 under 8037 and 28,002 under 8038. The authorization surcharge drives much of the v0.8 result.

For v0.7, EIP 8038 has the larger effect. Its footprint is 1,900,001 under 8038 and 1,323,830 under 8037. The revert rates are 38.7% and 20.3% — both up slightly from the prior edition of this report, and the gap between the two schedules is essentially unchanged. The same transaction population is used for both schedules. One possible cause is v0.7's heavy use of storage writes and value transfers during validation. This should be checked against the `account_write` and `call_value` parameters.

**Can they fix it?** The deployed EntryPoints are immutable, so none of the G4
transactions can be rescued by changing the submitted transaction gas limit.
Bundler re simulation and better gas estimates can reduce G3 failures, but they
do not fix the G4 cohort described here. G4 remediation requires changes to
future EntryPoint, smart account, factory, or validator implementations. These
changes may include revising the FailedOp accounting guard and reducing or
rebudgeting constructor code deposit and first storage writes. Existing accounts
may need to migrate to those implementations.

**Priority: HIGH.** EntryPoint is the largest entity in the dataset by transaction count and is central to ERC 4337 activity. The contracts cannot be patched in place. EntryPoint v0.7 also has a high failure rate under EIP 8038.

**Suggested outreach.** "Our replay shows many `handleOps` calls reverting under both EIP 8037 and EIP 8038 because the EntryPoint gas accounting guard fails after execution costs change. For v0.8 and v0.6, EIP 8037 produces revert rates of 68% and 26%. For v0.7, EIP 8038 produces a 38.7% revert rate, compared with 20.3% under EIP 8037. We would value your view on whether `account_write` and `call_value` explain the v0.7 result. We also found about 150,400 smart account deployments that run out of gas during `CREATE2` construction under EIP 8037. Gas estimation may reduce fixable G3 failures, but it cannot rescue these G4 cases. We would like to discuss changes for future EntryPoint, account, factory, and validator implementations, along with migration guidance for deployed accounts."

## ZeroDev (Kernel smart accounts)

**What it is.** ZeroDev's Kernel smart-account implementation contracts and their WeightedECDSAValidator module, part of an ERC-4337 account-abstraction stack. None of the five addresses is ever the tx `recipient` in this dataset; they appear purely as internal **halt** and **revert** *sites*, one to two call frames inside `EntryPoint.handleOps` batches (`ERC-4337 EntryPoint v0.7`, `0x0000000071727de22e5e9d8baf0edac6f37da032`, is the entry contract for essentially every affected row across all five shards).

**Contracts and roles** (OLI high-confidence, all `is_proxy: false`, `is_upgradable: false`):
- **Kernel** `0xbac849bb641841b44e965fb01a4bf5f074f84b4d`; 8037: `oog_site` halt_count 126,266 + `revert_site` revert_count 126,308 (252,574 total). 8038: `revert_site` revert_count 178 only; **zero** OOG halts.
- **Kernel** `0xd6cedde84be40893d153be9d467cd6ad37875b28`; 8037: halt 32,623 + revert 32,623 (65,246 total). 8038: revert 5.
- **Kernel** `0xd830d15d3dc0c269f3dbaa0f3e8626d33cfdabe1`; 8037: halt 15,553 + revert 15,553 (31,106 total). 8038: **no shard at all** (zero footprint).
- **Kernel** `0x94f097e1ebeb4eca3aae54cabb08905b239a7d27`; 8037: halt 3,066 + revert 3,100 (6,166 total). 8038: revert 38.
- **WeightedECDSAValidator** `0xed89244160cfe273800b58b1b534031699dfeeee`; 8037: halt 11,794 + revert 11,794 (23,588 total). 8038: no shard.

Summed, the four Kernel addresses carry **355,092** entry+halt+revert rows under eip-8037 and **221** under eip-8038; the validator module carries **23,588** under eip-8037 and **0** under eip-8038.

**Scale and failure rate.** `failure_rate` is `null` in every shard's `context`; these contracts are never a Xatu-trackable `recipient`, only internal sites, so there's no mainnet-traffic denominator. Block coverage shows this is a **chronic, whole-window** phenomenon, not a recent spike: Kernel `0xbac849b…` alone spans 112,845 distinct blocks from block 21,321,733 to 25,319,866; essentially the full 4M-block range from near its very first block. The other four addresses show the same full-range persistence (28,672 / 15,216 / 3,083 / 11,735 distinct blocks respectively).

**How it breaks and why.** One shape dominates every 8037 shard, and it repeats almost identically across all five contracts: an **SSTORE that is the transaction's very first storage write** (`drivers.sstore.p50 = 0` completed writes before it) fails inside the Kernel/validator dispatch path, one call frame below EntryPoint (`call_depth` 3 for Kernel, 4 for the validator, since it's called one hop deeper by Kernel). It surfaces as two mirror-image, near-equal-sized clusters: an **`oog_site`** cluster (`pattern_or_reason: storage_heavy`, split between `oog_bottleneck_kind: FractionalGas` and `FixedGas`; e.g. Kernel `0xbac849b…`: 61,172 FractionalGas / 55,060 FixedGas; 48%/44% of its OOG halts), and a same-count **`revert_site`** cluster one frame up, decoding to an **undecoded custom error `0x65c8fd4d`** (Kernel) or `0x220266b6` (deeper EXTCODESIZE-adjacent clusters). Drivers are consistent and small: `cold_account` p50 3 (Kernel) / 3 (validator), `sload` p50 6 to 12, `sstore` p50 0. Critically, `reservoir_exhausted_share` and `spillover_share` are **0.0 across every dominant cluster**; this is not the shared per-block state reservoir running dry, it's simply that the repriced cost of this one first-touch SSTORE, alone, now exceeds the UserOp's fixed `callGasLimit`. `gas_delta` is consistently **negative** (Kernel `0xbac849b…` avg −76,996 to −78,793 across the top clusters, p50 around −50,000; validator avg −106,797, p50 −100,359): the schedule run consumes *less* total gas than baseline before terminating; exactly what an early-abort revert/OOG looks like next to a baseline run that completed successfully. Notably, `state_gas_category: authorization` (the EIP-7702 tag) appears in only a **tiny minority** of rows in every cluster; 95 of 126,232 for Kernel `0xbac849b…` (0.08%), 47 of 11,739 for the validator (0.40%), 3 to 70 elsewhere: despite eip-8037 being pitched as targeting account creation / CREATE / CREATE2 / EIP-7702 authorization, the bulk of ZeroDev's breakage is **plain first-write SSTORE cost growth**, not authorization-tuple overhead. A gas-limit bump can never rescue any of this by construction; every row is G4 (confirmed by `g3_tx_count: 0` in every shard's `context`).

**Which EIP hurts more.** Overwhelmingly **eip-8037**. Under the revised, softer eip-8038, one of the five shards (`0xd830d15d…`, and the validator module) has **no shard at all**; zero footprint; and the other three carry a combined **221** reverts total, all rare edge cases. No eip-8038 shard shows more than one OOG halt; consistent with the eip-8038 spec revision (net less-breaking dataset-wide) and confirming ZeroDev's exposure is almost entirely a state-creation, not state-access, problem.

**Can they fix it?** `is_proxy: false`, `is_upgradable: false` for all five addresses; these are immutable implementation/module contracts. Fix has to come from ZeroDev: ship new Kernel and WeightedECDSAValidator implementations that budget more headroom around the first storage write in the validate/execute path, then get existing deployed smart accounts to point at the new implementation; which for many already-deployed Kernel accounts means a wallet-level migration, since the flagged contracts themselves cannot be upgraded in place.

**Priority: HIGH.** This is core ERC-4337 account-abstraction infrastructure, not an edge integration: 189K+ OOG halts and 189K+ mirrored reverts under eip-8037 (up from ~128K in the prior edition of this report), spread continuously across the entire 4M-block/~18-month window and reproduced near-identically across four Kernel address variants plus the shared validator module, on the single most basic action a smart account does (its first storage write during a UserOp). eip-8038 remains a near-non-issue for ZeroDev (221 rows total); eip-8037 as currently specified would make a large and growing share of routine Kernel UserOps unexecutable.

**Suggested outreach.** "Under EIP-8037 as currently specified, our replay of 4M mainnet blocks shows Kernel smart-account executions and WeightedECDSAValidator validations failing roughly 355K and 24K times respectively; consistently from the *first* storage write inside a UserOp's validate/execute path exceeding the account's `callGasLimit`, not from EIP-7702 authorization overhead. None of these are gas-limit-fixable; they're outright breaks. The same accounts show only 221 failures total under the revised EIP-8038 parameters, so this is specifically an EIP-8037 state-creation-surcharge issue on ordinary storage writes, worth flagging before EIP-8037 finalizes."

## Alchemy (Modular Account)

**What it is.** `SemiModularAccountBytecode` (`0x000000000000c5a9089039570dd36455b5c07383`), Alchemy's ERC-4337 smart-account implementation bytecode (`is_proxy: false`, `is_factory: false`, `is_upgradable: false`). Every failing transaction in both shards is routed through EntryPoint v0.7 (99.70% of 8037 calls, 99.96% of 8038 calls).

**Contracts and roles.** 8037: `oog_site.halt_count = 12,023` + `revert_site.revert_count = 12,023`; the same 12,023 failing calls tagged in both facets (every OOG halt is also recorded as a revert). 8038: `revert_site.revert_count = 29,958` only; **no `oog_site` role at all**; every one of this contract's 8038 failures is a non-OOG revert. Combined footprint = 24,046 (8037) + 29,958 (8038) = 54,004, split roughly 45/55.

**Scale and failure rate.** `context.failure_rate` is `null` in both shards. 8037 failures cluster tightly; 12,023 rows over just 11,581 distinct blocks (blocks 23,026,278 → 25,319,783), ~1.04/block. 8038 failures spread far wider; 29,958 rows over 28,170 distinct blocks (22,047,467 → 25,319,783), ~1.06/block but touching 2.43x as many blocks; a steadier, more continuous drip rather than 8037's tighter concentration.

**How it breaks.**
- **8037 (genuine OOG):** 99.72% of failures (11,989 of 12,023) are one cluster: selector `0x765e827f`, halting on **SSTORE** at call depth 5, `oog_bottleneck_kind: FractionalGas`, `pattern_or_reason: storage_heavy`. Drivers show `sstore.p50 = 0` completed writes at the halt point (it's the *first* SSTORE attempted) after `cold_account.p50 = 2` and `sload.p50 = 8`. Both `reservoir_exhausted_share` and `spillover_share` are `0.0`; this is a plain gas-limit shortfall from the costlier SSTORE, not a per-tx state-reservoir exhaustion event.
- **8038 (controlled revert, not OOG):** 99.96% of failures (29,947 of 29,958) are one cluster: same entry selector `0x765e827f`, but this time the divergence lands on **EXTCODECOPY** at call depth 4; one frame higher than the 8037 SSTORE site; with `kind: non_oog` and a distinct custom error `0x220266b6`. No OOG role exists in this shard at all; a deliberate revert rather than the EVM running out of gas mid-opcode, consistent with a gas-sufficiency check somewhere in the EntryPoint/account validation path tripping once per-call costs shift under the revised schedule. Gas deltas skew net negative on average (`avg -63,378`) but the `p90` is **positive** (`+2,954`); about 10% of these calls are genuinely more expensive under the revised 8038.

**Which EIP hurts more.** By raw footprint, 8038 edges out 8037 for this contract specifically (29,958 vs 24,046, +25%), over a much broader span of blocks; a notable exception to the dataset-wide trend that the revised 8038 spec is measurably less breaking overall, and essentially unchanged from the prior edition of this report. Mechanistically the two are different failure classes: 8037 produces genuine, narrowly-clustered OOG halts on a single SSTORE site; 8038 produces broader, steadier non-OOG reverts on an EXTCODECOPY-adjacent check.

**Can they fix it?** Implementation bytecode is immutable (`is_upgradable: false`), but Alchemy can ship a new implementation for future accounts and migrate. Because both failure sites are inside the account's own execution/validation path rather than a downstream dependency, an implementation-level fix is plausible without protocol-level changes.

**Priority: Medium.** ~54.0k affected calls across a widely-used AA wallet implementation is meaningful in absolute terms, but the mechanism is well-localized (one dominant selector per schedule) and all of it is already unrescuable by gas-limit changes; the actionable lever is implementation-side.

**Suggested outreach.** "Our repricing-impact analysis over 4M mainnet blocks (2024-12-03 → 2026-06-15) shows Alchemy's `SemiModularAccountBytecode` implementation hitting ~54.0k unrescuable failures split roughly evenly across the two candidate EVM gas repricings; under EIP-8037 as a genuine OOG on the account's first SSTORE (call depth 5), and under the revised EIP-8038 as a controlled revert one frame earlier, around an EXTCODECOPY check (call depth 4), both traced through EntryPoint v0.7. Neither is fixable by users raising their gas limit. We'd like to walk through the specific call traces so you can assess whether the account's internal write ordering or gas-sufficiency checks can be adjusted in a future implementation."

---

### Bridges

## Across (bridge)

**What it is.** Across is an intents-based cross-chain bridge; these are its Ethereum-side SpokePool contracts, which custody bridged funds and settle relayer fills/deposits. A broken fill/settlement call can leave bridged funds stuck mid-transit; a direct funds/UX risk.

**Contracts and roles** (both `is_proxy: true`, `is_upgradable: true`, `uups`):
- **Ethereum_SpokePool** `0x5e5b726c81f43b953a62ad87e2835c85c4d9dd3b`; eip-8037: `oog_site` halt_count 32,962 + `revert_site` revert_count 34,359 (37 distinct failure clusters, top-8 cover 98.97%). eip-8038: `revert_site` revert_count 6 only (no `oog_site`).
- **ETH Spokepool** `0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5` (`category: bridge`, `owner_project: across`); eip-8037: `revert_site` revert_count 35,332, **zero** `oog_site` (6 distinct clusters, all shown). eip-8038: `revert_site` revert_count 12.

Neither contract carries an `entry` role; the tx `recipient` in every affected row is a separate, generically-labeled `Proxy contract` at `0x9ccc2f3ecde026230e11a5c8799ac7524f2bb294`, the near-only caller into both SpokePools. Both are reached mid-call-chain, not as the tx's direct target.

**Scale and failure rate.** Combined unrescuable (G4) footprint: **102,653 txs under eip-8037** vs **18 txs under eip-8038** — essentially unchanged from the prior edition of this report (102,643 / 7) despite the window nearly doubling. No Xatu total-tx denominator resolved for either address, so no halt-rate/revert-rate percentage; only raw counts. Ethereum_SpokePool's `context` shows `g3_tx_count: 0, g2_drillin_tx_count: 0, af_tx_count: 0`; under eip-8037, **every** divergence through this contract is G4; none are gas-limit-fixable. ETH Spokepool is different: `g3_tx_count: 1,457,490` alongside its 35,332 G4; the large majority of its divergences ARE rescuable with a gas-limit bump, and only this 35,332-tx tail is genuinely stuck.

**How it breaks.** Under eip-8037, the dominant Ethereum_SpokePool mode is a single failing selector (`0x1bc74526`, undecoded) hitting a `CALL` at call-depth 5 in a multi-hop chain; **~30,949 txs (46% of the contract's G4, 94% of its OOG-tagged rows)**; tagged `oog_pattern: call_chain` / `oog_bottleneck_kind: FractionalGas` (gas exhausted progressively across the call chain) and reverting with custom error `0x93cfa3ee`. Secondary modes: an `SSTORE`-triggered `storage_heavy` OOG (1,084 txs) and a `PUSH2`/custom-error-`0x77ebef4d` non-OOG revert at depth 6 (1,095 txs); the same custom error that dominates ETH Spokepool. ETH Spokepool's failures are, notably, **100% non-OOG reverts**. The dominant cluster (**34,779 txs, 98.4% of its G4**) diverges at a `RETURNDATASIZE` opcode at depth 5 and reverts with custom error `0x77ebef4d`; the classic signature of a sub-call whose forwarded gas stipend is no longer sufficient under the repriced state costs: the inner call fails, returns no data, and the SpokePool's own `RETURNDATASIZE == 0` check reverts. Median `gas_delta` for this cluster is **-13,382** (the tx is cheaper on paper, identical to the prior edition of this report); it isn't a global gas shortfall, it's an internal gas-forwarding envelope becoming too tight for the callee's now-costlier state operations. All rows here are G4 by construction (`min_multiplier_to_succeed` NULL); bumping the gas limit, even to the observed 10× ceiling, never rescues them. Every cluster-level number above is essentially identical to the prior edition of this report; the newly-added ~1.9M blocks of history (back to 2024-12-03) added almost nothing to this failure mode, which appears concentrated in the more recent part of the window.

**Which EIP hurts more.** Overwhelmingly **eip-8037**. Combined footprint is 102,653 (eip-8037) vs 18 (eip-8038); a >5,700:1 ratio. The revised eip-8038 parameters are essentially non-breaking for Across's SpokePools, whereas the unchanged eip-8037 state-creation repricing remains a major hazard for its multi-hop relay-fill path.

**Can they fix it?** Yes, mechanically. Both SpokePools are UUPS-upgradable proxies, so Across governance can ship a logic upgrade; e.g., raising internal sub-call gas stipends / removing tight `RETURNDATASIZE`-gated call-failure checks that assume today's gas costs, and auditing the depth-5 call chain that dominates the OOG cluster for state-access reduction.

**Priority: HIGH.** A top-tier bridge with ~102.7k unrescuable transactions concentrated in its own relay-settlement logic under eip-8037, all unfixable by a gas-limit bump alone, with a fund-custody/UX blast radius if fills or deposit settlements can't complete.

**Suggested outreach.** "Under the current EIP-8037 (state-creation repricing) draft, our replay data shows ~102,700 mainnet transactions that succeed today but would revert unrecoverably (not fixable by raising the gas limit) across your two Ethereum SpokePool contracts; concentrated in a single relay-fill call path (selector `0x1bc74526`) that runs out of gas across a multi-hop internal call chain, plus a widespread `RETURNDATASIZE`/custom-error-`0x77ebef4d` revert pattern consistent with an internal gas-forwarding stipend becoming insufficient. EIP-8038 in its revised form is not a concern (18 transactions total). Since both SpokePools are UUPS-upgradable, we'd welcome a conversation about the specific call path and stipend sizing before EIP-8037 advances, to scope a fix."

## Socket

**What it is.** `SocketBatcher` (`0x87be3fc3edfe10cb8ce1244d6a1969fc55f9f83c`), the batch-execution entrypoint for the Socket / Bungee bridge aggregator. Every failure in both schedules routes through the single selector `0xfa98a33f`, fanning a batch out through a deep `DELEGATECALL`/`CALL` chain (depth 5 to 9). `is_proxy: false`, `is_upgradable: false`; immutable, non-proxy.

**Contracts and roles.**
- **8037**: `roles_summary` has **only `entry`**; `g4_tx_count: 16,717`, **100% OOG**. No `oog_site`/`revert_site` role; the halt always lands *inside* a delegatecall target, never at SocketBatcher's own frame.
- **8038**: `roles_summary` has **both `entry` (8,173) and `revert_site` (8,173)**; the same cohort viewed two ways: as *entry*, flagged OOG somewhere downstream; as *revert_site*, a plain revert **at SocketBatcher's own `EXTCODESIZE` frame** (`revert_decoded: "empty"` for 91.9%, `"TRANSFER_FAILED"` for 5.6%, `custom:0x90b8ec18` for 2.5%).

Downstream halt/delegatecall targets in both schedules: an unlabeled contract `0x407be335…8cf3` (dominant, 35 to 77% share), `FiatTokenV2_2` (USDC's implementation), `WETH`, `Wrapped BTC`, `Tether USDT`, and a few unlabeled adapters.

**Scale and failure rate** (real denominator: **88,463** total mainnet txs to SocketBatcher over the full window, identical in both shards — up from 53,055 as the window extended back to 2024-12-03).
- **8037: 16,717 G4 entry txs ≈ 18.90%** of all real SocketBatcher traffic — essentially unchanged from the prior edition's 19.06%. The *published* `context.failure_rate` object reads halt_rate 0.0/revert_rate 0.0, because that field is scoped to SocketBatcher as the halt/revert *site*, a role it never holds in 8037. The 0.0/0.0 headline is a schema artifact, not a real-world all-clear; the true entry-side break rate is roughly **1 in 5**.
- **8038: `revert_rate = 9.24%`** (8,173 / 88,463); down from 10.67% in the prior edition — the gap between the two schedules widened slightly as the window grew.
- G3 (fixable) counts diverge sharply too: 10,194 under 8037 vs only 1,102 under 8038.

**Which EIP hurts more.** **8037 is now the worse schedule for Socket**; 16,717 unrescuable G4 txs vs 8,173 under revised 8038 (≈2.0×), and by the real entry-side rate (18.9% vs 9.2%). `context.gas_delta.avg` dropped from **−86,212 gas/tx** under 8037 to **−6,628 gas/tx** under 8038; the 8038 spec revision measurably softened its bite here too, while 8037's spec, unchanged, keeps producing far more breaks. (Note: the raw index footprints, 16,717 vs 16,346, look "roughly balanced" but the 8038 figure double-counts the same 8,173 txs once as `entry` and once as `revert_site`; the real unique-tx comparison clearly favors 8037 as worse.)

**How it breaks.**
- **8037 (state-reservoir depletion):** all 16,717 failures are pure OOG, opcode `SSTORE` (`storage_heavy`) or `PUSH1` (`loop`), `oog_bottleneck_kind: FractionalGas`, call depth 5 to 6. The batch accumulates too much cumulative state-access cost across the token adapters it touches before completing. `reservoir_exhausted_share`/`spillover_share` are 0.0 across every cluster; this reads as straightforward cumulative-gas exhaustion from repriced per-op state costs, not a flagged reservoir-spillover event.
- **8038 (opcode-level repricing):** the same downstream OOG pattern exists, but because marginal costs are smaller, most OOG happens deep enough that it surfaces back at SocketBatcher's own frame as a decoded revert instead: `EXTCODESIZE` → empty returndata, `TRANSFER_FAILED`, one custom error.
- In both schedules the gas-limit bump never helps; this is G4 by construction.

**Can they fix it?** No; immutable, non-upgradable, no admin key. The fix is architectural: shrink batch size, split large batches into multiple transactions with independent gas budgets, or supply access lists / pre-warm touched storage slots.

**Priority: HIGH.** A widely used bridge aggregator whose batch executor already fails roughly 1 in 5 of its real transactions under 8037's current spec, none of it fixable by the caller.

**Suggested outreach.** "Under EIP-8037's current spec, our replay shows `SocketBatcher` (`0x87be3fc3…9f83c`, selector `0xfa98a33f`) failing on roughly 16,717 of 88,463 real mainnet transactions (~19%) over the last 4M blocks; batches that succeed today and become permanently unrescuable because cumulative state-access costs across the token adapters they touch (USDC, WETH, WBTC, USDT, and others) exceed the tx's gas budget deep in the call chain. EIP-8038's revised parameters cut this to ~8,173 failures (~9%, most surfacing as a decoded revert at your own contract rather than a silent OOG), but neither number is small for an immutable, non-upgradable batcher. We'd like to walk through the per-cluster breakdown with your team to see whether reducing per-tx batch depth or adding access-list pre-warming for the heaviest adapters is viable before either repricing ships."

---

### DEX settlement & aggregation routers

## CoW Protocol

**What it is.** GPv2Settlement (`0x9008d19f58aabd9ed0d60971565aa8510560ab41`), CoW Protocol's single settlement contract. Every failure is a batch settlement (`settle`, selector `0x13d79a0b`) dying mid-execution while paying out one of the many tokens in the batch.

**Contracts and roles.** eip-8037 only; the `affected/` shard for eip-8038 does not exist for this address, so CoW registers zero Potentially-broken impact under the revised schedule. Under eip-8037, CoW's only role is `entry`: 105,390 G4 tx, of which 105,388 are OOG halts and only 2 are non-OOG reverts (99.998% OOG). GPv2Settlement never appears as a halt site or revert site for anyone else's transactions.

**Scale and failure rate** (EIP 8037). There are 105,390 G4 transactions across 104,378 blocks from 21,319,990 to 25,319,882. Xatu records 1,569,513 mainnet transactions sent to GPv2Settlement over the same range, so about **6.71% of settlement traffic** falls into G4 — up slightly from 6.46% in the prior edition of this report. CoW accounts for about **3.92%** of the full EIP 8037 G4 cohort. The contract also has 204,323 G3 transactions, 2,457 G2 drill in transactions, and 3,910 transactions that already failed at baseline.

**How it breaks.** 859 distinct failure modes for this one contract (up from 625), but the top 8 (52.73% of its G4 volume; a lower concentration than the prior 61.49%, as the long tail grew faster than the head) are all the *same* mechanism repeated against different tokens: `settle` OOG on `SSTORE`, `pattern_or_reason: storage_heavy`, at call depth 3 to 4; the halt lands inside whichever ERC-20/aToken the batch is mid-transfer-into when gas runs out. Ranked halt sites: FiatTokenV2_2/USDC (30,312 tx, 28.8%), two unlabeled contracts (5,680 and 4,857, 5.4%/4.6%), Maker DAI (4,772, 4.5%), Aave V3 `DEFAULT_A_TOKEN_IMPL` (3,223, 3.1%), Lido (2,476, 2.4%), and two more unlabeled contracts (2,383 and 1,867, 2.3%/1.8%); a broad slice of the largest ERC-20s and lending-market tokens, exactly the assets CoW batches route through — the same mechanism and largely the same token set as before, just spread across more clusters. The driver signature is distinctive: `access_list_entries` remains elevated (p50 35 to 123, p90 82 to 168 per tx); the cost pressure comes from the sheer breadth of addresses/storage keys a mega-batch settlement touches, not from any single expensive opcode. Per-cluster gas deltas again skew net-cheaper at the median (p50 negative) but positive at the tail (p90 up to +39,100), and the full entry cohort nets to −2,653,366,436 gas (avg −25,177/tx); eip-8037 makes the *average* settlement modestly cheaper, but a long tail of large, storage-heavy batches gets pushed hard enough over the edge that ~6.7% of all traffic now fails outright. Because the halt occurs mid-batch inside another protocol's storage writes, a per-tx gas-limit increase never rescues it.

**Which EIP hurts more.** Only eip-8037 produces a shard for GPv2Settlement; eip-8038 registers zero G4 impact here. eip-8037's per-tx state-reservoir model is what bites CoW; its batch settlements touch far more distinct storage slots per transaction than a typical DeFi call, and it's exactly that breadth eip-8037 taxes.

**Can they fix it?** GPv2Settlement is immutable, and the failure isn't a CoW logic bug; it's the aggregate storage cost of settling many trades against many tokens in one atomic call. CoW cannot change gas accounting on its own contract. The lever sits entirely off-chain: the solver/driver stack chooses batch composition, so mitigations mean capping batch size or the number of distinct token legs per settlement, biasing solvers away from storage-heavy long-tail tokens, or splitting oversized batches.

**Priority: HIGH.** A single, immutable, high-value settlement contract absorbing ~6.7% real-traffic failure under eip-8037, concentrated in a small number of easily identifiable large tokens, is a clean, actionable, and high-visibility case for outreach.

**Suggested outreach.** "Under the current eip-8037 state-reservoir gas schedule, roughly 1 in 15 GPv2Settlement batch settlements over our 4M-block sample fails outright; not fixable by raising the tx gas limit, since the halt happens mid-batch inside token storage writes (USDC, DAI, Aave aTokens, Lido, and others make up the bulk of halt sites). Because this is driven by the breadth of distinct storage slots touched per settlement rather than any one expensive call, the only mitigation available to you is on the solver/driver side; bounding batch size or token-leg count; since the settlement contract itself can't be changed. We'd like to walk through the per-cluster breakdown with your team before this schedule is finalized."

## 1inch: validate before outreach

**What the data shows.** Aggregation Router v6
(`0x111111125421ca6dc452d289314280a0f8842a65`) has 40,727 combined G4 role
rows (up from 22,396 in the prior edition). EIP 8038 accounts for 29,001 and
EIP 8037 accounts for 11,726. Under EIP 8038, 100% of the rows fall into eight
clusters, all at `EXTCODESIZE`.

**Why attribution is uncertain.** Only one EIP 8037 transaction and no EIP 8038
transactions use the router as the G4 entry contract. The router is almost
always an inner revert site for a `StrategyExecutor`, proxy, flashloan wrapper,
or arbitrage bot. The repeated custom errors may come from router validation,
but they may also reflect caller profitability or slippage checks. The current
data does not separate those explanations.

**Next step.** Confirm the failing frame against verified Router v6 bytecode and
inspect the dominant entry contracts. Contact 1inch only if that work shows that
Router v6 owns the failing check or needs a new validation gas budget. Otherwise,
move the finding to the automated caller watchlist.

## 0x Protocol (Settler)

**What it is.** The 0x Protocol "Settler" contract (`0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb`), 0x's disposable, versioned on-chain settlement router. The shard's raw `owner_project: morpho-org` is a **misattribution**; nothing in the identity header (label `"0x Settler / Aggregation"`, category `defi_complex`) supports a Morpho link; this is 0x's Settler.

**Contracts and roles.** Settler never appears as the tx `entry` in either schedule's G4 rows. It is reached only as a nested `revert_site` (60 reverts / 8037; 11,707 / 8038) and, rarely, an `oog_site` (6 halts / 8037; 1 / 8038). Top callers are automated routers/bots: `StrategyExecutor` and, dominant under 8038, an "Aave v3 Flashloan User" contract; flashloan-funded arbitrage flows are the primary route into Settler's failures.

**Scale and failure rate.** `failure_rate` null. 8038: **11,708 G4** across 603 distinct clusters (up from 581), spread over 11,363 distinct blocks (21,323,597 → 25,319,862); essentially a daily-recurring failure across the whole window. 8037: **66 G4**, 60 blocks; 8038 dominates by **~177x** (down slightly from ~193x). 8037 has `g3_tx_count = 28,214` against only 66 G4 (`g4_vs_other_ratio ≈ 0.22%`); G3 absorbs almost everything, G4 is a tiny residue. 8038 instead shows `g3_tx_count = 23,002` vs `g4 = 11,708` (`g4_vs_other_ratio ≈ 31.6%`, down from ~46.9%); G3 grew faster than G4 here, so under the revised 8038 schedule a smaller — but still substantial — **roughly a third** of all repricing-affected Settler transactions are genuinely unrescuable.

**How it breaks.** 8038's top 8 clusters are keyed uniformly on `EXTCODESIZE` at revert_site, call depth 3 or 5, with `state_gas_category: access_list`. The single largest cluster (2,819 txs, 24.1%) decodes `Error(string): TR` with avg `gas_delta` of **+2.18M gas**; the revised cold-access/account-write repricing makes the EXTCODESIZE-guarded external-call check so much more expensive that Settler's internal check trips before completion. Remaining shown clusters decode as empty reverts; a bubbled failure from a starved nested call; at material gas_delta (+170K to +340K avg). Under 8037, the dominant mode is again `Error(string): TR` (51.7%) at a `DUP1` opcode, plus isolated single-tx OOGs (SLOAD/PUSH1/JUMPI/SHL) with large **negative** gas_delta; very large/expensive txs cut off early rather than reservoir-exhaustion per se. Same mechanism, same signature as the prior edition of this report; only the magnitudes and cluster count shifted.

**Can they fix it?** No. Settler is immutable/non-upgradable by design; there is no admin key or proxy to patch. The only levers are upstream: 0x's routing/solver backend would need to trim EIP-2930 access-list size on Settler calldata, and/or ship a new Settler build with cheaper internal existence/return-data checks. Gas-limit padding does not help; this is G4 by definition.

**Priority: HIGH.** Unlike the small, mostly-fixable 8037 exposure, 8038 shows a large, persistently recurring, and disproportionately unrescuable failure surface, driven through high-frequency flashloan-arbitrage callers.

**Suggested outreach.** "Under the current EIP-8038 parameters, we measure ~11.7k Settler-routed transactions per 4M-block sample that would newly and permanently fail; concentrated in swaps invoked via flashloan-funded arbitrage routers and carrying large EIP-2930 access lists. Since Settler instances are immutable, we'd like to flag this early so the routing/solver layer can reduce access-list size on these paths or adjust internal existence/call-success checks before any 8038 rollout."

---

### Finding requiring validation

## Sushi labeled cohort

**What the data shows.** There are 82,498 G4 reverts across more than 386
addresses labeled `sushi` by Ethlists (up from 26,568 across 328 addresses).
EIP 8038 accounts for 81,159. The label has medium confidence, the contracts
appear only as inner revert sites, and the named SushiSwap Router has ten
reverts (8038 only).

**Why attribution is uncertain.** The dominant EIP 8038 signature is unchanged
from the prior edition of this report: selector `0x78e111f6`, opcode
`EXTCODESIZE`, error `19`, and a gas delta of about 55,049. The Uniswap cohort
shows the same signature and attributes it to searcher or arbitrage bot logic.
Automated callers also dominate the Sushi labeled cohort. Repetition across many
addresses proves that code is shared, but it does not prove that Sushi owns the
failing code or that a Sushi pool template is defective.

**Next step.** Match the affected bytecode to verified Sushi deployments and
locate the exact failing check in the trace. Do not contact Sushi as a High
priority target until that attribution is confirmed.

---

## Appendix A: affected dependencies and watchlist

These findings are useful context but are not direct outreach targets. In most
cases the named project is a passive inner call site, the contract cannot be
changed, or the transaction is an automated strategy responding to different
gas costs.

| Entity | Measured finding | Why it is not a full outreach target | Where the action belongs |
| --- | --- | --- | --- |
| Uniswap | About 816,000 combined G4 role rows (up from ~315,000 — a disproportionate ~2.6x jump against the dataset's overall ~1.5x growth, worth a closer look); 199 genuine V4 PoolManager OOG transactions (up from 68) | Most rows are searcher profitability or slippage reverts | V4 engineering watch for the 199 OOG cases; otherwise searcher operators |
| Curve | About 7,600 combined rows | The pool is immutable and is not the entry contract | The two callers and their operators |
| PancakeSwap V3 | About 6,150 EIP 8038 rows, concentrated in one dominant cluster plus a long tail | The pool is an immutable inner call site | Strategy and flashloan operators |
| Balancer | About 64,000 rows, mostly Vault related bot profitability reverts | The report finds no Balancer owned remediation | Flashloan bots and CoW solvers |
| Circle and USDC | About 188,500 EIP 8037 role rows and 1,700 under EIP 8038 | USDC is usually the passive halt or revert site | CoW, Socket, and other calling integrators |
| Tether | About 30,900 USDT and 1,900 TetherToken role rows under EIP 8037 | The token contracts are immutable and the gas shortfall is caller side | Batchers, bundlers, and aggregators |
| Aave aToken and GHO | About 16,350 EIP 8037 rows and 427 under EIP 8038 | The main path is a CoW settlement gas forwarding interaction | CoW and solver batch composition |
| MakerDAO and DAI | About 14,200 EIP 8037 rows (up from ~3,900 — also a disproportionate jump, tracks CoW's growing DAI-payout halt cluster above) | DAI is a passive callee in large access list batches | Batch settlement integrators |
| Lido tokens | About 13,700 combined rows across stETH, wstETH, and LDO | The failures are in CoW, Socket, or MEV caller assumptions | CoW, Socket, and searcher operators |
| XEN | About 37,150 EIP 8037 role rows and 3,624 EIP 8038 halts | XEN is immutable and the affected traffic comes from batching bots | Batch minter and proxy bot operators |
| SKALE labeled arbitrage proxy | About 6,290 EIP 8037 reverts — essentially flat vs the prior edition | The bot correctly rejects unprofitable trades | No action required |
| Chainlink | About 925 combined rows across 22 feed/infra addresses (up from 299); the ETH/USD feed proxy alone accounts for 314 | The oracle is only an inner site and price integrity is not affected | Strategy operators |

### Other labeled projects

These projects fall below the detailed analysis threshold. Most are immutable
pools or tokens used as inner call sites, or automated strategies that revert
when a trade is no longer profitable. Contact a team only when a reachable owner
and a plausible mitigation are clear. Figures below were re-derived by matching
`label`/`owner_project` against the new `affected/index.json` (excluding the
generic bot-cohort labels tallied in Appendix B); spot-checks against the prior
edition's numbers land within a few percent, so treat these as good
approximations rather than exact re-runs of the original per-entity queries.

| Footprint | 8037 | 8038 | Project / label | Note |
| --- | --- | --- | --- | --- |
| 17,622 | 17,397 | 225 | **WETH** (maple-labs) | core wrapped token, inner site |
| 9,514 | 739 | 8,775 | **Synthetix** (+ ProxyERC20) | pool/proxy inner site |
| 9,215 | 744 | 7,727 + 744* | **Safe4337Module + SafeL2** | AA + wallet; Gnosis Safe's ERC-4337 module and base wallet, now consolidated into one line (*SafeL2 744 is 8037-only) |
| 9,385 | 7,657 | 1,728 | Erc_20: Ethereum_games | XEN-adjacent minter front-end |
| 8,447 | 3 | 8,444 | BadgerDAO | |
| 6,505 | 6,424 | 81 | **LI.FI / Socket Bridge** | bridge aggregator; worth a heads-up |
| 6,500 | 6,493 | 7 | totalproof.eth | unowned but named entity; bot-like, not analyzed |
| 3,772 | 8 | 3,764 | FunFair | token |
| 3,571 | 2,700 | 871 | Bancor (BancorConverter + bancornetwork + bancor) | DEX (converters) |
| 2,656 | 2,656 | 0 | GasToken | largely obsolete |
| 2,479 | 2,448 | 31 | Wrapped BTC (maple-labs) | core wrapped token |
| 2,263 | 518 | 1,745 | Uniswap V2DutchOrderReactor | include in the Uniswap watchlist |
| 1,198 | 1,198 | 0 | Liquity | stablecoin protocol |
| 1,187 | 1,183 | 4 | Ambient (CrocSwapDex) | DEX |
| 1,161 | 1 | 1,160 | MAGIC | token |
| 1,140 | 1,012 | 128 | Fluid / Instadapp (FluidLiquidityProxy) | proxy, likely upgradable |
| 1,123 | 245 | 878 | Supernova (AlgebraPool) | DEX pool (integrator-side); unchanged from the prior edition |
| 717 | 10 | 707 | Convex | yield |
| 698 | 696 | 2 | Bebop (BebopSettlement) | DEX aggregator |
| 562 | 535 | 27 | **Tether-owned TransparentUpgradeableProxy** | include in the Tether watchlist |
| 556 | 556 | 0 | Kyber v2 | DEX; up from 163 |
| 500 | 468 | 32 | Paxos Gold | token |
| 389 | 362 | 27 | Aave (7 other addresses) | small residual beyond the dedicated aToken/GHO |
| 372 | 372 | — | TransparentUpgradeableProxy (unlabeled) | wallet/generic proxy; re-derivation returned a different address mix than the prior edition — treat as approximate |
| 324 | 175 | 149 | Mooniswap | legacy 1inch AMM |
| 260 | 193 | 67 | KyberSwap (MetaAggregationRouter v2) | DEX aggregator; worth a heads-up; essentially flat |
| 223 | 0 | 223 | Beefy (StrategyBeefy) | yield strategy; unchanged from the prior edition |
| 154 | 154 | 0 | Ondo (TokenProxy) | RWA stablecoin; proxy; unchanged from the prior edition |

Below ~150 footprint: dozens more tokens/protocols with double- to single-digit
G4 counts (compound_v2, gitcoin, makermcd, dodo, ampere, Reserve Rights,
renproject, tornado_cash, inverse_finance, status, arbitrum, and others) :
statistically indistinguishable from noise; individual outreach not warranted.

## Appendix B: large *unattributable* cohorts (not outreach targets)

These carry huge G4 counts but resolve to no reachable team; generic
labels/heuristics, not manually-curated identities. They are mostly **MEV/arbitrage
bots and flashloan searchers** (self-correcting) and unlabeled proxy/deployer
wrappers. They are listed for completeness, not for outreach. The generic
`uniswap_v2`, `uniswap_v3`, `balancer`, and `curvefi` cohorts are summarized in
the watchlist. The `sushi` cohort remains in the validation section because its
ownership and failing frame are not confirmed.

| 8037 | 8038 | Cohort | What it is |
| --- | --- | --- | --- |
| 121,648 | 292,776 | "StrategyExecutor" | MEV/arb executors; mostly "no profit"/EXTCODESIZE reverts, self-correcting |
| 69,719 | 231,417 | "Aave v3 Flashloan User" | arb/flashloan bots; economic reverts, self-correct |
| 46,894 | 60,663 | "Balancer v2 Flashloan User" | arb/flashloan bots; economic reverts, self-correct |
| 194,125 | 16,557 | "Proxy contract" | unlabeled proxies (incl. Across relayer front, minter fronts) |
| 25,607 | 27,761 | "Contract Deployer" | fee-on-transfer token deploys / halt sites; unchanged from the prior edition |
| 10,906 | 11,755 | "ERC-20 token" | unlabeled tokens as inner SSTORE halt sites; unchanged from the prior edition |
| 10,604 | 0 | "quasi.bot" | MEV/arb bot; self-correcting |
| 6,493 | 7 | "totalproof.eth" | unowned, bot-like entity |

All eight cohorts grew (some substantially: StrategyExecutor and Aave v3
Flashloan User roughly doubled), consistent with the wider window surfacing more
bot activity; none of this changes the "not an outreach target" conclusion.

## Appendix C: methodology & caveats

- **Source.** `site/data/{eip-8037,eip-8038}/affected/`; per-contract G4
  failure-mode shards + `index.json` + `deploy_oog.json`. Pinned config
  `0x8ccad591661bfca557e688c41d8fbf14d8f51cc3b0239fcdc517c6592b780527`, blocks
  21,319,986 → 25,319,985 (~2024-12-03 → 2026-06-15).
- **Provenance.** Regenerated on 2026-08-24 from producer schema v11, run as two
  separate per-schedule precompute passes (eip-8038 then eip-8037). The pinned
  config hash is unchanged from the prior (2026-08-14) edition of this report —
  `0x8ccad591661bfca557e688c41d8fbf14d8f51cc3b0239fcdc517c6592b780527` — but its
  resolved block coverage grew from 2,102,648 blocks (2025-08-25 → 2026-06-15) to
  **4,000,000 blocks** (2024-12-03 → 2026-06-15) as more history was backfilled
  under the same config between runs. The end block is unchanged; only the start
  moved earlier. Don't assume this hash's range is fixed — re-check
  `meta.json.block_range` before citing it again.
- **Scope = G4 only** (Potentially broken): `baseline_success=true AND
  schedule_success=false AND min_multiplier_to_succeed IS NULL`. Excludes G3
  (fixable with more gas) and AF (already failing).
- **Review method.** Each detailed analysis and validation item uses the
  affected contract shards for that entity, read directly from the new data;
  every figure there is exact. The Appendix A/B and "other labeled projects"
  tables were re-derived by `label`/`owner_project` keyword matching against
  `index.json` (excluding generic bot-cohort labels) rather than re-running the
  original bespoke per-entity queries — see the note above those tables.
- **Lower bounds.** For high-traffic contracts the producer's per-block drill-in
  cap under-reports halts; counts are floors. `drill_ins_truncated_blocks` is 0
  for this run (down from a nonzero share previously), so truncation is not
  currently contributing to under-counting, but the per-block cap itself is
  unchanged and still applies once volume spikes.
- **Missing rates.** Many shards have no `failure_rate` (no Xatu total-tx
  denominator, esp. for pure halt/revert *sites*); those report absolute
  counts only. Where a rate exists, it's flagged.
- **Label caveats.** `0xbbbb…ffcb` is labeled `morpho-org` but is the **0x
  Protocol Settler**. `0x7f39c5…` is **wstETH**. `0x5a98fcbe…` is the **LDO**
  governance token. WETH and WBTC entries owned by `maple-labs` are the core
  wrapped token contracts, not Maple contracts.
- **Structural findings.** Generic cohorts such as `uniswap_v2`, `uniswap_v3`,
  `sushi`, `curvefi`, and `balancer` often show one mechanism spreading across
  many addresses. Shared signatures do not prove that every labeled project owns
  the failing code. Bytecode ownership and the exact failing frame must be
  confirmed before treating these cohorts as protocol defects.
