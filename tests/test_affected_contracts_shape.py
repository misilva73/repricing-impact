"""Fixture-based shape tests for the SHARDED `affected/` output (no DB needed).

These build the seeded sharded fixtures in-process (``make_fixtures``
``build_affected_contracts``) into a temp dir and assert the contract shape
documented in ``site/data/SCHEMA.md`` §5b and emitted by
``scripts/precompute.py::emit_affected_contracts``:

- ``affected/index.json``: top-level shape, name-searchable-only ``contracts`` list,
  ``affected_count`` == number of shard files;
- ``affected/{addr}.json`` shards: the per-contract record (roles_summary omission,
  failure_clusters count-desc ordering + top-8 cap, per-cluster role + kind +
  drivers, distinct_cluster_count / clusters_shown_share consistency, >= 1 cluster);
- the unlabeled affected contract has a shard but is absent from ``index.contracts``.

Kept hermetic and matching the style of ``tests/test_groups_predicate.py`` — pure
Python over the fixture builder, no warehouse access.
"""

import importlib.util
import json
import random
import tempfile
from pathlib import Path

# make_fixtures.py is a script (not an installed module); load it by path.
_MF_PATH = Path(__file__).resolve().parent.parent / "scripts" / "make_fixtures.py"
_spec = importlib.util.spec_from_file_location("make_fixtures", _MF_PATH)
make_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_fixtures)

# Cluster cap mirrored from the emitter (precompute.py AFFECTED_CLUSTER_TOP_N).
TOP_N = 8
VALID_ROLES = {"entry", "oog_site", "revert_site"}
VALID_KINDS = {"oog", "non_oog"}


def _build_sharded():
    """Deterministically build the eip-8038 sharded affected/ fixtures into a temp
    dir. Returns (index_dict, affected_dir_Path)."""
    cfg = dict(make_fixtures.SCHEDULES["eip-8038"])
    cfg["_name"] = "eip-8038"
    rng = random.Random(cfg["seed"])
    series = make_fixtures.build_overview_series(rng, cfg)
    tmp = Path(tempfile.mkdtemp(prefix="affected_fixture_"))
    index = make_fixtures.build_affected_contracts(rng, cfg, series, tmp)
    return index, tmp / "affected"


# Non-shard files in the affected/ dir (index + collapsed-class aggregates).
_NON_SHARD_FILES = {"index.json", "deploy_oog.json"}


def _shard_files(affected_dir):
    """All per-contract shard files (everything except index.json and the
    collapsed-class aggregate deploy_oog.json)."""
    return sorted(
        p for p in affected_dir.glob("*.json") if p.name not in _NON_SHARD_FILES
    )


def _load_shards(affected_dir):
    return {p.stem: json.loads(p.read_text()) for p in _shard_files(affected_dir)}


# --- index.json ---------------------------------------------------------------


def test_index_top_level_shape():
    index, _ = _build_sharded()
    assert index["schedule"] == "eip-8038"
    assert set(index["block_range"]) == {"start", "end"}
    assert isinstance(index["block_range"]["start"], int)
    assert isinstance(index["block_range"]["end"], int)
    assert isinstance(index["g4_total"], int)
    assert isinstance(index["affected_count"], int)
    assert isinstance(index["note"], str) and index["note"]
    assert isinstance(index["contracts"], list)


def test_affected_count_equals_shards_plus_deploy_oog():
    index, affected_dir = _build_sharded()
    # affected_count = shard files + collapsed deploy-OOG accounts (no shards).
    assert (
        index["affected_count"]
        == len(_shard_files(affected_dir)) + index["deploy_oog"]["count"]
    )


def test_index_written_alongside_shards():
    _, affected_dir = _build_sharded()
    assert (affected_dir / "index.json").is_file()
    assert len(_shard_files(affected_dir)) >= 1


def test_index_contracts_are_name_searchable_only():
    index, _ = _build_sharded()
    assert index["contracts"], "fixture must list >= 1 name-searchable contract"
    for row in index["contracts"]:
        addr = row["address"]
        assert addr == addr.lower() and addr.startswith("0x")
        label = row.get("label") or ""
        # name-searchable: a real (non-bare-address) label OR an owner_project set.
        has_real_label = bool(label) and label.lower() != addr.lower()
        assert has_real_label or bool(row.get("owner_project"))
        # role-count keys, when present, are positive; each key is optional.
        for k in ("entry_g4_tx_count", "halt_count", "revert_count"):
            if k in row:
                assert isinstance(row[k], int) and row[k] > 0


def test_index_contracts_sorted_by_footprint_desc():
    index, _ = _build_sharded()

    def footprint(row):
        return (
            row.get("entry_g4_tx_count", 0)
            + row.get("halt_count", 0)
            + row.get("revert_count", 0)
        )

    fps = [footprint(r) for r in index["contracts"]]
    assert fps == sorted(fps, reverse=True)


def test_every_index_contract_has_a_shard():
    index, affected_dir = _build_sharded()
    shards = _load_shards(affected_dir)
    for row in index["contracts"]:
        assert row["address"] in shards


def test_unlabeled_contract_has_shard_but_absent_from_index():
    index, affected_dir = _build_sharded()
    shards = _load_shards(affected_dir)
    indexed = {row["address"] for row in index["contracts"]}
    # There is at least one shard whose contract is unlabeled (bare-address label,
    # no owner_project) — it must exist as a shard yet be missing from the index.
    unlabeled = [
        addr
        for addr, rec in shards.items()
        if (rec.get("label") or "").lower() == addr.lower()
        and not rec.get("owner_project")
    ]
    assert unlabeled, "fixture must include an unlabeled affected contract"
    for addr in unlabeled:
        assert addr not in indexed


# --- per-contract shards ------------------------------------------------------


def test_shard_addressed_by_lowercase_filename():
    _, affected_dir = _build_sharded()
    for p in _shard_files(affected_dir):
        rec = json.loads(p.read_text())
        assert p.stem == p.stem.lower()
        assert rec["address"] == p.stem


def test_shard_identity_header_present():
    _, affected_dir = _build_sharded()
    required = {
        "address",
        "label",
        "category",
        "owner_project",
        "source",
        "confidence",
        "is_proxy",
        "is_factory",
        "is_safe",
        "erc_type",
        "is_upgradable",
        "upgrade_mechanism",
        "upgrade_admin",
        "is_mev_bot",
        "mev_role",
    }
    for rec in _load_shards(affected_dir).values():
        assert required <= set(rec)
        assert isinstance(rec["is_mev_bot"], bool)
        assert rec["confidence"] in ("high", "medium", "low")


def test_roles_summary_presence_and_omission_rules():
    _, affected_dir = _build_sharded()
    saw_omitted_role = False
    for rec in _load_shards(affected_dir).values():
        rs = rec["roles_summary"]
        assert set(rs) <= VALID_ROLES
        assert rs, "an affected contract must have >= 1 role"
        if "entry" in rs:
            e = rs["entry"]
            assert set(e) == {"g4_tx_count", "g4_oog_count", "g4_nonoog_count"}
            assert e["g4_tx_count"] > 0
            assert e["g4_oog_count"] + e["g4_nonoog_count"] == e["g4_tx_count"]
        if "oog_site" in rs:
            assert set(rs["oog_site"]) == {"halt_count"}
            assert rs["oog_site"]["halt_count"] > 0
        if "revert_site" in rs:
            assert rs["revert_site"]["revert_count"] > 0
        if set(rs) != VALID_ROLES:
            saw_omitted_role = True
    # The fixture exercises the omission rule (entry-only + site-only cases).
    assert saw_omitted_role


def test_every_shard_has_at_least_one_cluster():
    _, affected_dir = _build_sharded()
    for rec in _load_shards(affected_dir).values():
        assert len(rec["failure_clusters"]) >= 1


def test_clusters_ordered_by_count_desc_and_capped():
    _, affected_dir = _build_sharded()
    for rec in _load_shards(affected_dir).values():
        counts = [c["count"] for c in rec["failure_clusters"]]
        assert counts == sorted(counts, reverse=True)
        assert len(rec["failure_clusters"]) <= TOP_N


def test_each_cluster_carries_role_kind_and_drivers():
    _, affected_dir = _build_sharded()
    for rec in _load_shards(affected_dir).values():
        for cl in rec["failure_clusters"]:
            assert cl["role"] in VALID_ROLES
            assert cl["kind"] in VALID_KINDS
            assert isinstance(cl["count"], int) and cl["count"] > 0
            assert isinstance(cl["drivers"], dict) and cl["drivers"]
            # non-OOG reverts carry no OOG-halt-only driver keys.
            if cl["kind"] == "non_oog":
                assert "surcharge_at_oog" not in cl["drivers"]
                assert "gas_remaining_at_oog" not in cl["drivers"]
            assert set(cl["gas_delta"]) == {"avg", "p50", "p90"}
            assert 1 <= len(cl["examples"]) <= 2
            for ex in cl["examples"]:
                assert set(ex) == {"tx_hash", "block_number", "gas_delta"}


def test_cluster_role_is_consistent_with_roles_summary():
    _, affected_dir = _build_sharded()
    for rec in _load_shards(affected_dir).values():
        present = set(rec["roles_summary"])
        cluster_roles = {c["role"] for c in rec["failure_clusters"]}
        assert cluster_roles <= present


def test_distinct_cluster_count_and_shown_share_consistent():
    _, affected_dir = _build_sharded()
    saw_capped = False
    for rec in _load_shards(affected_dir).values():
        shown = rec["failure_clusters"]
        dcc = rec["distinct_cluster_count"]
        share = rec["clusters_shown_share"]
        assert dcc >= len(shown)
        assert len(shown) <= min(TOP_N, dcc)
        assert 0.0 <= share <= 1.0
        if dcc > len(shown):
            saw_capped = True
            assert share < 1.0
    # The fixture exercises the "more modes than shown" case.
    assert saw_capped


def test_share_of_role_and_contract_within_unit_interval():
    _, affected_dir = _build_sharded()
    for rec in _load_shards(affected_dir).values():
        for cl in rec["failure_clusters"]:
            for k in ("share_of_role", "share_of_contract"):
                v = cl[k]
                assert v is None or 0.0 <= v <= 1.0


def test_failure_rate_present_only_with_denominator():
    _, affected_dir = _build_sharded()
    saw_rate = saw_null = False
    for rec in _load_shards(affected_dir).values():
        fr = rec["context"]["failure_rate"]
        if fr is None:
            saw_null = True
        else:
            assert set(fr) == {"total_tx", "halt_rate", "revert_rate"}
            assert isinstance(fr["total_tx"], int) and fr["total_tx"] > 0
            saw_rate = True
    # Fixture exercises both the present-denominator and null-denominator cases.
    assert saw_rate and saw_null


def test_context_block_shape():
    _, affected_dir = _build_sharded()
    required = {
        "g3_tx_count",
        "g2_drillin_tx_count",
        "af_tx_count",
        "status_flips",
        "gas_delta",
        "block_span_start",
        "block_span_end",
        "distinct_blocks",
        "failure_rate",
        "entry_functions",
        "failing_functions",
        "halt_contracts",
        "revert_contracts",
        "entry_contracts",
    }
    for rec in _load_shards(affected_dir).values():
        ctx = rec["context"]
        assert required <= set(ctx)
        assert set(ctx["gas_delta"]) == {"avg", "sum", "p50", "p90"}
        for fn in ctx["entry_functions"] + ctx["failing_functions"]:
            assert set(fn) == {"selector", "signature", "count"}
        for mix in ("halt_contracts", "revert_contracts", "entry_contracts"):
            for row in ctx[mix]:
                assert set(row) == {"contract", "label", "category", "count"}


# --- deploy_oog collapsed class -----------------------------------------------

import re

_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_TXHASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_SELECTOR_RE = re.compile(r"^0x6[01]")


def _load_deploy_oog(affected_dir):
    return json.loads((affected_dir / "deploy_oog.json").read_text())


def test_deploy_oog_exists_and_top_level_shape():
    index, affected_dir = _build_sharded()
    assert (affected_dir / "deploy_oog.json").is_file()
    doog = _load_deploy_oog(affected_dir)
    assert doog["class"] == "deploy_oog"
    assert doog["schedule"] == "eip-8038"
    assert isinstance(doog["explainer"], str) and doog["explainer"]
    assert isinstance(doog["count"], int)
    assert doog["count"] == len(doog["accounts"]) == index["deploy_oog"]["count"]


def test_deploy_oog_aggregate_shape():
    _, affected_dir = _build_sharded()
    agg = _load_deploy_oog(affected_dir)["aggregate"]
    assert isinstance(agg["halt_opcode_split"], list) and agg["halt_opcode_split"]
    for row in agg["halt_opcode_split"]:
        assert set(row) == {"key", "count"}
        assert isinstance(row["key"], str)
        assert isinstance(row["count"], int)
    assert set(agg["gas_delta"]) == {"p50", "p90", "min", "max"}
    assert isinstance(agg["drivers"], dict) and agg["drivers"]


def test_deploy_oog_accounts_shape():
    _, affected_dir = _build_sharded()
    accounts = _load_deploy_oog(affected_dir)["accounts"]
    assert accounts
    for addr, acct in accounts.items():
        assert _ADDR_RE.match(addr), addr
        assert _TXHASH_RE.match(acct["tx"]), acct["tx"]
        assert isinstance(acct["block"], int)
        assert isinstance(acct["gas_delta"], int)
        assert isinstance(acct["opcode"], str)
        assert isinstance(acct["selector"], str)
        assert _SELECTOR_RE.match(acct["selector"]), acct["selector"]
        assert isinstance(acct["entry"], str)


def test_deploy_oog_accounts_are_collapsed_not_sharded():
    _, affected_dir = _build_sharded()
    accounts = _load_deploy_oog(affected_dir)["accounts"]
    shard_stems = {p.stem for p in _shard_files(affected_dir)}
    for addr in accounts:
        assert not (affected_dir / f"{addr}.json").exists(), addr
        assert addr not in shard_stems, addr


def test_deploy_oog_registered_in_index_but_accounts_absent():
    index, affected_dir = _build_sharded()
    accounts = _load_deploy_oog(affected_dir)["accounts"]
    assert index["deploy_oog"]["count"] == len(accounts)
    assert index["deploy_oog"]["file"] == "deploy_oog.json"
    indexed = {row["address"] for row in index["contracts"]}
    for addr in accounts:
        assert addr not in indexed, addr
