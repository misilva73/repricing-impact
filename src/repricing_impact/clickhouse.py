"""ClickHouse engine factory + query helper for the ``gas_analysis`` warehouse.

Promoted verbatim (logic-wise) from the connectivity cell of the original
EDA notebook (since removed). Reads credentials from the
gitignored ``secrets.json`` at the repo root (keys ``xatu_username`` /
``xatu_password``) and connects to the Xatu ClickHouse over HTTPS via
``clickhouse-sqlalchemy``.

Warehouse rules (see docs/warehouse.md) that every caller must respect:

- ``gas_analysis`` is a *database*; query the four **distributed** tables
  (``gas_analysis_run``, ``_block_coverage``, ``_block_summary``,
  ``_divergence``) and ignore the ``_local`` shard tables.
- ``force_primary_key`` is enforced, so every table query MUST filter on the
  leading PK column ``chain_id`` (``count()`` / ``SHOW TABLES`` are exempt).
- ``Array`` columns come back as string reprs over the HTTP driver — parse with
  ``repricing_impact.opcodes.parse_arr`` (``json.loads``).
- The ``*`` tables are ReplacingMergeTree; pre-merge rows can duplicate per key,
  so dedup (``FINAL`` or ``argMax``/``DISTINCT row_id``) when counting.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# Repo root = two levels up from this file (src/repricing_impact/clickhouse.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = REPO_ROOT / "secrets.json"

CLICKHOUSE_HOST = "clickhouse.xatu.ethpandaops.io"
CLICKHOUSE_PORT = 443


def load_secrets(secrets_path: Optional[os.PathLike] = None) -> dict:
    """Load the gitignored ``secrets.json`` (keys ``xatu_username`` / ``xatu_password``)."""
    path = Path(secrets_path) if secrets_path is not None else SECRETS_PATH
    with open(path, "r") as file:
        return json.load(file)


def build_db_url(secrets_path: Optional[os.PathLike] = None) -> str:
    """Build the clickhouse-sqlalchemy HTTPS connection URL from ``secrets.json``."""
    secrets_dict = load_secrets(secrets_path)
    xatu_user = secrets_dict["xatu_username"]
    xatu_pass = secrets_dict["xatu_password"]
    return (
        f"clickhouse+http://{xatu_user}:{xatu_pass}"
        f"@{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/default?protocol=https"
    )


@lru_cache(maxsize=1)
def get_engine(secrets_path: Optional[os.PathLike] = None) -> Engine:
    """Return a (cached) SQLAlchemy engine for the Xatu ClickHouse warehouse.

    The engine is cached so repeated calls in a process reuse one connection
    pool. Pass an explicit ``secrets_path`` to bypass the default repo-root
    location (e.g. in tests).
    """
    return create_engine(build_db_url(secrets_path))


def run_query(
    query: str, engine: Optional[Engine] = None, **read_sql_kwargs
) -> pd.DataFrame:
    """Run a SQL query and return the result as a pandas DataFrame.

    Thin wrapper over ``pd.read_sql``; uses the cached :func:`get_engine` when no
    engine is supplied. Remember the warehouse rules above — most table queries
    need a ``chain_id`` filter or ClickHouse rejects them (Code 277).
    """
    if engine is None:
        engine = get_engine()
    return pd.read_sql(query, con=engine, **read_sql_kwargs)
