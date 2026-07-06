/* repricing-impact — static gas-schedule dashboard client.
 *
 * Plain vanilla JS. No build, no framework. Each page is a static HTML file
 * that reads ?schedule=<eip-8038|eip-8037> and fetches
 *   data/<schedule>/<file>.json
 * then renders with Plotly (vendored at assets/plotly.min.js).
 *
 * ===========================================================================
 * PUBLISHED JSON CONTRACT  (the precompute step MUST emit exactly this shape)
 * Authoritative copy lives in site/data/SCHEMA.md. Keep both in sync.
 * Per schedule, under site/data/<schedule>/ :
 *
 * 1) meta.json
 *    { schedule: str, schedules_available: str[], analysis_config_hash: str,
 *      chain_id: int, generated_at: ISO8601,
 *      block_range: { start:int, end:int, count:int },
 *      date_range: { start:"YYYY-MM-DD", end:"YYYY-MM-DD" },
 *      totals: { tx_count, g1, g2, g3, g4, af, g5, rescues : int },
 *      group_labels: { g1, g2, g3, g4, af, g5 : str },
 *      truncation: { drill_ins_truncated_blocks:int, total_blocks:int,
 *                    truncated_share:float, note:str },
 *      manifest: { source, schedule_name, config_selected_by : str },
 *      pinned_config_note: str }
 *
 * 2) overview_series.json
 *    { schedule:str, bucket_by:"day"|"block",
 *      buckets: [ { date:"YYYY-MM-DD", block_start:int, block_end:int,
 *                   tx_count:int, g1:int, g2:int, g3:int, g4:int, af:int, g5:int,
 *                   rescues:int, drill_ins_truncated_blocks:int } , ... ],
 *      totals: { tx_count, g1, g2, g3, g4, af, g5, rescues,
 *                drill_ins_truncated_blocks : int },
 *      group_labels: { g1, g2, g3, g4, af, g5 : str } }
 *
 * 3) gas_delta_hist.json
 *    { schedule:str,
 *      groups: {
 *        "2": {   // Succeeds with changes — txs with a gas change (gas_delta != 0)
 *          label:str, signed:false, note:str, count:int,
 *          gas_bins: [ { lo:int, hi:int|null, count_gas_only:int,          // real gas
 *                        count_drillin:int, count:int }, ... ],            // units; hi excl,
 *          sum_gas_delta:int, min_gas_delta:int, max_gas_delta:int },      // null=catch-all
 *        "3"|"4": {   // signed exact per-tx log2 magnitude bins
 *          label:str, signed:true, note:str, count:int,
 *          bins: [ { bin_log2:int, sign:-1|1, count:int }, ... ],
 *          percentiles: { p01,p10,p25,p50,p75,p90,p99 : int },
 *          sum_gas_delta:int, min_gas_delta:int, max_gas_delta:int,
 *          pct_bins: [ { lo:int, hi:int|null, count:int }, ... ],  // % of baseline gas;
 *          pct_covered_count:int, pct_note:str } } }              // hi excl, null=≥500% catch-all
 *
 * 4) group_categories.json
 *    { schedule:str, flavour:"opcode"|"state",
 *      g2: { label, count, gas_only_count:int, drillin_count:int,
 *            state_driver_mix:[{key,count}],                     // gas_only cohort (native driver counts)
 *            state_driver_mix_drillin:[{key,count}],             // drill-in subset (state_gas_category)
 *            change_type_mix:[{key,count}], change_type_note:str }, // OVERLAPPING counts
 *      g3: { label, count, multiplier_histogram:[{multiplier,count}], // multiplier: 2|4|6|8|10, binned over the real (1,10] sweep (top bin open-ended)
 *            state_gas_category:[{key,count}],
 *            tx_shape_mix:[{key,count}],   // simple_transfer|contract_call|contract_creation|authorization
 *            tx_type_mix:[{key,count}],    // EIP-2718: legacy|access_list|dynamic_fee|blob|set_code|unknown
 *            is_create:{true,false}, oog_pattern:[{key,count}], reservoir?:{...} },
 *      g4: { label, count, fixability:[{key,count}], // gas-fixability via replay_halt_oog: not_gas_fixable|still_oog_at_ceiling|unknown
 *            break_reason:[{key,count}], oog_bottleneck_kind:[{key,count}], // break_reason = original-limit halt site, NOT fixability
 *            status_flip:[{key,count}], state_gas_category:[{key,count}],
 *            tx_shape_mix:[{key,count}], tx_type_mix:[{key,count}],   // same taxonomies as g3
 *            reservoir?:{ reservoir_exhausted:{true,false}, avg_initial_reservoir:int,
 *                         avg_runtime_state_gas_spillover:int } } }
 *
 * 4b) oog_forensics.json  — OOG halt-site forensics for the Potentially-broken
 *    (g4) cohort (rows with an OOG signal). Powers the "Out-of-gas failures"
 *    section of transaction-failures.html.
 *    { schedule:str, g4_total:int, oog_total:int, oog_share_of_g4:float,
 *      distinct_oog_recipients:int,              // distinct entry contracts with >=1 OOG halt
 *      oog_pattern:[{key,count}],
 *      gas_remaining_hist:[{bucket:str,count:int}],  // ordered magnitude buckets, gas left at halt
 *      oog_opcode:[{key,count}],                 // key = decoded mnemonic (no humanizeKey)
 *      call_depth_hist:[{depth:str,count:int}],  // "1".."8","9+" (totals the cohort)
 *      call_depth_percentiles:{p50,p90,p99,max : int|null},
 *      oog_contract_leaderboard:[{addr:str,label:str,count:int}],  // top-12 halt contracts
 *      oog_recipient_leaderboard:[{  // top entry contracts (the tx `recipient`) by OOG halts
 *        addr:str, label:str,
 *        category?:str, owner_project?:str,        // optional metadata (absent when unknown)
 *        source?:str, confidence?:str,             // source present ⇒ confidence present
 *        is_mev_bot?:true, mev_role?:str,          // present only when applicable
 *        is_proxy?:true, is_factory?:true, is_safe?:true, erc_type?:str,
 *        is_upgradable?:true,                      // present only when upgradable (clones are NOT)
 *        upgrade_mechanism?:str, upgrade_admin?:str, // eip1967_transparent|uups|beacon|diamond|minimal_proxy_immutable
 *        halt_count:int,                           // always present — OOG halts to this recipient
 *        total_tx:int|null,                        // all mainnet txs to recipient (null=unavailable)
 *        halt_rate:float|null }],                  // halt_count/total_tx (null when total_tx null/0)
 *      oog_recipient_rate_leaderboard:[{...}],     // same row shape, ranked by halt_rate desc
 *                                                  // among recipients with total_tx>=1000
 *      sankey: { nodes:[{label:str, addr:str|null, side:"entry"|"halt"}],
 *                links:[{source:int,target:int,value:int}] } }  // bipartite entry->halt
 *
 * 4c) nonoog_forensics.json — non-OOG revert forensics for the Potentially-broken
 *    (g4) cohort (rows that flipped status without an OOG signal). Powers the
 *    "Non-OOG reverts" section of transaction-failures.html.
 *    { schedule:str, g4_total:int, nonoog_total:int, nonoog_share_of_g4:float,
 *      failure_reason:[{key,count}],              // raw failure_reason enum
 *      revert_error_mix:[{key,count}],            // Error(string) message, top 5 + "Others"
 *      divergence_opcode:[{key,count}],           // key = decoded mnemonic (no humanizeKey)
 *      call_depth_hist:[{depth:str,count:int}],   // "1".."8","9+" (totals the cohort)
 *      call_depth_percentiles:{p50,p90,p99,max : int|null},
 *      sankey: { nodes:[{label:str, addr:str|null, side:"entry"|"revert"}],
 *                links:[{source:int,target:int,value:int}] } }  // bipartite entry->revert
 *
 * 5) contract_failures.json
 *    { schedule:str, g4_total:int, g3_total:int, note:str,
 *      contracts: [ { recipient:str, label:str, g4_tx_count:int, g3_tx_count:int,
 *        g2_drillin_tx_count:int, status_flips:int, avg_gas_delta:int, sum_gas_delta:int,
 *        min_mult_percentiles:{p50,p90,p99 : int|null},
 *        block_span_start:int, block_span_end:int, distinct_blocks_with_g4:int,
 *        avg_g4_per_block:float, g4_vs_other_ratio:float, cumulative_share:float,
 *        divergence_contract_mix:[{contract,label,count}],
 *        oog_contract_mix:[{contract,label,count}] }, ... ] }
 *
 * 6) examples.json
 *    { schedule:str, capped_at:int,
 *      examples: [ { tx_hash:str, block_number:int, group:3|4, recipient:str,
 *        recipient_label:str, gas_delta:int, min_multiplier_to_succeed:int|null,
 *        baseline_success:bool, schedule_success:bool, oog_pattern:str|null,
 *        divergence_opcode:str, divergence_contract:str, state_gas_category:str } ] }
 * ===========================================================================
 */

// Strip the "Loading chart..." placeholder before each newPlot (Plotly leaves
// non-Plotly children of the target div in place).
if (window.Plotly && Plotly.newPlot) {
  const _origNewPlot = Plotly.newPlot;
  Plotly.newPlot = function (gd, ...rest) {
    const el = typeof gd === 'string' ? document.getElementById(gd) : gd;
    if (el) el.querySelectorAll('.loading').forEach(p => p.remove());
    return _origNewPlot.call(this, gd, ...rest);
  };
}

// ── Schedule routing ─────────────────────────────────────────────────
const VALID_SCHEDULES = ['eip-8038', 'eip-8037'];
const DEFAULT_SCHEDULE = 'eip-8038';

function currentSchedule() {
  const p = new URLSearchParams(location.search).get('schedule');
  return VALID_SCHEDULES.includes(p) ? p : DEFAULT_SCHEDULE;
}
function dataUrl(file) {
  return `data/${currentSchedule()}/${file}`;
}
// Build a same-page link to another schedule (preserves the page).
function scheduleLink(schedule) {
  const u = new URL(location.href);
  u.searchParams.set('schedule', schedule);
  return u.pathname.split('/').pop() + u.search;
}
// Cross-page link keeping the current schedule.
function pageLink(page) {
  return `${page}?schedule=${currentSchedule()}`;
}

// ── Theme palette ────────────────────────────────────────────────────
// Modern Dark — group colors in GROUP_KEYS order (No change · Succeeds with
// changes · Fixable · Potentially broken · Already failing · Unknown):
// slate · cyan · amber · rose · violet · gray.
const MODERN_DARK = {
  groups: ['#64748b', '#22d3ee', '#fbbf24', '#fb7185', '#a78bfa', '#94a3b8'],
  pie: ['#64748b', '#22d3ee', '#fbbf24', '#fb7185', '#a78bfa', '#94a3b8'],
  pos: '#fb7185', neg: '#22d3ee', accent: '#818cf8',
  layout: {
    font: { family: 'Inter, system-ui, sans-serif', size: 13, color: '#b4bac6' },
    axis: { gridcolor: '#262b36', zerolinecolor: '#333a48', color: '#8b93a1' },
  },
};
function theme() { return MODERN_DARK; }
const GROUP_COLOR = i => theme().groups[i];   // i indexes GROUP_KEYS order

function baseLayout(overrides = {}) {
  const t = theme().layout;
  const merged = Object.assign({
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    margin: { l: 70, r: 16, t: 30, b: 60 },
    font: t.font,
    legend: { orientation: 'h', y: -0.2 },
  }, overrides);
  for (const k of ['xaxis', 'yaxis']) {
    if (merged[k]) {
      merged[k] = Object.assign({ gridcolor: t.axis.gridcolor, zerolinecolor: t.axis.zerolinecolor, color: t.axis.color, automargin: true }, merged[k]);
    }
  }
  return merged;
}

// ── Fetch + format helpers ───────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.json();
}
function fmtCount(n) {
  n = Number(n);
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}
function fmtGas(n) {
  n = Number(n);
  const s = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 1e9) return s + (a / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return s + (a / 1e6).toFixed(1) + 'M';
  if (a >= 1e3) return s + (a / 1e3).toFixed(1) + 'K';
  return Math.round(n).toString();
}
function fmtPct(n) {
  if (Math.abs(n) < 0.01) return n.toFixed(4) + '%';
  if (Math.abs(n) < 1) return n.toFixed(3) + '%';
  return n.toFixed(2) + '%';
}
function escHtml(s) { const d = document.createElement('div'); d.textContent = (s == null ? '' : s); return d.innerHTML; }
function shortAddr(a) { return a ? a.slice(0, 8) + '…' + a.slice(-4) : ''; }

// Humanize a raw DB enum string for display. The categorical breakdowns
// (state_gas_category, oog_pattern, oog_bottleneck_kind, status_flip, …) carry
// whatever enum values the warehouse emits — we never hardcode the set. Render
// snake_case as words and leave CamelCase tokens (FixedGas, FractionalGas,
// Stipend2300) intact, just spaced. Unknown values pass through readably.
const _ACRONYMS = { oog: 'OOG', aa: 'AA', erc: 'ERC', evm: 'EVM',
  dex: 'DEX', cex: 'CEX', nft: 'NFT', mev: 'MEV', defi: 'DeFi', oli: 'OLI' };
function humanizeKey(k) {
  if (k == null) return '—';
  let s = String(k);
  if (s.includes('_')) {
    s = s.replace(/_/g, ' ');                       // snake_case → words
  } else if (/[a-z][A-Z]/.test(s)) {
    s = s.replace(/([a-z])([A-Z])/g, '$1 $2');      // CamelCase → words (Stipend2300 intact)
  }
  // Title-case every word; uppercase known acronyms (so "non_oog_revert"
  // → "Non-OOG Revert"). Works for single tokens too ("none" → "None").
  s = s.split(' ').map(w =>
    _ACRONYMS[w.toLowerCase()] || (w ? w[0].toUpperCase() + w.slice(1) : w)
  ).join(' ');
  return s.replace(/\bNon OOG\b/, 'Non-OOG');
}
// Collapse a [{key,count}]-style list to the top-N by value plus an aggregated
// "Others" bucket (sum of the remainder). Keeps the full list untouched; returns
// a display copy. Used e.g. for the OOG opcode chart (top 5 + Others).
function topNWithOther(rows, n, opts = {}) {
  const valField = opts.valField || 'count';
  const keyField = opts.keyField || 'key';
  const otherLabel = opts.otherLabel || 'Others';
  const sorted = (rows || []).slice().sort((a, b) => b[valField] - a[valField]);
  if (sorted.length <= n) return sorted;
  const top = sorted.slice(0, n);
  const rest = sorted.slice(n).reduce((s, r) => s + (r[valField] || 0), 0);
  if (rest > 0) top.push({ [keyField]: otherLabel, [valField]: rest });
  return top;
}

// Render a label record's upgradability as a pill for table cells. Upgradability
// is narrower than proxy-ness: an EIP-1167 clone is a proxy but forwards to a
// bytecode-baked implementation that can never change, so it shows as an
// "Immutable clone" rather than upgradable. Returns an HTML string ('—' when
// unknown). `upgradeRank` gives a sortable ordering (upgradable > immutable >
// unknown). The admin address, when present, rides along as a tooltip.
const _UPGRADE_MECH_LABELS = {
  eip1967_transparent: 'Transparent',
  uups: 'UUPS',
  beacon: 'Beacon',
  diamond: 'Diamond',
  minimal_proxy_immutable: 'Immutable clone',
};
function upgradeTag(rec) {
  if (rec.is_upgradable) {
    const mech = _UPGRADE_MECH_LABELS[rec.upgrade_mechanism] || 'Yes';
    const tip = rec.upgrade_admin ? ` title="admin ${escHtml(rec.upgrade_admin)}"` : '';
    return `<span class="tag tag-upgradable"${tip}>${escHtml(mech)}</span>`;
  }
  if (rec.upgrade_mechanism === 'minimal_proxy_immutable') {
    return `<span class="tag tag-immutable">Immutable clone</span>`;
  }
  return '—';
}
function upgradeRank(rec) {
  if (rec.is_upgradable) return 2;
  if (rec.upgrade_mechanism === 'minimal_proxy_immutable') return 1;
  return 0;
}

function showError(id, err) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = `<p class="loading" style="color:var(--red)">load error: ${escHtml(err.message || err)}</p>`;
  console.error(err);
}

// ── Card rendering ───────────────────────────────────────────────────
function renderCards(containerId, cards) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = cards.map(c => `
    <div class="poster-card" ${c.color ? `style="border-top:2px solid ${c.color}"` : ''}>
      <div class="number" ${c.color ? `style="color:${c.color}"` : ''}>${c.value}</div>
      <div class="label">${escHtml(c.label)}</div>
    </div>`).join('');
}

// ── Generic Plotly renderers ─────────────────────────────────────────
// Display/legend order: the changed groups, then Already failing, then Unknown.
const GROUP_KEYS = ['g1', 'g2', 'g3', 'g4', 'af', 'g5'];

// Stacked composition over buckets. pct=true normalises each bucket to 100%.
// With a single bucket (e.g. a small validation window covering one day) a
// stacked area collapses to an invisible sliver, so we render a stacked bar
// instead; multi-bucket data keeps the filled-area shape.
function renderComposition(divId, series, pct) {
  const b = series.buckets;
  // x prefers date; fall back to the block range when block-bucketed / undated.
  const x = b.map(r => r.date || `${r.block_start}–${r.block_end}`);
  const labels = series.group_labels;
  const single = b.length <= 1;
  const traces = GROUP_KEYS.map((g, i) => {
    const y = pct
      ? b.map(r => r.tx_count ? (100 * r[g] / r.tx_count) : 0)
      : b.map(r => r[g]);
    const t = {
      x, y, name: labels[g],
      hovertemplate: pct ? '%{y:.3f}%<extra>' + labels[g] + '</extra>'
                         : '%{y:,}<extra>' + labels[g] + '</extra>',
    };
    if (single) {
      Object.assign(t, { type: 'bar', marker: { color: GROUP_COLOR(i) } });
    } else {
      Object.assign(t, {
        type: 'scatter', mode: 'lines', stackgroup: 'one',
        line: { width: 0.5, color: GROUP_COLOR(i) }, fillcolor: GROUP_COLOR(i),
      });
    }
    return t;
  });
  // Thin the category ticks so a long date range shows ~10 labels, laid out
  // horizontally instead of Plotly's auto-angled crowd.
  const maxTicks = 10;
  const dtick = single ? undefined : Math.max(1, Math.ceil(x.length / maxTicks));
  Plotly.newPlot(divId, traces, baseLayout({
    height: 460,
    barmode: single ? 'stack' : undefined,
    legend: { orientation: 'h', y: 1.08, yanchor: 'bottom' },
    xaxis: {
      title: single ? 'Block range' : 'Date', type: 'category',
      tickangle: 0, tickmode: dtick ? 'linear' : undefined, dtick,
    },
    yaxis: { title: pct ? 'Share of txs (%)' : 'Tx count', rangemode: 'tozero' },
  }), { responsive: true, displayModeBar: false });
}

function renderTotalsDonut(divId, totals, labels) {
  Plotly.newPlot(divId, [{
    type: 'pie', hole: 0.55,
    labels: GROUP_KEYS.map(g => labels[g]),
    values: GROUP_KEYS.map(g => totals[g]),
    marker: { colors: GROUP_KEYS.map((_, i) => GROUP_COLOR(i)) },
    textinfo: 'percent', sort: false,
    hovertemplate: '%{label}<br>%{value:,} (%{percent})<extra></extra>',
  }], baseLayout({ height: 420, margin: { l: 10, r: 10, t: 10, b: 10 } }),
  { responsive: true, displayModeBar: false });
}

// Signed log2 gas-delta histogram for one group. For signed groups, negative
// bins are mirrored to the left; magnitude-only groups are all positive.
function renderGasDeltaHist(divId, group, color) {
  const t = theme();
  // x axis: signed log2 magnitude (sign * bin_log2). bin 0 stays at 0.
  const rows = group.bins.map(bn => ({
    x: bn.sign * bn.bin_log2,
    label: `${bn.sign < 0 ? '-' : ''}2^${bn.bin_log2}`,
    count: bn.count,
    sign: bn.sign,
  })).sort((a, b) => a.x - b.x);
  Plotly.newPlot(divId, [{
    type: 'bar',
    x: rows.map(r => r.x),
    y: rows.map(r => r.count),
    marker: { color: color || rows.map(r => (r.sign < 0 ? t.neg : t.pos)) },
    customdata: rows.map(r => r.label),
    hovertemplate: 'gas Δ ≈ %{customdata}<br>%{y:,} txs<extra></extra>',
  }], baseLayout({
    height: 320,
    xaxis: { title: group.signed ? 'signed log2(|gas Δ|)' : 'log2(gas Δ magnitude)' },
    yaxis: { title: 'tx count' },
  }), { responsive: true, displayModeBar: false });
}

// Per-tx gas-diff distribution for a changed group (G3/G4). Prefers a percentage
// histogram (share of baseline gas) when the precompute has emitted pct_bins;
// otherwise falls back to the absolute signed log2 histogram. The G2 gas_only
// cohort cannot form a per-tx ratio, so it keeps renderG2GasHist (absolute).
function renderGroupGasHist(divId, group, color) {
  if (group && group.pct_bins) return renderPctHist(divId, group, color);
  return renderGasDeltaHist(divId, group, color);
}

// Label for a percent gas-diff bin ({lo, hi} — hi exclusive, null = catch-all).
function pctBinLabel(b) {
  if (b.hi == null) return '≥' + b.lo + '%';
  return b.lo + '–' + b.hi + '%';
}

// Percent gas-diff histogram: per-tx 100*gas_delta/baseline_gas_used over the
// fixed signed bin edges from docs/producer-data-recommendations.md. Negative
// bins (schedule cheaper) use the neg color, non-negative (costlier) the pos color.
function renderPctHist(divId, group, color) {
  const t = theme();
  const bins = group.pct_bins || [];
  const x = bins.map(pctBinLabel);
  Plotly.newPlot(divId, [{
    type: 'bar',
    x, y: bins.map(b => b.count),
    marker: { color: color || bins.map(b => (b.lo < 0 ? t.neg : t.pos)) },
    hovertemplate: 'gas Δ %{x} of baseline<br>%{y:,} txs<extra></extra>',
  }], baseLayout({
    height: 320,
    xaxis: { title: 'gas Δ (% of baseline gas used)', type: 'category' },
    yaxis: { title: 'tx count' },
  }), { responsive: true, displayModeBar: false });
}

// Real gas-unit label for a G2 gas_bin ({lo, hi} — hi exclusive, null = catch-all).
function gasBinLabel(b) {
  if (b.hi == null) return '≥' + fmtGas(b.lo);
  if (b.hi - b.lo === 1) return String(b.lo);
  return fmtGas(b.lo) + '–' + fmtGas(b.hi - 1);
}

// G2 "gas change" distribution: real gas-unit buckets over the whole group.
// The gas_only cohort and drill-in members are merged into a single count per
// bin. group.gas_bins is [{lo, hi, count_gas_only, count_drillin, count}] over
// bins 1..11 (count = gas_only + drill-in).
function renderG2GasHist(divId, group) {
  const t = theme();
  const bins = group.gas_bins || [];
  const x = bins.map(gasBinLabel);
  Plotly.newPlot(divId, [{
    type: 'bar',
    x, y: bins.map(b => b.count),
    marker: { color: t.groups[1] },
    hovertemplate: 'gas Δ %{x}<br>%{y:,} txs<extra></extra>',
  }], baseLayout({
    height: 360,
    xaxis: { title: 'gas Δ (gas units)', type: 'category' },
    yaxis: { title: 'tx count' },
  }), { responsive: true, displayModeBar: false });
}

// Horizontal bar from [{key/label..., count}] rows.
// y labels are humanized by default (raw DB enums → readable text); pass
// opts.humanize=false for pre-formatted labels (e.g. "2×"). Opcode mnemonics
// (SLOAD) pass through humanizeKey unchanged.
// opts.customdata: a per-row array aligned to `rows` in their ORIGINAL order.
// It is reordered together with the internal value-sort so it stays aligned
// with each bar. Pair with opts.hovertemplate (referencing %{customdata}) for
// custom hovers; both default to the plain "%{y}: %{x:,}" behaviour when unset.
function renderHBar(divId, rows, keyField, valField, opts = {}) {
  // Sort {row, cd} pairs together so customdata tracks the same order as data.
  const pairs = rows.map((r, i) => ({
    row: r,
    cd: opts.customdata ? opts.customdata[i] : undefined,
  })).sort((a, b) => a.row[valField] - b.row[valField]);
  const data = pairs.map(p => p.row);
  const hum = opts.humanize === false ? (v => v) : humanizeKey;
  Plotly.newPlot(divId, [{
    type: 'bar', orientation: 'h',
    y: data.map(r => hum(r[keyField])),
    x: data.map(r => r[valField]),
    customdata: opts.customdata ? pairs.map(p => p.cd) : undefined,
    marker: { color: opts.color || theme().accent },
    hovertemplate: opts.hovertemplate || '%{y}: %{x:,}<extra></extra>',
  }], baseLayout({
    height: opts.height || Math.max(180, 36 * data.length + 60),
    margin: { l: opts.left || 150, r: 16, t: 20, b: 40 },
    xaxis: { title: opts.xTitle || 'count' },
    yaxis: {},
  }), { responsive: true, displayModeBar: false });
}

// Grouped horizontal bar over a union of category keys, for comparing several
// named series (e.g. two cohorts). seriesList = [{ name, rows:[{key,count}],
// color }]. opts.pct normalises each series to its own total (so subsets of very
// different sizes stay comparable); raw counts show in the hover.
function renderHBarGrouped(divId, seriesList, opts = {}) {
  const keys = [];
  seriesList.forEach(s => (s.rows || []).forEach(r => {
    if (!keys.includes(r.key)) keys.push(r.key);
  }));
  const y = keys.map(humanizeKey);
  const traces = seriesList.map(s => {
    const m = Object.fromEntries((s.rows || []).map(r => [r.key, r.count]));
    const counts = keys.map(k => m[k] || 0);
    const total = counts.reduce((a, c) => a + c, 0) || 1;
    return {
      type: 'bar', orientation: 'h', name: s.name,
      y, x: opts.pct ? counts.map(c => 100 * c / total) : counts,
      customdata: counts,
      marker: { color: s.color },
      hovertemplate: '%{y} · ' + s.name +
        (opts.pct ? ': %{x:.2f}% (%{customdata:,})' : ': %{x:,}') + '<extra></extra>',
    };
  });
  Plotly.newPlot(divId, traces, baseLayout({
    height: opts.height || Math.max(220, 26 * keys.length * seriesList.length + 90),
    barmode: 'group',
    margin: { l: opts.left || 160, r: 16, t: 20, b: 40 },
    legend: { orientation: 'h', y: 1.08, yanchor: 'bottom' },
    xaxis: { title: opts.xTitle || (opts.pct ? 'share of subset (%)' : 'tx count') },
    yaxis: {},
  }), { responsive: true, displayModeBar: false });
}

// Cumulative-share concentration (Pareto) over ranked contracts.
function renderConcentration(divId, contracts) {
  const x = contracts.map((c, i) => i + 1);
  Plotly.newPlot(divId, [{
    type: 'scatter', mode: 'lines+markers',
    x, y: contracts.map(c => 100 * c.cumulative_share),
    line: { color: theme().accent, width: 2 }, marker: { size: 5 },
    text: contracts.map(c => c.label),
    hovertemplate: '#%{x} %{text}<br>cumulative %{y:.1f}%<extra></extra>',
  }], baseLayout({
    height: 320,
    xaxis: { title: 'contract rank (by potentially-broken tx count)' },
    yaxis: { title: 'cumulative share of potentially broken (%)', range: [0, 100] },
  }), { responsive: true, displayModeBar: false });
}

// Vertical bar over ordered ordinal buckets (e.g. call-depth histogram). Unlike
// renderHBar, x order is preserved as given (categorical), not value-sorted.
function renderVBar(divId, rows, keyField, valField, opts = {}) {
  Plotly.newPlot(divId, [{
    type: 'bar',
    x: rows.map(r => r[keyField]),
    y: rows.map(r => r[valField]),
    marker: { color: opts.color || theme().accent },
    hovertemplate: (opts.xTitle || 'bin') + ' %{x}: %{y:,}<extra></extra>',
  }], baseLayout({
    height: opts.height || 260,
    margin: { l: 64, r: 16, t: 20, b: 48 },
    xaxis: { title: opts.xTitle || '', type: 'category' },
    yaxis: { title: opts.yTitle || 'OOG txs' },
  }), { responsive: true, displayModeBar: false });
}

// Two-layer entry→target Sankey. sankey = { nodes:[{label,side}], links:[{source,
// target,value}] }. Entry (left) nodes are colored with the accent, the right-side
// nodes (halt/revert) with the "broken" rose so the left→right split reads at a
// glance. opts.unit labels the flow value in hovers (default "OOG txs").
function renderSankey(divId, sankey, opts = {}) {
  const t = theme();
  const unit = opts.unit || 'OOG txs';
  const nodes = sankey && sankey.nodes || [];
  const links = sankey && sankey.links || [];
  if (!nodes.length || !links.length) {
    document.getElementById(divId).innerHTML = '<p class="loading">No flow data.</p>';
    return;
  }
  const sides = [...new Set(nodes.map(n => n.side))];
  const perSide = sides.map(s => nodes.filter(n => n.side === s).length);
  Plotly.newPlot(divId, [{
    type: 'sankey', orientation: 'h', arrangement: 'snap',
    node: {
      label: nodes.map(n => n.label),
      color: nodes.map(n => n.side === 'entry' ? t.accent : t.pos),
      pad: 12, thickness: 14, line: { width: 0 },
      hovertemplate: '%{label}<br>%{value:,} ' + unit + '<extra></extra>',
    },
    link: {
      source: links.map(l => l.source),
      target: links.map(l => l.target),
      value: links.map(l => l.value),
      color: 'rgba(129,140,248,0.22)',
      hovertemplate: '%{source.label} → %{target.label}<br>%{value:,} ' + unit + '<extra></extra>',
    },
  }], baseLayout({
    height: opts.height || Math.max(360, 24 * Math.max(...perSide) + 60),
    margin: { l: 8, r: 8, t: 20, b: 20 },
    font: Object.assign({}, t.layout.font, { size: 12 }),
  }), { responsive: true, displayModeBar: false });
}

// ── Sortable HTML table ──────────────────────────────────────────────
// Render rows (in the given order) into a click-to-sort table. cols entries:
//   { title, get:(row,i)=>cellHTML, num?:bool, sortVal?:(row,i)=>rawValue }
// `num` right-aligns the column (.num); `sortVal` supplies a raw value in
// data-sort so sortTable orders by the underlying number, not the formatted
// text (e.g. "1.2K"). Falls back to a "No data." note on an empty list.
function renderTable(containerId, tableId, rows, cols) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!rows || !rows.length) { el.innerHTML = '<p class="loading">No data.</p>'; return; }
  const head = cols.map((c, i) =>
    `<th class="${c.num ? 'num' : ''}" onclick="sortTable('${tableId}', ${i})">${escHtml(c.title)}</th>`
  ).join('');
  const body = rows.map((r, i) => '<tr>' + cols.map(c => {
    const sv = c.sortVal ? c.sortVal(r, i) : null;
    return `<td class="${c.num ? 'num' : ''}"${sv != null ? ` data-sort="${escHtml(sv)}"` : ''}>${c.get(r, i)}</td>`;
  }).join('') + '</tr>').join('');
  el.innerHTML = `<table id="${tableId}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ── Table sorting (click headers) ────────────────────────────────────
const _sortDir = {};
function sortTable(tableId, col) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const key = tableId + col;
  const dir = _sortDir[key] = !(_sortDir[key] || false);
  rows.sort((a, b) => {
    const av = a.cells[col].getAttribute('data-sort') ?? a.cells[col].textContent.trim();
    const bv = b.cells[col].getAttribute('data-sort') ?? b.cells[col].textContent.trim();
    const an = parseFloat(String(av).replace(/[^0-9.\-]/g, ''));
    const bn = parseFloat(String(bv).replace(/[^0-9.\-]/g, ''));
    if (!isNaN(an) && !isNaN(bn)) return dir ? bn - an : an - bn;
    return dir ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv));
  });
  rows.forEach(r => tbody.appendChild(r));
}

// ── Shared nav / schedule-picker wiring ──────────────────────────────
// Pages call buildChrome() after DOM ready: fills nav links + schedule picker
// + footer, all preserving the active schedule.
function buildChrome(active) {
  const sched = currentSchedule();
  // nav links
  document.querySelectorAll('[data-nav]').forEach(a => {
    const page = a.getAttribute('data-nav');
    a.href = page === 'index' ? `index.html?schedule=${sched}` : pageLink(page + '.html');
    if (page === active) a.classList.add('active');
  });
  // schedule picker buttons
  document.querySelectorAll('[data-schedule]').forEach(a => {
    const s = a.getAttribute('data-schedule');
    a.href = scheduleLink(s);
    if (s === sched) a.classList.add('active');
  });
}
