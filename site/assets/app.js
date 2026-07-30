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
 *          gas_bins: [ { lo:int, hi:int|null, count_gas_only:int,     // real gas units;
 *                        count_drillin:int, count:int }, ... ],       // hi excl, null=catch-all
 *          sum_gas_delta:int, min_gas_delta:int, max_gas_delta:int,   // BOTH cohorts
 *          pct_cohort:"gas_only",                                     // pct_* cover gas_only ONLY
 *          pct_bins: [ { lo:int, hi:int|null, count:int }, ... ],     // 13 bins, hi excl, null=≥500%
 *          pct_covered_count:int, pct_note:str,
 *          sum_gas_delta_gas_only:int, baseline_gas_used_sum:int,
 *          gas_delta_pct_of_baseline:float|null },                    // ratio of sums, signed %
 *        "3"|"4": {   // signed exact per-tx log2 magnitude bins; pct_cohort:"drillin"
 *          label:str, signed:true, note:str, count:int,
 *          bins: [ { bin_log2:int, sign:-1|1, count:int }, ... ],
 *          percentiles: { p01,p10,p25,p50,p75,p90,p99 : int },
 *          sum_gas_delta:int, min_gas_delta:int, max_gas_delta:int,
 *          pct_cohort:"drillin",                                  // per-tx drill-in route
 *          pct_bins: [ { lo:int, hi:int|null, count:int }, ... ],  // % of baseline gas;
 *          pct_covered_count:int, pct_note:str } } }              // hi excl, null=≥500% catch-all
 *
 * 4) group_categories.json
 *    { schedule:str, flavour:"opcode"|"state",
 *      g2: { label, count, gas_only_count:int, drillin_count:int,
 *            state_driver_mix:[{key,count}],       // PARTITION of gas_only_count: no_state|runtime_state
 *            tx_shape_mix:[{key,count}],           // PARTITION of gas_only_count: simple_transfer|contract_call|contract_creation (no authorization member — see tx_overlay_mix)
 *            tx_type_mix:[{key,count}],            // PARTITION of gas_only_count, EIP-2718: legacy|access_list|dynamic_fee|blob|set_code|unknown (zero-filled)
 *            gas_only_mix_note:str,                // the three mixes above cover the gas_only cohort ONLY
 *            tx_overlay_mix:[{key,count}], tx_overlay_note:str,  // OVERLAY, not a partition: authorization
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
 *      distinct_oog_contracts:int,               // distinct halt-site contracts (oog_contract)
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
 *      distinct_nonoog_recipients:int,            // distinct entry contracts with >=1 non-OOG revert
 *      distinct_nonoog_contracts:int,             // distinct revert-site contracts (divergence_contract)
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
// slate · cyan · amber · rose · violet · emerald.
const MODERN_DARK = {
  groups: ['#64748b', '#22d3ee', '#fbbf24', '#fb7185', '#a78bfa', '#34d399'],
  pie: ['#64748b', '#22d3ee', '#fbbf24', '#fb7185', '#a78bfa', '#34d399'],
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
async function fetchText(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.text();
}

// ── Minimal Markdown → HTML (entity-report.html) ─────────────────
// Dependency-free renderer for the committed report markdown. Supports the
// subset the report uses: ATX headings, GFM pipe tables, ordered/unordered
// lists (with wrapped continuation lines), horizontal rules, paragraphs, and
// inline **bold** / `code` / [text](url). Full 0x… addresses (bare text OR a
// code span) are auto-linked into the Affected-contracts search, preserving the
// current schedule — so the report is a live index into the per-contract pages.
function _mdAddrLink(addr) {
  return `affected-contracts.html?schedule=${currentSchedule()}&addr=${addr.toLowerCase()}`;
}
function _mdInline(text) {
  let s = escHtml(text);
  // Extract code spans first into private-use-area sentinels (\uE000<idx>\uE001)
  // so their contents are not re-processed and no real digit can collide with a
  // placeholder; a full address inside a code span becomes a clickable <code>.
  const code = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => {
    const isAddr = /^0x[0-9a-fA-F]{40}$/.test(c);
    const html = isAddr
      ? `<a class="addr" href="${_mdAddrLink(c)}"><code>${c}</code></a>`
      : `<code>${c}</code>`;
    code.push(html);
    return `\uE000${code.length - 1}\uE001`;
  });
  s = s.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');   // bold (non-greedy; tolerates inner *)
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');                    // italic
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, href) => {
    const ext = /^https?:/i.test(href);
    return `<a href="${href}"${ext ? ' target="_blank" rel="noopener noreferrer"' : ''}>${t}</a>`;
  });
  // Auto-link bare full addresses (code spans are already placeholdered out).
  s = s.replace(/\b(0x[0-9a-fA-F]{40})\b/g, (_, a) => `<a class="addr" href="${_mdAddrLink(a)}">${a}</a>`);
  return s.replace(/\uE000(\d+)\uE001/g, (_, i) => code[+i]);
}
function _mdTable(rows) {
  const cells = r => r.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);        // rows[1] is the --- separator
  const th = head.map(h => `<th>${_mdInline(h)}</th>`).join('');
  const trs = body.map(r => '<tr>' + r.map(c => `<td>${_mdInline(c)}</td>`).join('') + '</tr>').join('');
  return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
}
function renderMarkdown(md) {
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  const isItem = l => /^\s*([-*])\s+/.test(l) || /^\s*\d+\.\s+/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { i++; continue; }
    let m;
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      const n = m[1].length; out.push(`<h${n}>${_mdInline(m[2])}</h${n}>`); i++; continue;
    }
    if (/^-{3,}$/.test(line.trim())) { out.push('<hr>'); i++; continue; }
    if (line.trim().startsWith('|')) {
      const tbl = []; while (i < lines.length && lines[i].trim().startsWith('|')) tbl.push(lines[i++]);
      if (tbl.length >= 2) out.push(_mdTable(tbl));
      continue;
    }
    if (isItem(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*\|/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])) {
        const l = lines[i];
        if (isItem(l)) items.push(l.replace(/^\s*(?:[-*]|\d+\.)\s+/, ''));
        else if (items.length) items[items.length - 1] += ' ' + l.trim();   // wrapped continuation
        else break;
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>` + items.map(t => `<li>${_mdInline(t)}</li>`).join('') + `</${tag}>`);
      continue;
    }
    // paragraph: gather wrapped lines until a blank/structural line
    const para = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !isItem(lines[i])
           && !/^\s*\|/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i]) && !/^-{3,}$/.test(lines[i].trim())) {
      para.push(lines[i].trim()); i++;
    }
    if (para.length) out.push(`<p>${_mdInline(para.join(' '))}</p>`);
  }
  return out.join('\n');
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
// Two counts in one poster card: each value with a small dimmed qualifier
// (e.g. "3,182 entry · 1,274 halt site"). Returns "—" only when both are null.
function twoStat(a, b, labelA, labelB) {
  if (a == null && b == null) return '—';
  const part = (v, lbl) =>
    `${v == null ? '—' : fmtCount(v)}<span style="font-size:0.5em; font-weight:400; color:var(--text-muted); margin-left:3px">${escHtml(lbl)}</span>`;
  return `${part(a, labelA)}<span style="color:var(--text-dim); margin:0 6px">·</span>${part(b, labelB)}`;
}
function shortAddr(a) { return a ? a.slice(0, 8) + '…' + a.slice(-4) : ''; }
// Display name for a contract row: its human label, or a shortened address when
// the row has no real label (upstream falls back to the bare 0x… address).
function contractName(r) {
  const bareAddr = r.label && /^0x[0-9a-fA-F]{40}$/.test(r.label);
  return (r.label && !bareAddr) ? r.label : shortAddr(r.addr);
}

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

// Per-tx gas-diff distribution for a changed group. This is the G3/G4 auto-router:
// it prefers the percentage histogram (share of baseline gas) when the precompute
// has emitted pct_bins, else falls back to the absolute signed log2 histogram.
//
// Do NOT route group "2" through here. G2 does have a percent view as of producer
// schema v11 (block_summary.gas_delta_pct_hist), but it covers only the gas_only
// aggregate cohort, whereas G2's absolute gas_bins cover gas_only + drill-in
// members. The two cover DIFFERENT cohorts, so neither subsumes the other and
// overview.html deliberately renders them as two separate panels — renderPctHist
// for the percent view and renderG2GasHist for the cross-cohort absolute view —
// each labelled with its own cohort. Auto-routing would silently swap one for the
// other and drop the drill-in members from the only chart that shows them.
function renderGroupGasHist(divId, group, color) {
  if (group && group.pct_bins) return renderPctHist(divId, group, color);
  return renderGasDeltaHist(divId, group, color);
}

// Label for a percent gas-diff bin ({lo, hi} — hi exclusive, null = catch-all).
function pctBinLabel(b) {
  if (b.hi == null) return '≥' + b.lo + '%';
  return b.lo + '–' + b.hi + '%';
}

// Percent gas-diff histogram: 100*gas_delta/baseline_gas_used over a fixed set of
// signed bin edges (originally proposed in docs/producer-data-recommendations.md,
// now shipped as the producer column block_summary.gas_delta_pct_hist). Negative
// bins (schedule cheaper) use the neg color, non-negative (costlier) the pos color
// — pass no `color` to keep that diverging scale.
//
// Serves BOTH routes, which share these edges and the {lo,hi,count} entry shape:
// G3/G4 pct_bins are computed per-tx from drill-in rows (pct_cohort "drillin");
// G2 pct_bins are the summed producer class-grain array over the gas_only cohort
// only (pct_cohort "gas_only"). The renderer is identical either way; the cohort
// difference is carried in the caller's note text, not here.
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

// ═════════════════════════════════════════════════════════════════════
//  affected-contracts.html — per-contract failure search (cluster-first)
//  See site/data/SCHEMA.md §5b and docs/affected-contracts-page.md.
//  SHARDED data layout:
//   · data/{schedule}/affected/index.json — small; loaded on page init. Holds
//     block_range, g4_total, affected_count, and a NAME-SEARCHABLE `contracts`
//     array (labeled contracts only, each with `address` + footprint counts).
//   · data/{schedule}/affected/{lowercase_addr}.json — one shard per affected
//     contract, FETCHED ON LOOKUP; its payload is exactly the record
//     renderContractDetail(rec) consumes (that render is unchanged).
//  A direct address fetches its shard straight away; a name search substring-
//  scans the index, then fetches the chosen entry's shard. A 404/failed shard
//  fetch is treated as "not affected" (banner), never an uncaught error.
// ═════════════════════════════════════════════════════════════════════

// Module-scoped handle to the loaded index, set once by the page init.
let _affected = null;
// Lazily-fetched, cached aggregate for the collapsed "fresh contract deployment
// OOG'd during construction" class (~102k long-tail accounts that no longer get
// an individual {addr}.json shard). undefined = not yet fetched, null = fetch
// failed / file absent, object = the deploy_oog.json payload.
let _deployOog = undefined;
function initAffectedContracts(index) {
  _affected = index || { contracts: [] };
  _deployOog = undefined;               // reset on a new index / schedule change
}

// Fetch the collapsed deploy-OOG aggregate once, on first miss. Potentially
// several MB, so it is never loaded on page init — only when a shard miss needs
// to consult it. Resolves to null on any failure so callers can fall through.
async function _fetchDeployOog() {
  if (_deployOog !== undefined) return _deployOog;
  try {
    _deployOog = await fetchJSON(dataUrl('affected/deploy_oog.json'));
  } catch (e) {
    _deployOog = null;
  }
  return _deployOog;
}

// Fetch a per-contract shard; resolve to null on any failure (404 / network /
// bad JSON) so callers can treat a miss as "not affected" without throwing.
async function _fetchAffectedShard(addr) {
  try {
    return await fetchJSON(dataUrl('affected/' + addr.toLowerCase() + '.json'));
  } catch (e) {
    return null;
  }
}
// Transient loading state in #detail while a shard is in flight.
function _showDetailLoading() {
  const d = document.getElementById('detail');
  if (d) d.innerHTML = '<div class="panel"><p class="loading">Loading…</p></div>';
}

// Etherscan links.
function _etherscanAddr(addr) {
  return `https://etherscan.io/address/${escHtml(addr)}`;
}
function _etherscanTx(hash) {
  return `https://etherscan.io/tx/${escHtml(hash)}`;
}

// A human label for the analysis window, reused in banners.
function _affectedWindowLabel() {
  const r = _affected && _affected.block_range;
  const sched = (_affected && _affected.schedule) || currentSchedule();
  if (!r) return sched;
  return `${sched}, blocks ${Number(r.start).toLocaleString()}–${Number(r.end).toLocaleString()}`;
}

// Persist the resolved/attempted lookup in the URL so views are shareable.
function _setAddrParam(addr) {
  const u = new URL(location.href);
  if (addr) u.searchParams.set('addr', addr);
  else u.searchParams.delete('addr');
  history.replaceState(null, '', u.pathname.split('/').pop() + u.search);
}

// A single row's identity fields used by contractName/upgradeTag reuse.
// The JSON contract stores the address under `address`; existing helpers
// (contractName, table addr cells) expect `addr`, so we normalize.
function _asRecord(rec) {
  if (!rec) return rec;
  if (rec.addr == null && rec.address != null) rec.addr = rec.address;
  return rec;
}

// Render a banner (miss / not-affected / bad input) into #banner and clear detail.
function _showBanner(html) {
  const b = document.getElementById('banner');
  if (b) b.innerHTML = `<div class="banner">${html}</div>`;
  const d = document.getElementById('detail');
  if (d) d.innerHTML = '';
}
function _clearBanner() {
  const b = document.getElementById('banner');
  if (b) b.innerHTML = '';
}

// The banner shown when an address has no shard (not affected / bad address).
function _notAffectedBanner(q) {
  _showBanner(`No failures were identified for <strong>${escHtml(q)}</strong> in the `
    + `analysis (${escHtml(_affectedWindowLabel())}). This contract does not appear in any `
    + `Potentially-broken (G4) transaction as an entry, out-of-gas halt, or non-OOG revert site.`);
  const d = document.getElementById('detail');
  if (d) d.innerHTML = '';
}

// Fetch + render a single contract's shard by address. Returns true on a hit.
async function _resolveAndRender(addr, missQuery) {
  _setAddrParam(addr);
  _showDetailLoading();
  const rec = await _fetchAffectedShard(addr);
  if (!rec) {
    // Shard miss: this address may be one of the collapsed fresh-deployment
    // OOG accounts (no individual shard). Consult the aggregate before giving up.
    const dj = await _fetchDeployOog();
    const acct = dj && dj.accounts ? dj.accounts[addr.toLowerCase()] : null;
    if (acct) { _clearBanner(); renderDeployOogDetail(addr.toLowerCase(), acct, dj); return true; }
    _notAffectedBanner(missQuery != null ? missQuery : addr);
    return false;
  }
  _clearBanner();
  renderContractDetail(_asRecord(rec));
  return true;
}

// Lookup entry point wired to the search box (and ?addr= deep-link). Normalizes
// input; exact 0x…40hex → fetch that shard directly; otherwise a case-insensitive
// substring search over the index's label/owner_project. Async: shards are fetched.
async function affectedLookup(raw) {
  const entries = (_affected && Array.isArray(_affected.contracts)) ? _affected.contracts : [];
  const q = (raw == null ? '' : String(raw)).trim();
  const detail = document.getElementById('detail');
  if (!q) {
    _clearBanner();
    if (detail) detail.innerHTML = '';
    _setAddrParam(null);
    return;
  }

  // Exact address → fetch the shard; 404/failure → not-affected banner.
  if (/^0x[0-9a-fA-F]{40}$/.test(q)) {
    await _resolveAndRender(q.toLowerCase(), q);
    return;
  }

  // Name / owner-project substring search over the index.
  const needle = q.toLowerCase();
  const matches = entries.filter(r => {
    const hay = [(r.label || ''), (r.owner_project || '')].join(' ').toLowerCase();
    return hay.includes(needle);
  });
  _setAddrParam(q);

  if (matches.length === 0) {
    _showBanner(`No affected contract matches <strong>${escHtml(q)}</strong> in the analysis `
      + `(${escHtml(_affectedWindowLabel())}). Try a full <code>0x…</code> address, or a different name.`);
    if (detail) detail.innerHTML = '';
    return;
  }
  if (matches.length === 1) {
    const addr = (matches[0].address || matches[0].addr || '').toLowerCase();
    await _resolveAndRender(addr, contractName(_asRecord(matches[0])));
    return;
  }
  // Several matches → clickable pick-list (each pick fetches its shard).
  _renderPickList(matches, q);
  if (detail) detail.innerHTML = '';
}

// A small clickable disambiguation list for a multi-match name search, built
// from index entries (label + footprint counts). Each pick fetches its shard.
function _renderPickList(matches, q) {
  const footprint = (r) => {
    const bits = [];
    if (r.entry_g4_tx_count) bits.push(`${fmtCount(r.entry_g4_tx_count)} entry`);
    if (r.halt_count) bits.push(`${fmtCount(r.halt_count)} halts`);
    if (r.revert_count) bits.push(`${fmtCount(r.revert_count)} reverts`);
    return bits.length ? ` · ${escHtml(bits.join(', '))}` : '';
  };
  const rows = matches.slice(0, 20).map(r => {
    const rec = _asRecord(r);
    const cat = rec.category ? ` · ${escHtml(humanizeKey(rec.category))}` : '';
    const proj = rec.owner_project ? ` · ${escHtml(rec.owner_project)}` : '';
    return `<li style="margin:4px 0"><a href="#" class="addr" data-pick="${escHtml(rec.addr)}">`
      + `${escHtml(contractName(rec))}</a> <span class="note">${escHtml(shortAddr(rec.addr))}${cat}${proj}${footprint(r)}</span></li>`;
  }).join('');
  const more = matches.length > 20 ? `<p class="note">Showing 20 of ${matches.length} matches — refine your search.</p>` : '';
  _showBanner(`<strong>${matches.length}</strong> contracts match `
    + `<strong>${escHtml(q)}</strong>. Pick one:<ul style="margin:8px 0 0; padding-left:18px">${rows}</ul>${more}`);
  // Wire the pick links.
  document.querySelectorAll('#banner [data-pick]').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const addr = a.getAttribute('data-pick');
      const box = document.getElementById('addrSearch');
      if (box) box.value = addr;
      affectedLookup(addr);
    });
  });
}

// Facet chip for a cluster/context role.
const _ROLE_LABELS = { entry: 'Entry', oog_site: 'Halt site', revert_site: 'Revert site' };
function _roleChip(role) {
  const lbl = _ROLE_LABELS[role] || humanizeKey(role);
  const cls = role === 'entry' ? 'tag-upgradable' : 'tag-immutable';
  return `<span class="tag ${cls}">${escHtml(lbl)}</span>`;
}

// Role cell for a (possibly merged) cluster: renders one chip per role. A cluster
// merged by _mergeSelfRoleClusters carries a `roles` array (e.g. entry + revert
// site); a plain cluster falls back to its single `role`.
function _rolesCell(cl) {
  const roles = (cl.roles && cl.roles.length) ? cl.roles : (cl.role ? [cl.role] : []);
  return roles.map(_roleChip).join(' ') || '—';
}

// Collapse the entry/halt-or-revert double-count for self-referential failures.
// When a contract is BOTH the entry (`recipient`) and the halt/revert site
// (`where_contract` == itself) of the same failure mode, precompute emits two
// rows keyed by role — one `entry`, one `oog_site`/`revert_site` — over the same
// self-halting transactions. They share every field except role (the site row is
// the superset, since it also counts txs entered via other contracts). We show a
// single row that keeps the site row's stats and tags BOTH roles.
function _mergeSelfRoleClusters(clusters) {
  const keyOf = (c) => JSON.stringify([
    c.kind, c.selector, c.where_contract, c.opcode,
    c.pattern_or_reason, c.oog_bottleneck_kind, c.call_depth, c.revert_decoded,
  ]);
  const groups = new Map();
  const order = [];
  for (const c of clusters) {
    const k = keyOf(c);
    if (!groups.has(k)) { groups.set(k, []); order.push(k); }
    groups.get(k).push(c);
  }
  const out = [];
  for (const k of order) {
    const g = groups.get(k);
    const site = g.find(c => c.role === 'oog_site' || c.role === 'revert_site');
    const entry = g.find(c => c.role === 'entry');
    if (g.length > 1 && entry && site) {
      // Superset site row carries the correct distinct-tx count and drivers.
      out.push({ ...site, roles: ['entry', site.role] });
    } else {
      for (const c of g) out.push({ ...c, roles: [c.role] });
    }
  }
  out.sort((a, b) => (b.count || 0) - (a.count || 0));
  return out;
}

// Compact "why" drivers cell for a cluster's failure row. Only renders the
// driver keys that are present (each key is optional per the JSON contract).
function _driversCell(drivers) {
  if (!drivers) return '—';
  const parts = [];
  const pctile = (o) => (o && o.p50 != null) ? o.p50 : null;
  const sstore = pctile(drivers.sstore);
  const sload = pctile(drivers.sload);
  const cold = pctile(drivers.cold_account);
  if (sstore != null) parts.push(`SSTORE ×${fmtCount(sstore)}`);
  if (sload != null) parts.push(`SLOAD ×${fmtCount(sload)}`);
  if (cold != null) parts.push(`cold ×${fmtCount(cold)}`);
  if (drivers.surcharge_at_oog && drivers.surcharge_at_oog.p50 != null)
    parts.push(`+${fmtGas(drivers.surcharge_at_oog.p50)} surcharge`);
  if (drivers.gas_remaining_at_oog && drivers.gas_remaining_at_oog.p50 != null)
    parts.push(`${fmtGas(drivers.gas_remaining_at_oog.p50)} short`);
  if (drivers.reservoir_exhausted_share != null && drivers.reservoir_exhausted_share > 0)
    parts.push(`reservoir exhausted ${fmtPct(100 * drivers.reservoir_exhausted_share)}`);
  else if (drivers.spillover_share != null && drivers.spillover_share > 0)
    parts.push(`spillover ${fmtPct(100 * drivers.spillover_share)}`);
  const scat = drivers.state_gas_category;
  if ((!parts.length) && Array.isArray(scat) && scat.length && scat[0].key != null)
    parts.push(humanizeKey(scat[0].key));
  return parts.length ? escHtml(parts.join(' / ')) : '—';
}

// "where" cell: the counterpart contract + its label (contract role site).
function _whereCell(cl) {
  if (!cl.where_contract) return '—';
  const rec = { addr: cl.where_contract, label: cl.where_label };
  const name = contractName(rec);
  const cat = cl.where_category ? ` <span class="note">${escHtml(humanizeKey(cl.where_category))}</span>` : '';
  return `<a class="addr" href="${_etherscanAddr(cl.where_contract)}" target="_blank" `
    + `rel="noopener noreferrer" title="${escHtml(cl.where_contract)}">${escHtml(name)}</a>${cat}`;
}

// "halt/revert detail" cell: opcode / pattern-or-reason / decoded revert.
function _detailCell(cl) {
  const bits = [];
  if (cl.opcode) bits.push(escHtml(cl.opcode));                       // opcode mnemonic, no humanize
  if (cl.pattern_or_reason) bits.push(escHtml(humanizeKey(cl.pattern_or_reason)));
  if (cl.oog_bottleneck_kind) bits.push(escHtml(humanizeKey(cl.oog_bottleneck_kind)));
  if (cl.revert_decoded) bits.push(`<code>${escHtml(cl.revert_decoded)}</code>`);   // decoded error, no humanize
  if (cl.call_depth != null) bits.push(`<span class="note">depth ${escHtml(cl.call_depth)}</span>`);
  return bits.length ? bits.join('<br>') : '—';
}

// Representative tx hash link(s) for a cluster.
function _examplesCell(examples) {
  const ex = examples || [];
  if (!ex.length) return '—';
  return ex.slice(0, 2).map(e =>
    `<a class="addr" href="${_etherscanTx(e.tx_hash)}" target="_blank" rel="noopener noreferrer" `
    + `title="${escHtml(e.tx_hash)}${e.block_number != null ? ' · block ' + e.block_number : ''}">`
    + `${escHtml(shortAddr(e.tx_hash))}</a>`
  ).join('<br>');
}

// The function cell: decoded signature, falling back to the raw selector.
function _functionCell(cl) {
  const sig = cl.selector_signature || cl.selector;
  if (!sig) return '—';
  const isRaw = !cl.selector_signature;
  return `<code${isRaw ? '' : ` title="${escHtml(cl.selector || '')}"`}>${escHtml(sig)}</code>`;
}

// Full per-contract detail render — cluster-first.
function renderContractDetail(rec) {
  rec = _asRecord(rec);
  const el = document.getElementById('detail');
  if (!el) return;
  const T = theme();

  // ── Identity / supportive header ────────────────────────────────
  const pills = [];
  if (rec.category) pills.push(`<span class="tag tag-immutable">${escHtml(humanizeKey(rec.category))}</span>`);
  if (rec.owner_project) pills.push(`<span class="tag tag-immutable">${escHtml(rec.owner_project)}</span>`);
  if (rec.source) pills.push(`<span class="tag tag-immutable">${escHtml(humanizeKey(rec.source))}${rec.confidence ? ' · ' + escHtml(humanizeKey(rec.confidence)) : ''}</span>`);
  if (rec.is_proxy) pills.push(`<span class="tag tag-immutable">Proxy</span>`);
  if (rec.is_factory) pills.push(`<span class="tag tag-immutable">Factory</span>`);
  if (rec.is_safe) pills.push(`<span class="tag tag-immutable">Safe</span>`);
  if (rec.erc_type) pills.push(`<span class="tag tag-immutable">${escHtml(humanizeKey(rec.erc_type))}</span>`);
  if (rec.is_mev_bot) pills.push(`<span class="tag tag-immutable">MEV${rec.mev_role ? ' · ' + escHtml(humanizeKey(rec.mev_role)) : ''}</span>`);
  const upg = upgradeTag(rec);
  if (upg && upg !== '—') pills.push(upg);

  const header = `
    <div class="panel">
      <h3>${escHtml(contractName(rec))}
        <a class="addr" href="${_etherscanAddr(rec.addr)}" target="_blank" rel="noopener noreferrer"
          title="${escHtml(rec.addr)}" style="font-size:0.6em; font-weight:400; margin-left:8px">${escHtml(shortAddr(rec.addr))} ↗</a>
      </h3>
      <div style="margin:8px 0 0">${pills.join(' ') || '<span class="note">No label metadata.</span>'}</div>
    </div>`;

  // ── Headline role cards ─────────────────────────────────────────
  const rs = rec.roles_summary || {};
  const cards = [];
  if (rs.entry) {
    if (rs.entry.g4_oog_count != null)
      cards.push({ value: fmtCount(rs.entry.g4_oog_count), label: 'OOG halt txs as entry site', color: T.accent });
    if (rs.entry.g4_nonoog_count != null)
      cards.push({ value: fmtCount(rs.entry.g4_nonoog_count), label: 'non-OOG revert txs as entry site', color: T.accent });
  }
  if (rs.oog_site)
    cards.push({ value: fmtCount(rs.oog_site.halt_count || 0), label: 'txs as OOG halt site', color: T.accent });
  if (rs.revert_site)
    cards.push({ value: fmtCount(rs.revert_site.revert_count || 0), label: 'txs as non-OOG revert site', color: T.accent });
  const fr = rec.context && rec.context.failure_rate;
  if (fr) {
    if (fr.halt_rate != null) cards.push({ value: fmtPct(100 * fr.halt_rate), label: 'OOG halt rate (all mainnet txs)', color: T.accent });
    else if (fr.revert_rate != null) cards.push({ value: fmtPct(100 * fr.revert_rate), label: 'Revert rate (all mainnet txs)', color: T.accent });
  }

  // ── The spine: failure-mode clusters table ──────────────────────
  // Merge the self-referential entry/site double-count into single rows.
  const clusters = _mergeSelfRoleClusters(rec.failure_clusters || []);
  const distinct = rec.distinct_cluster_count != null ? rec.distinct_cluster_count : clusters.length;
  const shownShare = rec.clusters_shown_share != null ? ` covering ${fmtPct(100 * rec.clusters_shown_share)} of this contract's G4 txs` : '';
  const caption = `Top ${clusters.length} of ${fmtCount(distinct)} failure modes${shownShare}.`;

  // ── Assemble the DOM shell, then render Plotly bits into it ──────
  el.innerHTML = `
    ${header}
    <div class="poster-grid" id="acCards"></div>
    <div class="panel">
      <h3>Failure modes</h3>
      <p class="note">${escHtml(caption)} Ranked by transaction count. Each row is a distinct
        failure mode: a failing function paired with a halt/revert signature, tagged with the
        role it plays for this contract, and annotated with the repriced state line items
        (the "why") behind it.</p>
      <div id="acClusters" class="chart"><p class="loading">No failure clusters.</p></div>
    </div>
    <div class="panel">
      <h3 style="cursor:pointer" id="acCtxToggle">Context ▸</h3>
      <div id="acContext" style="display:none"></div>
    </div>`;

  renderCards('acCards', cards);

  // Cluster table (spine).
  if (clusters.length) {
    renderTable('acClusters', 'acClustersTable', clusters, [
      { title: '#', num: true, get: (r, i) => i + 1, sortVal: (r, i) => i + 1 },
      { title: 'Function', get: r => _functionCell(r) },
      { title: 'Role', get: r => _rolesCell(r), sortVal: r => (r.roles || [r.role]).join(',') },
      { title: 'Kind', get: r => r.kind === 'oog' ? 'OOG' : (r.kind === 'non_oog' ? 'Revert' : humanizeKey(r.kind)), sortVal: r => r.kind || '' },
      { title: 'Where', get: r => _whereCell(r) },
      { title: 'Halt / revert detail', get: r => _detailCell(r) },
      { title: 'Why', get: r => _driversCell(r.drivers) },
      { title: 'Count', num: true, get: r => fmtCount(r.count), sortVal: r => r.count },
      { title: 'Share', num: true,
        get: r => r.share_of_contract != null ? fmtPct(100 * r.share_of_contract) : '—',
        sortVal: r => r.share_of_contract || 0 },
      { title: 'Example tx', get: r => _examplesCell(r.examples) },
    ]);
  }

  // Context strip (collapsed by default; expandable).
  _renderAffectedContext(rec);
  const toggle = document.getElementById('acCtxToggle');
  const ctx = document.getElementById('acContext');
  if (toggle && ctx) {
    toggle.addEventListener('click', () => {
      const open = ctx.style.display !== 'none';
      ctx.style.display = open ? 'none' : 'block';
      toggle.textContent = open ? 'Context ▸' : 'Context ▾';
      if (!open && window.rerenderAffectedContext) window.rerenderAffectedContext();
    });
  }
}

// Class detail for a collapsed fresh-deployment OOG account. Distinct from
// renderContractDetail: there is no per-contract cluster spine here — the
// account shares one aggregate summary (dj.aggregate) with ~dj.count peers.
// `acct` is the single account's slot from dj.accounts (all fields guarded).
function renderDeployOogDetail(addr, acct, dj) {
  const el = document.getElementById('detail');
  if (!el) return;
  const T = theme();
  acct = acct || {};
  const agg = (dj && dj.aggregate) || {};

  // ── Identity header + prominent class badge ─────────────────────
  const header = `
    <div class="panel">
      <h3><a class="addr" href="${_etherscanAddr(addr)}" target="_blank" rel="noopener noreferrer"
          title="${escHtml(addr)}">${escHtml(shortAddr(addr))} ↗</a></h3>
      <div style="margin:8px 0 0">
        <span class="tag tag-immutable">Fresh contract deployment — out-of-gas during creation</span>
      </div>
    </div>`;

  // ── Explanatory paragraph (prefer the producer-supplied explainer) ─
  const builtin = 'Under this repricing, this account\'s own deployment '
    + '(constructor execution / code-deposit) exceeds the transaction gas limit, '
    + 'so it halts out-of-gas while it is being created — it never finishes '
    + 'deploying. Most such accounts are ERC-4337 smart-account wallets deployed '
    + 'on first use.';
  const explainer = (dj && dj.explainer) ? dj.explainer : builtin;
  const countNote = (dj && dj.count != null)
    ? ` This is one of <strong>${fmtCount(dj.count)}</strong> such accounts in the analysis window (${escHtml(_affectedWindowLabel())}).`
    : '';

  // ── This account's specifics (guarded fields) ───────────────────
  const rows = [];
  if (acct.opcode) rows.push(['Halt opcode', escHtml(acct.opcode)]);
  if (acct.selector) rows.push(['Init-code selector', `<code>${escHtml(acct.selector)}</code>`]);
  if (acct.gas_delta != null) rows.push(['Gas Δ', escHtml(fmtGas(acct.gas_delta))]);
  if (acct.block != null) rows.push(['Block', Number(acct.block).toLocaleString()]);
  if (acct.entry) rows.push(['Entry contract',
    `<a class="addr" href="${_etherscanAddr(acct.entry)}" target="_blank" rel="noopener noreferrer" `
    + `title="${escHtml(acct.entry)}">${escHtml(shortAddr(acct.entry))} ↗</a>`]);
  if (acct.tx) rows.push(['Example tx',
    `<a class="addr" href="${_etherscanTx(acct.tx)}" target="_blank" rel="noopener noreferrer" `
    + `title="${escHtml(acct.tx)}">${escHtml(shortAddr(acct.tx))} ↗</a>`]);
  const acctTable = rows.length
    ? `<table><tbody>${rows.map(r =>
        `<tr><th style="text-align:left">${r[0]}</th><td>${r[1]}</td></tr>`).join('')}</tbody></table>`
    : '<p class="note">No per-account detail recorded.</p>';

  // ── Class-wide gas-delta summary ────────────────────────────────
  const gd = agg.gas_delta || {};
  const gdRows = [
    ['Median (p50)', gd.p50], ['p90', gd.p90], ['Min', gd.min], ['Max', gd.max],
  ].filter(r => r[1] != null);
  const gdTable = gdRows.length
    ? `<table><tbody>${gdRows.map(r =>
        `<tr><th style="text-align:left">${r[0]}</th><td>${escHtml(fmtGas(r[1]))}</td></tr>`).join('')}</tbody></table>`
    : '';

  // Guarded lists for the class-wide bar charts.
  const haltSplit = Array.isArray(agg.halt_opcode_split) ? agg.halt_opcode_split : [];
  const topEntries = Array.isArray(agg.top_entry_contracts) ? agg.top_entry_contracts : [];
  const driversLine = _driversCell(agg.drivers);

  el.innerHTML = `
    ${header}
    <div class="panel">
      <p>${escHtml(explainer)}${countNote}</p>
    </div>
    <div class="panel">
      <h3>This account</h3>
      ${acctTable}
    </div>
    <div class="panel">
      <h3>Across this class</h3>
      <p class="note">Aggregate over all collapsed fresh-deployment OOG accounts in this
        schedule. Individual accounts are not shown separately.</p>
      <div class="grid-2">
        <div>
          <h4>Halt opcode</h4>
          <div id="doOpcode" class="chart"><p class="loading">No data.</p></div>
        </div>
        <div>
          <h4>Top entry contracts</h4>
          <div id="doEntries" class="chart"><p class="loading">No data.</p></div>
        </div>
      </div>
      ${gdTable ? `<h4>Gas Δ (class-wide)</h4>${gdTable}` : ''}
      <p class="note" style="margin-top:12px"><strong>Why:</strong> ${driversLine}</p>
    </div>`;

  // Class-wide bar charts (guarded — only draw when rows are present).
  if (haltSplit.length) {
    renderHBar('doOpcode', haltSplit, 'key', 'count', { humanize: false, left: 120 });
  }
  if (topEntries.length) {
    // Prefer a human label over the bare address for the y-axis (fall back to a
    // shortened address); keep the count for the bar length.
    const entryRows = topEntries.map(e => ({
      name: contractName({ addr: e.contract, label: e.label }),
      count: e.count,
    }));
    renderHBar('doEntries', entryRows, 'name', 'count', {
      humanize: false, left: 150,
      hovertemplate: '%{y}: %{x:,}<extra></extra>',
    });
  }
}

// The collapsed context strip: gas-delta cards, function breakdowns,
// counterpart-contract mixes, and broader-context counts. Charts are drawn
// lazily on first expand (Plotly needs a visible container to size correctly).
function _renderAffectedContext(rec) {
  const ctx = rec.context || {};
  const el = document.getElementById('acContext');
  if (!el) return;
  const T = theme();

  // Gas-delta cards.
  const gd = ctx.gas_delta || {};
  const gasCards = [];
  if (gd.avg != null) gasCards.push({ value: fmtGas(gd.avg), label: 'Avg gas Δ' });
  if (gd.p50 != null) gasCards.push({ value: fmtGas(gd.p50), label: 'Median gas Δ' });
  if (gd.p90 != null) gasCards.push({ value: fmtGas(gd.p90), label: 'p90 gas Δ' });
  if (gd.sum != null) gasCards.push({ value: fmtGas(gd.sum), label: 'Total gas Δ' });

  // Broader-context mini-table.
  const miniRows = [
    ['Fixable with gas-limit bump (G3)', ctx.g3_tx_count],
    ['Succeeds with changes (G2 drill-in)', ctx.g2_drillin_tx_count],
    ['Already failing (AF)', ctx.af_tx_count],
    ['Status flips', ctx.status_flips],
    ['Distinct blocks with G4', ctx.distinct_blocks],
  ].filter(r => r[1] != null);
  const spanNote = (ctx.block_span_start != null && ctx.block_span_end != null)
    ? `<p class="note">Block span ${Number(ctx.block_span_start).toLocaleString()}–${Number(ctx.block_span_end).toLocaleString()}.</p>` : '';

  el.innerHTML = `
    ${gasCards.length ? '<div class="poster-grid" id="acGasCards"></div>' : ''}
    <div class="grid-2">
      <div class="panel">
        <h3>Which functions break — entry</h3>
        <p class="note">Top entry functions (<code>entry_selector</code>) of this contract in G4 txs.</p>
        <div id="acEntryFns" class="chart"><p class="loading">No data.</p></div>
      </div>
      <div class="panel">
        <h3>Which functions break — failing frame</h3>
        <p class="note">Top failing functions (the frame the halt/revert lands in).</p>
        <div id="acFailingFns" class="chart"><p class="loading">No data.</p></div>
      </div>
    </div>
    <div class="grid-2">
      <div class="panel">
        <h3>Counterpart contracts — OOG halt sites</h3>
        <p class="note">Where this contract's entry txs run out of gas.</p>
        <div id="acHaltContracts" class="chart"><p class="loading">No data.</p></div>
      </div>
      <div class="panel">
        <h3>Counterpart contracts — revert sites</h3>
        <p class="note">Where this contract's entry txs revert (non-OOG).</p>
        <div id="acRevertContracts" class="chart"><p class="loading">No data.</p></div>
      </div>
    </div>
    <div class="panel">
      <h3>Counterpart contracts — entry points (site roles)</h3>
      <p class="note">When this contract is a halt/revert site, the entry contracts that reach it.</p>
      <div id="acEntryContracts" class="chart"><p class="loading">No data.</p></div>
    </div>
    <div class="panel">
      <h3>Broader context</h3>
      ${spanNote}
      <div id="acMini"></div>
    </div>`;

  // Mini-table of broader-context counts.
  if (miniRows.length) {
    renderTable('acMini', 'acMiniTable', miniRows.map(r => ({ k: r[0], v: r[1] })), [
      { title: 'Metric', get: r => escHtml(r.k) },
      { title: 'Count', num: true, get: r => fmtCount(r.v), sortVal: r => r.v },
    ]);
  } else {
    document.getElementById('acMini').innerHTML = '<p class="note">No additional context.</p>';
  }

  // Function-breakdown HBars keyed on signature (fall back to selector). raw
  // 4-byte selector rides along in the hover tooltip; humanize disabled.
  const fnRows = (list) => (list || []).map(r => ({
    key: r.signature || r.selector || '—',
    count: r.count,
    _sel: r.selector || '',
  }));
  const drawFns = (divId, list, color) => {
    const rows = fnRows(list);
    if (!rows.length) return;
    renderHBar(divId, rows, 'key', 'count', {
      humanize: false, color, left: 220, xTitle: 'G4 txs',
      customdata: rows.map(r => r._sel),
      hovertemplate: '%{y}<br>selector %{customdata}<br>%{x:,} txs<extra></extra>',
    });
  };
  const cRows = (list) => (list || []).map(r => ({
    key: (r.label && !/^0x[0-9a-fA-F]{40}$/.test(r.label)) ? r.label : shortAddr(r.contract),
    count: r.count,
    _addr: r.contract || '',
    _cat: r.category || '',
  }));
  const drawContracts = (divId, list, color) => {
    const rows = cRows(list);
    if (!rows.length) return;
    renderHBar(divId, rows, 'key', 'count', {
      humanize: false, color, left: 200, xTitle: 'G4 txs',
      customdata: rows.map(r => r._addr),
      hovertemplate: '%{y}<br>%{customdata}<br>%{x:,} txs<extra></extra>',
    });
  };

  // Draw charts now if visible; otherwise defer to the toggle handler.
  const drawAll = () => {
    if (gasCards.length) renderCards('acGasCards', gasCards);
    drawFns('acEntryFns', ctx.entry_functions, T.pos);
    drawFns('acFailingFns', ctx.failing_functions, T.groups[2]);
    drawContracts('acHaltContracts', ctx.halt_contracts, T.neg);
    drawContracts('acRevertContracts', ctx.revert_contracts, T.groups[3]);
    drawContracts('acEntryContracts', ctx.entry_contracts, T.accent);
  };
  window.rerenderAffectedContext = drawAll;
}
