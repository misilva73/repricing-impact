# Contract & selector labeling — expansion plan

Plan to expand contract-address and function-selector labeling beyond the 40
hand-maintained entries in [`src/repricing_impact/labels.py`](../src/repricing_impact/labels.py),
adding a **category dimension** (complex DeFi / swaps / bots / bridges / … ) and
**4-byte selector decoding**, while staying inside the repo's constraints:
fully static build, no server runtime, batch-fetch-then-cache. The source survey
this plan is built on is inlined in §2; full citations in §11.

---

## 1. Goal & current state

**Today.** `labels.py` maps ~40 lowercased addresses → a flat display string
(`"Uniswap V2 Router"`). `label_address()` is called at five sites in
[`scripts/precompute.py`](../scripts/precompute.py) (lines 1234, 1286, 1498,
1636, 1672) to name recipients / divergence contracts / OOG halt contracts /
example-tx recipients in the published JSON. There is:

- **no category** — nothing groups a contract as swap vs. lending vs. bot;
- **no selector decoding** — the warehouse carries `divergence_opcode`
  (mnemonics via `opcodes.py`) but no 4-byte method IDs are named;
- **no provenance** — a label is just a string, with no source or confidence.

**Target.** Each contract resolves to a structured record:

```python
{
  "address":    "0xdac1...",
  "label":      "Tether USDT",        # display name (unchanged behaviour)
  "category":   "stablecoin",         # NEW — from the taxonomy in §3
  "owner_project": "tether",          # NEW — optional project/entity
  "source":     "manual",             # manual|oli|dune|ethlists|mev|heuristic
  "confidence": "high",               # high|medium|low
}
```

and each failing call optionally resolves its selector:
`0x38ed1739 → swapExactTokensForTokens(uint256,uint256,address[],address,uint256)`.

Backwards compatibility is a hard requirement: `label_address(addr) -> str`
keeps its exact current signature and fallback behaviour so the five precompute
call sites and the existing JSON schema do not break. Category is **additive**.

---

## 2. Source strategy

Prioritise free/open, licensing-safe sources. Compose several; resolve
conflicts by precedence (§4).

| Source | Provides | Category taxonomy | License / bulk use | Access |
| --- | --- | --- | --- | --- |
| **Dune `dune.labels`** (paid plan available) | address → name + `category` (cex/dex/dao/bridge/…), contributor, source | ✅ | SQL models open; **data export needs the paid plan we have** | API (`dune-client`) → CSV/parquet cache |
| **Open Labels Initiative (OLI)** | name, `owner_project`, `usage_category`, `is_proxy`, `erc_type`, ERC-4337 roles, `is_safe_contract` | ✅ **37-value set** (§3) | **MIT**, open label pool | oli-python / GraphQL → parquet cache |
| **ethereum-lists/contracts + /tokens** | contract → project, token metadata, decimals | ⚠️ project-level only | Open (git) | `git clone --depth 1`, parse JSON |
| **Sourcify sig DB + 4byte.directory** | 4-byte selector → signature(s) | n/a | Open, free | `api.4byte.sourcify.dev` REST + BigQuery bulk mirror |
| **mev-inspect / Flashbots Data / ZeroMEV / Dune MEV spells** | arb/sandwich/liquidation → **bot addresses** | behavioural (bots) | Open (mev-inspect deprecated → Flashbots Data) | dataset/query → address CSV |
| **On-chain heuristics** | proxy/factory/ERC-165/ERC-20-721 detection | structural | n/a (our own probes) | archive-node batch |
| ~~Etherscan name tags~~ | high-quality tags | ✅ | ❌ **terms forbid bulk reproduction / dataset use** | **excluded** |
| ~~Nansen/Arkham/Allium/Chainalysis~~ | rich entity labels | ✅ | ❌ commercial/restrictive | **excluded** |

**Why these.** Dune (we hold a paid plan) and OLI (MIT) both ship a category
dimension out of the box and are legally clean for a public dashboard.
ethereum-lists adds project attribution for the long tail. Sourcify/4byte is the
only open path to selector names. MEV bots are a *behavioural* class no registry
carries, so they must be derived. Heuristics catch what nothing labels.

---

## 3. Category taxonomy

Small, forensics-relevant enum (we explain gas-repricing failures, not build a
block explorer). Each value maps to OLI `usage_category` and/or Dune `category`
so imported labels slot in with a lookup table.

| Our `category` | Meaning | Primary signal | OLI `usage_category` |
| --- | --- | --- | --- |
| `precompile` | 0x01–0x0a | address range (already known) | — |
| `stablecoin` | USDT/USDC/DAI… | Dune/OLI + ethereum-lists | `stablecoin` |
| `token` | ERC-20/721/1155 | `erc_type` / on-chain ERC-165 | `fungible_tokens`, `non_fungible_tokens` |
| `swap_dex` | routers, pools, aggregators | Dune/OLI + owner_project | `dex`, `trading` |
| `defi_complex` | lending / derivatives / vaults / staking | Dune/OLI | `lending`, `derivative`, `yield_vaults`, `staking`, `index`, `rwa`, `insurance` |
| `bridge` | cross-chain | Dune/OLI | `bridge`, `cc_communication`, `settlement` |
| `account_abstraction` | ERC-4337 EntryPoint/paymaster/bundler | OLI `erc4337` + `is_paymaster`/`is_bundler` | `erc4337` |
| `wallet_safe` | Safe / multisig / smart wallet | OLI `is_safe_contract` | — |
| `mev_bot` | arb / sandwich / liquidation | **MEV sources only** | — (no OLI category) |
| `nft` | marketplace / collection / NFT-fi | Dune/OLI | `nft_marketplace`, `non_fungible_tokens`, `nft_fi` |
| `oracle` | price feeds etc. | Dune/OLI | `oracle` |
| `infra` | dev tools / identity / DePIN / AI / privacy | OLI | `developer_tools`, `identity`, `depin`, `ai`, `privacy`, `inscriptions` |
| `cex` | centralized-exchange deposit/hot wallets | Dune/OLI | `cex` |
| `other` | labeled but off-taxonomy | — | `gaming`, `governance`, `payments`, … |
| `unknown` | no label from any source | fallback | — |

The full OLI value set (37 values) and its structural tags (`is_proxy`,
`is_factory_contract`, `is_safe_contract`, `erc_type`, `is_blacklist.usdc/usdt`)
are documented in the OLI repo; we import the subset above and bucket the rest
into `other`. The mapping table lives in code as `OLI_CATEGORY_MAP` /
`DUNE_CATEGORY_MAP` so the taxonomy stays a single source of truth.

**Note:** OLI has no `mev_bot` category (`cybercrime` is not it). Bots come only
from the MEV tier (§2), so `mev_bot` always carries `source="mev"` and typically
`confidence="medium"` — weaker evidence than an attested OLI/Dune label, and the
dashboard should be able to show that distinction.

---

## 4. Architecture

### 4.1 New module: `src/repricing_impact/label_sources/`

One fetcher per source, each **normalising to the same record schema** (§1) and
writing a cache file. Fetchers never run on the dashboard request path — they run
at build time, like precompute.

```text
src/repricing_impact/label_sources/
  __init__.py
  schema.py        # the LabelRecord dataclass + category enum + OLI/Dune maps
  dune.py          # dune.labels pull via dune-client   -> label_cache/dune.parquet
  oli.py           # OLI label pool via oli-python/GraphQL -> label_cache/oli.parquet
  ethereum_lists.py# git clone + JSON parse             -> label_cache/ethlists.parquet
  mev.py           # Flashbots Data / ZeroMEV / Dune MEV -> label_cache/mev_bots.parquet
  selectors.py     # Sourcify/4byte bulk                -> label_cache/selectors.parquet
  heuristics.py    # on-chain probes (optional, phase 4)-> label_cache/heuristic.parquet
  build.py         # merge/resolve all sources          -> label_cache/contract_labels.parquet
```

### 4.2 Cache format & location

- **Format: parquet** (already a build-time format here; gitignored via the
  `*.parquet` rule in `.gitignore`). One file per source + one merged file.
- **Location:** a new gitignored `label_cache/` at repo root (add to
  `.gitignore` next to `data/`). Raw source pulls are **not** committed.
- **What *is* committed:** only the tiny slice of labels that actually appears in
  the published JSON, already inlined by precompute into `site/data/**` — same as
  today. We never publish the full label dump (keeps size down and sidesteps any
  redistribution ambiguity).
- **Refresh cadence: manual**, matching precompute (README §Deployment). A label
  refresh is `python -m repricing_impact.label_sources.build` before
  `scripts/precompute.py`.

### 4.3 Resolver precedence

`build.py` merges per-source parquet into one `contract_labels.parquet` keyed by
lowercased address, resolving conflicts by a fixed precedence:

```text
manual override (labels.py ADDRESS_PROJECT_LABELS)   # highest — curated truth
  > OLI attested
  > Dune labels
  > ethereum-lists project
  > MEV-derived (bot flag)
  > on-chain heuristic
  > unknown                                           # lowest — fallback
```

`mev_bot` is special: it is a **behavioural overlay**, not a replacement. A
contract can be both `swap_dex` (from OLI) and flagged as an arbitrage bot's
target; the record keeps the taxonomy category and adds a boolean `is_mev_bot` +
`mev_role` so we don't lose either signal. (Simpler alternative for phase 1:
`mev_bot` wins the `category` slot only when no other category exists.)

Every merged record records the winning `source` and a `confidence` derived from
it, so the dashboard can style/annotate low-confidence (heuristic) labels
differently from attested ones.

### 4.4 Refactor `labels.py`

Keep `ADDRESS_PROJECT_LABELS` as the **manual override layer** (highest
precedence, curated). Change the resolution functions to read the merged parquet
with a graceful fallback when the cache is absent (so tests and a fresh checkout
still work):

- `label_address(addr) -> str` — **unchanged signature/behaviour.** Now:
  merged-cache name → `ADDRESS_PROJECT_LABELS` → raw address → `"unknown"`.
- `classify_address(addr) -> LabelRecord` — **new.** Full record incl. category.
- `infer_project_label(...)` — the existing heuristic ladder
  ([`labels.py:91`](../src/repricing_impact/labels.py#L91)) becomes the
  last-resort tier inside the resolver.

Loading the parquet is memoised (`functools.lru_cache`) so precompute pays the
read once. If `label_cache/contract_labels.parquet` is missing, the resolver
transparently degrades to the hardcoded map — **current behaviour exactly**.

### 4.5 Integration into precompute

Minimal, additive. The five `label_address()` call sites keep working. Where the
JSON schema can carry more (contract-mix lists, the failing-contracts leaderboard
at [`precompute.py:1498`](../scripts/precompute.py#L1498)), add `category` /
`owner_project` fields alongside the existing `label`. Selector decoding is a new
optional field on the failing-method breakdowns, joined from
`selectors.parquet`. Update [`site/data/SCHEMA.md`](../site/data/SCHEMA.md) for
every new field.

### 4.6 Config & dependencies

- **`secrets.json`** — add `dune_api_key` (gitignored; README §Credentials
  updated). OLI onchain writes would need a key too, but we only **read**, so no
  key required for OLI.
- **`config.py`** — add `LABEL_CACHE = REPO_ROOT / "label_cache"` and source
  toggles/URLs.
- **`requirements.txt`** — add `dune-client` (Dune), `pyarrow` (parquet;
  likely already pulled by duckdb/pandas — verify), `requests` (OLI GraphQL / 4byte
  REST). No web-server deps (constraint preserved). `oli-python` optional — the
  GraphQL read endpoint can be hit with `requests` if we want to avoid the dep.
- **`.gitignore`** — add `label_cache/`.

---

## 5. Dune specifics (we have a paid plan)

- **Client:** `dune-client` (official Python SDK) reading `dune_api_key` from
  `secrets.json` via the existing `load_secrets()`.
- **Flow:** create a saved query in the Dune UI (get `query_id`) selecting from
  `dune.labels` filtered `blockchain = 'ethereum'`; execute via API; fetch
  results (CSV/parquet). Poll `execute → status → results`.
- **Schema to import:** `address, name, category, contributor, source` from
  `dune.labels` (plus `labels.owner_addresses` / `labels.contracts` if useful).
- **Cost control — two modes:**
  1. **Targeted enrichment (default, cheap):** collect the *distinct contract
     addresses that actually appear in precompute output* (recipients,
     `divergence_contract`, `oog_contract`) and query Dune for only those. Bounded
     set → low credit cost. Pass addresses as a query parameter or a temp
     address list.
  2. **Bulk pull:** full `dune.labels` for ethereum, cached once, refreshed
     rarely. Larger credit/row cost — gated on the plan tier's export limits.
- **Validation-first test:** a saved query returning `dune.labels` rows for ~20
  addresses we already hardcode (USDT, Uniswap routers, EntryPoints) — confirms
  schema + `category` values + credit cost before scaling. See §8.

**Licensing:** Dune Spellbook SQL is open; **label data export is a paid-plan
feature we hold**. We publish only the small enriched slice inlined into
`site/data`, not the raw Dune dump.

---

## 6. Selector decoding (Sourcify / 4byte)

- **Bulk:** the Sourcify signature DB (openchain + 4byte + etherface + verified
  contracts, ~4.7M sigs) has a BigQuery mirror for bulk download → cache as
  `selectors.parquet` (`selector, text_signature, source`).
- **API fallback:** `api.4byte.sourcify.dev` (openchain-compatible) for
  on-demand lookups of the handful of selectors that dominate our failure tables.
- **Collisions are real** (1.2M+ sigs share the 4-byte space): store **all**
  candidate signatures per selector; disambiguate using the contract's category
  where possible (e.g. a `swap_dex` contract's `0x38ed1739` is the Uniswap
  `swapExactTokensForTokens`, not a colliding junk signature).
- **Source of selectors in our data:** we need the first 4 bytes of transaction
  calldata / the failing internal call's input. Confirm what the warehouse
  exposes (calldata may not be present — `trace_payload` is off-limits per the
  warehouse rules). If calldata is unavailable, selector decoding is **descoped
  to phase 5** pending a producer-side field (cross-ref
  [`producer-data-recommendations.md`](producer-data-recommendations.md) — note
  that doc is now marked **SHIPPED**, but only for its Recommendations 1 and 2 in
  producer v11; **no calldata/selector field shipped**, so this descope still
  stands).

---

## 7. MEV bot tier

- **Primary:** Flashbots Data (historical mev-inspect output — mev-inspect-py
  itself is deprecated) and/or ZeroMEV API for arb/sandwich/liquidation labels.
  Note ZeroMEV's convention: the `sandwich` label denotes the **victim**, not the
  attacker — extract attacker addresses from frontrun/backrun senders.
- **Alternative:** Dune MEV spells (arbitrage/sandwich/liquidation collections) —
  convenient since we already have the Dune plan; join on our contract set.
- **Output:** `mev_bots.parquet` (`address, mev_role ∈ {arb,sandwich,liquidation}`),
  merged as the `is_mev_bot` overlay (§4.3), `confidence="medium"`.

---

## 8. Phased rollout

1. **Phase 0 — scaffolding.** Add `label_sources/` package, `schema.py`
   (`LabelRecord` + category enum + OLI/Dune maps), `build.py` skeleton, cache
   dir + gitignore, requirements. `label_address()` refactor to read merged
   parquet **with fallback to today's map** (no behaviour change when cache
   absent). Tests green.
2. **Phase 1 — Dune (validate → enrich).** Add `dune_api_key`, `dune.py`. Run the
   ~20-address validation query (§5). Then targeted-enrichment pull for addresses
   in current precompute output. Populate `category`. Wire `category` into the
   failing-contracts JSON + `SCHEMA.md`.
3. **Phase 2 — OLI.** Add `oli.py` (GraphQL read). Merge with precedence below
   Dune-or-above per §4.3 (decide final order after comparing coverage on our
   contract set). Adds `owner_project`, structural tags, AA roles.
4. **Phase 3 — ethereum-lists.** `git clone` the two repos, parse, fill project
   attribution for the long tail.
5. **Phase 4 — MEV bots + heuristics.** `mev.py` overlay; optional on-chain
   `heuristics.py` (ERC-165 / EIP-1967 / factory-of / ERC-20-721) for the
   still-unknown tail.
6. **Phase 5 — selector decoding.** Gated on calldata availability (§6). Sourcify
   bulk cache + join into failing-method breakdowns.

Each phase is independently shippable and leaves the dashboard working.

---

## 9. Testing

- **Unit:** resolver precedence (manual > OLI > Dune > … > unknown), category
  mapping, cache-absent fallback to the hardcoded map (parity with current
  `label_address`). Extend `tests/`.
- **Fixtures:** small synthetic per-source parquet under `tests/` so the merge is
  tested without network. No live API calls in CI.
- **Golden:** re-run precompute on `fixtures_scratch/` and assert the enriched
  JSON matches expected `category`/`label` for a known set of addresses.
- **Manual smoke:** the Dune validation query (§5); an OLI GraphQL read for a few
  known addresses; a 4byte lookup for `0x38ed1739`.

---

## 10. Risks & open questions

- **Calldata for selectors.** May not be in the warehouse (no `trace_payload`).
  If absent, selector decoding needs a producer-side field — descope to phase 5.
- **Dune credits.** Bulk `dune.labels` export cost depends on plan tier; default
  to targeted enrichment. Measure with the validation query first.
- **OLI coverage/quality.** Community-submitted → uneven on obscure contracts,
  variable quality by submitter. The manual override layer stays the source of
  truth for the contracts that dominate our failure tables.
- **4-byte collisions.** Keep all candidate signatures; disambiguate by category.
- **mev-inspect deprecated.** Use Flashbots Data / Dune MEV spells, not the tool.
- **Etherscan excluded.** Terms forbid bulk reproduction / dataset use of name
  tags; do not ingest Etherscan labels (or third-party scraped dumps of them)
  into the published dataset.
- **Public exposure.** Enriched labels are inlined into a world-readable Pages
  site (README §Deployment). All sources used are open/licensed-for-this;
  re-confirm before enabling any commercial source.

---

## 11. Sources

- OLI: <https://github.com/openlabelsinitiative/OLI> ·
  <https://www.openlabelsinitiative.org/> · SDK
  <https://github.com/openlabelsinitiative/oli-python>
- Dune labels: <https://docs.dune.com/data-catalog/curated/labels/overview> ·
  Spellbook <https://github.com/duneanalytics/spellbook>
- ethereum-lists: <https://github.com/ethereum-lists/contracts> ·
  <https://github.com/ethereum-lists/tokens>
- Selectors: <https://4byte.sourcify.dev/> · <https://www.4byte.directory/docs/>
- MEV: <https://github.com/flashbots/mev-inspect-py> ·
  <https://info.zeromev.org/api.html>
- Heuristics: ERC-165 <https://eips.ethereum.org/EIPS/eip-165> · EIP-1967 proxy
  storage slots
- Etherscan terms (why excluded): <https://etherscan.io/terms>
