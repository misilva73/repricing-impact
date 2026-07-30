# Plan — "Where — top halt contracts" card: entry-contract leaderboard with rich labels + real failure rate

Status: **proposed** (not yet implemented). Date: 2026-07-03.

## Goal

Improve the **"Where — top halt contracts"** card in the **Out-of-gas failures**
section of the transaction-failures page so it answers three questions:

1. **Which _entry_ contracts see the most OOG halts** — leaderboard keyed on the
   transaction `recipient` (the contract the tx calls), not only `oog_contract`
   (where the halt landed inside a nested call).
2. **All the labels/attributes we know for that contract** — the full resolved
   label record (name + category + owner project + source + confidence +
   structural/MEV tags), not just the display name + category.
3. **Failure rate** — OOG halts to that recipient as a share of **all** mainnet
   transactions that targeted the same recipient over the analysis window.

## Feasibility — verified 2026-07-03

- **(1)** `recipient` is already a materialized column of the build-time DuckDB
  `divergence_tx` table ([`scripts/precompute.py:344`](../scripts/precompute.py#L344))
  and is already the left side of the OOG Sankey. No new warehouse read.
- **(2)** The merged label cache (`label_cache/contract_labels.parquet`) already
  carries `label`, `category`, `owner_project`, `source`, `confidence`,
  `is_mev_bot`/`mev_role`, and structural tags (`is_proxy`/`is_factory`/`is_safe`/
  `erc_type`) per address — see
  [`LabelRecord`](../src/repricing_impact/label_sources/schema.py#L210) and
  [`classify_address`](../src/repricing_impact/labels.py#L165). We only need to
  surface more of these fields; no new source ingestion.
- **(3)** The denominator (total txs to a recipient) is **not** in `gas_analysis`
  — `_divergence` only stores diverging txs, and coverage/summary have no
  recipient dimension. It **is** available on the same ClickHouse host in the
  Xatu EL table `default.canonical_execution_transaction`, confirmed to have:
  - `block_number UInt64`, `to_address Nullable(String)`, `success Bool`,
    `meta_network_name LowCardinality(String)`
  - full coverage of the pinned window (a 20k-block slice returned 10.3M txs over
    `24319986→24339985`; top recipient Tether = 1.74M txs).

  This is a **cross-source join** — a deliberate departure from the repo's
  "gas_analysis tables only" rule (see [Risks](#risks--caveats)).

## Design

### Denominator query (bounded, not a full scan)

Do **not** scan the whole EL table. Compute the top-N halt recipients from
`divergence_tx` first, then fetch denominators for **only those N addresses** in
a single query:

```sql
SELECT lower(to_address) AS recipient, count(*) AS total_tx
FROM default.canonical_execution_transaction
WHERE meta_network_name = 'mainnet'
  AND block_number BETWEEN {block_start} AND {block_end}   -- ctx.block_start/end
  AND to_address IN ({the N recipient addrs from the leaderboard})
GROUP BY recipient
```

`block_start`/`block_end` come from `RunContext` (the pinned config's range,
`24,319,986 → 25,319,985`). `chain_id = 1` (gas_analysis) ↔ `meta_network_name =
'mainnet'` (Xatu) are the same cohort. Runs once per schedule via the existing
`_ch(query, ctx.engine)` helper.

### `scripts/precompute.py`

1. **New rich-label helper.** Add `_labeled_leaderboard_rich(ctx, col, where,
   limit)` (or extend `_labeled_leaderboard`) that, per address, emits the full
   record from `classify_address`:
   `{addr, label, category, owner_project, source, confidence, is_mev_bot,
   mev_role, is_proxy, is_factory, is_safe, erc_type, count}`. Drop always-null
   fields to keep JSON lean (mirror the existing `_addr_category` "null when
   unknown" convention).
2. **New denominator+rate helper.** `_recipient_failure_rates(ctx, addrs)`:
   issues the bounded Xatu query above, returns `{addr: total_tx}`, and the
   caller computes `halt_rate = halt_count / total_tx` (guard divide-by-zero →
   `null`).
3. **Wire into `emit_oog_forensics`** ([`scripts/precompute.py:1417`](../scripts/precompute.py#L1417)):
   add a new key `oog_recipient_leaderboard` (keep the existing
   `oog_contract_leaderboard` — the two answer different questions: *who was
   called* vs *where it died*). Each row:
   ```jsonc
   { "addr": "0x…", "label": "…", "category": "swap_dex", "owner_project": "…",
     "source": "oli", "confidence": "high", "is_mev_bot": false,
     "halt_count": 41771, "total_tx": 1250000, "halt_rate": 0.0334 }
   ```

### `site/data/SCHEMA.md`

Extend §4b (`oog_forensics.json`) to document `oog_recipient_leaderboard`: the
new fields, that `halt_count`/`total_tx`/`halt_rate` are the numerator/denominator/
ratio, that `total_tx` comes from the Xatu EL table over the pinned block range,
and that `halt_rate` is `null` when `total_tx` is 0 or unavailable.

### Frontend — `site/transaction-failures.html` + `site/assets/app.js`

- Add a panel (or retitle the existing one) "Where — top entry contracts by OOG
  halts" reading `oog.oog_recipient_leaderboard`.
- Render with `renderHBar` on `halt_count`
  ([`app.js:171`](../site/assets/app.js#L171),
  [`renderHBar`](../site/assets/app.js#L412)); enrich the hovertemplate to show
  `label`, `owner_project`/`source`/`category`, and the failure rate
  (`halt_rate` as a %) alongside the raw `halt_count`/`total_tx`. `renderHBar`'s
  `hovertemplate` needs a small extension (pass a `customdata` array) to carry
  the extra fields.
- Keep the existing `oog_contract` card and the entry→halt Sankey unchanged.

## Work items

- [ ] `precompute.py`: `_labeled_leaderboard_rich` + `_recipient_failure_rates` +
      `oog_recipient_leaderboard` in `emit_oog_forensics`.
- [ ] Regenerate `site/data/{eip-8037,eip-8038}/oog_forensics.json`.
- [ ] `SCHEMA.md` §4b: document the new key + fields.
- [ ] `app.js` + `transaction-failures.html`: new/retitled card + richer hover.
- [ ] `docs/warehouse.md` + `AGENTS.md`: note the sanctioned Xatu cross-source
      read (table, why, that it's read-only and off the request path).
- [ ] `black` the Python; local preview via `scripts/serve.py`.

## Risks & caveats

- **Cross-source rule departure.** The repo mandates `gas_analysis`-only queries.
  This adds one read of `default.canonical_execution_transaction`. Keep it
  read-only, bounded to the pinned block range, and documented in AGENTS.md /
  warehouse.md so it's a sanctioned exception, not silent drift.
- **Drill-in cap undercounts the numerator.** `_divergence` drill-ins are capped
  at the producer's per-block drill-in cap (`max_divergences_per_block`; the live
  value is pinned in [`../AGENTS.md`](../AGENTS.md) and
  [`warehouse.md`](warehouse.md) — it has already been raised once by a producer
  bump, so do not hardcode it here). Blocks with `drill_ins_truncated = true` can
  under-report halts for a busy recipient, biasing `halt_rate` **low**. Note this
  in the card/schema; consider surfacing the truncated-block share as a confidence
  caveat.
- **Recipient vs halt site.** `recipient` (entry) ≠ `oog_contract` (halt site).
  The new card is deliberately entry-keyed; the Sankey already shows the mapping.
- **Denominator scope.** `total_tx` counts every mainnet tx to the recipient
  (all statuses, all gas schedules are irrelevant — it's canonical mainnet),
  matching the intended "% of all traffic to X that OOG-halted".
</content>
</invoke>
