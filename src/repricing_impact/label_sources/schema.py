"""The shared label-record contract for every source fetcher.

Every fetcher under :mod:`repricing_impact.label_sources` normalises its raw
source data into :class:`LabelRecord` rows and writes them to a per-source
parquet cache with the **canonical columns** defined here
(:data:`CONTRACT_COLUMNS`). :mod:`repricing_impact.label_sources.build` then
merges those per-source parquet files, by the precedence in the expansion plan
§4.3, into a single ``contract_labels.parquet`` that :mod:`repricing_impact.labels`
reads at precompute time.

Two things keep the taxonomy a *single source of truth*:

- :class:`Category` — the small forensics-relevant enum (plan §3).
- :data:`OLI_CATEGORY_MAP` / :data:`DUNE_CATEGORY_MAP` — lookup tables that fold
  each upstream taxonomy's value into one of ours, so imported labels slot in.

Parquet I/O goes through **duckdb** (already a build-time dependency) rather than
``pandas.to_parquet``/``pyarrow`` so the cache layer works without pulling an
extra dependency. The functions here are the only place that touches parquet
files, so the read/write column order stays consistent everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import duckdb
import pandas as pd

# --- Taxonomy (plan §3) ---------------------------------------------------


class Category(str, Enum):
    """Small, forensics-relevant contract taxonomy (plan §3).

    A ``str`` enum so values serialise straight to JSON / parquet as their
    string value (e.g. ``Category.SWAP_DEX == "swap_dex"``).
    """

    PRECOMPILE = "precompile"
    STABLECOIN = "stablecoin"
    TOKEN = "token"
    SWAP_DEX = "swap_dex"
    DEFI_COMPLEX = "defi_complex"
    BRIDGE = "bridge"
    ACCOUNT_ABSTRACTION = "account_abstraction"
    WALLET_SAFE = "wallet_safe"
    MEV_BOT = "mev_bot"
    NFT = "nft"
    ORACLE = "oracle"
    INFRA = "infra"
    CEX = "cex"
    OTHER = "other"
    UNKNOWN = "unknown"


VALID_CATEGORIES = frozenset(c.value for c in Category)


class UpgradeMechanism(str, Enum):
    """How (or whether) a contract's executing code can be replaced (heuristics §4.1).

    Upgradability is **narrower than proxy-ness**: ``minimal_proxy_immutable`` is
    the important non-upgradable case — an EIP-1167 clone *is* a proxy, but it
    forwards to an implementation baked into its own bytecode that can never
    change. ``none`` means no upgrade mechanism was detected. The remaining
    values name the detected pattern.
    """

    NONE = "none"
    EIP1967_TRANSPARENT = "eip1967_transparent"
    UUPS = "uups"
    BEACON = "beacon"
    DIAMOND = "diamond"
    MINIMAL_PROXY_IMMUTABLE = "minimal_proxy_immutable"


VALID_UPGRADE_MECHANISMS = frozenset(m.value for m in UpgradeMechanism)


class Source(str, Enum):
    """Provenance of a label (plan §1)."""

    MANUAL = "manual"
    OLI = "oli"
    DUNE = "dune"
    ETHLISTS = "ethlists"
    MEV = "mev"
    HEURISTIC = "heuristic"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Default confidence implied by each source (plan §4.3 — attested registries are
#: high; project attribution and behavioural MEV flags are medium; our own
#: on-chain heuristics are low). A fetcher may override per-record.
SOURCE_CONFIDENCE: Dict[str, str] = {
    Source.MANUAL.value: Confidence.HIGH.value,
    Source.OLI.value: Confidence.HIGH.value,
    Source.DUNE.value: Confidence.HIGH.value,
    Source.ETHLISTS.value: Confidence.MEDIUM.value,
    Source.MEV.value: Confidence.MEDIUM.value,
    Source.HEURISTIC.value: Confidence.LOW.value,
    Source.UNKNOWN.value: Confidence.LOW.value,
}


#: Source precedence, highest first (plan §4.3). ``build.py`` resolves conflicts
#: on the same address by this order. ``mev`` is intentionally absent here: it is
#: a behavioural *overlay* (``is_mev_bot`` / ``mev_role``), not a category
#: replacement (see :func:`build.merge_records`).
SOURCE_PRECEDENCE: List[str] = [
    Source.MANUAL.value,
    Source.OLI.value,
    Source.DUNE.value,
    Source.ETHLISTS.value,
    Source.HEURISTIC.value,
    Source.UNKNOWN.value,
]


# --- Upstream taxonomy → our taxonomy (plan §3) ---------------------------

#: OLI ``usage_category`` → our :class:`Category`. Unmapped OLI values fall to
#: ``other``; structural tags (``is_safe_contract``, ``erc4337`` roles, ...) are
#: handled in :mod:`repricing_impact.label_sources.oli`, not here.
OLI_CATEGORY_MAP: Dict[str, str] = {
    "stablecoin": Category.STABLECOIN.value,
    "fungible_tokens": Category.TOKEN.value,
    "non_fungible_tokens": Category.NFT.value,
    "dex": Category.SWAP_DEX.value,
    "trading": Category.SWAP_DEX.value,
    "lending": Category.DEFI_COMPLEX.value,
    "derivative": Category.DEFI_COMPLEX.value,
    "yield_vaults": Category.DEFI_COMPLEX.value,
    "staking": Category.DEFI_COMPLEX.value,
    "index": Category.DEFI_COMPLEX.value,
    "rwa": Category.DEFI_COMPLEX.value,
    "insurance": Category.DEFI_COMPLEX.value,
    "bridge": Category.BRIDGE.value,
    "cc_communication": Category.BRIDGE.value,
    "settlement": Category.BRIDGE.value,
    "erc4337": Category.ACCOUNT_ABSTRACTION.value,
    "nft_marketplace": Category.NFT.value,
    "nft_fi": Category.NFT.value,
    "oracle": Category.ORACLE.value,
    "developer_tools": Category.INFRA.value,
    "identity": Category.INFRA.value,
    "depin": Category.INFRA.value,
    "ai": Category.INFRA.value,
    "privacy": Category.INFRA.value,
    "inscriptions": Category.INFRA.value,
    "cex": Category.CEX.value,
    # Off-taxonomy but labeled → other.
    "gaming": Category.OTHER.value,
    "governance": Category.OTHER.value,
    "payments": Category.OTHER.value,
}

#: Dune ``category`` → our :class:`Category`. The legacy ``dune.labels`` spellbook
#: was retired; the live source is ``labels.addresses`` (verified 2026-07-03),
#: whose ethereum ``category`` domain differs — the values below the divider are
#: the ones actually observed there. The generic ``contracts`` tag ("this is a
#: known contract", not a real class) is intentionally left to fold to ``other``.
DUNE_CATEGORY_MAP: Dict[str, str] = {
    # Legacy dune.labels values (kept for saved queries still on the old schema).
    "cex": Category.CEX.value,
    "dex": Category.SWAP_DEX.value,
    "dao": Category.OTHER.value,
    "bridge": Category.BRIDGE.value,
    "lending": Category.DEFI_COMPLEX.value,
    "stablecoin": Category.STABLECOIN.value,
    "token": Category.TOKEN.value,
    "nft": Category.NFT.value,
    "oracle": Category.ORACLE.value,
    "infrastructure": Category.INFRA.value,
    "safe": Category.WALLET_SAFE.value,
    "erc4337": Category.ACCOUNT_ABSTRACTION.value,
    "mev": Category.MEV_BOT.value,
    # Live labels.addresses values observed on ethereum.
    "social": Category.OTHER.value,
    "institution": Category.CEX.value,
    "rollup": Category.INFRA.value,
    "tornado_cash": Category.INFRA.value,
    "ofac_sanction": Category.OTHER.value,
    "balancer_v1_pool": Category.SWAP_DEX.value,
    "balancer_v2_pool": Category.SWAP_DEX.value,
    "balancer_v3_pool": Category.SWAP_DEX.value,
    "balancer_cowswap_amm_pool": Category.SWAP_DEX.value,
    "balancer_gauges": Category.SWAP_DEX.value,
}


def map_oli_category(usage_category: Optional[str]) -> str:
    """Fold an OLI ``usage_category`` into our taxonomy (unknown → ``other``)."""
    if not usage_category:
        return Category.UNKNOWN.value
    return OLI_CATEGORY_MAP.get(usage_category.strip().lower(), Category.OTHER.value)


def map_dune_category(category: Optional[str]) -> str:
    """Fold a Dune ``category`` into our taxonomy (unknown → ``other``)."""
    if not category:
        return Category.UNKNOWN.value
    return DUNE_CATEGORY_MAP.get(category.strip().lower(), Category.OTHER.value)


# --- Precompile helper ----------------------------------------------------

_PRECOMPILE_ADDRS = frozenset("0x" + f"{i:040x}" for i in range(0x01, 0x0A + 1))


def is_precompile(address: Optional[str]) -> bool:
    """True for the 0x01–0x0a precompile addresses (plan §3)."""
    if not isinstance(address, str):
        return False
    return address.lower() in _PRECOMPILE_ADDRS


# --- The record -----------------------------------------------------------


@dataclass
class LabelRecord:
    """One resolved contract label (plan §1 target record + MEV overlay §4.3).

    ``address`` is always stored lowercased. ``category`` is one of
    :class:`Category`. ``is_mev_bot`` / ``mev_role`` are the behavioural overlay
    that MEV sources set without stealing the taxonomy ``category`` slot. The
    structural tags (``is_proxy`` / ``is_factory`` / ``is_safe`` / ``erc_type``)
    come from OLI or on-chain heuristics and are optional.
    """

    address: str
    label: str
    category: str = Category.UNKNOWN.value
    owner_project: Optional[str] = None
    source: str = Source.UNKNOWN.value
    confidence: str = Confidence.LOW.value
    # MEV behavioural overlay (plan §4.3 / §7).
    is_mev_bot: bool = False
    mev_role: Optional[str] = None  # arb | sandwich | liquidation
    # Structural tags (OLI / on-chain heuristics).
    is_proxy: Optional[bool] = None
    is_factory: Optional[bool] = None
    is_safe: Optional[bool] = None
    erc_type: Optional[str] = None
    # Upgradability (on-chain heuristics §4.1). ``is_upgradable`` is the narrow
    # bit: EIP-1167 clones are proxies but NOT upgradable, so it is not an alias
    # for ``is_proxy``. ``upgrade_mechanism`` is an :class:`UpgradeMechanism`
    # value; ``upgrade_admin`` is the EIP-1967 admin address (low 20 bytes of the
    # admin slot) when one is set, else ``None``.
    is_upgradable: Optional[bool] = None
    upgrade_mechanism: Optional[str] = None
    upgrade_admin: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.address, str):
            self.address = self.address.lower()
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"unknown category: {self.category!r}")
        if (
            self.upgrade_mechanism is not None
            and self.upgrade_mechanism not in VALID_UPGRADE_MECHANISMS
        ):
            raise ValueError(f"unknown upgrade_mechanism: {self.upgrade_mechanism!r}")
        # Fill confidence from the source default when the caller left it unset.
        if self.confidence is None:
            self.confidence = SOURCE_CONFIDENCE.get(self.source, Confidence.LOW.value)

    def to_row(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_row(cls, row: dict) -> "LabelRecord":
        known = {f.name for f in fields(cls)}
        clean: dict = {}
        for k, v in row.items():
            if k not in known:
                continue
            # Optional fields round-trip through parquet as pandas NaN, not None;
            # coerce back so NaN never leaks downstream (e.g. into JSON as `NaN`).
            if v is not None and not isinstance(v, str) and pd.isna(v):
                v = None
            clean[k] = v
        return cls(**clean)


#: Canonical parquet column order for per-source and merged contract caches.
CONTRACT_COLUMNS: List[str] = [f.name for f in fields(LabelRecord)]

#: Canonical parquet columns for the selector cache (plan §6). ``text_signature``
#: is a single candidate signature; a selector with N collisions is N rows.
SELECTOR_COLUMNS: List[str] = ["selector", "text_signature", "source"]


# --- Parquet I/O (duckdb; no pyarrow dependency) --------------------------


def _coerce_contract_df(records: Iterable[LabelRecord]) -> pd.DataFrame:
    rows = [r.to_row() for r in records]
    df = pd.DataFrame(rows, columns=CONTRACT_COLUMNS)
    if df.empty:
        # Give duckdb a typed-but-empty frame so COPY still writes a valid file.
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in CONTRACT_COLUMNS})
    return df


def write_contract_parquet(records: Iterable[LabelRecord], path: Path | str) -> Path:
    """Write label records to ``path`` as parquet (via duckdb), overwriting."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _coerce_contract_df(records)  # noqa: F841 — referenced by duckdb below
    con = duckdb.connect()
    try:
        con.register("df", df)
        con.execute(
            f"COPY (SELECT {', '.join(CONTRACT_COLUMNS)} FROM df) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return path


def read_contract_parquet(path: Path | str) -> List[LabelRecord]:
    """Read a contract parquet back into :class:`LabelRecord` rows."""
    df = read_contract_df(path)
    return [LabelRecord.from_row(row) for row in df.to_dict("records")]


def read_contract_df(path: Path | str) -> pd.DataFrame:
    """Read a contract parquet as a DataFrame with the canonical columns.

    **Forward-compatible with older caches.** Any canonical column absent from the
    file (e.g. a parquet written before a field was added to :class:`LabelRecord`)
    is backfilled as ``NULL`` instead of raising a duckdb binder error, and columns
    are always returned in :data:`CONTRACT_COLUMNS` order. This keeps a schema
    addition from silently nuking every pre-existing cache read — the projection is
    built only from our own constant, so there is no injection surface.
    """
    p = Path(path).as_posix()
    con = duckdb.connect()
    try:
        present = set(con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [p]).df())
        projection = ", ".join(
            col if col in present else f"NULL AS {col}" for col in CONTRACT_COLUMNS
        )
        df = con.execute(f"SELECT {projection} FROM read_parquet(?)", [p]).df()
    finally:
        con.close()
    return df


def write_selector_parquet(rows: Iterable[dict], path: Path | str) -> Path:
    """Write selector rows (``selector, text_signature, source``) as parquet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(list(rows), columns=SELECTOR_COLUMNS)
    if df.empty:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in SELECTOR_COLUMNS})
    con = duckdb.connect()
    try:
        con.register("df", df)
        con.execute(
            f"COPY (SELECT {', '.join(SELECTOR_COLUMNS)} FROM df) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return path


def read_selector_parquet(path: Path | str) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT {', '.join(SELECTOR_COLUMNS)} FROM read_parquet(?)",
            [Path(path).as_posix()],
        ).df()
    finally:
        con.close()
    return df
