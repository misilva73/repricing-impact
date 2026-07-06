"""ethereum-lists label-source fetcher (expansion plan §2, phase 3).

Pulls two community registries maintained by the ``ethereum-lists`` org and
normalises them into the shared :class:`~repricing_impact.label_sources.schema.LabelRecord`:

- ``ethereum-lists/contracts`` — per-chain directory
  ``contracts/contracts/1/<address>.json`` mapping a mainnet contract to its
  ``project`` (an attribution slug, e.g. ``"tether"``) plus a contract ``name``
  and a ``source`` (the registry's own provenance, ignored here). We use the
  ``project`` slug as both the ``label`` and ``owner_project`` and leave
  ``category="unknown"``: the registry attributes contracts to projects but does
  **not** classify them into our taxonomy, so a higher-precedence source (OLI /
  Dune) should fill the category. Guessing here would poison the merge.
- ``ethereum-lists/tokens`` — ``tokens/tokens/eth/<address>.json`` with token
  metadata (``name``, ``symbol``, ``decimals``, optional ``type`` like
  ``"ERC20"``, plus website/logo/social blocks we ignore). Every entry is a
  token, so these get ``category="token"`` with the token ``name`` (falling back
  to the ``symbol``) as label and the ``type`` recorded in ``erc_type``. The
  tokens registry carries no project attribution, so ``owner_project`` is left
  ``None``.

Both map to ``source="ethlists"`` / ``confidence="medium"`` (community-attested
project attribution, plan §4.3). The registry stores files under their
**checksummed (mixed-case)** address; our address is that filename with ``.json``
stripped and lowercased.

The clone (:func:`clone_repos` / :func:`refresh`) is the only part that touches
the network — a shallow ``git clone --depth 1`` via :mod:`subprocess`. The two
``parse_*`` functions are pure filesystem walks over an existing checkout, so
they are unit-testable offline against synthetic JSON trees.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from ..config import (
    ETHEREUM_LISTS_CONTRACTS_REPO,
    ETHEREUM_LISTS_TOKENS_REPO,
    LABEL_CACHE,
)
from .schema import (
    Category,
    Confidence,
    LabelRecord,
    Source,
    write_contract_parquet,
)

#: Cache filename this fetcher writes (merged by ``build.py``).
CACHE_FILENAME = "ethlists.parquet"

#: Checkout subdirectory names under a work dir.
_CONTRACTS_DIRNAME = "contracts"
_TOKENS_DIRNAME = "tokens"

#: The mainnet subtrees inside each checkout.
_CONTRACTS_CHAIN_SUBDIR = ("contracts", "1")  # contracts/1/<address>.json
_TOKENS_CHAIN_SUBDIR = ("tokens", "eth")  # tokens/eth/<address>.json

# Real registry keys (verified against the live repos, 2026-07):
#   contracts/1/<addr>.json -> {"project": <slug>, "name": <contract name>,
#                               "source": <registry provenance>}
#   tokens/eth/<addr>.json  -> {"symbol", "name", "decimals", "type"?, ...}
# We attribute a contract by its ``project`` slug, falling back to ``name`` for
# the handful of older files that predate the slug convention. Token labels use
# ``name`` then ``symbol``.
_CONTRACT_NAME_KEYS = ("project", "name")
_TOKEN_NAME_KEYS = ("name", "symbol")


def clone_repos(dest_dir: Path | str) -> tuple[Path, Path]:
    """Shallow-clone both ethereum-lists repos into ``dest_dir``.

    Returns ``(contracts_repo, tokens_repo)`` — the two checkout paths. Uses
    ``git clone --depth 1`` so only the current tip is fetched (the full history
    is large and irrelevant to a snapshot refresh). Network-touching; not called
    by the offline tests.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    contracts_repo = dest_dir / _CONTRACTS_DIRNAME
    tokens_repo = dest_dir / _TOKENS_DIRNAME
    _shallow_clone(ETHEREUM_LISTS_CONTRACTS_REPO, contracts_repo)
    _shallow_clone(ETHEREUM_LISTS_TOKENS_REPO, tokens_repo)
    return contracts_repo, tokens_repo


def _shallow_clone(url: str, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
    )


def _address_from_path(path: Path) -> str:
    """Address = the JSON filename with ``.json`` stripped, lowercased."""
    return path.stem.lower()


def _first_str(data: dict, keys) -> Optional[str]:
    """First non-empty string among ``data[key]`` for ``key`` in ``keys``."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _iter_json_files(directory: Path):
    """Yield ``*.json`` files directly under ``directory`` (sorted, if it exists)."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        if path.is_file():
            yield path


def parse_contracts(contracts_repo: Path | str) -> List[LabelRecord]:
    """Parse ``contracts/1/<address>.json`` into project-attribution records.

    Each file names the ``project`` owning the contract; we use it as both
    ``label`` and ``owner_project`` and leave ``category`` unknown (the registry
    does not classify — see module docstring). Files that carry no usable name,
    or that fail to parse as JSON, are skipped.
    """
    root = Path(contracts_repo).joinpath(*_CONTRACTS_CHAIN_SUBDIR)
    records: List[LabelRecord] = []
    for path in _iter_json_files(root):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        project = _first_str(data, _CONTRACT_NAME_KEYS)
        if not project:
            continue
        records.append(
            LabelRecord(
                address=_address_from_path(path),
                label=project,
                category=Category.UNKNOWN.value,
                owner_project=project,
                source=Source.ETHLISTS.value,
                confidence=Confidence.MEDIUM.value,
            )
        )
    return records


def parse_tokens(tokens_repo: Path | str) -> List[LabelRecord]:
    """Parse ``tokens/eth/<address>.json`` into ``token`` records.

    Every entry is a token, so ``category="token"``. The label is the token
    ``name`` (falling back to ``symbol``), and the ``type`` field (e.g.
    ``"ERC20"``) is recorded in ``erc_type`` when present. The tokens registry
    carries no project attribution, so ``owner_project`` stays ``None``. Files
    with neither a name nor a symbol, or that fail to parse, are skipped.
    """
    root = Path(tokens_repo).joinpath(*_TOKENS_CHAIN_SUBDIR)
    records: List[LabelRecord] = []
    for path in _iter_json_files(root):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        name = _first_str(data, _TOKEN_NAME_KEYS)
        if not name:
            continue
        records.append(
            LabelRecord(
                address=_address_from_path(path),
                label=name,
                category=Category.TOKEN.value,
                owner_project=None,
                source=Source.ETHLISTS.value,
                confidence=Confidence.MEDIUM.value,
                erc_type=_first_str(data, ("type",)),
            )
        )
    return records


def refresh(
    cache_dir: Path | str = LABEL_CACHE,
    work_dir: Optional[Path | str] = None,
) -> Path:
    """Clone both repos, parse them, and write ``ethlists.parquet``.

    Clones into ``work_dir`` (a fresh temp dir when ``None``), parses the
    contracts and tokens trees, concatenates the records, and writes the merged
    per-source cache to ``cache_dir / ethlists.parquet``. Returns the cache path.
    Network-touching (the clone); not exercised by the offline tests.
    """
    cache_dir = Path(cache_dir)
    out_path = cache_dir / CACHE_FILENAME

    if work_dir is not None:
        contracts_repo, tokens_repo = clone_repos(work_dir)
        records = parse_contracts(contracts_repo) + parse_tokens(tokens_repo)
        write_contract_parquet(records, out_path)
        return out_path

    with tempfile.TemporaryDirectory(prefix="ethlists-") as tmp:
        contracts_repo, tokens_repo = clone_repos(tmp)
        records = parse_contracts(contracts_repo) + parse_tokens(tokens_repo)
        write_contract_parquet(records, out_path)
    return out_path


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=LABEL_CACHE,
        help="directory to write ethlists.parquet into (default: label_cache/)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="directory to clone into (default: a fresh temp dir, auto-removed)",
    )
    args = parser.parse_args(argv)
    out = refresh(cache_dir=args.cache_dir, work_dir=args.work_dir)
    from .schema import read_contract_parquet

    records = read_contract_parquet(out)
    print(f"ethereum-lists: wrote {len(records)} labels -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
