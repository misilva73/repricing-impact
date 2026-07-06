"""Offline unit tests for the Xatu-backed structural label source (plan §4.1).

Exercises the pure :func:`classify_inputs` adapter against synthetic per-address
inputs (the shape :func:`fetch_structural_inputs` produces). No warehouse access:
``fetch_structural_inputs`` / ``refresh`` are never called. The detection logic
itself is covered by ``test_heuristics.py``; these tests only assert the adapter
wires Xatu-shaped inputs into the classifier correctly.
"""

from __future__ import annotations

from repricing_impact.label_sources.heuristics import (
    EIP1167_PREFIX,
    EIP1167_SUFFIX,
    EIP1967_ADMIN_SLOT,
    EIP1967_IMPLEMENTATION_SLOT,
)
from repricing_impact.label_sources.schema import Source, UpgradeMechanism
from repricing_impact.label_sources.xatu_structural import classify_inputs

_ADMIN = "1234567890abcdef1234567890abcdef12345678"
_NONZERO = "0x" + "0" * 24 + _ADMIN
_CLONE = "0x" + EIP1167_PREFIX + _ADMIN + EIP1167_SUFFIX


def test_classify_inputs_transparent_proxy_from_slots():
    # Impl + admin slot both set (the shape storage_reads ∪ storage_diffs yields).
    recs = {
        r.address: r
        for r in classify_inputs(
            {
                "0x00000000000000000000000000000000000000aa": {
                    "code_hex": None,
                    "storage": {
                        EIP1967_IMPLEMENTATION_SLOT: _NONZERO,
                        EIP1967_ADMIN_SLOT: _NONZERO,
                    },
                }
            }
        )
    }
    rec = recs["0x00000000000000000000000000000000000000aa"]
    assert rec.is_proxy is True
    assert rec.is_upgradable is True
    assert rec.upgrade_mechanism == UpgradeMechanism.EIP1967_TRANSPARENT.value
    assert rec.upgrade_admin == "0x" + _ADMIN
    assert rec.source == Source.HEURISTIC.value


def test_classify_inputs_clone_is_not_upgradable():
    recs = {
        r.address: r
        for r in classify_inputs(
            {"0x00000000000000000000000000000000000000bb": {"code_hex": _CLONE}}
        )
    }
    rec = recs["0x00000000000000000000000000000000000000bb"]
    assert rec.is_proxy is True  # a clone has DELEGATECALL
    assert rec.is_upgradable is False
    assert rec.upgrade_mechanism == UpgradeMechanism.MINIMAL_PROXY_IMMUTABLE.value


def test_classify_inputs_plain_contract_unknown():
    recs = {
        r.address: r
        for r in classify_inputs(
            {
                "0x00000000000000000000000000000000000000cc": {
                    "code_hex": "0x6080604052",
                    "storage": {},
                }
            }
        )
    }
    rec = recs["0x00000000000000000000000000000000000000cc"]
    assert bool(rec.is_upgradable) is False
    assert rec.upgrade_mechanism == UpgradeMechanism.NONE.value
    assert rec.upgrade_admin is None


def test_classify_inputs_empty():
    assert classify_inputs({}) == []
