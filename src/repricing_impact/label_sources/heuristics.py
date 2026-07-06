"""On-chain structural heuristics label-source (expansion plan §4.1, phase 4).

The **lowest-precedence, last-resort** label source (plan §4.3). When no
attested registry (manual / OLI / Dune / ethereum-lists) knows an address, we
fall back to reading the contract's *own bytecode and storage* over JSON-RPC and
deriving a structural descriptor:

- **proxy** — EIP-1967 implementation / beacon / admin storage slots, or a
  ``DELEGATECALL`` in the runtime code.
- **ERC type** — ERC-721 / ERC-1155 via their ERC-165 interface ids; ERC-20 by
  the presence of its canonical function selectors (it has no ERC-165 id).
- **factory** — a ``CREATE`` / ``CREATE2`` opcode in the runtime code.

Everything emitted here is ``source="heuristic"`` / ``confidence="low"`` (plan
§4.3): a structural guess, never an attestation. ``build.py`` only lets these
win for the still-unknown tail.

The pure classification functions (:func:`is_erc165`, :func:`detect_proxy`,
:func:`detect_erc_type`, :func:`detect_factory`, :func:`classify`) take raw
bytecode-hex / storage / ``supportsInterface`` inputs and return
booleans / tags / a :class:`~repricing_impact.label_sources.schema.LabelRecord`,
so they are unit-testable offline against synthetic inputs. Only :func:`probe`
(and :func:`refresh`, which drives it) touches the network — a batch of
``eth_getCode`` / ``eth_getStorageAt`` / ``eth_call`` JSON-RPC requests.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..config import LABEL_CACHE
from .schema import (
    Category,
    Confidence,
    LabelRecord,
    Source,
    UpgradeMechanism,
    write_contract_parquet,
)

#: Cache filename this fetcher writes (merged by ``build.py``).
CACHE_FILENAME = "heuristic.parquet"

#: Environment variable read for the JSON-RPC endpoint when no ``rpc_url`` is
#: passed. Kept out of ``config.py`` on purpose — this is deployment config, not
#: an analysis constant.
RPC_URL_ENV_VAR = "ETH_RPC_URL"

# --- EIP-1967 proxy storage slots -----------------------------------------
# keccak256("eip1967.proxy.implementation") - 1, etc. (EIP-1967).
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
EIP1967_BEACON_SLOT = (
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
)
EIP1967_ADMIN_SLOT = (
    "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
)

#: All EIP-1967 slots, lowercased, keyed by role.
EIP1967_SLOTS: Dict[str, str] = {
    "implementation": EIP1967_IMPLEMENTATION_SLOT,
    "beacon": EIP1967_BEACON_SLOT,
    "admin": EIP1967_ADMIN_SLOT,
}

# --- ERC-165 interface ids -------------------------------------------------
# Standard ERC-165 interface ids (4-byte). ERC-20 intentionally absent: it has
# no ERC-165 id, so we sniff it from function selectors instead (see below).
ERC721_INTERFACE_ID = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"

#: 4-byte function selectors that, taken together, identify an ERC-20. ERC-20
#: predates ERC-165 and has no registered interface id (plan note / caveat), so
#: the heuristic is "does the runtime code embed all of these selectors" —
#: ``transfer(address,uint256)`` + ``balanceOf(address)`` + ``totalSupply()``.
#: This is a low-confidence sniff, hence ``confidence="low"`` throughout.
ERC20_SELECTORS = ("a9059cbb", "70a08231", "18160ddd")

# --- EVM opcodes we look for in runtime code ------------------------------
CREATE_OPCODE = "f0"  # CREATE
CREATE2_OPCODE = "f5"  # CREATE2
DELEGATECALL_OPCODE = "f4"  # DELEGATECALL (minimal-proxy / delegate pattern)

# --- EIP-1167 minimal proxy (clone) bytecode template ---------------------
# Canonical runtime code is  <prefix> <20-byte impl address> <suffix>:
#   363d3d373d3d3d363d73 <addr> 5af43d82803e903d91602b57fd5bf3
# The implementation address is baked into the bytecode, so a clone is a proxy
# that can NEVER be upgraded. Matching this template is the exclusion that keeps
# ``is_upgradable`` narrower than ``is_proxy``. Optimised / vanity variants
# (e.g. 0age's push-tuned clone) differ slightly and are not matched here — a
# deliberately conservative, high-precision check.
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"

# --- Upgrade-mechanism selectors / interface ids --------------------------
# UUPS (ERC-1822 / OpenZeppelin) upgrade entrypoints, 4-byte selectors:
#   proxiableUUID()                 = 52d1902d
#   upgradeTo(address)              = 3659cfe6
#   upgradeToAndCall(address,bytes) = 4f1ef286
# These live in the *implementation*, so they are found when probing an impl
# contract's code; a UUPS *proxy* is caught by its EIP-1967 slots instead.
UUPS_SELECTORS = ("52d1902d", "3659cfe6", "4f1ef286")

# EIP-2535 Diamond: the DiamondLoupe ERC-165 interface id, and the diamondCut
# selector `diamondCut((address,uint8,bytes4[])[],address,bytes)` = 1f931c1c.
# Diamonds route to many facets and do NOT use the EIP-1967 slots, so they are
# detected by these two signals rather than by storage.
DIAMOND_LOUPE_INTERFACE_ID = "0x48e2b093"
DIAMOND_CUT_SELECTOR = "1f931c1c"


# --- helpers ---------------------------------------------------------------


def _strip_hex(value: Optional[str]) -> str:
    """Lowercase a hex string and strip an optional ``0x`` prefix (``""`` if empty)."""
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    if v.startswith("0x"):
        v = v[2:]
    return v


def _is_empty_storage(value: Optional[str]) -> bool:
    """True if a storage slot value is absent or all-zero (i.e. unset)."""
    stripped = _strip_hex(value)
    return stripped == "" or set(stripped) == {"0"}


# --- pure classification functions (offline-testable) ---------------------


def is_erc165(supports_interface_results: Optional[Dict[str, bool]]) -> bool:
    """True if the contract answered ``supportsInterface`` for a known id.

    ``supports_interface_results`` maps a 4-byte interface id (``"0x…"``) to the
    boolean the contract returned for ``supportsInterface(id)``. Any ``True``
    means the contract implements ERC-165 (the mechanism that answered) for at
    least one interface we probed.
    """
    if not supports_interface_results:
        return False
    return any(bool(v) for v in supports_interface_results.values())


def detect_proxy(
    code_hex: Optional[str],
    storage: Optional[Dict[str, str]] = None,
) -> bool:
    """True if the contract looks like a proxy (EIP-1967 slots or delegatecall).

    Two independent signals:

    - any EIP-1967 slot (implementation / beacon / admin) holds a non-zero value
      in ``storage`` (a mapping ``slot -> stored value``); or
    - the runtime ``code_hex`` embeds a ``DELEGATECALL`` opcode (the forwarding
      pattern every proxy uses).
    """
    storage = storage or {}
    for slot in EIP1967_SLOTS.values():
        value = storage.get(slot)
        if value is None:
            # Tolerate un-prefixed slot keys from callers.
            value = storage.get(_strip_hex(slot))
        if value is not None and not _is_empty_storage(value):
            return True

    code = _strip_hex(code_hex)
    return DELEGATECALL_OPCODE in _iter_opcode_bytes(code)


def detect_erc_type(
    supports_interface_results: Optional[Dict[str, bool]] = None,
    code_hex: Optional[str] = None,
) -> Optional[str]:
    """Return ``"erc721"`` / ``"erc1155"`` / ``"erc20"`` (or ``None``).

    ERC-721 and ERC-1155 are detected from their ERC-165 interface ids in
    ``supports_interface_results`` (a truthy value for the id). ERC-20 has **no**
    ERC-165 interface id, so it is sniffed from ``code_hex`` by the presence of
    all of :data:`ERC20_SELECTORS` — a deliberately low-confidence fallback.
    """
    results = supports_interface_results or {}
    normalised = {_strip_hex(k): bool(v) for k, v in results.items()}
    if normalised.get(_strip_hex(ERC721_INTERFACE_ID)):
        return "erc721"
    if normalised.get(_strip_hex(ERC1155_INTERFACE_ID)):
        return "erc1155"

    code = _strip_hex(code_hex)
    if code and all(sel in code for sel in ERC20_SELECTORS):
        return "erc20"
    return None


def detect_factory(code_hex: Optional[str]) -> bool:
    """True if the runtime ``code_hex`` embeds a ``CREATE`` / ``CREATE2`` opcode.

    A contract that can deploy other contracts is a factory signal. We scan the
    disassembled opcode stream (skipping ``PUSH`` immediates) so a ``0xf0`` /
    ``0xf5`` byte that is merely push data does not trigger a false positive.
    """
    code = _strip_hex(code_hex)
    opcodes = _iter_opcode_bytes(code)
    return CREATE_OPCODE in opcodes or CREATE2_OPCODE in opcodes


@dataclass(frozen=True)
class Upgradability:
    """The upgradability verdict for one contract (:func:`detect_upgradability`).

    ``is_upgradable`` is the headline bit; ``mechanism`` is an
    :class:`~repricing_impact.label_sources.schema.UpgradeMechanism` value naming
    the detected pattern (or ``none`` / ``minimal_proxy_immutable``); ``admin`` is
    the EIP-1967 admin address when the admin slot is set, else ``None``.
    """

    is_upgradable: bool
    mechanism: str
    admin: Optional[str] = None


def is_minimal_proxy(code_hex: Optional[str]) -> bool:
    """True if ``code_hex`` is an EIP-1167 minimal-proxy (clone) runtime.

    Matches the canonical ``<prefix><20-byte address><suffix>`` template exactly
    (:data:`EIP1167_PREFIX` / :data:`EIP1167_SUFFIX`). A clone forwards to a
    fixed, bytecode-embedded implementation, so it is a proxy that is **not**
    upgradable — this is the exclusion that separates upgradability from mere
    proxy-ness. Optimised / vanity clone variants are intentionally not matched.
    """
    code = _strip_hex(code_hex)
    return (
        code.startswith(EIP1167_PREFIX)
        and code.endswith(EIP1167_SUFFIX)
        and len(code) == len(EIP1167_PREFIX) + 40 + len(EIP1167_SUFFIX)
    )


def _slot_word(storage: Dict[str, str], slot: str) -> Optional[str]:
    """Return the stored word at ``slot`` if set (non-zero), tolerating un-``0x``ed
    keys; ``None`` when the slot is absent or all-zero."""
    value = storage.get(slot)
    if value is None:
        value = storage.get(_strip_hex(slot))
    if value is None or _is_empty_storage(value):
        return None
    return value


def _slot_address(word: Optional[str]) -> Optional[str]:
    """Extract the low-20-byte address from a 32-byte storage ``word`` (or ``None``)."""
    if word is None:
        return None
    return "0x" + _strip_hex(word).rjust(64, "0")[-40:]


def _supports_interface(
    supports_interface_results: Optional[Dict[str, bool]], interface_id: str
) -> bool:
    """True if ``supports_interface_results`` reports ``interface_id`` as supported."""
    results = supports_interface_results or {}
    normalised = {_strip_hex(k): bool(v) for k, v in results.items()}
    return bool(normalised.get(_strip_hex(interface_id)))


def detect_upgradability(
    code_hex: Optional[str],
    storage: Optional[Dict[str, str]] = None,
    supports_interface_results: Optional[Dict[str, bool]] = None,
) -> Upgradability:
    """Classify how ``code_hex`` / ``storage`` can be upgraded (plan §4.1).

    Signals, in precedence order:

    1. **EIP-1167 minimal proxy** — checked *first*: a clone has a
       ``DELEGATECALL`` and would otherwise trip the slot/selector signals, but
       it is immutable. Returns ``minimal_proxy_immutable`` / not upgradable.
    2. **EIP-2535 Diamond** — DiamondLoupe ERC-165 id or the ``diamondCut``
       selector in the runtime code (diamonds don't use the EIP-1967 slots).
    3. **EIP-1967 slots** (the three :func:`probe` already reads): a set *beacon*
       slot → beacon proxy; a set *admin* slot → transparent proxy; an *impl*
       slot with no admin → the UUPS shape (upgrade auth lives in the logic).
    4. **UUPS selectors embedded in the code** — catches an implementation
       contract probed directly, or a proxy that inlines the upgrade machinery.

    All positive verdicts are structural guesses (``source="heuristic"`` /
    ``confidence="low"`` at the record level). ``admin`` carries the EIP-1967
    admin address when present so callers can later ask "EOA vs multisig/timelock".
    """
    if is_minimal_proxy(code_hex):
        return Upgradability(False, UpgradeMechanism.MINIMAL_PROXY_IMMUTABLE.value)

    storage = storage or {}
    impl = _slot_word(storage, EIP1967_IMPLEMENTATION_SLOT)
    beacon = _slot_word(storage, EIP1967_BEACON_SLOT)
    admin = _slot_word(storage, EIP1967_ADMIN_SLOT)
    admin_addr = _slot_address(admin)
    code = _strip_hex(code_hex)

    if (
        _supports_interface(supports_interface_results, DIAMOND_LOUPE_INTERFACE_ID)
        or DIAMOND_CUT_SELECTOR in code
    ):
        return Upgradability(True, UpgradeMechanism.DIAMOND.value, admin_addr)

    if beacon is not None:
        return Upgradability(True, UpgradeMechanism.BEACON.value, admin_addr)
    if admin is not None:
        return Upgradability(
            True, UpgradeMechanism.EIP1967_TRANSPARENT.value, admin_addr
        )
    if impl is not None:
        return Upgradability(True, UpgradeMechanism.UUPS.value)

    if any(selector in code for selector in UUPS_SELECTORS):
        return Upgradability(True, UpgradeMechanism.UUPS.value)

    return Upgradability(False, UpgradeMechanism.NONE.value)


def _iter_opcode_bytes(code: str) -> List[str]:
    """Return the *opcode* bytes of runtime ``code`` (hex, no ``0x``), skipping
    ``PUSHn`` immediate data so push arguments are not mistaken for opcodes.

    ``PUSH1..PUSH32`` are opcodes ``0x60..0x7f`` followed by ``n`` immediate
    bytes; those bytes are operands, not instructions, so a ``0xf5`` inside a
    ``PUSH`` payload must not be read as ``CREATE2``. Returns the list of
    two-char opcode hex tokens.
    """
    opcodes: List[str] = []
    i = 0
    n = len(code)
    while i + 1 < n or i + 2 <= n:
        byte = code[i : i + 2]
        if len(byte) < 2:
            break
        opcodes.append(byte)
        value = int(byte, 16)
        if 0x60 <= value <= 0x7F:  # PUSH1..PUSH32 -> skip (value - 0x5f) operand bytes
            skip = value - 0x5F
            i += 2 + skip * 2
        else:
            i += 2
    return opcodes


def classify(
    address: str,
    code_hex: Optional[str],
    storage: Optional[Dict[str, str]] = None,
    supports_interface_results: Optional[Dict[str, bool]] = None,
) -> LabelRecord:
    """Combine the structural probes for ``address`` into one :class:`LabelRecord`.

    Sets ``is_proxy`` / ``is_factory`` / ``erc_type`` from the pure detectors.
    ``category`` is ``token`` when an ERC type was detected, else ``unknown``.
    The ``label`` is a human-readable structural descriptor (``"ERC-721 token"``,
    ``"Proxy contract"``, ``"Factory contract"``) or the raw address when nothing
    is detected. Always ``source="heuristic"`` / ``confidence="low"``.
    """
    is_proxy = detect_proxy(code_hex, storage)
    is_factory = detect_factory(code_hex)
    erc_type = detect_erc_type(supports_interface_results, code_hex)
    upgradability = detect_upgradability(code_hex, storage, supports_interface_results)

    if erc_type is not None:
        category = Category.TOKEN.value
        label = _ERC_LABELS.get(erc_type, "Token")
    else:
        category = Category.UNKNOWN.value
        if is_proxy:
            label = "Proxy contract"
        elif is_factory:
            label = "Factory contract"
        else:
            label = address

    return LabelRecord(
        address=address,
        label=label,
        category=category,
        source=Source.HEURISTIC.value,
        confidence=Confidence.LOW.value,
        is_proxy=is_proxy,
        is_factory=is_factory,
        erc_type=erc_type,
        is_upgradable=upgradability.is_upgradable,
        upgrade_mechanism=upgradability.mechanism,
        upgrade_admin=upgradability.admin,
    )


#: Display labels per detected ERC type.
_ERC_LABELS: Dict[str, str] = {
    "erc20": "ERC-20 token",
    "erc721": "ERC-721 token",
    "erc1155": "ERC-1155 token",
}


# --- live JSON-RPC probe (network; not exercised by the offline tests) -----


def _resolve_rpc_url(rpc_url: Optional[str]) -> str:
    """Return the RPC URL, falling back to ``$ETH_RPC_URL``; raise if absent."""
    url = rpc_url or os.environ.get(RPC_URL_ENV_VAR)
    if not url:
        raise ValueError(
            "no JSON-RPC URL: pass rpc_url=... or set the "
            f"{RPC_URL_ENV_VAR} environment variable"
        )
    return url


def _rpc_call(
    url: str, method: str, params: list, request_id: int = 1
) -> Optional[str]:
    """One JSON-RPC 2.0 call; returns the ``result`` string (``None`` on error)."""
    resp = requests.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload and payload["error"]:
        return None
    return payload.get("result")


def _supports_interface_call_data(interface_id: str) -> str:
    """ABI-encode ``supportsInterface(bytes4)`` calldata for ``interface_id``."""
    # selector for supportsInterface(bytes4) = 0x01ffc9a7; the bytes4 arg is
    # left-aligned in its 32-byte word.
    arg = _strip_hex(interface_id).ljust(8, "0")[:8]
    return "0x01ffc9a7" + arg + "0" * (64 - 8)


def probe(address: str, rpc_url: Optional[str] = None) -> dict:
    """Fetch the raw structural inputs for ``address`` over JSON-RPC.

    Issues ``eth_getCode`` (runtime bytecode), ``eth_getStorageAt`` for each
    EIP-1967 slot, and ``eth_call`` of ``supportsInterface`` for the ERC-721 /
    ERC-1155 interface ids. Returns the ``dict`` shape :func:`classify` accepts:
    ``{"address", "code_hex", "storage", "supports_interface_results"}``. This is
    the only network-touching function; the offline tests never call it.
    """
    url = _resolve_rpc_url(rpc_url)

    code_hex = _rpc_call(url, "eth_getCode", [address, "latest"])

    storage: Dict[str, str] = {}
    for slot in EIP1967_SLOTS.values():
        value = _rpc_call(url, "eth_getStorageAt", [address, slot, "latest"])
        if value is not None:
            storage[slot] = value

    supports_interface_results: Dict[str, bool] = {}
    for interface_id in (
        ERC721_INTERFACE_ID,
        ERC1155_INTERFACE_ID,
        DIAMOND_LOUPE_INTERFACE_ID,
    ):
        result = _rpc_call(
            url,
            "eth_call",
            [
                {"to": address, "data": _supports_interface_call_data(interface_id)},
                "latest",
            ],
        )
        # A non-empty word ending in 1 means supportsInterface returned true.
        supports_interface_results[interface_id] = bool(
            result and int(_strip_hex(result) or "0", 16) == 1
        )

    return {
        "address": address,
        "code_hex": code_hex,
        "storage": storage,
        "supports_interface_results": supports_interface_results,
    }


def refresh(
    addresses: List[str],
    cache_dir: Path | str = LABEL_CACHE,
    rpc_url: Optional[str] = None,
) -> Path:
    """Probe + classify each address and write ``heuristic.parquet``.

    Network-touching (drives :func:`probe`); not exercised by the offline tests.
    Returns the cache path (``cache_dir / heuristic.parquet``).
    """
    cache_dir = Path(cache_dir)
    out_path = cache_dir / CACHE_FILENAME

    records: List[LabelRecord] = []
    for address in addresses:
        raw = probe(address, rpc_url=rpc_url)
        records.append(
            classify(
                address=raw["address"],
                code_hex=raw.get("code_hex"),
                storage=raw.get("storage"),
                supports_interface_results=raw.get("supports_interface_results"),
            )
        )

    write_contract_parquet(records, out_path)
    return out_path


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addresses",
        nargs="+",
        required=True,
        help="contract addresses (0x…) to probe and classify",
    )
    parser.add_argument(
        "--rpc-url",
        default=None,
        help=f"JSON-RPC endpoint (default: ${RPC_URL_ENV_VAR})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=LABEL_CACHE,
        help="directory to write heuristic.parquet into (default: label_cache/)",
    )
    args = parser.parse_args(argv)
    out = refresh(
        addresses=args.addresses,
        cache_dir=args.cache_dir,
        rpc_url=args.rpc_url,
    )
    from .schema import read_contract_parquet

    records = read_contract_parquet(out)
    print(f"heuristics: wrote {len(records)} labels -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
