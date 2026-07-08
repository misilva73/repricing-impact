# Repricing-impact outreach report — dapps to warn about EIP-8037 / EIP-8038

**Purpose.** A prioritized list of projects to contact ahead of the candidate gas
repricings **EIP-8037** (state-creation) and **EIP-8038** (state-access/write),
with, for each entity, *what breaks and why*. Built entirely from the dashboard's
pre-computed **G4 "Potentially broken"** data (`site/data/{schedule}/affected/`).

**What "G4 / Potentially broken" means.** A transaction that **succeeds on
mainnet today** but **fails under the repriced schedule** *and* **cannot be
rescued by raising the gas limit** (`min_multiplier_to_succeed IS NULL`). These
are genuine breaks — the tx *logic* fails (a hardcoded 2300-gas stipend, a
gas-dependent branch, or a 63/64 call-forwarding shortfall that starves an inner
frame) — not just "needs more gas" (that cohort is G3, excluded here).

**The two EIPs.**
- **EIP-8037 — state-creation repricing.** Per-tx "state reservoir"; surcharges
  new-account creation, contract creation (CREATE/CREATE2), and EIP-7702
  authorization.
- **EIP-8038 — state-access/write repricing.** SSTORE write 2,800→10,000, cold
  account/storage access →3,000, account write →8,000.

## How to read this list (important framing)

The affected set splits into three very different kinds of "affected", and the
right action differs for each:

1. **Genuine, actionable protocol breakage.** Contracts whose own users hit real
   failures the team can and should mitigate — account-abstraction EntryPoints and
   wallet implementations, bridges, and settlement/aggregation routers. **These are
   the real outreach targets.**
2. **Immutable tokens as innocent halt/revert *sites*.** USDC, USDT, DAI, stETH,
   Aave aTokens/GHO, XEN etc. mostly appear because a break *landed inside* their
   transfer code while they were called deep in someone else's call tree. The
   token can't be patched and usually isn't the bug — **warn the integrators, not
   the token team.**
3. **Economic self-correction (not really "breakage").** A large share of DEX/pool
   volume (Balancer Vault, Sushi/Pancake/Curve pools, Chainlink feed reads) breaks
   only because MEV/arbitrage bots hit their own **"no profit" guards** once gas
   costs shift. This traffic simply stops being submitted; searchers re-tune
   automatically. **Low urgency.**

Two data caveats apply throughout: (a) for high-traffic contracts, halt/revert
counts are a **lower bound** (per-block drill-in cap of 1024); (b) many shards
have **no `failure_rate`** (no Xatu total-tx denominator), so we report absolute
G4 counts and flag where a percentage-of-traffic figure is unavailable.

---

## Prioritized outreach summary

| Priority | Entity | Category | Worst EIP | Headline impact | Who fixes it |
| --- | --- | --- | --- | --- | --- |
| **HIGH** | ERC-4337 EntryPoints (eth-infinitism) | Account abstraction | 8037 (deploy-block) / 8038 (48% v0.8) | >900k G4 tx; 13–48% of `handleOps` revert; +102k blocked wallet deployments (8037) | Bundlers + account vendors (EntryPoint is immutable) |
| **HIGH** | Across | Bridge | 8037 | ~100k G4 events; funds-stuck/UX risk; cold-access + 63/64 starvation | **Across** (UUPS-upgradable) |
| **HIGH** | Socket / Bungee | Bridge aggregator | 8038 | **9.76% of live traffic fails**; immutable batcher | Socket (redeploy + relayer gas) |
| **HIGH** | CoW Protocol | DEX settlement | 8037 | ~17.2k settlements (~5.3%) OOG at payout SSTORE | CoW (solver batch sizing; contract immutable) |
| **HIGH** | 1inch | DEX aggregator | 8038 | ~17.9k router reverts (~1.8%); immutable router | 1inch (new router version) |
| **HIGH** | ZeroDev (Kernel) | Smart accounts | 8037 | ~73k G4 tx; EIP-7702 authorization surcharge | ZeroDev (new impl + bundler gas) |
| Medium | Alchemy (Modular Account) | Smart accounts | 8038 | ~14.9k G4 reverts | Alchemy (new impl + bundler gas) |
| Medium | Circle (USDC) | Stablecoin | 8037 | ~83k halt/revert *sites* on impl contract | Integrators (USDC is innocent callee) |
| Medium | Uniswap | DEX | 8038 | 238k V2 fee-on-transfer OOG (2300 stipend) | Token/integrator side (routers immutable) |
| Medium | Tether (USDT) | Stablecoin | 8037 | ~16k halt/revert *sites* | Integrators (USDT immutable) |
| Medium | Aave | Lending | 8037 | ~6.5k G4 (aToken impl + GHO) via CoW/DeFiSaver | Integrators (impl immutable) |
| Medium | 0x Protocol (Settler) | DEX aggregator | 8038 | ~4.8k Settler reverts | 0x (versioned redeploy) |
| Medium | XEN Crypto | Token | 8038 | ~2.7k mint/approve OOG | Batch-minter integrators (immutable) |
| Medium | Lido | Liquid staking | mixed | ~1.5k (8037, wstETH) / 433 (8038, stETH) | Integrators (tokens immutable/innocent) |
| Medium | Curve | DEX | 8038 | ~1.1k tricrypto reverts (Vyper asserts) | Dominant integrator (immutable pool) |
| Medium | PancakeSwap V3 | DEX | 8038 | ~1.7k pool reverts | Integrators (pool immutable) |
| Medium | MakerDAO (DAI) | Stablecoin | 8037 | ~1.5k DAI transferFrom breaks via CoW | Integrators (DAI immutable) |
| Low-Med | Balancer | DEX | 8038 | ~18.9k Vault reverts — **mostly arb "no profit"** | Searchers self-correct (Vault immutable) |
| Low | Sushi | DEX | 8038 | ~895 RouteProcessor reverts (arb bots) | Integrators / self-correct |
| Low | SKALE (arb proxy) | MEV/arb | 8037 | 6,289 self-imposed "no profit" reverts | Bot operator (self-correct) |
| Low | Chainlink | Oracle | 8038 | 328 revert-*sites*; feed is blameless | Integrator bots (feed immutable) |

**Bottom line for outreach.** The six HIGH entities are where real users hit real
failures and a team can act: the **account-abstraction stack** (eth-infinitism +
ZeroDev + Alchemy + bundlers), the **two bridges** (Across, Socket), and the **two
settlement/aggregation routers** (CoW, 1inch). Everything else is either an
integrator-education problem (immutable tokens) or self-correcting arbitrage
economics.

---

## Detailed analyses

### Account abstraction

## eth-infinitism (ERC-4337 EntryPoint)

**What it is.** The canonical ERC-4337 account-abstraction EntryPoint singletons maintained by eth-infinitism. Every smart-account UserOperation on mainnet is bundled through `handleOps` on one of these three contracts (v0.6, v0.7, v0.8). This is the single largest-impact entity in the dataset: >900k G4 transactions across the two EIPs, and it is the origin point for the ~102k collapsed "fresh-deployment OOG" failures below.

**Contracts & roles** (all immutable singletons; `is_proxy=false`, `is_upgradable=false`):
- **EntryPoint v0.7** `0x0000000071727de22e5e9d8baf0edac6f37da032` — 8037: entry 462,943 (oog 177,383 / non-oog 285,560); oog_site 1,291; revert_site 274,874. 8038: entry 418,965; revert_site 399,466.
- **EntryPoint v0.8** `0x4337084d9e255ff0702461cf8895ce9e3b5ff108` — 8037: entry 289,291; oog_site 11; revert_site 278,490. 8038: entry 93,129; revert_site 93,017.
- **EntryPoint v0.6** `0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789` — 8037: entry 202,918 (oog 36,568); oog_site 11,475; revert_site 178,101. 8038: entry 13,461; revert_site 13,429.

Failing selectors are the batch handlers `0x765e827f` (`handleOps`, v0.7/v0.8) and `0x1fad948c` (`handleOps`, v0.6). Almost all reverts land back at the EntryPoint (call_depth 2) as `custom:0x220266b6` — the EntryPoint's `FailedOp`/`FailedOpWithRevert` error (a UserOp rejected mid-batch).

**Scale & failure rate** (real `handleOps` denominators):
- *8037:* v0.6 **22.3% revert + 1.43% halt** (worst rates); v0.7 **13.3% revert** on 2.07M tx (462,943 G4 — worst by count); v0.8 **16.2% revert**.
- *8038:* v0.8 **48.5% revert** (278,490 of 573,778 — highest single rate); v0.7 **19.3% revert** (399,466); v0.6 1.68%.
- v0.7's rates are a **floor** (drill-in cap), but even the floor is systemic.

**How it breaks & why.**
1. **Validation-time reverts (bulk, both EIPs).** `handleOps` reverts at depth 2 (`custom:0x220266b6`): the repricing makes an inner frame exceed the op's declared `verificationGasLimit`/`callGasLimit`, so the EntryPoint aborts with `FailedOp`. `gas_delta` is usually **negative** (dies early). A gas-limit bump cannot help — the EntryPoint enforces the op's own declared limits.
2. **EIP-8037 authorization surcharge (7702).** 8037 shards are dominated by `state_gas_category = "authorization"` (v0.7: 96,952 in the top cluster; v0.8 PUSH0 cluster 100% authorization). These carry EIP-7702 authorizations; 8037's surcharge pushes them over the validation budget — several clusters have large *positive* gas_delta (+105k–112k), the added cost that tips them into `FailedOp`.
3. **EIP-8037 SSTORE OOG halts inside smart accounts.** v0.6 has 11,475 oog_site halts on SSTORE; v0.7's OOG halts land in ZeroDev Kernel accounts on SSTORE. 8037's surcharge starves the inner frame on the write.
4. **Fresh-deployment OOG — 102,124 accounts (8037-only headline).** Freshly CREATE2-deployed ERC-4337 wallets that run out of gas during their **own construction**. ~99.8% deploy via EntryPoint v0.7 (91,411) / v0.6 (10,623) inside `handleOps`. Halt opcode: RETURN 84,047 (code-deposit) / SSTORE 18,128 (first storage write). Initcode families `0x6100` (68,643) / `0x603d` (26,811) = wallet-factory prefixes. 102,048 revert `FailedOp`. `gas_remaining_at_oog` p50 67,030 in the *outer* frame while the inner CREATE sub-call is exhausted — a **63/64 forwarding shortfall**, so raising the tx limit does not rescue them. Net: 102k first-time wallet activations that would fail to deploy under 8037.

**Which EIP hurts more.** Split: **8037 is higher priority** (only EIP producing OOG halts + the 102k deployment-blocking failures — unrecoverable), while **8038 produces the highest single revert rate** (v0.8 48.5%). 8037's deployment-blocking is the worst failure mode.

**Can they fix it?** EntryPoints are **immutable, non-proxy singletons** — no in-place patch. Fixes live one layer out: **bundlers/SDKs** must re-estimate `verificationGasLimit`/`callGasLimit`/`preVerificationGas` under the new schedule; **account & factory authors** (ZeroDev Kernel, Safe4337Module, Alchemy SemiModularAccount all appear as halt/revert sites) must raise gas for constructor code-deposit and first storage writes; a protocol-level change needs a new EntryPoint version.

**Priority: HIGH.** Largest break count in the dataset, double-digit-to-48% failure rates on live traffic, an unrecoverable deployment-blocking mode, and an immutable core requiring a coordinated fix across the whole AA stack. Warn first.

**Suggested outreach.** "Under EIP-8037 and EIP-8038, a large fraction of ERC-4337 `handleOps` traffic on all three EntryPoint versions fails as they stand today — 13–22% of ops revert with `FailedOp` under 8037 and up to ~48% under 8038 for v0.8, and separately ~102k smart-account CREATE2 deployments run out of gas during their own construction under 8037. Because the EntryPoints are immutable, the fixes are on your side: bundlers must re-derive gas limits under the new schedules, and account/factory contracts need more headroom for constructor code-deposit and first storage writes. We'd like to share the per-version breakdown and example transactions."

## ZeroDev (Kernel smart accounts)

**What it is.** ZeroDev's Kernel smart-account implementation contracts and their WeightedECDSAValidator module (an ERC-4337 stack). They appear almost entirely as halt/revert *sites* inside `EntryPoint.handleOps` batches (selector `0x765e827f`), not as the entry point.

**Contracts & roles** (OLI high-confidence):
- **Kernel** `0xd6cedde84be40893d153be9d467cd6ad37875b28` — 8037: oog_site 32,169 + revert_site 32,169. 8038: 4.
- **Kernel** `0xbac849bb641841b44e965fb01a4bf5f074f84b4d` — 8037: 23,439 + 23,444. 8038: 1.
- **Kernel** `0xd830d15d3dc0c269f3dbaa0f3e8626d33cfdabe1` — 8037: 9,782 + 9,782.
- **WeightedECDSAValidator** `0xed89244160cfe273800b58b1b534031699dfeeee` — 8037: 8,021 + 8,021.

**Scale & failure rate.** `failure_rate` is `null` (internal sites — no Xatu denominator). Under **8037** the four contracts carry ~73,400 halts and ~73,400 reverts (same ~73k txs, both roles). Under **8038** they collapse to essentially zero (a ~15,000× drop). 8037 is the entire story.

**How it breaks & why.** One mode dominates every 8037 shard: `handleOps` halting/reverting **inside the Kernel/validator on `SSTORE`** at depth 3–7, `pattern_or_reason: storage_heavy`, revert `custom:0x65c8fd4d`. Driver: `state_gas_category: authorization` — **EIP-7702 authorization**, exactly what 8037 surcharges. The 7702-delegated account's state-creating validation work is inflated and OOGs mid-`SSTORE`. **A gas-limit bump won't help** (`g3_tx_count: 0` on all shards): the shortfall lands in a nested frame receiving only 63/64 of parent gas. Deeply negative `gas_delta` (p50 −50k to −430k) confirms early death.

**Which EIP hurts more.** **8037, overwhelmingly** (~99.99% of exposure); 8038 barely touches these. Culprit is 8037's 7702 authorization surcharge + state reservoir.

**Can they fix it?** `is_proxy: false`, `is_upgradable: false` — these implementations are **immutable**. Fix = ZeroDev **ships a new Kernel implementation** and points new/upgraded proxies at it, plus **bundler/SDK gas provisioning** for the 7702 surcharge, plus account migration.

**Priority: HIGH.** ~73k broken G4 tx under 8037 in one team's stack, clear single mechanism, no gas-limit rescue, immutability = long lead time.

**Suggested outreach.** "Under EIP-8037 we see ~73,000 of your Kernel smart-account user-operations breaking in replay — they run out of gas on internal `SSTORE`s during EIP-7702-authorization flows, and a higher gas limit does not rescue them. eip-8038 has essentially no effect. Since the affected Kernel and WeightedECDSAValidator contracts are immutable, the fix likely means a new Kernel implementation plus a bundler/SDK gas-provisioning change and an account migration — happy to share failing tx hashes."

## Alchemy (Modular Account)

**What it is.** `SemiModularAccountBytecode` (`0x000000000000c5a9089039570dd36455b5c07383`), Alchemy's ERC-4337 smart-account implementation, driven through EntryPoint v0.7 (~99.7% of affected txs).

**Contracts & roles.** 8037: oog_site 5,720 + revert_site 5,720. 8038: revert_site 14,920.

**Scale & failure rate.** `failure_rate` null. **8038 is larger: 14,920 G4 reverts** (one cluster, selector `0x765e827f`, = 14,909). 8037: 5,720 halts + 5,720 reverts (same txs). Lower bounds (drill-in cap).

**How it breaks & why.**
- **8038 (14,909):** non-OOG revert at `EXTCODECOPY`, depth 4, `custom:0x220266b6`. Drivers: `cold_account` p50 2/p90 6, `sload` p50 5/p90 15, zero SSTORE — **cold-access repricing** changes gas forwarded to an inner frame which reverts. Negative gas_delta (avg −65,859).
- **8037 (5,686):** OOG halt on `SSTORE`, `storage_heavy`, `FractionalGas`, depth 5 — a **63/64 forwarding starvation** on the write. gas_delta avg −111,504.

**Which EIP hurts more.** **8038** by count (14,920 vs 5,720) and cleaner signal (pure cold-access, no SSTORE/access-list).

**Can they fix it?** Implementation bytecode is immutable (`is_upgradable: false`), but Alchemy can ship a **new implementation** for future accounts and migrate. The real lever is the gas forwarded into the failing inner frame + **bundler/paymaster gas estimation** accounting for repriced cold-access/SSTORE.

**Priority: Medium.** Meaningful volume, single core selector, major vendor — knocked down from High by no failure_rate denominator and a clear remediation path.

**Suggested outreach.** "Under EIP-8037/8038 your SemiModularAccountBytecode implementation (`0x0000…7383`) fails on its main user-op path (`0x765e827f`) in thousands of replayed txs — under 8038 as cold-access reverts, under 8037 as `SSTORE` OOG from 63/64 call-forwarding starvation. A gas-limit bump won't fix these. We'd like to share the failing txs so you can re-tune bundler/paymaster gas estimation and evaluate a new implementation version."

---

### Bridges

## Across (bridge)

**What it is.** Across is an intents-based cross-chain bridge; these are its Ethereum SpokePool contracts (custody funds, settle relayer fills/deposits). A broken fill/settlement can leave bridged funds stuck — a direct funds/UX risk.

**Contracts & roles** (both `is_proxy: true`, `is_upgradable: true`, `uups`):
- **Ethereum_SpokePool** `0x5e5b726c81f43b953a62ad87e2835c85c4d9dd3b` — 8037: oog_site 32,961 + revert_site 34,358. 8038: 18.
- **ETH Spokepool** `0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5` — 8037: revert_site 25,705. (Absent under 8038.)

Nearly all traffic enters through one relayer front-end (`0x9ccc…b294`).

**Scale & failure rate.** `failure_rate` null → counts are lower bounds. Under **8037** the two pools carry ~67k revert-site + ~33k OOG-site events, concentrated in selector `0x1bc74526`. (`0x5c7bcd…` also has 222,866 G3 txs a limit bump *would* rescue — the honest G4 break is ~25,705.) Under **8038**: 18 events (negligible).

**How it breaks & why.** Dominant modes: selector `0x1bc74526` reverting `custom:0x93cfa3ee` / `0x77ebef4d` deep in a nested `CALL`/`RETURNDATASIZE` (depth 5–6), plus smaller genuine `SSTORE`/`EXTCODESIZE` OOGs. `gas_delta` negative (dies early). Drivers: ~6 cold accounts / ~8 SLOADs, zero SSTORE, `surcharge_at_oog: 0`, reservoir 0. **8037's cold-account surcharges inflate gas consumed before the deepest call, and the 63/64 forwarding rule then starves the inner settlement frame.** The shortfall is proportional, not a fixed ceiling — a limit bump can't rescue it.

**Which EIP hurts more.** **8037 overwhelmingly** (~100k combined G4 events vs 18); 8038 essentially harmless. Cold-account access + call-forwarding is the mechanism (not SSTORE — reservoir never exhausts).

**Can they fix it?** **Yes.** Both SpokePools are **UUPS-upgradable** and owned by Across — ship a fixed implementation without redeploying the endpoint or migrating funds. Likely fix: leave more gas headroom in the settlement/fill sub-call so the child frame survives 8037's higher cold-access cost.

**Priority: HIGH.** ~100k G4 events (lower bound) on a cross-chain bridge with funds-stuck/UX consequences — but cleanly attributable to one EIP + one selector, and upgradable, so a single fix resolves the bulk.

**Suggested outreach.** "Under EIP-8037 we observe tens of thousands of your Ethereum SpokePool fills/settlements (selector `0x1bc74526`) failing with custom errors `0x93cfa3ee`/`0x77ebef4d` — the tx reverts deep in a nested call and a gas-limit increase does not rescue them. Root cause looks like cold-account surcharges plus 63/64 call-forwarding starving your inner settlement frame; EIP-8038 shows near-zero impact. Since your SpokePools are UUPS-upgradable, we'd like to compare traces and confirm the forwarded-gas fix before this ships."

## Socket

**What it is.** `SocketBatcher` (`0x87be3fc3edfe10cb8ce1244d6a1969fc55f9f83c`), the batch-execution entrypoint for the Socket / Bungee bridge aggregator (single selector `0xfa98a33f`), fanning batches through a deep call tree. Immutable, non-proxy.

**Contracts & roles.** 8038: entry 4,064 (all OOG) + revert_site 4,064. 8037: entry 5,233 (all OOG).

**Scale & failure rate** (real denominator: 41,636 txs).
- **8038 (worse): revert_rate = 9.76%** of all real SocketBatcher traffic. 4,064 G4 entry txs.
- **8037: 5,233 G4 entry txs**, but published rate rounds to 0.0 — the halt/revert lands at downstream sites, so the entry contract's own rate reads 0; treat 5,233 as the real 8037 exposure.

**How it breaks & why.**
- **8038 (3,971, 97.7%):** batch reverts *inside SocketBatcher* at depth 2 on `EXTCODESIZE`, `revert_decoded: empty` (bare REVERT bubble-up). The real halt is one frame down at `0x407be335…` on `DELEGATECALL`, depth 6, `FractionalGas`. `surcharge_at_oog` p50 700 with `gas_remaining_at_oog = 0` and ~11 cold accounts — **8038 cold-access repricing adds just enough surcharge along a deep delegatecall chain that a 63/64-funded inner frame is starved.**
- **8037 (3,091, 59.1%):** OOG at `SSTORE`, `storage_heavy`, depth 6; remaining clusters halt on SSTORE inside tokens reached through the batch (USDC 801, WBTC 556, WETH 277). `surcharge_at_oog` 0, gas_delta large negative (sum ≈ −389M).

**Which EIP hurts more.** **8038** — concrete 9.76% real-traffic failure rate, break inside SocketBatcher's own revert path (user-visible bridge failures). 8037 has more raw G4 but cheaper/earlier deaths and a 0.0 published rate (downstream attribution).

**Can they fix it?** Immutable, non-upgradable — no in-place patch. Fix is architectural: deploy a new batcher that forwards more explicit gas to inner frames (or reduces call depth), and/or add a headroom multiplier in the Socket relayer's gas estimation on `0xfa98a33f` batches. **Warn Socket/Bungee to migrate.**

**Priority: HIGH.** ~9.76% of live bridge traffic breaks under 8038 on the critical path of a major bridge aggregator; immutable, so no quick fix.

**Suggested outreach.** "Under EIP-8038, roughly 9.8% of live SocketBatcher (`0x87be…f83c`) transactions would fail — the batch aborts with an empty revert when a deep inner call frame is starved by higher cold-access costs and the 63/64 gas-forwarding rule (raising the tx gas limit does not fix it). Because SocketBatcher is immutable, this needs a re-deployed batcher and/or a headroom bump in your relayer's gas estimation. Happy to share failing traces."

---

### DEX settlement & aggregation routers

## CoW Protocol

**What it is.** GPv2Settlement (`0x9008d19f58aabd9ed0d60971565aa8510560ab41`), CoW Protocol's single settlement contract. Every failure is a batch settlement (`settle`, `0x13d79a0b`) dying mid-execution.

**Contracts & roles.** 8037 only. Role: `entry` only — **17,188 G4 tx** (17,187 OOG). Never an internal site itself; halts land in the tokens it pays.

**Scale & failure rate** (8037). 17,188 G4 tx over 16,985 blocks. `failure_rate` reports 0.0 by **rounding** — 17,188 / 323,404 ≈ **5.3%** of real settlement traffic (~1 in 19 settlements breaks). Also 22,921 G3 (gas-fixable) beyond the hard breaks. `gas_delta` sum −324M (dies early).

**How it breaks & why.** Every top cluster: `settle` OOG on **SSTORE** (`storage_heavy`) at depth 3–4, inside the token being paid: **FiatTokenV2_2 (USDC) 6,554 (38.1%)**, Aave V3 aToken 3,068 (17.9%), DAI 649, GHO 499, stETH 297. Drivers: reservoir/spillover/surcharge all 0; the one populated driver is **`access_list_entries` p50 45 (up to 124/169)**. A CoW batch touches dozens of accounts in one tx; 8037's per-tx state accounting across that wide fan-out exhausts the budget before the final payout SSTOREs. Runs against the block gas limit in one tx — **a per-tx limit bump doesn't rescue** (G4).

**Which EIP hurts more.** Only **8037** produces a shard; 8038 does not register.

**Can they fix it?** GPv2Settlement is **immutable** (`is_upgradable: false`) — and the failure isn't CoW's logic, it's aggregate batch cost. Mitigations sit in CoW's **off-chain solver/driver stack**: smaller batches, per-settlement gas budgeting against 8037 costs, access-list pre-warming.

**Priority: HIGH.** ~17.2k broken settlements (~5.3%) is a large relative hit; immutable, so the fix must come from CoW's solver infra (needs lead time).

**Suggested outreach.** "Under EIP-8037, our replay shows ~17,000 GPv2Settlement `settle` batches (~5% of your volume) running out of gas — the halts consistently land on SSTOREs inside the tokens you pay out (USDC, Aave aTokens, DAI, GHO, stETH), driven by the wide per-batch account fan-out that 8037 surcharges. Since the contract is immutable and a gas-limit bump doesn't rescue these, the fix likely lives in your solver/driver gas budgeting and batch-sizing. Happy to share failing tx hashes and the per-token breakdown."

## 1inch

**What it is.** DEX aggregator. The **Aggregation Router v6** (`0x111111125421ca6dc452d289314280a0f8842a65`) carries essentially all impact; the 1INCH token (`0x1111…c302`) is negligible.

**Contracts & roles.** Router (immutable): 8038 revert_site 17,867 (+15 oog, +76 entry); 8037 revert_site 2,252 (+31 oog).

**Scale & failure rate.** Worst: **8038 revert_rate 1.81%** (17,867 of 989,477) + halt_rate 0.0015%. 8037: 0.23%. Almost entirely the Router as an *inner* revert site inside swaps entered by MEV/arb proxies. Lower bound (drill-in cap).

**How it breaks & why.** Top clusters revert on **EXTCODESIZE** at depth 3–4, entry selector `0x07ed2379`, custom errors (`0xe9a477f6`, `0x4e47f8ea`, `0x3b5c3088` — 1inch's min-return/slippage guards). Drivers: `custom:0xe9a477f6` (4,954 tx) has p50 SLOAD 47 / cold accounts 15 (**cold-access** dominant); others carry `state_gas_category: access_list` (8–39 entries). **8038's cold-access + access-list charges inflate multi-hop route gas; an inner frame runs short and trips the min-return guard.** Logic revert (not top-level OOG) → limit bump can't help. Router gas_delta negative (dies early).

**Which EIP hurts more.** **8038 far worse** (~8× reverts and rate vs 8037). 8037 barely touches the Router (reservoir 0).

**Can they fix it?** Router is **immutable** — fix = **deploy a new router version** with route-gas assumptions recalibrated for the new cold-access/access-list prices (more conservative estimation/route selection).

**Priority: HIGH.** Large volume (17.9k) + non-trivial 1.81% real-traffic rate on an immutable contract needing a redeploy + integrator migration.

**Suggested outreach.** "Under EIP-8038, ~1.8% of Aggregation Router traffic — ~17.9k transactions — reverts on your internal min-return/slippage guards mid-route, because repriced cold-access costs starve inner swap frames; a gas-limit bump does not rescue them. Since the router is immutable, this needs a new router version with recalibrated route-gas/estimation logic. We'd like to share failing tx hashes and the driver breakdown."

## 0x Protocol (Settler)

**What it is.** The 0x Protocol "Settler" (`0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb`), the on-chain settlement router for 0x-routed swaps. (The manifest's `owner_project: morpho-org` is a **misattribution** — the identity header confirms 0x's Settler.)

**Contracts & roles.** `revert_site` only. 8038: 4,826 (320 clusters). 8037: 38.

**Scale & failure rate.** `failure_rate` null. **8038: 4,826 G4** (127× the 8037 count). Context: large G3 (6,142 / 4,983) — most repricing-affected Settler txs are gas-fixable; G4 is the broken residue.

**How it breaks & why** (8038): reverts at **EXTCODESIZE**, depth 3+. Top mode `Error(string): TR` (1,530, 31.7%), `state_gas_category: access_list`, **access_list_entries p50 306 / p90 1,096**, `surcharge_at_oog` p50 6,700. **Access-list/cold-access repricing consumes the budget before an inner call check, which returns empty and Settler reverts "TR".** A "no profit" cluster (216) is cold-account/SLOAD-driven arb economics. 8037's reservoir is a non-factor.

**Can they fix it?** Settler is **immutable/non-upgradable** by design (0x ships disposable, versioned contracts). Fix = **redeploy a new Settler version** tolerant of higher access costs + solver-side access-list/profitability recalibration. Many "no profit" reverts are correct behavior that self-corrects.

**Priority: Medium.** Meaningful 8038 volume (4,826, a floor) on an immutable contract, but no failure_rate denominator, 0x already ships rapidly-versioned Settlers, and a share of breaks self-correct.

**Suggested outreach.** "Under EIP-8038 we see ~4,800 mainnet Settler txs that succeed today but would revert (mostly 'TR'/empty reverts at your EXTCODESIZE call-checks from higher cold-access/access-list costs, plus some 'no profit' aborts). Because Settler is immutable, this needs a new version with more headroom for repriced access costs, plus solver-side recalibration. eip-8037 shows only trivial impact. Happy to share failing tx hashes and per-cluster gas deltas."

---

### DEXes & pools (largely economic / integrator-side)

## Uniswap

**What it is.** Affected contracts span the immutable **V2 Router** (`0x7a25…488d`), **V3 Router**/pools, and **V4 PoolManager**/**Universal Router**. All non-proxy, non-upgradable.

**Contracts & roles** (headline). **V2 Router** `0x7a250d5630b4cf539739df2c5dacb4c659f2488d` — 8038: entry **238,006 (all OOG)**. V4 PoolManager `0x000000000004444c5dc75cb358380d2e3de08a90` — 8037: revert_site 2,513. V3 pools `0xc7bbec…0e9b` (8038 revert 16,107), `0xe0554a…939f` (8038 revert 15,534). V3 Router `0xe592…1564` (8038 revert 3,231).

**Scale & failure rate.** Overwhelmingly one place: **V2 Router under 8038 — 238,006 G4, all OOG, all on selector `0x791ac947`** (`swapExactTokensForTokensSupportingFeeOnTransferTokens`). The shard's `failure_rate` reads ~0% (halt lands inside the token, not the Router) — **treat 238k as the real count and ~0% as a measurement artifact/floor**. Elsewhere: small and MEV-bot-dominated.

**How it breaks & why.**
- **V2 (the one that matters):** 100% `oog_bottleneck_kind: Stipend2300` at SLOAD, depth 3, inside fee-on-transfer tokens. **The 2300-gas transfer stipend** no longer fits the token's transfer logic (p50 10 SLOADs, 2 cold accounts) once 8038 raises cold access to 3,000. The stipend is a hardcoded constant → limit bump can't help. gas_delta strongly negative (dies early).
- **V3/V4:** mostly **arbitrage bots** (Aave/Balancer flashloan users, StrategyExecutor) hitting "TR"/"no profit"/`Panic(0x11)` after `access_list`-heavy repricing (positive gas_delta) or arithmetic underflow — **not core swap-logic breakage for ordinary users**.

**Which EIP hurts more.** **8038 decisively** (the V2 cluster). 8037 barely touches Uniswap (reservoir 0).

**Can they fix it?** All affected contracts are **immutable/non-upgradable** — **not fixable on Uniswap's side**. The V2 issue is on the **fee-on-transfer token contracts** (assume 2300 gas suffices) and **integrators** calling that path. V3/V4 "breaks" are arb bots re-tuning.

**Priority: Medium.** 238k is real and large but unfixable on Uniswap's side, confined to legacy V2 fee-on-transfer + specific tokens — more a **policy signal to EIP authors** (2300 stipend vs cold-access repricing) than an emergency for Uniswap Labs. Worth a heads-up to Uniswap + major integrators.

**Suggested outreach.** "Under EIP-8038 we measured ~238k historical Uniswap V2 Router transactions on the fee-on-transfer swap path (`0x791ac947`) that would now run out of gas — the 2,300-gas transfer stipend is too small once cold-SLOAD/account-access is repriced, and a gas-limit increase does not fix it. Since the V2 Router is immutable, this can't be patched on-chain; we'd like to flag it so you can advise integrators and fee-on-transfer token teams. V3/V4 exposure is minimal and limited to arbitrage bots."

## Curve

**What it is.** The Curve.fi `tricrypto` pool (`0xd51a44d3fae010294c616388b506acda1bfaae46`), a **Vyper** contract. Never called directly here — every affected tx enters via routers/strategies (chiefly one `StrategyExecutor`).

**Contracts & roles.** 8038: revert_site 1,145 + oog_site 2. 8037: revert_site 92.

**Scale & failure rate.** `failure_rate` null. **8038: ~1,148 G4** over 1,146 blocks, ~97% via one `StrategyExecutor` (`0x5050e086…`). 8037: 92. (G3 small, af 30 → genuinely new failures.)

**How it breaks & why.** Dominant (939 tx, 82%): `Error(string): "33"` revert at EXTCODESIZE, selector `0x78e111f6`, depth 4 (a second, "34", is 177). Drivers: no state creation, but cold_account p50 6 / SLOAD p50 31, positive gas_delta ~+129k — **8038's cold-access + SSTORE repricing inflates the pool's internal accounting, tripping a Vyper assert/slippage revert**. A `Panic(0x11)` cluster (both EIPs) is Vyper safe-math overflow with large negative gas_delta (dies early). **Vyper-specific**: bare numeric revert strings ("33"/"34"/"TR") and `Panic(0x11)` from Vyper's auto-inserted bounds checks.

**Which EIP hurts more.** **8038** (~12× more). 8037 hurts a smaller slice (large-access-list "TR" + shared arithmetic panics).

**Can they fix it?** Curve pools are immutable Vyper deployments; never the entry (all via routers). **Warn the integrators** (dominant `StrategyExecutor` operator) — widen slippage/gas headroom, avoid oversized access lists, or route around. Contacting the dominant integrator resolves most of it.

**Priority: Medium.** Real, concentrated 8038 volume on an immutable contract, but no failure_rate and traffic funnels through a single integrator (narrow, high-leverage outreach).

**Suggested outreach.** "Under EIP-8038, ~1,100+ of your swaps routed through the Curve tricrypto pool (`0xd51a44d3…`) would newly revert (Vyper '33'/'34' assertions and arithmetic panics), and a gas-limit increase does not fix them. The Curve pool is immutable, so the fix is on your side — review slippage/gas headroom and access-list sizing on that route, and consider routing around this pool. Happy to share failing tx hashes."

## PancakeSwap V3

**What it is.** `PancakeV3Pool` (`0x1445f32d1a74872ba41f3d8cf4022e9996120b31`), an immutable concentrated-liquidity pool clone, hit as an inner call site (never entry).

**Contracts & roles.** 8038: revert_site 1,682 + oog_site 8. 8037: 8 + 1.

**Scale & failure rate.** `failure_rate` null. **8038: 1,690 G4** (revert-dominated). 8037: 9 (negligible).

**How it breaks & why.** `EXTCODESIZE` reverts dominate: `Error(string): "ctdt"` (275) with cold_account p50 6 / SLOAD p50 22 (no SSTORE) — **8038 cold-access repricing** inflates the callback/verification path until a require fires. Several `access_list`-driven clusters (72/54/52/43) with high access-list entries. One SSTORE OOG halt (immaterial). Non-OOG → limit bump can't help.

**Which EIP hurts more.** **8038** (~188× vs 8037) — Pancake swaps are SLOAD/cold-access heavy, write-light.

**Can they fix it?** **Immutable pool** — no in-place patch; fix would be a new pool/router + migration. Because it's always an inner site, breakage surfaces in **integrators** (routers, StrategyExecutor, flashloan users) who must widen callback gas assumptions.

**Priority: Medium.** Meaningful 8038 volume, but no failure_rate, pool immutable (fix on integrators), 8037 negligible.

**Suggested outreach.** "Under EIP-8038, ~1,690 transactions that succeed today break when routed through your immutable PancakeSwap V3 pool at 0x1445…0b31 — overwhelmingly reverts on the swap/callback path (EXTCODESIZE checks and access-list-heavy calls) from higher cold-access/SSTORE pricing, not fixable by raising the gas limit. eip-8037 barely affects you. Since the pool is non-upgradable, the practical fix sits with your routers and integrators: please re-test pool-callback gas assumptions against the 8038 schedule."

## Balancer

**What it is.** The Balancer V2 Vault (`0xba12222222228d8ba445958a75a0704d566bf2c8`), the monolithic contract settling every swap/join/exit/flash-loan. Always an inner revert site (never entry).

**Contracts & roles.** 8038: revert_site 18,920 + oog_site 2. 8037: revert_site 348.

**Scale & failure rate.** `failure_rate` null (data gap — proportional severity unknown). **8038: 18,922 G4** (~54× the 8037 count). Top callers: "Aave v3 Flashloan User" (11,687), "StrategyExecutor" (5,029).

**How it breaks & why.** Non-OOG reverts at depth 3+ inside the Vault — **downstream profit/slippage guards unmasked by higher gas cost, not gas exhaustion at the Vault**. Largest cluster (5,029): `Error(string): TR`, `state_gas_category: access_list`, **access-list p50 249 / p90 729**, surcharge_at_oog p50 4,850 — large multi-hop arb/flashloan txs whose profitability no longer clears once 8038 makes access expensive; they revert on `TR`/"no profit"/`:(`/`rltc`. 8037 shows zero reservoir pressure.

**Which EIP hurts more.** **8038** (~54×), driven by access-list/cold-access surcharges. 8037 nearly irrelevant.

**Can they fix it?** Vault is **immutable** — and the breakage is in the **calling arb/flashloan strategies**, not the Vault. These bots reprice automatically; most reverting txs simply won't be submitted. No code fix Balancer owes. **Warn searcher integrators, not Balancer.**

**Priority: Low-to-Medium.** 18,922 looks alarming but the Vault is immutable, failures are third-party profit guards, and failure_rate is null. FYI to Balancer + a heads-up to major searchers.

**Suggested outreach.** "Under EIP-8038, ~19k historical transactions routed through the Balancer V2 Vault would flip from success to revert — but on inspection these are third-party arbitrage/flashloan strategies hitting their own profit/slippage guards once the trade stops clearing, not failures in the Vault. The Vault is immutable and needs no change; we wanted to flag that marginal-arb volume through the Vault will drop (and note we couldn't compute a real-traffic failure rate for this address)."

## Sushi

**What it is.** Three SushiSwap RouteProcessor router contracts, hit **only as internal revert sites** — every failing tx enters through MEV/arb bots (mostly `StrategyExecutor`).

**Contracts & roles.** `0xceff5175…` (8038 revert 327 / 8037 3), `0x06da0fd4…` (293 / 6), `0x397ff154…` (275 / 8). All `revert_site`.

**Scale & failure rate.** `failure_rate` null. **8038: 895 combined**; 8037: 17. Small.

**How it breaks & why.** Dominant (~72–76%): selector `0x78e111f6`, revert at EXTCODESIZE, depth 4, `Error(string): "19"`/"20". Drivers: ~4–5 cold accounts, ~11–15 SLOADs, no access-list/SSTORE — **8038 cold-access repricing** starves a deeply-nested routing frame, tripping a Sushi require. Tail clusters are access-list-driven. Not user swaps — automated arbitrage.

**Which EIP hurts more.** **8038** (~50×).

**Can they fix it?** Identity unknown; RouteProcessors are versioned redeploys in practice. Actionable audience is the **bot operators** and Sushi's routing/SDK team (cold-access gas budgeting, access-list prewarming).

**Priority: Low.** Small counts, no failure_rate, automated-arb flow, inner revert-site only.

**Suggested outreach.** "Under EIP-8038, a few hundred historical arbitrage/routing transactions through your RouteProcessor contracts start to revert mid-route — a deeply-nested call reading many cold accounts/slots, where the added cold-access cost trips a require ('19'/'20'). A raised gas limit does not rescue these, and EIP-8037 is essentially unaffected; you and your integrators may want to review cold-access gas budgeting and access-list prewarming."

---

### Immutable tokens & stablecoins (warn the integrators)

## Circle (USDC)

**What it is.** USDC (Circle) — an ERC-20 behind an upgradeable proxy (`0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48`) whose logic is the immutable **FiatTokenV2_2** (`0x43506849d7c04f9138d1a2050bbf3a0c054402dd`). The implementation runs via DELEGATECALL, so it shows up as a halt/revert *site*, not entry.

**Contracts & roles.**
- **USDC proxy** (`erc20`, `is_proxy: true`) — 8037: revert_site 3,627 + oog_site 23. 8038: 840 + 40.
- **FiatTokenV2_2 impl** — 8037: **oog_site 41,771 + revert_site 41,772** (heaviest). 8038: 350 + 302.

**Scale & failure rate.** `failure_rate` null in all four shards (gap). Worst: **8037 on the impl — ~41.8k OOG halts + ~41.8k reverts landing inside USDC's transfer code** (depth 4–10), entered by third parties (top `0x9ccc…b294` "Proxy contract" 27,036; CoW `settle` 6,571). Lower bounds (drill-in cap + low clusters_shown_share 0.30).

**How it breaks & why.**
- **8037:** OOG on **SSTORE**/LOG3 inside FiatTokenV2_2 (depth 4–10), `custom:0x93cfa3ee` reverts, negative gas_delta. Drivers: cold_account p50 6–9, `surcharge_at_oog: 0`, reservoir 0 — **63/64 call-forwarding shortfalls**: 8037's upstream surcharges shrink the gas passed into the deeply-nested USDC call, which OOGs mid-SSTORE.
- **8038:** dominant mode is **`Panic(0x11)` (arithmetic overflow) at RETURNDATASIZE**, depth 7–14, huge negative gas_delta — **not USDC's logic** but the **caller's** accounting underflowing when USDC returns a (correct) different value under the repriced schedule.

**Which EIP hurts more.** **8037** by footprint (~83.5k impl sites vs ~650 for 8038).

**Can they fix it?** USDC's proxy **is** admin-upgradable by Circle (the `is_upgradable: false` reflects the current impl bytecode, not the proxy). But the fix is **largely not Circle's** — USDC's transfer logic is standard and not the bug. The parties to change code are the **integrators** (CoW, SocketBatcher, StrategyExecutor bots, Aave-flashloan users, proxy routers). Inform Circle for coordination.

**Priority: Medium.** Very large absolute footprint (~83k impl sites, 8037), but the failing logic is in third-party integrators and the missing denominator means the % of USDC's enormous traffic is likely tiny. Warn Circle for awareness; the actionable outreach is the integrators.

**Suggested outreach.** "Under EIP-8037/8038, tens of thousands of transactions that succeed today would fail with USDC transfers as the halt/revert site — driven by gas-forwarding shortfalls (8037) and arithmetic-panic reverts in the calling contracts (8038), not by USDC's own logic. USDC's proxy is upgradeable so Circle retains flexibility, but the breaks originate in integrator call paths (DEX settlement, bridges, strategy/flashloan bots). We wanted to flag it for awareness and ask whether Circle would help coordinate testing with major integrators."

## Tether (USDT)

**What it is.** USDT (`0xdac17f958d2ee523a2206206994597c13d831ec7`), an **immutable** ERC-20, plus a Tether-owned TransparentUpgradeableProxy (`0x68749665…`). USDT shows up almost entirely as a halt/revert *site* inside others' call trees.

**Contracts & roles.** USDT — 8037: oog_site 7,910 + revert_site 7,912. 8038: 330 + 1. Tether proxy — 8037: 220. 8038: 26.

**Scale & failure rate.** `failure_rate` null (lower bound — very high traffic). Worst: **8037 on USDT — ~7,910 OOG + ~7,912 reverts** (dominant cluster selector `0x1bc74526`, 5,412). 8038: 330 halts.

**How it breaks & why.**
- **8037:** SSTORE `storage_heavy` OOG at depth 6; `surcharge_at_oog: 0`, reservoir 0 — the tx runs out **upstream** (cold-account touches earlier in a 6-deep tree) and dies at USDT's SSTORE. Structural to how the caller forwards gas.
- **8038:** `CALL` `call_chain` OOG (depth 7–14), `FractionalGas`, real `surcharge_at_oog` (p50 300–1300), high cold-account — the **cold-access tax + 63/64 forwarding** starves the innermost frame; USDT is just last on the stack.
- **Tether proxy:** `Panic(0x11)` arithmetic in the *caller's* logic (large negative gas_delta).

**Which EIP hurts more.** **8037 decisively** (~7.9k halts vs 330). 8037's damage is indirect; 8038's smaller footprint is the direct cold-access tax.

**Can they fix it?** **USDT: no in-place fix** (immutable) — and it's not the bug. USDT's non-standard no-boolean-return ERC-20 is orthogonal to these breaks, but it's why integrators wrap it defensively, and those wrappers' fixed gas assumptions + post-call arithmetic are the fragility. **Warn integrators** (aggregators, flashloan strategies, batchers, AA bundlers) — stop hardcoding gas stipends for USDT interactions.

**Priority: Medium.** Large 8037 volume, but token immutable/innocent, no failure_rate, fix spread across many integrators.

**Suggested outreach.** "Under EIP-8037 (and to a lesser degree 8038), thousands of transactions that succeed today fail when they route a USDT transfer deep inside a call tree — halting OOG on USDT's SSTORE, or reverting with Panic(0x11) in the calling contract. USDT itself is immutable and not the bug; the exposure is in integrator code assuming today's gas cost for USDT or forwarding gas via 63/64 into deep call chains. If your contracts route USDT, please re-simulate against the repriced schedules and remove hardcoded gas stipends."

## Aave

**What it is.** The **V3 default aToken implementation** (`0xadc4…7ada`, shared logic behind every V3 aToken) and **GHO** (`0x40d1…6c2f`). Most failing traffic routes through CoW `GPv2Settlement` and MEV/solver bots.

**Contracts & roles.** aToken impl — 8037: oog_site 5,451 + revert_site 5,468. 8038: 3 + 815. GHO — 8037: 1,012 + 1,012 (no 8038 shard).

**Scale & failure rate.** `failure_rate` null (lower bounds). Worst: **8037 aToken impl ~5,451 OOG + ~5,468 reverts** + GHO ~1,012 each, dominated by CoW `settle` (`0x13d79a0b`). 8038 aToken: 815 reverts.

**How it breaks & why.**
- **8037:** OOG on **SSTORE** inside CoW settlement (depth 3–5), `storage_heavy`, `custom:0xfb8f41b2`/`Panic(0x11)`, large access lists (p50 ~120). Cumulative 8037 batch cost starves the aToken/GHO write via 63/64 forwarding. Negative gas_delta (dies early).
- **8038:** `EXTCODESIZE` cold-access reverts (`TR`, custom errors) via DeFiSaver StrategyExecutor; access-list p50 134, positive gas_delta.

**Which EIP hurts more.** **8037** (hits both contracts; 8038 leaves GHO untouched).

**Can they fix it?** Both are immutable as-deployed (`is_upgradable: false`); redeploy would be an Aave-governance action. Real remediation is with **CoW settlement / DeFiSaver strategy** integrators (batch sizing, gas budgeting). Warn integrators.

**Priority: Medium.** Meaningful 8037 counts on immutable contracts, but no denominator and breaks concentrated in a few integrators. Escalate if a denominator shows a non-trivial settlement halt_rate.

**Suggested outreach.** "Under EIP-8037, several thousand transactions moving Aave V3 aTokens and GHO inside CoW batched settlement fail OOG at an internal SSTORE — and because they die in a call-forwarded inner frame, raising the gas limit does not rescue them. A smaller set breaks under EIP-8038 via cold-access reverts in DeFiSaver flows. Since the aToken implementation and GHO are immutable, the fix lives with the settlement/strategy integrators (batch sizing, gas budgeting); we'd like to share failing traces."

## MakerDAO (DAI)

**What it is.** The DAI stablecoin (`0x6b175474e89094c44da98b954eedeac495271d0f`). Not the entry — called deep inside other protocols (top caller CoW `settle`); the break lands in DAI's `transferFrom`.

**Contracts & roles.** 8037: oog_site 1,540 + revert_site 1,540 (pair 1:1 → ~1,540 distinct txs). 8038: 8 halts.

**Scale & failure rate.** `failure_rate` null (floor). **8037: ~1,540 distinct G4** — selectors `0x13d79a0b` (CoW settle, 655), `0x4a7cf362` (460), `0x6d7b7040` (284), all OOG at DAI's SSTORE (depth 3–4). (G3 = 215,962 — most affected DAI txs are gas-fixable; G4 is the residue.) 8038: 8.

**How it breaks & why.** CoW batches: OOG at DAI `SSTORE` inside `transferFrom`, revert `Dai/insufficient-allowance`. Driver not SSTORE/cold-heavy but **access-list-heavy** (p50 34, p90 66) — **8037's state-creation surcharge across a large per-tx access footprint** in a multi-leg batch. Negative gas_delta. Second cluster (460) is colder-access. 8038's 8 txs are deep 63/64 shortfalls (immaterial).

**Which EIP hurts more.** **8037** (~1,540 vs 8).

**Can they fix it?** **No — DAI is immutable** (`is_upgradable: false`, manual/high confidence). DAI is a passive callee; the break is in the callers' batched flows. **Warn integrators** (CoW/GPv2Settlement, aggregators, bots), not MakerDAO/Sky.

**Priority: Medium.** Modest steady count, DAI unfixable — but concentrated in a few high-value integrators (CoW is #1 caller); true rate unknown. Outreach to integrators.

**Suggested outreach (to integrators, e.g. CoW).** "Under EIP-8037, ~1,540 of your DAI-touching settlement/aggregation batches would revert or run out of gas at DAI's internal `transferFrom` storage write — the txs die earlier than today (often negative net gas), so raising the gas limit does not rescue them. The trigger is the new per-tx state-creation surcharge applied across your batches' large access-list footprint (p50 ~34 entries). DAI itself is immutable; the fix has to be on the integration side. Happy to share failing tx hashes."

## Lido

**What it is.** Lido's liquid-staking tokens: **stETH** (`0xae7ab9…`) and a second token at `0x7f39c5…` (labeled "lido"; behavior consistent with **wstETH**). Integrated across DeFi.

**Contracts & roles.** stETH (`is_proxy: true`, `is_upgradable: false`) — 8038: revert_site 433. 8037: 115. `0x7f39c5…` — 8037: oog_site 775 + revert_site 775. 8038: 3 + 4.

**Scale & failure rate.** `failure_rate` null (raw counts only). Worst: **8037 `0x7f39c5…` token — 775 OOG + 775 reverts**; **8038 stETH — 433 reverts**.

**How it breaks & why.**
- **8037 (`0x7f39c5…`):** storage-heavy OOG at **SSTORE** inside CoW/DEX settlement (`0x13d79a0b`), `ERC20: transfer amount exceeds allowance`/`C19`. Negative gas_delta; state-creation surcharge tips it over.
- **8038 (stETH):** non-OOG reverts at **EXTCODESIZE**, `Error(string): "no profit"` — **MEV/arb bundles** (Aave flashloan, StrategyExecutor) whose profitability flips under cold-access/access-list repricing.

**Which EIP hurts more.** Token-dependent: 8037 dominates `0x7f39c5…`; 8038 dominates stETH. Combined, 8037 has the larger footprint.

**Can they fix it?** stETH is a proxy but `is_upgradable: false`; neither token is where the logic fails (all sites reached from third parties). **Warn integrators** (CoW, flashloan bots, StrategyExecutor, SocketBatcher) — Lido itself likely needs no change.

**Priority: Medium.** Non-trivial counts on a systemically-important protocol, but failure_rate unknown and breakage lives in integrators. Warn the integrator ecosystem.

**Suggested outreach.** "Our replay of EIP-8037/8038 shows transactions touching Lido's stETH/wstETH breaking — but the failures land in *integrator* code, not the tokens: CoW-style settlement SSTORE paths (8037) and flash-loan 'no-profit' guards (8038). These are not fixable by raising the gas limit; the affected logic (settlement accounting, gas-dependent profit branches) needs review. We can share tx hashes and the per-cluster breakdown."

## XEN Crypto

**What it is.** XEN Crypto (`0x06450dee7fd2fb8e39061434babcfc05599a6fb8`), an immutable ERC-20 "fair launch" token driven by permissionless mass minting via throwaway proxy/minter contracts.

**Contracts & roles.** 8037: oog_site 1,811 + revert_site 1,897. 8038: oog_site 2,747. Entry contracts are batch-minter bots.

**Scale & failure rate.** `failure_rate` null (floor). **8038: 2,747 halts** (`approve` 2,402, `transfer` 324). 8037: 3,708 events (halt+revert of the same txs).

**How it breaks & why.**
- **8038 (dominant):** `approve` OOG at `CALL`, depth 8, in a long minter chain. p50 356 SLOADs / 36 cold accounts, **hugely positive gas_delta** (avg +4.24M, up to +49.6M) — cold-access + SSTORE repricing across the deep call tree; 63/64 forwarding starves an inner `CALL`.
- **8037:** SSTORE/POP `storage_heavy`/loop OOG (p50 ~100+ SSTOREs/SLOADs), negative gas_delta. **Notably, 8037's creation-surcharge is NOT the mechanism** — reservoir/spillover/surcharge all 0; the minimal-proxy shim (`0x3d602d80`) appears but the breaks are storage-heavy mint/approve loops, not CREATE charges.

**Which EIP hurts more.** **8038** (higher halt count + actively inflates gas by millions).

**Can they fix it?** **No — XEN is immutable.** Warn the **batch-minter integrators** (top entries `0x66a3c2fa…cabaf`, `0x3fc29836…0ead`) to reduce per-tx SLOAD/SSTORE/cold-access fan-out or split across smaller txs.

**Priority: Medium.** Thousands of confirmed breaks on an unfixable target, but traffic is automated mass-minting (not end-user funds) and the actionable audience is a handful of integrators.

**Suggested outreach.** "Under EIP-8037/8038 we see thousands of currently-succeeding XEN `approve`/`transfer`/mint transactions that would fail and that a gas-limit increase cannot rescue; eip-8038 is the sharper hit, inflating gas by millions across your minter call chains via cold-account and storage-write repricing. Since the XEN token is immutable, the fix has to happen in your batch-minting integrations — reducing per-transaction storage/cold-account fan-out or splitting across smaller transactions."

---

### Oracle / infra

## SKALE (arbitrage proxy)

**What it is.** An EIP-1967 transparent upgradeable proxy attributed to **skale** (`0xc04a10fd5e6513242558f47331568abd6185a310`) behaving as an arbitrage/MEV executor. Appears **only under 8037**, only as a `revert_site`.

**Contracts & roles.** 8037: revert_site 6,289 (entered exclusively by one "Aave v3 Flashloan User", in a ~390-block burst). Not affected under 8038.

**How it breaks & why.** Both clusters (100%): selector `0x0053be41`, `non_oog` revert at RETURNDATASIZE, `Error(string): no profit`. A flash-loan arb bot that **deliberately reverts when unprofitable**. 8037's per-tx surcharge (~12 cold-account touches, ~26 SLOADs) raises effective cost and flips the profitability check. gas_delta constant −10,822 (bails out early) — a classic economic branch flip, not resource exhaustion.

**Can they fix it?** Proxy **is** upgradable (`eip1967_transparent`), but there's nothing to fix — the reverts are the bot declining unprofitable trades. The operator will re-tune its profitability model automatically. No user funds or protocol at risk.

**Priority: Low.** 6,289 self-imposed "no profit" reverts from one bot in one burst; intended safety valve, not a broken dapp.

**Suggested outreach.** "Under EIP-8037 we observed ~6,289 of your flash-loan arbitrage transactions (proxy `0xc04a…a310`) reverting with your own 'no profit' guard, because the state-creation surcharge (~12 cold-account touches per tx) raises effective execution cost and flips your profitability check. Nothing is broken — but you'll want to recalibrate your profitability thresholds to the new schedule so you don't leave viable arbs on the table."

## Chainlink

**What it is.** Chainlink's `EACAggregatorProxy` ETH/USD feed (`0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419`), read by DeFi via `latestAnswer()`/`latestRoundData()`. Appears **only as a revert_site** — never entry/OOG.

**Contracts & roles.** 8038: revert_site 328. 8037: 21.

**Scale & failure rate.** `failure_rate` null (floors). Tiny counts. Failing txs enter via arb/flashloan bots + EntryPoint v0.6 and revert while touching the feed deep in the tree (depth 3–11).

**How it breaks & why.** Not the oracle's logic — reverts bubble through it while on the stack of larger txs. 8038 modes: `Error(string): "no profit"` (120, arb guard), `access_list`-driven empty reverts (104), `"NP"` (58). **8038's cold-access/access-list surcharge (~+170–190k gas) pushes profit-gated arb txs below threshold.** Several 8037 modes have negative gas_delta yet still revert — logic-branch flips, not OOG.

**Which EIP hurts more.** **8038** (~15.6×).

**Can they fix it?** Immutable, and blameless (only ever the revert site). **Chainlink has nothing to patch.** Warn the **integrating bot/strategy operators** to recalibrate profit thresholds.

**Priority: Low.** Small counts, no denominator, oracle immutable/blameless, self-correcting arb economics.

**Suggested outreach.** "Heads-up: under EIP-8038, ~328 mainnet transactions that read the Chainlink ETH/USD `EACAggregatorProxy` (0x5f4e…8419) reverted — not in the oracle, but in caller-side 'no profit'/threshold guards after cold-access and access-list costs rose. The feed itself is fine and needs no change; if you operate arbitrage/flash-loan strategies that read this feed, please re-check hardcoded gas/profitability branches against the new schedule."

---

## Appendix A — long-tail identifiable projects (no dedicated analysis)

Labeled affected projects below the dedicated-analysis threshold (total footprint =
sum of entry + halt + revert role counts across both schedules). Most are the same
patterns as above (immutable pools/tokens hit as inner sites, or arb-bot economic
reverts). Worth a lightweight heads-up only if a specific team is easy to reach.

| Footprint | 8037 | 8038 | Project / label | Note |
| --- | --- | --- | --- | --- |
| 4,483 | 0 | 4,483 | **Safe4337Module** | AA — Gnosis Safe's ERC-4337 module (same class as ZeroDev/Alchemy; warn Safe) |
| 2,561 | 2,561 | 0 | quasi.bot | MEV/arb bot (self-correcting) |
| 1,454 | 674 | 780 | Erc_20: Ethereum_games | XEN-adjacent minter front-end |
| 1,361 | 245 | 1,116 | Supernova (AlgebraPool) | DEX pool (integrator-side) |
| 1,192 | 1,190 | 2 | TetherToken (2nd contract) | fold into Tether outreach |
| 896 | 29 | 867 | Synthetix | pool/proxy inner site |
| 747 | 736 | 11 | LI.FI / Socket Bridge | bridge aggregator — worth a heads-up |
| 741 | 482 | 259 | Bancor | DEX (converters) |
| 586 | 306 | 280 | **Uniswap Permit2** | fold into Uniswap outreach |
| 540 | 3 | 537 | FunFair | token |
| 486 | 273 | 213 | Fluid / Instadapp (FluidLiquidityProxy) | defi_complex — proxy, likely upgradable |
| 387 | 386 | 1 | Ambient (CrocSwapDex) | DEX |
| 365 | 14 | 351 | MultiSign | wallet |
| 340 | 292 | 48 | Gnosis Safe (SafeL2) | wallet (see Safe4337Module) |
| 279 | 0 | 279 | Beefy (StrategyBeefy) | yield strategy |
| 262 | 259 | 3 | Bebop (BebopSettlement) | DEX aggregator |
| 262 | 196 | 66 | Paxos Gold | token |
| 221 | 6 | 215 | Integral (SIZE) | DEX |
| 202 | 202 | 0 | GasToken | (largely obsolete) |
| 192 | 114 | 78 | KyberSwap (MetaAggregationRouter v2) | DEX aggregator — worth a heads-up |
| 134 | 134 | 0 | Liquity | stablecoin protocol |
| 125 | 45 | 80 | Mooniswap | legacy 1inch AMM |
| 111 | 2 | 109 | Convex | yield |
| 109 | 109 | 0 | Ondo (TokenProxy) | RWA stablecoin — proxy |

Below ~80 footprint: dozens of tokens/protocols with single- to double-digit G4
counts (Curve satellites, dYdX, Compound v2, Bancor pools, NFTX, Pendle, Frax,
Synthetix satellites, etc.) — statistically indistinguishable from noise;
individual outreach not warranted.

## Appendix B — large *unattributable* cohorts (not outreach targets)

These carry huge G4 counts but resolve to no reachable team — generic
labels/heuristics. They are mostly **MEV/arbitrage bots and flashloan searchers**
(self-correcting) and **freshly-deployed accounts** (the 8037 deploy-OOG story,
already counted under eth-infinitism). Listed for completeness, not for outreach.

| 8037 | 8038 | Cohort | What it is |
| --- | --- | --- | --- |
| 55,807 | 130,715 | "Flashloan User" (Aave/Balancer) | arb/flashloan bots — economic reverts, self-correct |
| 135,430 | 20,243 | "Proxy contract" | unlabeled proxies (incl. Across relayer front, minter fronts) |
| 9,073 | 136,296 | "Contract Deployer" | fee-on-transfer token deploys / halt sites (Uniswap V2 story) |
| 32,105 | 42,100 | "StrategyExecutor" | MEV/arb executors — "no profit" reverts, self-correct |
| 10,906 | 14,864 | "ERC-20 token" | unlabeled tokens as inner SSTORE halt sites |

## Appendix C — methodology & caveats

- **Source.** `site/data/{eip-8037,eip-8038}/affected/` — per-contract G4
  failure-mode shards + `index.json` + `deploy_oog.json`. Pinned config
  `0xc17ac709…f37d2`, blocks 24,319,986 → 25,319,985 (~2026-01-26 → 2026-06-15).
- **Scope = G4 only** (Potentially broken): `baseline_success=true AND
  schedule_success=false AND min_multiplier_to_succeed IS NULL`. Excludes G3
  (fixable with more gas) and AF (already failing).
- **One agent per entity.** 21 dedicated analyses (above); each read only its own
  contract's shard(s) and grounded every figure in that data.
- **Lower bounds.** For high-traffic contracts the per-block drill-in cap (1024)
  under-reports halts — counts are floors.
- **Missing rates.** Many shards have no `failure_rate` (no Xatu total-tx
  denominator, esp. for pure halt/revert *sites*) — those report absolute counts
  only. Where a rate exists, it's flagged.
- **Label caveats found during analysis:** `0xbbbb…ffcb` mislabeled `morpho-org`
  is actually **0x Protocol Settler**; the `maple-labs`-owned WETH/WBTC are core
  wrapped-token contracts, not Maple; `0x7f39c5…` labeled "lido" is almost
  certainly wstETH.
