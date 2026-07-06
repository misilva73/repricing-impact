"""labels.py resolver: cache-absent parity + merged-cache resolution + classify.

The load-bearing guarantee is that with **no** merged cache present,
``label_address`` behaves exactly as it did before the expansion (manual map
only). The rest exercises the new merged-cache path and ``classify_address``.
"""

import pytest

from repricing_impact import labels
from repricing_impact.label_sources import schema as s


@pytest.fixture
def merged_cache(tmp_path, monkeypatch):
    """Point the resolver at a fresh tmp merged cache and clear its memoisation."""

    def _write(records):
        path = tmp_path / "contract_labels.parquet"
        s.write_contract_parquet(records, path)
        monkeypatch.setattr(labels, "MERGED_LABELS_PATH", path)
        labels._reset_label_cache()
        return path

    # Default: no cache file (path points at a nonexistent file).
    monkeypatch.setattr(labels, "MERGED_LABELS_PATH", tmp_path / "absent.parquet")
    labels._reset_label_cache()
    yield _write
    labels._reset_label_cache()


# --- Cache-absent parity: identical to the pre-expansion behaviour ------------


def test_label_address_cache_absent_parity(merged_cache):
    # merged_cache fixture leaves no file present.
    assert labels.label_address(None) == "unknown"
    assert labels.label_address("") == "unknown"
    assert labels.label_address(123) == "unknown"  # non-string
    # Known manual address resolves via ADDRESS_PROJECT_LABELS.
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert labels.label_address(usdt) == "Tether USDT"
    assert labels.label_address(usdt.upper()) == "Tether USDT"  # case-insensitive
    # Unknown address falls back to the raw (original-case) address.
    assert labels.label_address("0xDeadBeef") == "0xDeadBeef"


def test_classify_address_cache_absent(merged_cache):
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    rec = labels.classify_address(usdt)
    assert rec.label == "Tether USDT"
    assert rec.source == "manual"
    # A precompile self-classifies even from the manual map.
    pre = labels.classify_address("0x0000000000000000000000000000000000000001")
    assert pre.category == "precompile"
    # A totally unknown address → unknown record with the raw address as label.
    unk = labels.classify_address("0x0000000000000000000000000000000000009999")
    assert unk.category == "unknown"
    assert unk.source == "unknown"


# --- Merged-cache path --------------------------------------------------------


def test_label_address_prefers_merged_cache(merged_cache):
    merged_cache(
        [
            s.LabelRecord(
                address="0x1234",
                label="Cached Name",
                category="swap_dex",
                source="dune",
                confidence="high",
            )
        ]
    )
    assert labels.label_address("0x1234") == "Cached Name"
    # Still falls back to the manual map for addresses absent from the cache.
    assert (
        labels.label_address("0xdac17f958d2ee523a2206206994597c13d831ec7")
        == "Tether USDT"
    )


def test_classify_address_returns_full_record_from_cache(merged_cache):
    merged_cache(
        [
            s.LabelRecord(
                address="0x1234",
                label="Cached Name",
                category="swap_dex",
                owner_project="uniswap",
                source="dune",
                confidence="high",
            )
        ]
    )
    rec = labels.classify_address("0x1234")
    assert rec.category == "swap_dex"
    assert rec.owner_project == "uniswap"
    assert rec.confidence == "high"


def test_manual_label_records_cover_the_map():
    recs = labels.manual_label_records()
    assert len(recs) == len(labels.ADDRESS_PROJECT_LABELS)
    assert all(r.source == "manual" for r in recs)
    # Precompiles self-classify; other manual entries stay category-unknown.
    by = {r.address: r for r in recs}
    assert by["0x0000000000000000000000000000000000000001"].category == "precompile"
