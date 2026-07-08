# Affected contracts — feature reference

Maintainer notes for the **Affected contracts** search page. The page answers
*"what happens to **this specific contract** under the repricing, and — for each
distinct way it breaks — **why**?"* by resolving an address (or name) to a
per-contract analysis of its **Potentially-broken (G4)** transactions, organized
as a ranked list of distinct **failure modes** (clusters), each annotated with
the causal repricing drivers.

> The published-JSON **contract** (exact field shapes) is authoritative in
> [`site/data/SCHEMA.md`](../site/data/SCHEMA.md) §5b (`index.json`, per-contract
> shard, `deploy_oog.json`). The warehouse **columns** it's built from are in
> [`docs/warehouse.md`](warehouse.md) (`entry_selector`, `tier1_failing_selector`,
> `failure_selector_path`, `surcharge_at_oog`, …). This file records the *design
> rationale* and *where the moving parts live* — it does not restate those shapes.

## Design decisions & rationale

- **Gate = G4 only** (Potentially-broken: `baseline_success=true AND
  schedule_success=false AND min_multiplier_to_succeed IS NULL`). A contract is
  *affected* iff it appears in **any** G4 tx as the **entry** contract
  (`recipient`), the **OOG halt site** (`oog_contract`), or the **non-OOG revert
  site** (`divergence_contract`). Everything else → "no failures identified"
  banner. G4 is the set where the repricing plausibly *broke* something a
  gas-limit bump can't rescue, so it's the honest scope for "affected". Predicates
  are the single source of truth in [`groups.py`](../src/repricing_impact/groups.py)
  (`G4_PREDICATE` etc.) — reused, never re-derived.
- **Cluster-first, not overview-first.** The page's primary unit is the failure
  **cluster** (failing function × halt/revert signature), ranked by tx count. The
  three roles (entry / oog-site / revert-site) are a **facet tag** on each cluster,
  not three duplicated sections. Contract-level state (gas-delta stats, block span,
  g3/g2/af context, failure rate) lives in a collapsible context strip.
- **Self-referential roles collapse to one row.** When a contract is BOTH the
  entry (`recipient`) and the halt/revert site (`where_contract` == itself) of the
  same failure mode, precompute emits two role-keyed clusters — one `entry`, one
  `oog_site`/`revert_site` — over the same self-halting txs. The frontend
  (`_mergeSelfRoleClusters` in `app.js`) merges them into a **single row tagged
  with both roles**, keeping the site row's stats (the superset: it also counts
  txs entered via other contracts, so its `count`/`drivers` are the correct
  distinct-tx figures). This is a display-only grouping over the emitted cluster
  list — the JSON contract (one `role` per cluster) is unchanged; a future
  precompute regeneration produces the same rows either way.
- **Every cluster carries a "why".** Each is annotated with the causal repricing
  drivers — the repriced state line items (cold-account / SLOAD / SSTORE / access-
  list counts), the shortfall magnitude (`surcharge_at_oog`, `oog_gas_remaining`),
  and the reservoir signals — so it says *which lever* broke it and *by how much*.
- **Sharded output** (changed from the original single-file plan). The affected set
  is tens of thousands of contracts per schedule, so a single JSON would be too
  large to load eagerly. Instead: a small name-searchable `affected/index.json`
  loaded once on page init, plus **one `affected/{lowercase_addr}.json` shard per
  affected contract**, fetched lazily on lookup. A missing shard fetch = "not
  affected" (banner), never an error.
- **Deploy-OOG long tail is collapsed.** Under the state-creation repricing
  (eip-8037) ~102k of ~118k affected contracts are freshly-deployed accounts
  (mostly ERC-4337 CREATE2 wallets) that OOG during their own construction — each a
  single-tx self-halt whose shard would be near-identical to every other. They are
  **not** written as individual shards; they're collapsed into one
  `affected/deploy_oog.json` aggregate, counted in `index.affected_count`, and
  pointed to by `index.deploy_oog`. (eip-8038 is state-access, so its count is ~0.)
- **Per-opcode gas attribution stays on the overview**, not here: `block_summary`
  is (block, class) grain with no recipient, so it can't be attributed to a single
  contract. The F12 "tax" decomposition is excluded pending producer clarification
  (`warehouse.md`).

## Where the moving parts live

| Concern | Location |
| --- | --- |
| Precompute emitter | `_build_affected_contracts` / `emit_*` in [`scripts/precompute.py`](../scripts/precompute.py) (~line 1942); `DIVERGENCE_TX_COLUMNS` carries the selector + driver columns |
| Published data | `site/data/{schedule}/affected/{index,deploy_oog,<addr>}.json` (committed) |
| JSON contract | [`site/data/SCHEMA.md`](../site/data/SCHEMA.md) §5b |
| Warehouse columns | [`docs/warehouse.md`](warehouse.md) (selector + driver families) |
| Frontend page | [`site/affected-contracts.html`](../site/affected-contracts.html) |
| Frontend logic | lookup + `renderContractDetail(rec)` in [`site/assets/app.js`](../site/assets/app.js) (search "affected-contracts.html —") |
| Fixtures | `build_affected_contracts` in [`scripts/make_fixtures.py`](../scripts/make_fixtures.py) → `fixtures_scratch/` (never `site/data/`) |
| Shape tests | [`tests/test_affected_contracts_shape.py`](../tests/test_affected_contracts_shape.py) (fixture-based, no DB) |

## Interfacing / maintenance going forward

- **Regenerate** (needs warehouse creds, runs locally): re-run
  `.venv/bin/python scripts/precompute.py`, then commit the regenerated
  `site/data/{schedule}/affected/**` per the manual refresh cadence. All affected
  aggregation is local DuckDB over the existing `divergence_tx` materialization —
  **no new `_divergence` scans**.
- **Changing the JSON shape:** update the emitter, `SCHEMA.md` §5b, the fixture
  builder, and the shape test together — they are a strict contract. The frontend
  (`renderContractDetail`) consumes the shard shape directly.
- **New driver / selector columns:** confirm the exact name, type, and
  G4-populated rate with a live `DESCRIBE gas_analysis.gas_analysis_divergence`,
  add to `DIVERGENCE_TX_COLUMNS`, and document in `warehouse.md`. Only materialize
  columns that are actually populated for G4 rows; drivers omit keys whose source
  column is absent/empty.
- **Deploy-OOG classification** is keyed off selector heuristics
  (`DEPLOY_OOG_SELECTOR_REGEX` / `is_initcode_selector`); revisit if a new schedule
  produces a different self-halt long tail.
