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
- `gas_analysis_*` are **ReplacingMergeTree** — **dedup when counting.** At full
  scale `_divergence` carries pre-merge duplicate `row_id`s; use the deduped
  relation from `groups.deduped_divergence_subquery` (`argMax(col, updated_at)`
  per `row_id`) rather than `FINAL` on the 113M-row table. `_block_coverage` /
  `_block_summary` have no duplicates.
- `Array` columns arrive as **string reprs** — parse with `opcodes.parse_arr`.
- **Never** `SELECT trace_payload` (a large blob).
- Divergence scans (~50M+ rows/schedule) are expensive — run them as
  infrequent, chunked, off-peak batches, **never on a request path**.
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
schema v10 (our data), so we **re-derive** the partition downstream and
**precompute** aggregates to keep the site instant.

## Key facts

- **Pinned config** (auto-picked; override with `REPRICING_CONFIG_HASH`):
  `0xc17ac709e44c2100b9ee61cc17b5167643620462fb8a69cc6bad0d61d35f37d2` — v10,
  covers all schedules, blocks **24,319,986 → 25,319,985** (~2026-01-26 →
  2026-06-15), `max_divergences_per_block = 1024`.
- **Gas-limit sweep is `{1×, 10×}`, not `[1,2,4,8]`.** The manifest's
  `gas_limit_multipliers = [1,2,4,8]` is **wrong** (verified empirically
  2026-07-03): the producer re-runs each failing tx at exactly two limits — `1×`
  (original) and `10×` (ceiling). `min_multiplier_to_succeed` is the **measured
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
site/             # static site published to Pages (index/overview/transaction-failures + assets)
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
  — still-open additive-column proposals for the upstream producer.
- [`README.md`](README.md) — human-facing project overview and setup.
</content>
</invoke>
