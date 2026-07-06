"""Offline tests for the 4-byte selector decoder (expansion plan §6).

These exercise the pure :func:`~repricing_impact.label_sources.selectors.normalize`
and :func:`~repricing_impact.label_sources.selectors.decode_selector` cores plus a
parquet round-trip — no network, and ``requests`` (imported lazily inside
``fetch_signatures``) is never touched here.

The ``raw_rows`` fixtures below are the already-flattened
``{selector, text_signature, source}`` rows that ``fetch_signatures`` emits. The
live openchain API (``GET /signature-database/v1/lookup?function=...&filter=false``,
verified 2026-07-03) returns, per selector, a list of candidates under
``result.function.<selector>`` — e.g. ``0x38ed1739`` yields the Uniswap V2 swap
signature, and heavily-oversubscribed selectors such as ``0xa9059cbb`` /
``0x70a08231`` return a dozen-plus colliding candidates (``transfer`` /
``balanceOf`` alongside junk). ``fetch_signatures`` flattens each candidate's
``name`` into one raw row per ``(selector, text_signature)``, which is what these
tests feed into ``normalize``.
"""

from __future__ import annotations

from repricing_impact.label_sources import selectors
from repricing_impact.label_sources.schema import write_selector_parquet

# The real Uniswap V2 swap selector and a synthetic colliding junk signature.
SWAP_SIG = "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
JUNK_SIG = "someJunkCollision(bytes)"


def test_normalize_dedups_lowercases_and_keeps_collisions():
    raw_rows = [
        # Uppercased + no-0x-prefix forms of the same selector; both normalise
        # to 0x38ed1739 and yield two DISTINCT candidate signatures.
        {"selector": "0x38ED1739", "text_signature": SWAP_SIG, "source": "4byte"},
        {"selector": "38ed1739", "text_signature": JUNK_SIG, "source": "4byte"},
        # Exact duplicate of the swap pair -> deduped away.
        {"selector": "0x38ed1739", "text_signature": SWAP_SIG, "source": "4byte"},
        # A different selector.
        {"selector": "0xa9059cbb", "text_signature": "transfer(address,uint256)"},
        # Dropped: unparseable selector and blank signature.
        {"selector": "0xzz", "text_signature": "bad()"},
        {"selector": "0x12345678", "text_signature": "   "},
    ]

    rows = selectors.normalize(raw_rows)

    # 2 candidates for the swap selector (dedup removed the 3rd) + 1 transfer.
    assert len(rows) == 3

    by_selector: dict[str, list[str]] = {}
    for row in rows:
        assert row["selector"].startswith("0x") and len(row["selector"]) == 10
        assert row["selector"] == row["selector"].lower()
        by_selector.setdefault(row["selector"], []).append(row["text_signature"])

    # Multiple candidates kept for one selector, first-seen order preserved.
    assert by_selector["0x38ed1739"] == [SWAP_SIG, JUNK_SIG]
    assert by_selector["0xa9059cbb"] == ["transfer(address,uint256)"]

    # Missing source defaults to "4byte".
    transfer_row = next(r for r in rows if r["selector"] == "0xa9059cbb")
    assert transfer_row["source"] == "4byte"


def test_parquet_round_trip_and_load_selector_map(tmp_path):
    rows = selectors.normalize(
        [
            {"selector": "0x38ed1739", "text_signature": SWAP_SIG, "source": "4byte"},
            {"selector": "0x38ed1739", "text_signature": JUNK_SIG, "source": "4byte"},
            {
                "selector": "0xa9059cbb",
                "text_signature": "transfer(address,uint256)",
                "source": "4byte",
            },
        ]
    )

    out = tmp_path / "selectors.parquet"
    write_selector_parquet(rows, out)

    mapping = selectors.load_selector_map(out)
    assert mapping["0x38ed1739"] == [SWAP_SIG, JUNK_SIG]
    assert mapping["0xa9059cbb"] == ["transfer(address,uint256)"]


def test_decode_selector_uses_category_then_deterministic_fallback():
    selector_map = {"0x38ed1739": [SWAP_SIG, JUNK_SIG]}

    # With a matching category, the swap signature wins over the junk collision.
    assert (
        selectors.decode_selector(
            "0x38ed1739", category="swap_dex", selector_map=selector_map
        )
        == SWAP_SIG
    )

    # With no category, a deterministic candidate is returned (shortest here is
    # the junk collision — the point is that it is stable, not that it is right).
    no_cat = selectors.decode_selector("0x38ed1739", selector_map=selector_map)
    assert no_cat in (SWAP_SIG, JUNK_SIG)
    assert no_cat == selectors.decode_selector("0x38ed1739", selector_map=selector_map)
    assert no_cat == JUNK_SIG  # shortest signature, deterministic fallback


def test_load_selector_map_missing_path_returns_empty(tmp_path):
    assert selectors.load_selector_map(tmp_path / "does_not_exist.parquet") == {}


def test_decode_selector_unknown_returns_none():
    assert selectors.decode_selector("0xdeadbeef", selector_map={}) is None
    assert selectors.decode_selector("not-a-selector", selector_map={}) is None
