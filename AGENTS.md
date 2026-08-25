# AGENTS.md

Canonical guide for agents working in this repo. Deeper reference is split into
linked docs so this file stays small — read those only when the task needs them.

## What this repo is

A **consumer/analysis** repo over the ClickHouse `gas_analysis` warehouse,
measuring the **gas and contract-failure impact of candidate EVM gas
repricings** (focus: **eip-8037** and **eip-8038**). The warehouse holds
per-block/per-tx replays of mainnet history under each candidate gas schedule,
produced upstream by the `reth-research` ExEx.

It **does not produce data.** It reads the warehouse, re-derives a transaction
partition from raw replay facts, precomputes small aggregates, and publishes a
**fully static** dashboard (HTML + Plotly.js + precomputed JSON).

There is **no server runtime.** Do not add FastAPI / uvicorn / Streamlit / a
Node build. Python does all the data work; the site is static files only.

## Environment

- **Python 3.12** in a local `.venv` at the repo root. Always use it
  (`.venv/bin/python`, `.venv/bin/pip`) — never system Python.
- Install: `pip install -r requirements.txt` then `pip install -e .` (editable
  src-layout install so `import repricing_impact` works from scripts).
- Credentials live in the gitignored `secrets.json` (keys `xatu_username` /
  `xatu_password`). **Never commit secrets.**

## Formatting & conventions

- All Python (`src/`, `scripts/`, `tests/`) uses
  [**black**](https://black.readthedocs.io/), default settings. Run
  `black src/ scripts/ tests/` before committing.
- **Package layout:** shared helpers live in `src/repricing_impact/`; scripts
  import from the installed package, never via path hacks.
- **One source of truth:** the transaction partition is defined once, in
  [`src/repricing_impact/groups.py`](src/repricing_impact/groups.py), and reused
  by every aggregate. Do not re-derive group predicates elsewhere.

## Warehouse rules (mandatory — enforced by the DB)

- **Always filter `chain_id = 1`** — `force_primary_key` rejects any table query
  without the leading PK column (Code 277). `count()` / `SHOW TABLES` are exempt.
- Query the **distributed** tables (`gas_analysis_run`, `_block_coverage`,
  `_block_summary`, `_divergence`); ignore the `_local` shard tables.
- Pin one `analysis_config_hash` (via `config.resolve_config_hash`) and filter
  `schedule_name`; chunk by `block_number` to keep scans cheap.
- `gas_analysis_*` are **ReplacingMergeTree** — **dedup when counting.**
  `_divergence` **can** carry pre-merge duplicate `row_id`s; use the deduped
  relation from `groups.deduped_divergence_subquery` (`argMax(col, updated_at)`
  per `row_id`) rather than `FINAL` on the ~105M-row table. `_block_coverage` /
  `_block_summary` have no duplicates. Whether duplicates are *currently* visible
  is a function of background-merge timing and must never be assumed either way:
  the v10 run had ~2.2k in one 2k-block window, the v11 run has **zero across the
  full 1M-block range for both schedules** (measured 2026-07-30). Always dedup
  anyway — a later run, or the same run mid-merge, can have them again.
- `Array` columns arrive as **string reprs** — parse with `opcodes.parse_arr`.
- **Never** `SELECT trace_payload` (a large blob).
- Divergence scans (~50M+ rows/schedule) are expensive — run them as
  infrequent, chunked, off-peak batches, **never on a request path**.
- ⚠️ **Wide `_divergence` reads can come back SHORT with no error.** The
  ClickHouse HTTP driver intermittently truncates these ~2M-row × ~50-column
  chunk reads, returning a short DataFrame and **raising nothing** (observed on
  ~1 chunk per full-range run, a different chunk each time, 2026-07-30). Never
  trust the row count of a wide read — verify it. `stage_divergence_tx` checks
  every chunk against `block_coverage.retained_drill_in_count` (available locally
  in `block_groups`, so verification is free) and re-reads on a shortfall; a
  retry has always returned the full count. Any new bulk per-tx read needs the
  same guard, or it will silently publish a truncated cohort.
- **One sanctioned cross-source exception:** precompute may do a single
  bounded, read-only read of the Xatu EL table
  `default.canonical_execution_transaction` (top-N addresses, pinned block range)
  to compute OOG failure-rate denominators — `gas_analysis` has no total-tx
  count. Details in [`docs/warehouse.md`](docs/warehouse.md).

Full connection details, table grains, column semantics, and enum domains:
[`docs/warehouse.md`](docs/warehouse.md).

## Architecture — precompute → static site

1. **Precompute** ([`scripts/precompute.py`](scripts/precompute.py)) runs
   locally (needs warehouse creds GitHub runners can't reach). It runs
   chunked, server-side ClickHouse `GROUP BY`s, stages them in a build-time
   **DuckDB** file (never published), and emits small aggregate JSON under
   `site/data/{schedule}/`. Those JSON files are **committed**.
2. **GitHub Pages** publishes `site/` — no build step. The workflow
   ([`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml))
   just uploads `site/` as a Pages artifact on every push to `main`.

Refresh cadence is **manual**: re-run precompute when new replay data lands,
commit the regenerated JSON, push. The published JSON is aggregate public-mainnet
analysis (no secrets) — the Pages site is **world-readable**.

The JSON files are a strict contract between precompute and the frontend
(`site/*.html` + `site/assets/app.js`): [`site/data/SCHEMA.md`](site/data/SCHEMA.md).

**Why this shape** (vs the sibling `repricing-forensics`): forensics live-scanned
a 113M-row table (30–60s loads) and read an older producer schema that
pre-classified txs into editorial buckets. Those buckets were removed in producer
schema v10 (we now read v11), so we **re-derive** the partition downstream and
**precompute** aggregates to keep the site instant.

## Key facts

- **Pinned config** (auto-picked; override with `REPRICING_CONFIG_HASH`):
  `0x8ccad591661bfca557e688c41d8fbf14d8f51cc3b0239fcdc517c6592b780527` — v11,
  covers both schedules. **The hash itself has been stable** since at least
  2026-08-14, but its block coverage under `resolve_config_hash`'s "most
  blocks + most recent updated_at covering both schedules" rule **keeps
  growing backward in time** as more history gets backfilled under the same
  config: 2,102,648 blocks (23,217,338 → 25,319,985, ~2025-08-25 → 2026-06-15)
  as of the 2026-08-14 run, **4,000,000 blocks** (21,319,986 → 25,319,985,
  ~2024-12-03 → 2026-06-15) as of 2026-08-24 — same end block both times, the
  start keeps moving earlier. **Don't hardcode the block range**; always
  re-derive it from the resolved config (`resolve_config_hash()` /
  `meta.json.block_range`) rather than trusting a number written down here.
  (Prior *hash*, now superseded: `0x6617c5db…66ed3`, ~1,000,000 blocks,
  24,319,986 → 25,319,985, verified 2026-07-29; `0xc17ac709…f37d2` v10, no
  longer exists.)
- **Producer v11 grew `block_summary` 43 → 54 columns** (verified 2026-07-29) —
  the eleven additions ship Recommendations 1 + 2 of
  [`docs/producer-data-recommendations.md`](docs/producer-data-recommendations.md):
  a six-way EIP-2718 taxonomy `tx_count_type_{legacy,access_list,dynamic_fee,
  blob,set_code,other}` (the six sum to `tx_count`), `tx_count_simple_transfer` +
  `tx_count_contract_call`, the 13-bin `gas_delta_pct_hist` (`Array(Int32)`; bin
  sum == `tx_count`; **empty ⟺ written pre-v11** — pad, never filter), and the
  class-grain denominator `baseline_gas_used_sum`. ⚠️ **BREAKING:**
  `tx_count_creation` was **redefined** to tx-level creations (`to IS NULL`) — it
  is no longer the v10 state-op creation count. The two **exact** partitions of
  `tx_count` are `no_state + runtime_state` and `creation + simple_transfer +
  contract_call`; `tx_count_authorization` and the redefined `tx_count_creation`
  are **overlapping overlays**, members of neither. `_divergence` (113 cols) and
  `_block_coverage` (22) are unchanged — v11 was purely additive on
  `block_summary`. Column semantics: [`docs/warehouse.md`](docs/warehouse.md).
- **Gas-limit sweep is `{1×, 10×}`, not `[1,2,4,8]`.** The manifest's
  `gas_limit_multipliers = [1,2,4,8]` is **wrong** (verified empirically
  2026-07-03; **re-confirmed against v11 2026-07-30**, measured
  `min_multiplier_to_succeed` running continuously `0.0031 … 9.9979` over the
  full 1M-block window, both schedules): the producer
  re-runs each failing tx at exactly two limits — `1×` (original) and `10×`
  (ceiling). `min_multiplier_to_succeed` is the **measured
  ratio** `schedule_gas_used / tx_gas_limit` from the run that completes (`0 <
  min_mult <= 10`; NULL ⟺ not rescued even at `10×`), **not** a swept tier. So
  `TOP_MULTIPLIER = 10`, and `min_mult > 8` rows are genuinely rescued at `≤10×`
  (still G3), not "beyond the swept range". See the
  [`groups.py`](src/repricing_impact/groups.py) docstring.
- **The partition** is **6 groups** — No change (`g1`), Succeeds with changes
  (`g2`), Fixable with gas-limit increase (`g3`), Potentially broken (`g4`),
  Already failing (`af`), Unknown (`g5`). The changed groups (`g2`/`g3`/`g4`)
  require a working baseline (`baseline_success = true`); baseline failures are
  `af`. Exact predicates and their verified rationale live in the
  [`groups.py`](src/repricing_impact/groups.py) docstring — read it before
  touching any aggregate.

## Repo map

```
src/repricing_impact/
  clickhouse.py   # SQLAlchemy engine from secrets.json + run_query
  opcodes.py      # opcode byte->mnemonic table + parse_arr/opcode_name/explode_opcodes
  labels.py       # known mainnet address labels (label_address, ...)
  config.py       # CHAIN_ID, schedules, paths, pinned-config resolver
  groups.py       # the transaction partition — SINGLE SOURCE OF TRUTH
scripts/
  precompute.py   # CH -> build-time DuckDB -> site/data/**/*.json (re-runnable CLI)
  make_fixtures.py# seeded offline fixtures (fixtures_scratch/, NOT site/data/)
  serve.py        # local preview: static http.server over site/
site/             # static site published to Pages (index/overview/transaction-failures/affected-contracts + assets)
  entity-report.md# committed narrative outreach report; rendered by entity-report.html (app.js renderMarkdown)
  data/{schedule} # committed aggregate JSON (see site/data/SCHEMA.md)
tests/            # partition predicate + DB partition-identity tests
```

## Reference docs

- [`docs/warehouse.md`](docs/warehouse.md) — warehouse connection, the 4 tables,
  column semantics, opcode arrays, schedules, upstream source files.
- [`site/data/SCHEMA.md`](site/data/SCHEMA.md) — the published-JSON contract
  between precompute and the frontend (authoritative; mirrored in `app.js`).
- [`docs/verification-findings.md`](docs/verification-findings.md) — dated
  empirical investigation that locked the partition predicate. `groups.py` is the
  current truth where they differ.
- [`docs/producer-data-recommendations.md`](docs/producer-data-recommendations.md)
  — Recommendations 1 and 2 **shipped in producer v11**; the doc is kept as a
  dated record of what was proposed vs what landed (incl. the naming deltas).
  Remaining proposals — e.g. the calldata/selector field referenced from
  [`docs/labeling-expansion-plan.md`](docs/labeling-expansion-plan.md) — are
  still open.
- [`README.md`](README.md) — human-facing project overview and setup.
</content>
</invoke>
