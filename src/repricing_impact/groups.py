"""The transaction partition — **single source of truth**.

Every aggregate in the precompute pipeline derives the group partition from
this module so the predicate is defined exactly once. The partition is computed
**per (schedule, block)**, pinned to ``chain_id = 1`` and one
``analysis_config_hash`` (see :mod:`repricing_impact.config`).

The groups
----------

The three "changed" drill-in groups (Succeeds with changes, Fixable,
Potentially broken) all require a **working baseline** (``baseline_success =
true``): they describe transactions the repricing changed relative to a
baseline that itself succeeded. Every retained drill-in whose baseline replay
already failed is instead **Already failing** — it was broken before the
repricing, so a schedule-side failure is not "newly broken".

==  =================================  ============================================
Group                                  Source / definition
==  =================================  ============================================
No change                              ``block_coverage.tx_count_unchanged`` —
                                        identical outcome + traces (aggregate only).
Succeeds with changes                  ``block_coverage.tx_count_gas_only``
                                        **+** retained ``_divergence`` rows with a
                                        working baseline that still succeed at the
                                        original limit (``baseline_success = true
                                        AND schedule_success = true`` ⟺ ``min_mult
                                        <= 1``). Trace / event-log / output-only
                                        drill-ins.
Fixable with gas-limit increase        Retained ``_divergence`` rows with a working
                                        baseline that fail at the original limit but
                                        complete at the ``10x`` tier, carrying a
                                        measured rescue multiplier:
                                        ``baseline_success = true AND
                                        schedule_success = false AND min_mult >
                                        1`` (any value above 1, up to the ``10x``
                                        sweep ceiling).
Potentially broken                     Retained ``_divergence`` rows with a working
                                        baseline that are **not rescued at any
                                        multiplier** (``min_mult`` is NULL):
                                        ``baseline_success = true AND
                                        schedule_success = false AND min_mult IS
                                        NULL``. **Excludes** transactions that
                                        already failed in the baseline.
Already failing                        Retained ``_divergence`` rows whose baseline
                                        replay failed (``baseline_success = false``),
                                        regardless of the schedule outcome. Holds
                                        both baseline-fail -> schedule-success
                                        *rescues* and permanent fail -> fail rows.
Unknown                                ``tx_count - (No change + Succeeds + Fixable
                                        + Potentially broken + Already failing)`` —
                                        txs with no retained drill-in (dropped at
                                        the producer's per-block
                                        ``drill_ins_truncated`` cap). 0 on
                                        non-truncated blocks.
==  =================================  ============================================

Internally the group keys stay ``g1`` (No change), ``g2`` (Succeeds with
changes), ``g3`` (Fixable), ``g4`` (Potentially broken), ``af`` (Already
failing), ``g5`` (Unknown).

Predicate rationale (from ``docs/verification-findings.md`` — verified facts)
-----------------------------------------------------------------------------

The split is keyed on **both** ``schedule_success`` and
``min_multiplier_to_succeed`` for defensiveness. Verification established, over
2.48M drill-in rows with zero leakage, the biconditional::

    schedule_success = true  <=>  min_multiplier_to_succeed <= 1

so the G2/G4 boundary is safe either way. We key the success side on
``schedule_success`` directly (robust if a future producer re-run leaves
``min_mult`` NULL for txs that already pass at 1x) and use ``min_mult`` only to
split the *failing* side into G3 (needs a bump) vs G4 (not rescued). A unit
test asserts the biconditional so a producer regression is caught.

Caveats (surface in the UI; numbered per the implementation plan)
-----------------------------------------------------------------

1. ``min_multiplier_to_succeed`` NULL means "did not complete even at the ``10x``
   ceiling". G4 is *potentially* broken; the ``replay_halt_oog`` sub-split (below)
   is the authoritative "does more gas help" signal (``false`` = genuinely
   unfixable non-gas halt; ``true`` = gas-bound beyond ``10x``).
2. eip-8037's per-tx state reservoir **is** exercised in this run (the sweep is
   not EIP-7825-capped), so the G3/G4 split for state-bound txs is trustworthy
   and the reservoir columns are genuine signal.
3. The ``<=1 / >1 / NULL`` boundary is **load-bearing**. ``min_mult`` is a
   *continuous measured ratio* (``schedule_gas_used / tx_gas_limit``,
   ``Nullable(Float64)``), not a discrete tier; values below 1 are common. The
   boundary cleanly separates "succeeds as-is" / "needs a bump (up to 10x)" /
   "not rescued at 10x".
4. Unknown = truncation + non-drill-in gap. Surface the ``drill_ins_truncated``
   share so users know how much of Unknown is dropped drill-ins.
5. **No change / Succeeds with changes have no recipient** (the aggregate-only
   cohorts), so per-contract ratios are over the drill-in cohort (Succeeds
   drill-in + Fixable + Potentially broken) only.
6. **Potentially broken excludes baseline failures.** A drill-in that already
   failed in the baseline cannot be "newly broken" by the repricing, so it is
   carved into the Already-failing group (``baseline_success = false``) rather
   than counted as Potentially broken.

How the multiplier is actually produced (verified empirically 2026-07-03)
-------------------------------------------------------------------------

⚠️ The run manifest's ``gas_limit_multipliers = [1, 2, 4, 8]`` is **wrong**. The
producer re-runs each tx at exactly **two** gas limits: ``1x`` (the original
limit) and ``10x``. ``min_multiplier_to_succeed`` is not a swept tier — it is the
exact ratio::

    min_multiplier_to_succeed = schedule_gas_used / tx_gas_limit

measured from the run that *completes*. So:

- If the tx completes at ``10x``: ``min_mult`` is the measured completion ratio
  (continuous, ``0 < min_mult <= 10``; empirically maxes at 9.9979, never > 10 —
  measured on v10 and **re-measured unchanged over the full v11 window**,
  2026-07-30),
  and ``replay_halt_oog`` is NULL (the top tier did not halt).
- If the tx fails even at ``10x``: ``min_mult`` is NULL (**G4**), and
  ``replay_halt_oog`` records the top-tier halt kind — ``true`` (still OOG at
  ``10x``) or ``false`` (a non-gas halt at ``10x``).

Confirmed across all 53M non-null eip-8037 rows: ``min_mult ==
schedule_gas_used / tx_gas_limit`` exactly, and the ratio hard-caps at 10.000.

Boundary note: ``min_mult > 8`` rows are **not** an "estimate beyond the swept
range" — the real ceiling is ``10x``, so they were genuinely rescued and
measured (fixable, **G3**). G4 is strictly the not-rescued-at-``10x`` cohort
(``min_mult IS NULL``).

G4 fixability sub-split (keyed on the top-tier halt kind)
--------------------------------------------------------

The ``oog_*`` halt-site columns describe the *failing* (original-limit) halt,
**not** the top-tier outcome, so they are *not* a fixability signal. The only
authoritative "does more gas help" signal is ``replay_halt_oog``:

- ``replay_halt_oog = false`` — non-gas halt at ``10x``: no gas-limit increase
  rescues it → **genuinely broken** (empirically ~99.98% of G4).
- ``replay_halt_oog = true`` — still OOG at ``10x``: gas-bound, would need a
  ``> 10x`` bump or is an unbounded loop → fixability *unknown* (a tiny sliver).

De-duplication
--------------

All ``gas_analysis_*`` tables are ReplacingMergeTree. The small validation
window showed **zero** duplicate ``row_id``, but at **full scale the
``_divergence`` table DOES carry pre-merge duplicates** (~2.2-2.4k duplicate
``row_id`` over the full 1M-block range, all localized to blocks
24,400,000-24,499,999). A plain ``count()`` / ``GROUP BY`` would double-count
them. ``block_coverage`` and ``block_summary`` were verified to have **zero**
duplicates even at full scale (one row per block / per (block,class)).

The chosen dedup approach — decided once and applied to **every**
``_divergence`` aggregate via :func:`deduped_divergence_subquery` — is an inner
``argMax(col, updated_at) GROUP BY row_id`` subquery feeding the outer
``GROUP BY`` / ``count()``. This is preferred over ``FINAL`` on the 113M-row
distributed table (expensive) and never selects ``trace_payload``. On that
deduped relation the :data:`DEDUP_GUARD_SQL` invariant
(``count() - uniqExact(row_id) == 0``) holds by construction; precompute asserts
it post-dedup and only raises if dedup itself failed, while reporting the raw
duplicate count as an informational stat.
"""

from __future__ import annotations

import re

# Top gas-limit multiplier the producer actually replays at. The manifest's
# ``gas_limit_multipliers = [1,2,4,8]`` is WRONG (verified 2026-07-03,
# re-confirmed against producer v11 2026-07-30): the real
# sweep is two points, ``1x`` and ``10x``. Not a G3/G4 partition boundary — it is
# the sweep ceiling and the top edge of the G3 multiplier histogram. Every
# rescued (G3) ``min_mult`` is measured and ``<= 10`` (empirical max 9.9979 on
# both the v10 and the v11 run).
TOP_MULTIPLIER = 10

# --- Per-drill-in-row SQL predicates (use inside _divergence aggregates) ------
#
# These are mutually exclusive and exhaustive over every *retained* drill-in
# row: each row is exactly one of Already-failing / G2-drill-in / G3 / G4.
# Combined, ``af + g2_drillin + g3 + g4 == retained_drill_in_count``.
#
# The three "changed" groups (G2 drill-in, G3, G4) are all gated on
# ``baseline_success = true`` so they describe transactions the repricing
# changed relative to a working baseline. Every drill-in whose baseline replay
# already failed is instead the Already-failing cohort.

# Already failing: the baseline replay itself failed. Independent of the schedule
# outcome (both fail->success rescues and permanent fail->fail rows land here).
# This is the single predicate that carves baseline failures out of the changed
# groups (so "Potentially broken" excludes transactions that failed in baseline).
AF_PREDICATE = "baseline_success = false"

# G2 drill-in member: worked in the baseline and still succeeds at the original
# limit under the schedule (trace / gas / log / output-only changes).
G2_DRILLIN_PREDICATE = "baseline_success = true AND schedule_success = true"

# G3: worked in the baseline, fails at the original limit, but completes at the
# ``10x`` ceiling with a measured rescue multiplier (any ``min_mult > 1``, up to
# ``10`` — see "How the multiplier is actually produced" in the module docstring).
G3_PREDICATE = (
    "baseline_success = true "
    "AND schedule_success = false "
    "AND min_multiplier_to_succeed > 1"
)

# G4: worked in the baseline but was never rescued at any multiplier
# (``min_mult IS NULL``). Excludes transactions that already failed in the
# baseline (those are Already failing).
G4_PREDICATE = (
    "baseline_success = true "
    "AND schedule_success = false "
    "AND min_multiplier_to_succeed IS NULL"
)

# G4 fixability sub-split — keyed on ``replay_halt_oog`` (the TOP-tier halt kind),
# the only authoritative "does more gas help" signal. NOT the ``oog_*`` halt-site
# columns, which describe the failing *original-limit* run, not the ceiling run.
# These two are mutually exclusive and (with the ~0 NULL remainder) exhaust G4.
#
# Non-gas halt at the 10x ceiling: no gas-limit increase rescues it -> genuinely
# broken by the repricing (empirically ~99.98% of G4).
G4_NOT_GAS_FIXABLE_PREDICATE = f"({G4_PREDICATE}) AND replay_halt_oog = false"

# Still OOG at the 10x ceiling: gas-bound; would need a >10x bump or is an
# unbounded loop -> fixability unknown (a tiny sliver of G4).
G4_STILL_OOG_PREDICATE = f"({G4_PREDICATE}) AND replay_halt_oog = true"

# Rescue: a beneficial baseline-fail -> schedule-success flip (an Already-failing
# sub-cohort; surfaced separately so beneficial flips aren't buried).
RESCUE_PREDICATE = "baseline_success = false AND schedule_success = true"

# Corroborating (not authoritative) G3 sub-signal — see caveat R2.
OUTER_LIMIT_ONLY_FAILURE_PREDICATE = "outer_limit_only_failure = 1"

# --- Deploy-OOG detector (collapses freshly-deployed self-halting accounts) ----
#
# Contract init code (a constructor, or a minimal-proxy clone's setup) universally
# begins with a PUSH of the free-memory pointer / clone bootstrap: the first byte
# is ``0x60xx`` (PUSH1) or ``0x61xx`` (PUSH2). Canonical Solidity creation code
# opens ``6080604052…`` (PUSH1 0x80, PUSH1 0x40, MSTORE — set the free-memory
# pointer); an ERC-1167 clone opens ``603d3d81…``; other clone/proxy bootstraps
# open ``61…3d``. A *dispatched* function is reached via its 4-byte selector,
# which is a keccak hash truncation (effectively random), so it essentially never
# starts with ``0x60``/``0x61``. Hence a ``0x60xx``/``0x61xx`` prefix on the
# failing-frame selector is a reliable marker that the frame is executing
# constructor / init code rather than a dispatched function.
DEPLOY_OOG_SELECTOR_REGEX = r"^0x6[01]"

# The account-level rule the precompute emitter applies (authoritative here).
#
# An **affected contract** — the G4-only affected set: an address that appears as
# a recipient, ``oog_contract``, or ``divergence_contract`` in any G4 tx — is a
# **"deploy-OOG account"** iff ALL of:
#
#   (a) it is NOT name-searchable: no real label distinct from its ``0x`` address,
#       and no ``owner_project``;
#   (b) it never appears as a tx recipient (its entry-role count == 0);
#   (c) it appears >= 1 time as an OOG halt site (``oog_contract == addr``);
#   (d) EVERY one of its OOG-halt rows lands in init code —
#       ``is_initcode_selector(coalesce(tier1_failing_selector, entry_selector))``
#       is True for all of them.
#
# Accounts matching this rule are collapsed out of the per-contract shards into a
# single aggregate file (they are freshly-deployed contract accounts — mostly
# ERC-4337 smart-account wallets created via CREATE2 inside EntryPoint.handleOps —
# that run out of gas during their OWN construction, appearing only as a self-halt
# site and never as a real named contract).
#
# Verified: 102,124 / 117,905 (86.6%) of eip-8037 (a state-CREATION repricing)
# affected contracts match; ~0 for eip-8038 (state-ACCESS). Among the matches the
# halt opcodes are RETURN (code-deposit) and SSTORE (constructor storage) — so the
# detector keys on the **init-code selector**, NOT the halt opcode.
DEPLOY_OOG_RULE_DOC = """Deploy-OOG account rule (single source of truth).

An affected contract (the G4-only affected set: appears as recipient,
oog_contract, or divergence_contract in any G4 tx) is a "deploy-OOG account"
iff ALL of:

  (a) it is NOT name-searchable — no real label distinct from its 0x address,
      and no owner_project;
  (b) it never appears as a tx recipient (its entry-role count == 0);
  (c) it appears >= 1 time as an OOG halt site (oog_contract == addr);
  (d) EVERY one of its OOG-halt rows lands in init code:
      is_initcode_selector(coalesce(tier1_failing_selector, entry_selector))
      is True.

These accounts are freshly-deployed contract accounts (mostly ERC-4337
smart-account wallets created via CREATE2 inside EntryPoint.handleOps) that run
out of gas during their OWN construction; they appear only as a self-halt site,
never as a real named contract. The emitter collapses them out of the
per-contract shards into a single aggregate file.

Verified: 102,124/117,905 (86.6%) of eip-8037 (state-creation) affected
contracts match; ~0 for eip-8038 (state-access). Halt opcodes among them are
RETURN (code-deposit) + SSTORE (constructor storage) — detection keys on the
init-code selector, NOT the opcode.
"""


def is_initcode_selector(selector) -> bool:
    """True when a 4-byte failing-frame selector looks like contract init code.

    Contract init code (constructor / minimal-proxy clone setup) universally
    begins with a PUSH of the free-memory pointer or clone bootstrap — ``0x60xx``
    (PUSH1) or ``0x61xx`` (PUSH2), e.g. ``6080604052…`` (canonical Solidity
    creation), ``603d3d81…`` (ERC-1167 clone), ``61…3d``. A real dispatched
    function's 4-byte selector is a keccak-hash truncation (effectively random),
    so an init-code prefix at the failing frame is a reliable marker that the
    frame is executing constructor / init code rather than a dispatched function.

    Returns ``False`` for ``None`` / empty; otherwise the truthiness of
    ``re.match(DEPLOY_OOG_SELECTOR_REGEX, str(selector))``.
    """
    if not selector:
        return False
    return bool(re.match(DEPLOY_OOG_SELECTOR_REGEX, str(selector)))


# Cheap ReplacingMergeTree dedup guard: on a row_id-DEDUPED relation this must be
# 0 (one row per row_id by construction). On the RAW table it can be > 0 at full
# scale (verified: ~2.2-2.4k duplicate row_ids over the full 1M-block range,
# absent in small windows). Hence every _divergence aggregate reads from the
# deduped relation built by :func:`deduped_divergence_subquery`.
DEDUP_GUARD_SQL = "count() - uniqExact(row_id)"

# Fully-qualified distributed _divergence table (never the _local shards).
DIVERGENCE_TABLE = "gas_analysis.gas_analysis_divergence"


def deduped_divergence_subquery(columns, where: str, alias: str = "d") -> str:
    """A row_id-deduped ``_divergence`` relation: ``(... ) AS <alias>``.

    ReplacingMergeTree keeps ``updated_at`` as the version and ``row_id`` as the
    identity. Pre-merge, the raw table can carry duplicate ``row_id`` rows, which
    would double-count in a plain ``GROUP BY`` / ``count()``. This collapses to
    **one row per ``row_id``** by taking ``argMax(col, updated_at)`` for each
    requested column — the dedup approach chosen once and applied everywhere
    (per ``docs/verification-findings.md`` recommendation), in preference to
    ``FINAL`` on the 113M-row distributed table (expensive) and never touching
    ``trace_payload``.

    Parameters
    ----------
    columns:
        Iterable of column names to project (e.g. ``["schedule_success",
        "min_multiplier_to_succeed", "gas_delta"]``). ``row_id`` is added
        automatically as the GROUP BY key; do **not** pass ``trace_payload``.
    where:
        The full inner ``WHERE`` body (chain_id / config / schedule / block
        range filters) — keeps the dedup scan cheap and PK-valid.
    alias:
        Outer alias for the subquery relation (default ``d``).

    Returns a string like::

        (SELECT row_id,
                argMax(schedule_success, updated_at) AS schedule_success,
                argMax(gas_delta, updated_at)        AS gas_delta
         FROM (SELECT row_id, updated_at, schedule_success, gas_delta
               FROM gas_analysis.gas_analysis_divergence
               WHERE <where>)
         GROUP BY row_id) AS d

    The WHERE filter lives in an **inner** projection (raw columns only) so the
    ``argMax(col) AS col`` aliases in the dedup layer cannot shadow the columns
    referenced by ``<where>`` (e.g. ``block_number``) — ClickHouse otherwise
    rejects ``argMax(block_number, ...) AS block_number`` used in WHERE with
    ``ILLEGAL_AGGREGATION``.
    """
    cols = list(dict.fromkeys(columns))  # de-dupe + preserve order
    if "trace_payload" in cols:
        raise ValueError("never select trace_payload from _divergence")
    cols = [c for c in cols if c not in ("row_id", "updated_at")]
    inner_cols = ", ".join(["row_id", "updated_at", *cols])
    proj = ",\n               ".join(f"argMax({c}, updated_at) AS {c}" for c in cols)
    sep = ",\n               " if proj else ""
    return (
        f"(SELECT row_id{sep}{proj}\n"
        f"        FROM (SELECT {inner_cols}\n"
        f"              FROM {DIVERGENCE_TABLE}\n"
        f"              WHERE {where})\n"
        f"        GROUP BY row_id) AS {alias}"
    )


def divergence_group_counts_sql() -> str:
    """SQL ``SELECT``-list fragment counting the drill-in groups per group-by.

    Intended to be embedded in a ``GROUP BY block_number`` (or per-recipient)
    aggregate over ``gas_analysis_divergence`` (already filtered to chain_id /
    config / schedule / block range). Returns columns ``g2_drillin``, ``g3``,
    ``g4``, ``af`` (Already failing), ``rescues``, and the dedup guard
    ``dup_rows``.
    """
    return (
        f"countIf({G2_DRILLIN_PREDICATE})            AS g2_drillin,\n"
        f"        countIf({G3_PREDICATE})            AS g3,\n"
        f"        countIf({G4_PREDICATE})            AS g4,\n"
        f"        countIf({AF_PREDICATE})            AS af,\n"
        f"        countIf({RESCUE_PREDICATE})        AS rescues,\n"
        f"        ({DEDUP_GUARD_SQL})                AS dup_rows"
    )


# --- Pure-Python predicate (mirror of the SQL; used by tests + any local pass) -


def classify_drill_in(schedule_success: bool, min_mult, baseline_success: bool) -> str:
    """Classify one retained ``_divergence`` drill-in row into a group key.

    Mirrors the SQL predicates above exactly. ``min_mult`` is
    ``min_multiplier_to_succeed`` (``float`` or ``None``).

    - **"af"** (Already failing): baseline replay failed
      (``baseline_success`` falsy), regardless of the schedule outcome.
    - **"g2"** (Succeeds with changes): working baseline **and**
      ``schedule_success`` (succeeds at the original limit).
    - **"g3"** (Fixable): working baseline, not success **and** ``min_mult >
      1`` (completes at the ``10x`` ceiling; measured rescue multiplier <= 10).
    - **"g4"** (Potentially broken): working baseline, not success **and**
      ``min_mult`` is None (did not complete even at the ``10x`` ceiling).

    Returns the group key (``"af"``, ``"g2"``, ``"g3"`` or ``"g4"``). Every
    retained drill-in is exactly one of these.
    """
    if not baseline_success:
        return "af"
    if schedule_success:
        return "g2"
    if min_mult is not None and min_mult > 1:
        return "g3"
    # working baseline, not success, and min_mult is None (not rescued anywhere)
    return "g4"


def block_group_counts(
    *,
    tx_count: int,
    tx_count_unchanged: int,
    tx_count_gas_only: int,
    g2_drillin: int,
    g3: int,
    g4: int,
    af: int,
) -> dict:
    """Assemble the per-block group counts from coverage aggregates + drill-ins.

    Mirrors the block-level definitions in the module docstring::

        g1 = tx_count_unchanged
        g2 = tx_count_gas_only + g2_drillin
        g3 = g3
        g4 = g4
        af = af                                    # Already failing (baseline fail)
        g5 = tx_count - (g1 + g2 + g3 + g4 + af)   # >= 0; 0 on non-truncated blocks

    ``g2_drillin`` / ``g3`` / ``g4`` / ``af`` are the retained-drill-in counts
    produced by :func:`classify_drill_in` (or the equivalent SQL). Returns a dict
    with keys ``g1``..``g5`` plus ``af`` and ``tx_count``.
    """
    g1 = tx_count_unchanged
    g2 = tx_count_gas_only + g2_drillin
    g5 = tx_count - (g1 + g2 + g3 + g4 + af)
    return {
        "tx_count": tx_count,
        "g1": g1,
        "g2": g2,
        "g3": g3,
        "g4": g4,
        "af": af,
        "g5": g5,
    }
