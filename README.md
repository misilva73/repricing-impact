# repricing-impact

Analysis and dashboard over the ClickHouse `gas_analysis` warehouse, measuring
the **gas and contract-failure impact of candidate EVM gas repricings**
(focus: **EIP-8037** and **EIP-8038**). The warehouse holds per-block / per-tx
replays of mainnet history under each candidate gas schedule, produced upstream
by the `reth-research` ExEx (see [`docs/warehouse.md`](docs/warehouse.md)).

This is a **consumer/analysis** repo — it reads the warehouse, re-derives a
transaction partition from raw facts, precomputes small aggregates, and
publishes them as a **fully static** dashboard (HTML + Plotly.js fed by
precomputed JSON; no server, no Node build). See [`AGENTS.md`](AGENTS.md) for the
architecture and [`docs/warehouse.md`](docs/warehouse.md) for warehouse details.

## Repository layout

- **`src/repricing_impact/`** — core Python package.
  - `clickhouse.py` — SQLAlchemy engine factory (reads `secrets.json`) +
    `get_engine()` / `run_query()` helpers.
  - `opcodes.py` — opcode byte→mnemonic table + `parse_arr` / `opcode_name` /
    `explode_opcodes` for the sparse parallel opcode arrays.
  - `labels.py` — known mainnet contract address labels
    (`ADDRESS_PROJECT_LABELS`, `label_address`, `infer_project_label`).
  - `config.py` — `CHAIN_ID`, schedule list, repo paths, and the pinned-config
    resolver (`resolve_config_hash`).
  - `groups.py` — the transaction partition (single source of truth).
- **`site/`** — the static site published to GitHub Pages; `site/data/`
  holds generated JSON aggregates.
- **`docs/`** — implementation plan and design notes.
- **`requirements.txt`** — Python dependencies (no web-server deps — the site is
  static).
- **`secrets.json`** — Xatu ClickHouse credentials (gitignored, see below).

## Setup

The project is developed against **Python 3.12** using a local virtual
environment (`.venv`).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .            # editable src-layout install -> `import repricing_impact`
```

### Credentials

The engine reads credentials from a `secrets.json` file at the repo root. The
file is **gitignored** — create it locally with this shape:

```json
{
    "xatu_username": "<xatu username>",
    "xatu_password": "<xatu password>",
    "dune_api_key": "<dune api key>"
}
```

It connects over HTTPS to `clickhouse.xatu.ethpandaops.io:443` via
`clickhouse-sqlalchemy`.

`dune_api_key` is **optional** — only the Dune label fetcher
(`repricing_impact.label_sources.dune`) reads it. Every other label source is
keyless, and the label merge/resolver + `precompute` run without any key (they
degrade to the built-in manual labels when no cache is present). See
[`docs/labeling-expansion-plan.md`](docs/labeling-expansion-plan.md).

## Usage

Connectivity smoke test:

```bash
python -c "from repricing_impact.clickhouse import get_engine; import pandas as pd; print(pd.read_sql('SELECT version()', get_engine()))"
```

Resolve the pinned `analysis_config_hash` for the focus schedules and print the
per-schedule block coverage:

```bash
python -m repricing_impact.config
```

Override the auto-picked config with an env var:

```bash
REPRICING_CONFIG_HASH=0x... python -m repricing_impact.config
```

## Warehouse rules (read before querying)

These are enforced by the warehouse and mandatory for every query (full detail
in [`docs/warehouse.md`](docs/warehouse.md)):

- **Always filter `chain_id = 1`.** `force_primary_key` rejects any table query
  without the leading PK column (`count()` / `SHOW TABLES` are exempt).
- Query the four **distributed** tables (`gas_analysis_run`,
  `gas_analysis_block_coverage`, `gas_analysis_block_summary`,
  `gas_analysis_divergence`); ignore the `_local` shard tables.
- The `gas_analysis_*` tables are **ReplacingMergeTree** — pre-merge rows can
  duplicate per key, so **dedup** when counting (`FINAL`, or `argMax` /
  `count(DISTINCT row_id)`).
- ClickHouse `Array` columns arrive as **string reprs** over the HTTP driver —
  parse with `repricing_impact.opcodes.parse_arr` (`json.loads`).
- **Never** `SELECT trace_payload` (a large blob); pin `analysis_config_hash`
  and `schedule_name`, and chunk by `block_number` to keep scans cheap.

## Testing

```bash
python -m pytest tests/ -v
```

## Formatting

All Python code (`src/`, `scripts/`, `tests/`) uses
[**black**](https://black.readthedocs.io/) with default settings.

## Deployment

The dashboard is a **fully static site** (`site/`) published to **GitHub Pages**.
Because precompute needs ClickHouse access that GitHub-hosted runners cannot
reach (the warehouse is internal), the model splits the two halves:

1. **Precompute runs locally** (or in any trusted env with warehouse creds). It
   reads ClickHouse, builds a throwaway DuckDB/parquet intermediate, and emits
   the small aggregate JSON under `site/data/{schedule}/`. Those JSON files are
   **committed** to the repo:

   ```bash
   source .venv/bin/activate
   # optional: refresh contract-label enrichment (categories / owner_project)
   # before precompute — see docs/labeling-expansion-plan.md. Needs network and
   # (for the Dune source only) dune_api_key. Skips cleanly if omitted.
   python -m repricing_impact.label_sources.build   # -> label_cache/ (gitignored)
   python scripts/precompute.py          # writes site/data/**/*.json
   git add site/data && git commit -m "data: refresh aggregates"
   ```

   The `label_cache/` parquet dumps are **gitignored**; only the tiny label
   slice that lands in the published JSON (inlined by precompute) is committed.

2. **GitHub Pages publishes `site/`.** The workflow at
   `.github/workflows/deploy-pages.yml` runs on every push to `main` (and via
   manual `workflow_dispatch`). It has **no build step** — it just uploads the
   `site/` directory as a Pages artifact and deploys it (`actions/configure-pages`
   → `actions/upload-pages-artifact` → `actions/deploy-pages`). All data work is
   done locally in step 1; the workflow never touches ClickHouse or Python.

**Refresh cadence is manual:** re-run precompute (step 1) when new replay data
lands, commit the regenerated JSON, and push — the Pages deploy fires
automatically. The published JSON is small (aggregates only), so committing it
is viable; if it grows, consider a separate data branch or `git lfs`.

**One-time setup to enable Pages** (repo admin, in the GitHub UI):
*Settings → Pages → Build and deployment → **Source: GitHub Actions***. No
branch selection is needed (the workflow handles the deploy). After that, every
push to `main` redeploys.

**Public exposure:** a GitHub Pages site is **world-readable**. The published
data is aggregate public-mainnet analysis (no secrets) — `secrets.json` is
gitignored and never enters `site/` — which matches the existing public
forensics dashboard (`repricing-forensics.carlbeek.com`). Confirm public
exposure is intended before enabling Pages.
