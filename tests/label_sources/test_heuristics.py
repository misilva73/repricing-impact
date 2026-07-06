"""Offline unit tests for the on-chain heuristics label source (plan §4.1).

Feeds synthetic bytecode / storage / ``supportsInterface`` inputs to the pure
classification functions and round-trips a record through parquet. No network /
RPC access — :func:`probe` and :func:`refresh` are never called.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from repricing_impact.label_sources.heuristics import (
    DIAMOND_CUT_SELECTOR,
    DIAMOND_LOUPE_INTERFACE_ID,
    EIP1167_PREFIX,
    EIP1167_SUFFIX,
    EIP1967_ADMIN_SLOT,
    EIP1967_BEACON_SLOT,
    EIP1967_IMPLEMENTATION_SLOT,
    ERC721_INTERFACE_ID,
    ERC1155_INTERFACE_ID,
    ERC20_SELECTORS,
    UUPS_SELECTORS,
    classify,
    detect_erc_type,
    detect_factory,
    detect_proxy,
    detect_upgradability,
    is_erc165,
    is_minimal_proxy,
)
from repricing_impact.label_sources.schema import (
    Category,
    Confidence,
    Source,
    UpgradeMechanism,
    read_contract_parquet,
    write_contract_parquet,
)

# A non-zero 32-byte word (an EIP-1967 implementation address, left-padded).
_ADMIN_ADDRESS = "1234567890abcdef1234567890abcdef12345678"
_NONZERO_SLOT_VALUE = "0x" + "0" * 24 + _ADMIN_ADDRESS
_ZERO_SLOT_VALUE = "0x" + "0" * 64

# A canonical EIP-1167 minimal-proxy runtime (clone) forwarding to _ADMIN_ADDRESS.
_MINIMAL_PROXY_CODE = "0x" + EIP1167_PREFIX + _ADMIN_ADDRESS + EIP1167_SUFFIX

# Plain runtime code with no CREATE/CREATE2/DELEGATECALL and no ERC selectors.
_PLAIN_CODE = "0x6080604052348015600f57600080fd5b50"

# Runtime code containing a bare CREATE2 (0xf5) opcode.
_CREATE2_CODE = "0x60806040" + "f5" + "00"


def test_is_erc165():
    assert is_erc165({ERC721_INTERFACE_ID: True}) is True
    assert is_erc165({ERC721_INTERFACE_ID: False}) is False
    assert is_erc165({}) is False
    assert is_erc165(None) is False


def test_detect_proxy_eip1967_storage():
    storage = {EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE}
    assert detect_proxy(_PLAIN_CODE, storage) is True


def test_detect_proxy_zero_slot_is_not_a_proxy():
    storage = {EIP1967_IMPLEMENTATION_SLOT: _ZERO_SLOT_VALUE}
    assert detect_proxy(_PLAIN_CODE, storage) is False


def test_detect_proxy_delegatecall_opcode():
    # 0xf4 = DELEGATECALL as a standalone opcode signals the forwarding pattern.
    assert detect_proxy("0x60806040f400", None) is True


def test_detect_erc_type_erc721():
    assert detect_erc_type({ERC721_INTERFACE_ID: True}) == "erc721"


def test_detect_erc_type_erc1155():
    assert detect_erc_type({ERC1155_INTERFACE_ID: True}) == "erc1155"


def test_detect_erc_type_erc20_from_selectors():
    code = "0x" + "".join(ERC20_SELECTORS) + "0000"
    assert detect_erc_type(None, code) == "erc20"


def test_detect_erc_type_none():
    assert detect_erc_type({}, _PLAIN_CODE) is None
    assert detect_erc_type(None, None) is None


def test_detect_factory_create2():
    assert detect_factory(_CREATE2_CODE) is True


def test_detect_factory_none():
    assert detect_factory(_PLAIN_CODE) is False


def test_detect_factory_ignores_push_immediate():
    # A 0xf5 byte that is PUSH1's immediate operand must NOT count as CREATE2.
    # 60 f5 = PUSH1 0xf5 ; the f5 is push data, not an opcode.
    assert detect_factory("0x60f500") is False


def test_is_minimal_proxy():
    assert is_minimal_proxy(_MINIMAL_PROXY_CODE) is True
    # A near-miss (wrong-length body) is not a canonical clone.
    assert (
        is_minimal_proxy("0x" + EIP1167_PREFIX + "dead" + EIP1967_ADMIN_SLOT) is False
    )
    assert is_minimal_proxy(_PLAIN_CODE) is False


def test_detect_upgradability_minimal_proxy_is_not_upgradable():
    # The key case: a clone IS a proxy (has DELEGATECALL) but can never change.
    assert detect_proxy(_MINIMAL_PROXY_CODE) is True
    up = detect_upgradability(_MINIMAL_PROXY_CODE)
    assert up.is_upgradable is False
    assert up.mechanism == UpgradeMechanism.MINIMAL_PROXY_IMMUTABLE.value


def test_detect_upgradability_transparent_from_admin_slot():
    up = detect_upgradability(
        _PLAIN_CODE,
        storage={
            EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE,
            EIP1967_ADMIN_SLOT: _NONZERO_SLOT_VALUE,
        },
    )
    assert up.is_upgradable is True
    assert up.mechanism == UpgradeMechanism.EIP1967_TRANSPARENT.value
    assert up.admin == "0x" + _ADMIN_ADDRESS


def test_detect_upgradability_beacon_slot():
    up = detect_upgradability(
        _PLAIN_CODE, storage={EIP1967_BEACON_SLOT: _NONZERO_SLOT_VALUE}
    )
    assert up.mechanism == UpgradeMechanism.BEACON.value


def test_detect_upgradability_uups_impl_slot_without_admin():
    up = detect_upgradability(
        _PLAIN_CODE, storage={EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE}
    )
    assert up.is_upgradable is True
    assert up.mechanism == UpgradeMechanism.UUPS.value
    assert up.admin is None


def test_detect_upgradability_uups_from_selectors_in_code():
    code = "0x" + "".join(UUPS_SELECTORS) + "00"
    up = detect_upgradability(code)
    assert up.mechanism == UpgradeMechanism.UUPS.value


def test_detect_upgradability_diamond_from_loupe_interface():
    up = detect_upgradability(
        _PLAIN_CODE, supports_interface_results={DIAMOND_LOUPE_INTERFACE_ID: True}
    )
    assert up.mechanism == UpgradeMechanism.DIAMOND.value


def test_detect_upgradability_diamond_from_cut_selector():
    up = detect_upgradability("0x" + DIAMOND_CUT_SELECTOR + "00")
    assert up.mechanism == UpgradeMechanism.DIAMOND.value


def test_detect_upgradability_none():
    up = detect_upgradability(_PLAIN_CODE)
    assert up.is_upgradable is False
    assert up.mechanism == UpgradeMechanism.NONE.value
    assert up.admin is None


def test_classify_sets_upgradability_fields():
    rec = classify(
        "0x00000000000000000000000000000000000000ee",
        _PLAIN_CODE,
        storage={
            EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE,
            EIP1967_ADMIN_SLOT: _NONZERO_SLOT_VALUE,
        },
    )
    assert rec.is_upgradable is True
    assert rec.upgrade_mechanism == UpgradeMechanism.EIP1967_TRANSPARENT.value
    assert rec.upgrade_admin == "0x" + _ADMIN_ADDRESS


def test_classify_clone_is_proxy_but_not_upgradable():
    rec = classify("0x00000000000000000000000000000000000000ff", _MINIMAL_PROXY_CODE)
    assert rec.is_proxy is True
    assert rec.is_upgradable is False
    assert rec.upgrade_mechanism == UpgradeMechanism.MINIMAL_PROXY_IMMUTABLE.value


def test_classify_proxy():
    rec = classify(
        "0x00000000000000000000000000000000000000aa",
        _PLAIN_CODE,
        storage={EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE},
    )
    assert rec.is_proxy is True
    assert "proxy" in rec.label.lower()
    assert rec.category == Category.UNKNOWN.value
    assert rec.source == Source.HEURISTIC.value
    assert rec.confidence == Confidence.LOW.value


def test_classify_erc721_token():
    rec = classify(
        "0x00000000000000000000000000000000000000bb",
        _PLAIN_CODE,
        supports_interface_results={ERC721_INTERFACE_ID: True},
    )
    assert rec.erc_type == "erc721"
    assert rec.category == Category.TOKEN.value
    assert "erc-721" in rec.label.lower()
    assert rec.source == Source.HEURISTIC.value
    assert rec.confidence == Confidence.LOW.value


def test_classify_factory():
    rec = classify("0x00000000000000000000000000000000000000cc", _CREATE2_CODE)
    assert rec.is_factory is True


def test_classify_nothing_detected():
    addr = "0x00000000000000000000000000000000000000dd"
    rec = classify(addr, _PLAIN_CODE)
    assert rec.category == Category.UNKNOWN.value
    assert rec.label == addr  # falls back to the raw (lowercased) address
    assert rec.is_proxy is False
    assert rec.is_factory is False
    assert rec.erc_type is None
    assert rec.source == Source.HEURISTIC.value
    assert rec.confidence == Confidence.LOW.value


def test_parquet_round_trip_preserves_structural_tags(tmp_path: Path):
    records = [
        classify(
            "0x00000000000000000000000000000000000000aa",
            _CREATE2_CODE,
            storage={EIP1967_IMPLEMENTATION_SLOT: _NONZERO_SLOT_VALUE},
            supports_interface_results={ERC1155_INTERFACE_ID: True},
        ),
        classify("0x00000000000000000000000000000000000000dd", _PLAIN_CODE),
    ]
    out = tmp_path / "heuristic.parquet"
    write_contract_parquet(records, out)
    assert out.exists()

    by_addr = {r.address: r for r in read_contract_parquet(out)}
    tagged = by_addr["0x00000000000000000000000000000000000000aa"]
    assert tagged.is_proxy is True
    assert tagged.is_factory is True
    assert tagged.erc_type == "erc1155"
    assert tagged.category == Category.TOKEN.value

    plain = by_addr["0x00000000000000000000000000000000000000dd"]
    assert bool(plain.is_proxy) is False
    assert bool(plain.is_factory) is False
    # An unset optional column round-trips through parquet as a missing value
    # (None or pandas NaN); either way it is not a real erc type.
    assert pd.isna(plain.erc_type) or plain.erc_type in (None, "")
