"""Offline unit tests for the MEV-bot overlay fetcher (plan §7, §4.3).

Exercises :func:`repricing_impact.label_sources.mev.normalize` on synthetic rows
and a parquet round-trip. No network: :func:`fetch_zeromev` is never called.
"""

from __future__ import annotations

from repricing_impact.label_sources import mev
from repricing_impact.label_sources.schema import (
    read_contract_parquet,
    write_contract_parquet,
)

# Fixtures mirror the REAL ZeroMEV /v1/mevBlock schema: a flat array of
# per-transaction rows, each with a `mev_type` and the actor in `address_from`.
# A sandwich attack is modelled as separate frontrun/backrun (attacker) rows
# bracketing one or more `sandwich` (victim) rows.
ATTACKER = "0xAAaaAAaaAAaAaAAaAaaAaaAAAAaaAaAAAaAaAaA1"
VICTIM = "0xBBBBbbBBBbBBBbbBbBBbBbBBbbBBbBbBbbbbBbB2"
ARB_BOT = "0xCcccCCCcCcccCccCcCCcCCcCcCCCcCCccCcCcCC3"
LIQUIDATOR = "0xDdDdddDDDDddddDDDddddddddddDDddDDDDddDD44"


def _synthetic_rows():
    return [
        # arb: the arbitrageur is the tx sender (address_from).
        {"mev_type": "arb", "tx_index": 0, "address_from": ARB_BOT},
        # Sandwich, as ZeroMEV emits it — three separate tx rows. The frontrun and
        # backrun rows are the ATTACKER's txs (same searcher address_from); the
        # `sandwich` row is the VICTIM. We must emit the attacker, never the victim.
        {"mev_type": "frontrun", "tx_index": 1, "address_from": ATTACKER},
        {"mev_type": "sandwich", "tx_index": 2, "address_from": VICTIM},
        {"mev_type": "backrun", "tx_index": 3, "address_from": ATTACKER},
        # liquid: the liquidator is the tx sender.
        {"mev_type": "liquid", "tx_index": 4, "address_from": LIQUIDATOR},
        # swap: non-MEV volume row — must be skipped.
        {"mev_type": "swap", "tx_index": 5, "address_from": VICTIM},
    ]


def test_roles_and_overlay_flags():
    records = mev.normalize(_synthetic_rows())
    by_role = {r.mev_role: r for r in records}

    assert set(by_role) == {"arb", "sandwich", "liquidation"}

    for rec in records:
        assert rec.is_mev_bot is True
        assert rec.source == "mev"
        assert rec.confidence == "medium"
        assert rec.category == "mev_bot"

    assert by_role["arb"].address == ARB_BOT.lower()
    assert by_role["liquidation"].address == LIQUIDATOR.lower()


def test_sandwich_emits_attacker_not_victim():
    records = mev.normalize(_synthetic_rows())
    sandwich = [r for r in records if r.mev_role == "sandwich"]

    # The frontrun and backrun rows both resolve to the attacker's address; the
    # `sandwich` (victim) row maps to no role and is dropped.
    assert sandwich  # at least one sandwich-role record
    assert {r.address for r in sandwich} == {ATTACKER.lower()}

    # The victim must NOT appear anywhere in the emitted records — not from the
    # `sandwich` row nor the non-MEV `swap` row (both carry the victim address).
    emitted = {r.address for r in records}
    assert VICTIM.lower() not in emitted


def test_skips_unmapped_type_and_missing_attacker():
    rows = [
        {"mev_type": "unknown_thing", "address_from": ARB_BOT},
        # A `sandwich` row is the victim, not a bot -> never emitted.
        {"mev_type": "sandwich", "address_from": VICTIM},
        # `swap` rows are non-MEV volume -> skipped.
        {"mev_type": "swap", "address_from": ARB_BOT},
        # Mapped type but no address_from -> skipped.
        {"mev_type": "arb", "tx_index": 7},
    ]
    assert mev.normalize(rows) == []


def test_synthetic_label_fallback():
    (rec,) = mev.normalize([{"mev_type": "arb", "address_from": ARB_BOT}])
    assert rec.label == "MEV bot (arb)"


def test_parquet_roundtrip_preserves_overlay(tmp_path):
    records = mev.normalize(_synthetic_rows())
    path = tmp_path / "mev_bots.parquet"
    write_contract_parquet(records, path)

    round_tripped = read_contract_parquet(path)
    by_role = {r.mev_role: r for r in round_tripped}

    assert set(by_role) == {"arb", "sandwich", "liquidation"}
    for rec in round_tripped:
        assert bool(rec.is_mev_bot) is True
        assert rec.mev_role in {"arb", "sandwich", "liquidation"}
        assert rec.source == "mev"
    assert by_role["sandwich"].address == ATTACKER.lower()
