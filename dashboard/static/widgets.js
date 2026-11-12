'use strict';
/* Board widgets drawn with RouterCharts: Usage chart, Stat tile, Allowance gauge, Reset timeline, Breakdown (+ Top models),
   Activity heatmap, Live pulse, Health, Retry recovery and Notes. Each registers with RouterBoards (boards.js) and renders
   into the host it's given; content is host-scoped (no ids), because one type can appear twice on a board.
   Data: /api/series, /api/breakdown, /api/heatmap, /api/retry (docs/boards-api.md) through ctx.api, and today's
   /api/overview (ctx.overview) for allowances and health. */
(function () {
const RC = window.RouterCharts;
const {esc, fmt} = RC;
const isNum = v => typeof v === 'number' && Number.isFinite(v);
const nf = new Intl.NumberFormat();
const compact = new Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1});

/* ---------- vocabulary ---------- */
const METRICS = {
  requests: {name: 'Requests', unit: 'count', noun: 'requests', pick: true},
  failures: {name: 'Failures', unit: 'count', noun: 'failed requests', pick: true, upBad: true},
  success_rate: {name: 'Success rate', unit: 'percent', rate: true, noun: 'requests', upGood: true},
  tokens: {name: 'Total tokens', unit: 'tokens', noun: 'tokens'},
  input_tokens: {name: 'Input tokens', unit: 'tokens', noun: 'input tokens'},
  output_tokens: {name: 'Output tokens', unit: 'tokens', noun: 'output tokens'},
  cached_tokens: {name: 'Cached-input tokens', unit: 'tokens', noun: 'cached input'},
  cache_hit_rate: {name: 'Cache hit rate', unit: 'percent', rate: true, noun: 'requests', upGood: true},
  avg_duration: {name: 'Average duration', unit: 'ms', rate: true, noun: 'requests', upBad: true},
};
const isRate = m => !!METRICS[m]?.rate;
// "View as table" shows exact values: counts and tokens in full, rates as on the chart.
const exact = (v, unit) => isNum(v) && (unit === 'count' || unit === 'tokens') ? nf.format(Math.round(v)) : fmt(v, unit);
const STAT_TITLES = {requests: 'Requests', failures: 'Failures', success_rate: 'Success rate', tokens: 'Tokens', input_tokens: 'Input tokens',
  output_tokens: 'Output tokens', cached_tokens: 'Cached-input tokens', cache_hit_rate: 'Cached input', avg_duration: 'Average duration', retry_recoveries: 'Retry recoveries'};
const GROUPS = {none: 'None', provider: 'Provider', account: 'Account', model: 'Model', outcome: 'Outcome'};
// An Auto-height chart keeps its natural plot: a legacy options.height (short/medium/tall, no longer offered in settings) or
// medium. A fixed layout height sizes the plot to the box instead and ignores options.height (charts.css, RC.fills).
const HEIGHTS = {short: 120, medium: 190, tall: 290}, PREVIEW_HEIGHT = 92;
const metricChoices = keys => keys.map(value => ({value, label: METRICS[value]?.name || STAT_TITLES[value]}));
const SERIES_METRICS = Object.keys(METRICS);
const COUNT_METRICS = ['requests', 'failures', 'tokens', 'input_tokens', 'output_tokens', 'cached_tokens'];

// Chart-type icons for the settings tiles (same drawings as the approved design).
const I = (inner, box = '0 0 26 20') => `<svg viewBox="${box}" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
const ICONS = {
  stacked: I('<path d="M2 18.5h22"/><rect x="4" y="8" width="4.5" height="10" rx="1"/><path d="M4 12.5h4.5"/><rect x="10.75" y="3.5" width="4.5" height="14.5" rx="1"/><path d="M10.75 10h4.5"/><rect x="17.5" y="10" width="4.5" height="8" rx="1"/><path d="M17.5 14h4.5"/>'),
  bars: I('<path d="M2 18.5h22"/><rect x="4" y="8" width="4.5" height="10" rx="1"/><rect x="10.75" y="3.5" width="4.5" height="14.5" rx="1"/><rect x="17.5" y="10" width="4.5" height="8" rx="1"/>'),
  grouped: I('<path d="M2 18.5h22"/><rect x="3.5" y="9" width="3" height="9" rx=".8"/><rect x="7.5" y="5" width="3" height="13" rx=".8"/><rect x="14.5" y="7" width="3" height="11" rx=".8"/><rect x="18.5" y="11" width="3" height="7" rx=".8"/>'),
  line: I('<path d="M2 18.5h22"/><path d="M3 14l5-5 4 3 5-7 6 4"/>'),
  area: I('<path d="M2 18.5h22"/><path d="M3 14l5-5 4 3 5-7 6 4V18H3z" fill="currentColor" fill-opacity=".2"/>'),
  sarea: I('<path d="M2 18.5h22"/><path d="M3 15l5-3 4 2 5-4 6 2V18H3z" fill="currentColor" fill-opacity=".4" stroke="none"/><path d="M3 11l5-4 4 2 5-5 6 3V18H3z" fill="currentColor" fill-opacity=".14"/>'),
  donut: I('<circle cx="13" cy="10" r="7.5"/><circle cx="13" cy="10" r="3.5"/><path d="M13 2.5V6.5M20 13l-3.6-1.4M7.5 15.5l2.7-2.6"/>'),
  ranked: I('<path d="M4 4.5h17M4 10h12M4 15.5h7" stroke-width="3"/>'),
  treemap: I('<rect x="3" y="2.5" width="20" height="15" rx="1.5"/><path d="M14 2.5v15M14 10.5h9M8.5 10.5v7M3 10.5h11"/>'),
  weekhour: I('<g stroke="none" fill="currentColor"><rect x="3" y="3" width="4" height="4" rx="1" opacity=".3"/><rect x="8" y="3" width="4" height="4" rx="1"/><rect x="13" y="3" width="4" height="4" rx="1" opacity=".6"/><rect x="18" y="3" width="4" height="4" rx="1" opacity=".3"/><rect x="3" y="8" width="4" height="4" rx="1" opacity=".6"/><rect x="8" y="8" width="4" height="4" rx="1" opacity=".3"/><rect x="13" y="8" width="4" height="4" rx="1"/><rect x="18" y="8" width="4" height="4" rx="1" opacity=".6"/><rect x="3" y="13" width="4" height="4" rx="1"/><rect x="8" y="13" width="4" height="4" rx="1" opacity=".6"/><rect x="13" y="13" width="4" height="4" rx="1" opacity=".3"/><rect x="18" y="13" width="4" height="4" rx="1"/></g>'),
  calendar: I('<rect x="3" y="3" width="20" height="15" rx="2"/><path d="M3 8h20M8 8v10M13 8v10M18 8v10M3 13h20"/>'),
  ring: I('<circle cx="13" cy="10" r="7" opacity=".45"/><path d="M13 3a7 7 0 0 1 5.5 11.3" stroke-width="2.6"/>'),
  bar: I('<rect x="3" y="8" width="20" height="4" rx="2" opacity=".45"/><path d="M5 10h10" stroke-width="4"/>'),
};
const svgUse = (id, cls = '') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
const ARROW_UP = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
const ARROW_DOWN = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14m-6-6 6 6 6-6"/></svg>';

/* ---------- time and ranges ---------- */
const pad2 = n => String(n).padStart(2, '0');
// ISO 8601 with the browser's offset (the API's bucket alignment uses tz; the instant is what matters here).
function iso(d) {
  const z = -d.getTimezoneOffset(), a = Math.abs(z);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}${z >= 0 ? '+' : '-'}${pad2(Math.floor(a / 60))}:${pad2(a % 60)}`;
}
const floorMinute = d => { const x = new Date(d); x.setSeconds(0, 0); return x; };
const RANGE_PHRASE = {today: 'today', '24h': 'in the last 24 hours', '7d': 'in the last 7 days', week: 'in the last 7 days', '30d': 'in the last 30 days',
  month: 'in the last 30 days', '90d': 'in the last 90 days', all: 'recorded', '60m': 'in the last hour'};
const RANGE_VS = {today: 'vs yesterday', '24h': 'vs the 24 hours before', '7d': 'vs prior 7 days', week: 'vs prior 7 days', '30d': 'vs prior 30 days',
  month: 'vs prior 30 days', '90d': 'vs prior 90 days', all: 'vs prior period'};
const rangeKey = ctx => ctx.range?.key || 'today';
const phrase = ctx => RANGE_PHRASE[rangeKey(ctx)] || (ctx.range?.label ? 'in ' + ctx.range.label.toLowerCase() : 'in this range');
// /api/series and /api/heatmap take tz; /api/breakdown and /api/retry reject it (unknown parameters are a 400).
function rangeParams(ctx, extra = {}, tz = true) {
  const r = ctx.range || {}, p = {since: iso(floorMinute(r.since || new Date(new Date().setHours(0, 0, 0, 0))))};
  if (r.until && Date.now() - r.until.getTime() > 120000) p.until = iso(floorMinute(r.until));
  if (tz && ctx.tz) p.tz = ctx.tz;
  return {...p, ...extra};
}
const shortDate = d => d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
const hourName = d => d.toLocaleTimeString(undefined, {hour: 'numeric'});
function dayWord(d, now = new Date()) {
  const a = new Date(d); a.setHours(0, 0, 0, 0);
  const b = new Date(now); b.setHours(0, 0, 0, 0);
  const diff = Math.round((b - a) / 86400000);
  return diff === 0 ? 'Today' : diff === 1 ? 'Yesterday' : d.toLocaleDateString(undefined, {weekday: 'short', month: 'short', day: 'numeric'});
}
function nextBucket(d, bucket) {
  const x = new Date(d);
  if (bucket === 'minute') x.setMinutes(x.getMinutes() + 1);
  else if (bucket === 'hour') x.setHours(x.getHours() + 1);
  else if (bucket === 'week') x.setDate(x.getDate() + 7);
  else x.setDate(x.getDate() + 1);
  return x;
}
function xTicks(n) {
  if (n <= 1) return [0];
  if (n === 24) return [0, 7, 15, 23];
  if (n <= 7) return [...Array(n).keys()];
  return [...new Set([0, Math.floor((n - 1) / 3), Math.floor(2 * (n - 1) / 3), n - 1])];
}
function ago(value, now = Date.now()) {
  const t = typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
}
function duration(seconds) {
  seconds = Math.max(0, Math.ceil(seconds));
  const hours = Math.floor(seconds / 3600), days = Math.floor(hours / 24);
  return days ? `${days}d ${hours % 24}h` : hours ? `${hours}h ${Math.ceil((seconds % 3600) / 60)}m` : `${Math.max(1, Math.ceil(seconds / 60))}m`;
}

/* ---------- identities and colours ---------- */
const MARKS = {codex: 'openai', claude: 'claude', xai: 'xai', gemini: 'gemini', meta: 'meta', antigravity: 'antigravity', kimi: 'kimi', devin: 'devin', compat: 'compat'};
const providers = ctx => Array.isArray(ctx?.overview?.providers) ? ctx.overview.providers : [];
const accentOf = (ctx, id) => { try { return ctx?.accentOf?.(id) || 'other'; } catch (_) { return 'other'; } };
const familyColor = accent => `var(--${accent === 'other' || !accent ? 'blue' : accent})`;
function providerName(ctx, id) {
  const p = providers(ctx).find(p => p.id === id);
  return p?.name || (id ? id.charAt(0).toUpperCase() + id.slice(1) : 'Unknown');
}
function providerIcon(ctx, id) {
  const accent = accentOf(ctx, id), mark = MARKS[accent];
  return `<span class="provider-icon ${esc(accent)}-mark">${mark ? svgUse(mark) : `<span class="monogram" aria-hidden="true">${esc(providerName(ctx, id).charAt(0).toUpperCase())}</span>`}</span>`;
}
const local = name => typeof name === 'string' && /^[^@\s]+@[^@\s]+$/.test(name) ? name.slice(0, name.indexOf('@')) : name;
function accountRow(provider, a) { return {provider, auth_index: a.auth_index || a.id || null, email: a.email || null, account_key: a.key || null}; }
function nameOf(ctx, row) { try { return ctx?.accountName?.(row) || row.email || 'Removed account'; } catch (_) { return row.email || 'Removed account'; } }
function colorOf(ctx, row) { try { return ctx?.accountColor?.(row) || familyColor(accentOf(ctx, row.provider)); } catch (_) { return familyColor(accentOf(ctx, row.provider)); } }
// Accounts in the order the gateway lists them; removed accounts' history goes last.
function accountRank(ctx) {
  const m = new Map();
  let r = 0;
  for (const p of providers(ctx)) for (const a of p.accounts || []) {
    const rank = r++;
    if (a.key) m.set('key|' + a.key, rank);
    for (const id of [a.auth_index, a.id]) if (id) m.set(p.id + '|' + id, rank);
    if (a.email) m.set(p.id + '|@' + a.email, rank);
  }
  return s => m.get('key|' + s.account_key) ?? m.get(s.provider + '|' + s.auth_index) ?? m.get(s.provider + '|@' + s.email) ?? 1e6;
}
function findAccount(ctx, provider, id) {
  for (const p of providers(ctx)) if (!provider || p.id === provider) for (const a of p.accounts || []) if (a.auth_index === id || a.id === id || a.key === id) return {p, a};
  return null;
}
// Models take their family's shades in first-seen order, four per family; the rest fold into "Other models". Failure
// reasons take danger, amber, blue-3, xai-2, then faint. Both are assigned once per session so a colour follows its entity.
const MODEL_SHADES = new Map(), FAIL_SHADES = [];
const SHADE = ['', '-2', '-3', '-4'];
function modelColor(accent, model) {
  const list = MODEL_SHADES.get(accent) || (MODEL_SHADES.set(accent, []).get(accent));
  let i = list.indexOf(model);
  if (i < 0 && list.length < 4) { list.push(model); i = list.length - 1; }
  return i < 0 ? null : `var(--${accent === 'other' ? 'blue' : accent}${SHADE[i]})`;
}
const FAIL_COLORS = ['var(--danger)', 'var(--amber)', 'var(--blue-3)', 'var(--xai-2)'];
function failColor(kind) {
  let i = FAIL_SHADES.indexOf(kind);
  if (i < 0) { FAIL_SHADES.push(kind); i = FAIL_SHADES.length - 1; }
  return FAIL_COLORS[i] || 'var(--faint)';
}
const shadeIndex = color => { const m = /-(\d)\)\s*$/.exec(color || ''); return m ? +m[1] - 1 : 0; };
const kindOf = key => String(key || '').replace(/^failure_reason:/, '');

// API series → chart keys with names, colours and legend groups, in a stable order (never by rank).
function seriesKeys(ctx, resp, o) {
  const group = resp.group || o.group, list = resp.series || [];
  if (group === 'none') return list.map(s => ({key: s.key, name: METRICS[o.metric]?.name || s.label || 'Total', color: 'var(--blue)', series: s}));
  if (group === 'outcome') return [['outcome:ok', 'Completed', 'var(--mint)'], ['outcome:failed', 'Failed', 'var(--danger)']]
    .filter(([k]) => list.some(s => s.key === k)).map(([key, name, color]) => ({key, name, color, series: list.find(s => s.key === key), failed: key === 'outcome:failed'}));
  const order = providers(ctx).map(p => p.id);
  const pRank = id => { const i = order.indexOf(id); return i < 0 ? 1e3 : i; };
  if (group === 'provider') return list.map(s => ({key: s.key, name: providerName(ctx, s.provider), color: familyColor(accentOf(ctx, s.provider)), series: s, provider: s.provider}))
    .sort((a, b) => pRank(a.provider) - pRank(b.provider));
  if (group === 'account') {
    const rank = accountRank(ctx);
    // Like today's legend, every connected account is listed, including ones with nothing in this range.
    const all = list.slice();
    if (!o.provider && !o.account) for (const p of providers(ctx)) for (const a of p.accounts || []) {
      const id = a.auth_index || a.id;
      if (!id || all.some(s => s.account_key && s.account_key === a.key || s.provider === p.id && s.auth_index === id)) continue;
      all.push({key: `account:${p.id}:${id}`, label: a.email || a.label, provider: p.id, auth_index: id, email: a.email || null, account_key: a.key || null, values: [], total: 0, idle: true});
    }
    const keys = all.map(s => {
      const row = {provider: s.provider, auth_index: s.auth_index, email: s.email, account_key: s.account_key};
      const name = nameOf(ctx, row), accent = accentOf(ctx, s.provider);
      return {key: s.key, name, color: colorOf(ctx, row), title: `${providerName(ctx, s.provider)} · ${name}`, series: s, provider: s.provider, account: s.auth_index,
        group: {id: s.provider, name: providerName(ctx, s.provider), accent}, rank: rank(s)};
    });
    // Light mode can't separate shades 3 and 4 next to each other, so a family of three or more stacks 1, 3, 2, 4.
    const fam = new Map();
    for (const k of keys) fam.set(k.provider, (fam.get(k.provider) || 0) + 1);
    const pos = [0, 2, 1, 3];
    keys.sort((a, b) => pRank(a.provider) - pRank(b.provider) || (fam.get(a.provider) >= 3 ? pos[shadeIndex(a.color)] - pos[shadeIndex(b.color)] : 0) || a.rank - b.rank);
    for (const g of new Set(keys.map(k => k.provider))) {
      const same = keys.filter(k => k.provider === g);
      for (const k of same) { k.short = same.filter(x => local(x.name) === local(k.name)).length === 1 ? local(k.name) : k.name; k.tip = `${k.group.name} · ${k.short}`; }
    }
    return keys;
  }
  if (group === 'model') {
    const out = [], other = {key: 'model:other', name: 'Other models', color: 'var(--faint)', fold: [], provider: null};
    for (const s of list) {
      // Rates and means can't fold (no denominators), so models past the palette keep their own faint line.
      const accent = accentOf(ctx, s.provider), color = modelColor(accent, s.provider + ':' + (s.label || s.key)) || (isRate(o.metric) ? 'var(--faint)' : null);
      if (color) out.push({key: s.key, name: s.label || String(s.key).split(':').slice(2).join(':'), color, series: s, provider: s.provider, shade: color === 'var(--faint)' ? 9 : shadeIndex(color)});
      else other.fold.push(s);
    }
    out.sort((a, b) => pRank(a.provider) - pRank(b.provider) || a.shade - b.shade);
    // Rates and means need denominators to combine; show only the named model lines.
    if (other.fold.length && !isRate(o.metric)) out.push(other);
    return out;
  }
  return list.map(s => ({key: s.key, name: s.label || s.key, color: 'var(--blue)', series: s}));
}
const valuesOf = (k, field = 'values') => k.fold ? null : k.series?.[field];
function sumFold(k, i, prevSeries) {
  let v = null;
  for (const s of k.fold) { const x = (prevSeries ? prevSeries.find(p => p.key === s.key)?.values : s.values)?.[i]; if (isNum(x)) v = (v || 0) + x; }
  return v;
}

/* ---------- series → chart model ---------- */
function chartModel(ctx, resp, o) {
  const bucket = resp.bucket || ctx.range?.bucket || 'hour';
  let starts = (resp.buckets || []).map(s => new Date(s));
  const keys = seriesKeys(ctx, resp, o);
  let rows = starts.map((_, i) => {
    const r = {};
    for (const k of keys) { const v = k.fold ? sumFold(k, i) : valuesOf(k)?.[i]; if (isNum(v)) r[k.key] = v; }
    return r;
  });
  let prev = null;
  if (resp.previous?.series) {
    const ps = resp.previous.series;
    prev = starts.map((_, i) => {
      const r = {};
      for (const k of keys) { const v = k.fold ? sumFold(k, i, ps) : ps.find(s => s.key === k.key)?.values?.[i]; if (isNum(v)) r[k.key] = v; }
      return r;
    });
  }
  let current = Number.isInteger(resp.current) ? resp.current : null;
  // A range longer than recorded history starts at the first recorded bucket ("since Sep 22").
  let since = null;
  const first = Date.parse(resp.first_recorded);
  if (Number.isFinite(first) && starts.length > 1 && bucket !== 'minute') {
    let cut = 0;
    while (cut < starts.length - 1 && nextBucket(starts[cut], bucket).getTime() <= first) cut++;
    if (cut > 0) {
      starts = starts.slice(cut); rows = rows.slice(cut); if (prev) prev = prev.slice(cut);
      if (current != null) current = current >= cut ? current - cut : null;
      since = new Date(first);
    }
  }
  // Today keeps today's 24 hourly slots; hours still to come stay empty.
  if (rangeKey(ctx) === 'today' && bucket === 'hour' && starts.length) {
    const end = new Date(starts[0]); end.setHours(24, 0, 0, 0);
    while (starts.length < 30 && nextBucket(starts[starts.length - 1], 'hour') < end) { starts.push(nextBucket(starts[starts.length - 1], 'hour')); rows.push(null); if (prev) prev.push(null); }
  }
  const ends = starts.map((d, i) => starts[i + 1] || nextBucket(d, bucket));
  const now = new Date();
  const labels = starts.map((d, i) => {
    let label = '', tip = '';
    if (bucket === 'hour') { label = d.toLocaleTimeString(undefined, {hour: '2-digit', hour12: false}); tip = `${hourName(d)} – ${hourName(ends[i])} · ${dayWord(d, now)}`; }
    else if (bucket === 'minute') { label = d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', hour12: false}); tip = d.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'}); }
    else if (bucket === 'week') { label = shortDate(d); tip = 'Week of ' + shortDate(d); }
    else { label = shortDate(d); tip = d.toLocaleDateString(undefined, {weekday: 'short', month: 'short', day: 'numeric'}); }
    if (i === current) tip += ' · so far';
    return {label, tip, start: d, end: ends[i]};
  });
  const asked = ctx.range?.since?.getTime?.();
  if (!since && starts.length && bucket !== 'minute' && (rangeKey(ctx) === 'all' || Number.isFinite(asked) && starts[0].getTime() - asked >= (bucket === 'hour' ? 3600e3 : 86400e3))) since = starts[0];
  const caption = ({hour: 'By hour · your local time', day: 'By day', week: 'By week', minute: 'By minute'}[bucket] || '') + (since ? ` · since ${shortDate(since)}` : '');
  return {bucket, keys, rows, prev, labels, current, caption, since, unit: resp.unit || METRICS[o.metric]?.unit || 'count', resp};
}
const allEmpty = m => !m.rows.some(r => r && Object.values(r).some(v => isNum(v) && (v !== 0 || m.unit === 'percent' || m.unit === 'ms')));

/* ---------- states ----------
   Loading, error and empty go inside the widget's own card body, so the card and its title stay. Stale and "updated"
   chips go to the widget (ctx.state with the host); 'ready' clears them. */
function stateBox(el, kind, info = {}) {
  if (kind === 'loading') el.innerHTML = info.skeleton ? '<span class="rc-sk" style="width:58%;height:28px;margin-top:12px"></span><span class="rc-sk" style="width:38%;height:9px;margin-top:10px"></span>' : '<div class="rc-state loading">Loading usage…</div>';
  else if (kind === 'error') {
    el.innerHTML = `<div class="rc-state err${info.compact ? ' row' : ''}" role="alert">${svgUse('alert')}<span>${esc(info.message || "Couldn't load usage")}</span><button type="button" class="resets-button">${info.compact ? 'Retry' : 'Try again'}</button></div>`;
    el.querySelector('button').addEventListener('click', () => info.retry?.());
  } else if (kind === 'empty') el.innerHTML = `<div class="rc-state${info.compact ? ' row' : ''}">${esc(info.message || 'Nothing to show yet.')}</div>`;
  else if (kind === 'removed') {
    el.innerHTML = `<div class="rc-state${info.compact ? ' row' : ''}">${svgUse('off')}<span>${esc(info.message || 'This account was removed')}</span><button type="button" class="resets-button">Choose another</button></div>`;
    el.querySelector('button').addEventListener('click', () => info.retry?.());
  }
}
function setState(ctx, el, kind, info = {}) {
  const local = ['loading', 'error', 'empty', 'removed'].includes(kind);
  if (typeof ctx.state === 'function' && !(local && info.compact)) { try { ctx.state(el, kind, info); return; } catch (_) {} }
  if (local) stateBox(el, kind, info);
}
// Usage data is stale while the collector is behind; allowance data while its reading is old; both while the gateway is offline.
function usageStaleMinutes(ctx) {
  const o = ctx.overview || {}, now = Date.now();
  if (o.gateway?.online === false) return Math.max(1, Math.round((now - Date.parse(o.gateway?.checked_at || o.generated_at || now)) / 60000)) || 1;
  const last = Date.parse(o.health?.usage_collection?.last_success_at);
  if (Number.isFinite(last) && now - last > 5 * 60000) return Math.round((now - last) / 60000);
  return 0;
}

/* ---------- widget scaffolding ---------- */
function shell(host, type, variant, html) {
  let root = host.querySelector(':scope > .rc');
  const tag = type + ':' + variant;
  if (!root || root.dataset.shell !== tag) {
    if (root) { RC.destroy(root); root.remove(); }
    clearInterval(host.__rcw?.timer);
    host.querySelectorAll(':scope > .widget-state:not(.state-chip)').forEach(n => n.remove());
    root = document.createElement('div');
    root.className = `rc rc-w-${type}`;
    root.dataset.shell = tag;
    root.innerHTML = html;
    host.prepend(root);
    host.__rcw = {seq: (host.__rcw?.seq || 0) + 1, drawn: false};
  }
  if (host.__rcCtx) reserveTools(root, host.__rcCtx);
  return root;
}
const titleFor = (def, ctx) => ctx.widget?.title || def.title(ctx.options, ctx);
function card({cls = '', title, caption = '', side = '', body = '', tight = false, after = '', live = false}) {
  return `<article class="rc-card${tight ? ' tight' : ''} ${cls}"><div class="rc-head"><div class="rc-head-main"><div class="rc-eyebrow-row"><div class="card-eyebrow rc-title">${esc(title)}</div>${live ? '<span class="rc-live" title="Live" aria-hidden="true"></span>' : ''}</div>${caption !== null ? `<p class="rc-cap">${esc(caption)}</p>` : ''}</div><div class="rc-head-side" data-widget-head-side>${side}</div></div><div class="rc-body">${body}</div>${after}</article>`;
}
function reserveTools(root, ctx) {
  const chip = ctx.range?.overridden && !ctx.preview ? String(ctx.range.label || '').length * 6.2 + 28 : 0;
  root.style.setProperty('--rc-tools', Math.round(chip + (ctx.preview ? 0 : 22)) + 'px');
}
function setText(root, sel, text) { const n = root.querySelector(sel); if (n && n.textContent !== text) n.textContent = text; }
// Fetch, then draw; a newer render wins. While a refetch runs the previous render stays at 45%.
async function load(host, ctx, body, {fetch, draw, empty, compact, skeleton, stale = usageStaleMinutes}) {
  const st = host.__rcw || (host.__rcw = {seq: 0});
  const seq = ++st.seq;
  const retry = () => load(host, ctx, body, {fetch, draw, empty, compact, skeleton, stale});
  // The previous render stays, dimmed to 45% once a refetch takes long enough to notice (a quick periodic refresh
  // shouldn't flicker).
  clearTimeout(st.dimTimer);
  if (st.drawn) st.dimTimer = setTimeout(() => { if (seq === st.seq) RC.dim(body, true); }, 250);
  else setState(ctx, body, 'loading', {compact, skeleton});
  let data;
  try { data = await fetch(); } catch (error) {
    if (seq !== st.seq) return;
    clearTimeout(st.dimTimer);
    RC.dim(body, false);
    if (st.drawn) setState(ctx, host, 'updated', {at: st.at});
    else { st.drawn = false; setState(ctx, body, 'error', {message: "Couldn't load usage", retry, compact}); }
    return;
  }
  if (seq !== st.seq || !host.isConnected && !ctx.preview) return;
  clearTimeout(st.dimTimer);
  RC.dim(body, false);
  const message = empty?.(data);
  if (message) { st.drawn = false; RC.destroy(body); body.replaceChildren(); setState(ctx, body, 'empty', {message, compact}); }
  else { draw(data); st.drawn = true; }
  st.at = new Date();
  setState(ctx, host, 'ready');
  const minutes = stale ? stale(ctx, data) : 0;
  if (minutes > 0) setState(ctx, host, 'stale', {minutes});
}
// "This account was removed · Choose another" (opens the widget's settings).
function removedBox(host, ctx, body, message, compact = false) {
  body.replaceChildren();
  const retry = () => {
    if (typeof ctx.openSettings === 'function') ctx.openSettings();
    else host.dispatchEvent(new CustomEvent('router:widget-settings', {bubbles: true, detail: {id: ctx.widget?.id}}));
  };
  setState(ctx, body, 'removed', {message, retry, compact});
  if (host.__rcw) host.__rcw.drawn = false;
}
const HIDDEN = new Map();   // legend state per widget, for the session only
const hiddenFor = ctx => HIDDEN.get(ctx.widget?.id) || new Set();

/* ---------- Usage chart ---------- */
function chartFetch(ctx, o) {
  return ctx.api('/api/series', rangeParams(ctx, {metric: o.metric, group: o.group, bucket: ctx.range?.bucket || 'hour', compare: o.compare ? 1 : 0}));
}
function kindFor(o) {
  if (isRate(o.metric)) return 'line';
  if (o.group === 'none') return o.chart === 'stacked' || o.chart === 'grouped' ? 'bars' : o.chart === 'sarea' ? 'area' : o.chart;
  return o.chart || 'stacked';
}
function pickRequests(ctx, m, o, i, key) {
  const lab = m.labels[i];
  if (!lab) return;
  const until = new Date(Math.min(lab.end.getTime(), Date.now()));
  const k = m.keys.find(x => x.key === key);
  const q = {until};
  if (k?.provider) q.provider = k.provider;
  if (k?.account) q.account = k.account;
  if (o.metric === 'failures' || k?.failed) q.failed = true;
  ctx.openRequests?.(q);
}
function drawChart(host, ctx, def, root, m) {
  const o = ctx.options, body = root.querySelector('.rc-body'), lg = root.querySelector('.rc-legend');
  const hidden = hiddenFor(ctx);
  for (const k of [...hidden]) if (!m.keys.some(x => x.key === k)) hidden.delete(k);
  const kind = kindFor(o), title = titleFor(def, ctx), M = METRICS[o.metric] || METRICS.requests;
  setText(root, '.rc-cap', m.caption);
  let plot = body.querySelector(':scope > .rc-plot');
  if (!plot) { RC.destroy(body); body.replaceChildren(); plot = document.createElement('div'); plot.className = 'rc-plot'; body.append(plot); }
  // The settings preview box is short (boards.css caps it), so the preview draws a compact chart.
  const height = plotHeight(ctx);
  plot.style.setProperty('--rc-plot-min', height + 'px');
  const total = m.rows.reduce((s, r) => s + (r ? Object.values(r).reduce((a, b) => a + (isNum(b) ? b : 0), 0) : 0), 0);
  const prevName = rangeKey(ctx) === 'today' ? 'Yesterday' : 'Previous';
  RC.time(plot, {
    kind, keys: m.keys, hidden, rows: m.rows, prev: o.compare ? m.prev : null, labels: m.labels, xTicks: xTicks(m.rows.length), current: m.current,
    unit: m.unit, height, prevName, pickable: !!M.pick && !ctx.customizing && !ctx.preview,
    label: `${title}. ${M.name}, ${m.caption.toLowerCase()}${!M.rate ? `, ${fmt(total, m.unit)} in total` : ''}. Use the arrow keys for each period, or View as table.`,
    onPick: (i, key) => pickRequests(ctx, m, o, i, key),
  });
  const line = kind === 'line' || kind === 'area';
  const extra = o.compare ? [{name: rangeKey(ctx) === 'today' ? 'Yesterday' : 'Previous period', line: true, color: 'var(--faint)'}] : [];
  if (m.keys.length > 1 || extra.length) {
    RC.legend(lg, {keys: m.keys, hidden, line, extra, onChange: next => { HIDDEN.set(ctx.widget?.id, next); drawChart(host, ctx, def, root, m); }});
  } else { lg.replaceChildren(); lg.hidden = true; }
}
const plotHeight = ctx => ctx.preview ? PREVIEW_HEIGHT : ctx.customizing && window.innerWidth <= 760 ? 90 : HEIGHTS[ctx.options?.height] || HEIGHTS.medium;
function chartEmptyText(ctx, o) {
  const M = METRICS[o.metric] || METRICS.requests;
  return o.metric === 'failures' ? `No failed requests ${phrase(ctx)}.` : M.unit === 'tokens' ? `No ${M.noun} ${phrase(ctx)}.` : `No requests ${phrase(ctx)}.`;
}
const chartDef = {
  type: 'chart', name: 'Usage chart', section: 'Charts', span: 6, min: 4, compact: false, minRows: 4, maxRows: 20,
  defaults: {chart: 'stacked', metric: 'requests', group: 'account', range: 'board', compare: false},
  title(o) { const M = METRICS[o.metric] || METRICS.requests; return o.group && o.group !== 'none' ? `${M.name} by ${GROUPS[o.group].toLowerCase()}` : M.name; },
  fields(o) {
    const rate = isRate(o.metric), none = o.group === 'none';
    const why = rate ? 'Rates read best as a line' : '';
    return [
      {key: 'chart', label: 'Chart', kind: 'tiles', choices: [
        {value: 'stacked', label: none ? 'Bars' : 'Stacked bars', icon: none ? ICONS.bars : ICONS.stacked, disabled: rate, reason: why},
        {value: 'grouped', label: 'Grouped bars', icon: ICONS.grouped, disabled: rate || none, reason: why || (none ? 'Needs a Group by' : '')},
        {value: 'line', label: 'Line', icon: ICONS.line},
        {value: 'area', label: 'Area', icon: ICONS.area, disabled: rate, reason: why},
        {value: 'sarea', label: 'Stacked area', icon: ICONS.sarea, disabled: rate || none, reason: why || (none ? 'Needs a Group by' : '')}]},
      {key: 'metric', label: 'Metric', kind: 'select', choices: metricChoices(SERIES_METRICS)},
      {key: 'group', label: 'Group by', kind: 'segmented', choices: Object.entries(GROUPS).map(([value, label]) => ({value, label,
        disabled: value === 'outcome' && o.metric !== 'requests', reason: value === 'outcome' && o.metric !== 'requests' ? 'Only for Requests' : ''}))},
      {key: 'range', label: 'Time range', kind: 'range'},
      {key: 'compare', label: 'Compare with previous period', kind: 'switch'},
    ];
  },
  normalize(options) {
    const o = {...options}, notes = [];
    if (!METRICS[o.metric]) o.metric = 'requests';
    if (!GROUPS[o.group]) o.group = 'account';
    if (!['stacked', 'grouped', 'line', 'area', 'sarea'].includes(o.chart)) o.chart = 'stacked';
    if (o.group === 'outcome' && o.metric !== 'requests') { o.group = 'account'; notes.push('Grouped by account. Outcome is for requests only.'); }
    if (isRate(o.metric) && o.chart !== 'line') { o.chart = 'line'; notes.push('Switched to a line chart. Rates read best as a line.'); }
    if (o.group === 'none' && (o.chart === 'grouped' || o.chart === 'sarea')) { o.chart = o.chart === 'sarea' ? 'area' : 'stacked'; notes.push(o.chart === 'area' ? 'Switched to an area chart. Stacked area needs a Group by.' : 'Switched to bars. Grouped bars need a Group by.'); }
    return {options: o, note: notes.join(' ') || undefined};
  },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options;
    const root = shell(host, 'chart', 'card', card({cls: 'rc-chart', title: titleFor(this, ctx), caption: '', body: '', after: '<div class="legend rc-legend" hidden></div>'}));
    root.classList.toggle('rc-preview', !!ctx.preview);
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    body.style.setProperty('--rc-body-min', plotHeight(ctx) + 'px');
    return load(host, ctx, body, {
      fetch: () => chartFetch(ctx, o).then(resp => chartModel(ctx, resp, o)),
      empty: m => allEmpty(m) ? chartEmptyText(ctx, o) : null,
      draw: m => drawChart(host, ctx, this, root, m),
    }).then(() => { if (!host.__rcw?.drawn) { const lg = root.querySelector('.rc-legend'); lg.replaceChildren(); lg.hidden = true; } });
  },
  preview(host) { host.innerHTML = thumb('chart'); },
  async table(ctx) {
    const o = ctx.options, m = chartModel(ctx, await chartFetch(ctx, o), o);
    const counted = !isRate(o.metric) && m.keys.length > 1;
    return {columns: ['Period', ...m.keys.map(k => k.title || k.name), ...(counted ? ['Total'] : [])],
      rows: m.labels.map((l, i) => { const r = m.rows[i]; if (!r) return null; const vals = m.keys.map(k => exact(r[k.key] ?? null, m.unit)); return [l.tip.replace(' · so far', ''), ...vals, ...(counted ? [exact(Object.values(r).reduce((a, b) => a + (b || 0), 0), m.unit)] : [])]; }).filter(Boolean)};
  },
  destroy(host) { RC.destroy(host); },
};

/* ---------- Stat tile ---------- */
function statFilter(ctx, o) {
  const f = String(o.filter || 'all');
  if (f.startsWith('provider:')) { const id = f.slice(9); return {params: {provider: id}, label: providerName(ctx, id), provider: id, ok: providers(ctx).some(p => p.id === id)}; }
  if (f.startsWith('account:')) {
    const [, provider, ...rest] = f.split(':'), id = rest.join(':'), hit = findAccount(ctx, provider, id);
    // One account under its provider reads as the provider ("Success rate · Grok"); otherwise provider and name.
    const label = !hit ? 'Removed account' : (hit.p.accounts || []).length === 1 ? providerName(ctx, provider) : `${providerName(ctx, provider)} ${local(nameOf(ctx, accountRow(provider, hit.a)))}`;
    return {params: {provider, account: id}, provider, account: id, removed: !hit, label, ok: !!hit};
  }
  return {params: {}, label: '', ok: true};
}
function usageTotals(rows) {
  const t = {requests: 0};
  for (const r of rows) t.requests += r.requests || 0;
  for (const kind of ['cache_read', 'cache_creation']) {
    const known = rows.filter(r => r[`${kind}_requests`] > 0 && r[`${kind}_tokens`] != null);
    t[`${kind}_requests`] = known.reduce((n, r) => n + r[`${kind}_requests`], 0);
    t[`${kind}_tokens`] = known.length ? known.reduce((n, r) => n + r[`${kind}_tokens`], 0) : null;
  }
  return t;
}
// Read and write are lower bounds ("≥") when the split was recorded for only some requests, as on today's Usage tab.
function cacheAmount(t, kind) {
  const count = t[`${kind}_requests`] || 0, value = t[`${kind}_tokens`];
  if (!count || value == null) return {text: '—', title: 'Not recorded'};
  const v = fmt(value, 'tokens');
  return count < (t.requests || 0) ? {text: '≥' + v, title: `Recorded for ${nf.format(count)} of ${nf.format(t.requests)} requests`} : {text: v, title: ''};
}
async function cacheSplit(ctx, f) {
  // Same call as the board's usage tables (boards.js usageFor), so ctx.api shares one request for the range.
  const usage = rangeKey(ctx) === 'today' && ctx.overview?.usage ? ctx.overview.usage
    : (await ctx.api('/api/overview', {since: ctx.range?.since || new Date(new Date().setHours(0, 0, 0, 0))}))?.usage;
  if (!usage?.available) return null;
  const sources = usage.providers || {codex: usage, ...(usage.claude ? {claude: usage.claude} : {})};
  const rows = Object.entries(sources).flatMap(([provider, s]) => (s.accounts || []).map(r => ({...r, provider: r.provider || provider})))
    .filter(r => (!f.provider || r.provider === f.provider) && (!f.account || r.auth_index === f.account));
  const result = usageTotals(rows), app = window.RouterApp;
  if (app?.promptUsage && app?.totals && app?.cachedShareLabel) {
    const totals = app.totals(rows.map(r => ({...r,...app.promptUsage(r.provider,r)})));
    result.shareLabel = app.cachedShareLabel(totals);
    result.shareComplete = totals.cached_prompt_requests === totals.requests && totals.requests > 0;
  }
  return result;
}
function retryRecovered(r, ctx) {
  const since = Date.parse(r?.evidence_since), scanned = Date.parse(r?.last_scanned_at);
  return r?.available === true && Number.isInteger(r.recovered_requests) && r.recovered_requests > 0 && Number.isFinite(since) &&
    since <= (ctx.range?.since?.getTime?.() ?? Date.now()) && Number.isFinite(scanned) && Date.now() - scanned < 300000 ? r.recovered_requests : 0;
}
async function statData(ctx, o) {
  const f = statFilter(ctx, o);
  if (o.metric === 'retry_recoveries') {
    const cur = await ctx.api('/api/retry', rangeParams(ctx, {}, false));
    let prev = null;
    if (o.delta && ctx.range?.since) {
      const s = ctx.range.since.getTime(), e = (ctx.range.until || new Date()).getTime();
      prev = await ctx.api('/api/retry', {since: iso(floorMinute(new Date(s - (e - s)))), until: iso(floorMinute(new Date(s)))}).catch(() => null);
    }
    return {unit: 'count', value: cur?.available ? cur.recovered_requests ?? null : null, prev: prev?.available ? prev.recovered_requests ?? null : null, spark: null, f, unavailable: !cur?.available};
  }
  const resp = await ctx.api('/api/series', rangeParams(ctx, {metric: o.metric, group: 'none', bucket: ctx.range?.bucket || 'hour', compare: o.delta ? 1 : 0, ...f.params}));
  const s = resp.series?.[0], M = METRICS[o.metric];
  const sum = v => (v || []).reduce((a, b) => a + (isNum(b) ? b : 0), 0);
  const value = isNum(s?.total) ? s.total : M.rate ? null : sum(s?.values);
  const ps = resp.previous?.series?.[0];
  const prev = ps ? (isNum(ps.total) ? ps.total : M.rate ? null : sum(ps.values)) : null;
  const out = {unit: resp.unit || M.unit, value, prev, spark: s?.values || [], current: resp.current, f};
  if (o.metric === 'success_rate') {
    const [fails, retry] = await Promise.all([
      ctx.api('/api/series', rangeParams(ctx, {metric: 'failures', group: 'none', bucket: ctx.range?.bucket || 'hour', compare: 0, ...f.params})),
      f.provider ? null : ctx.api('/api/retry', rangeParams(ctx, {}, false)).catch(() => null),
    ]);
    const fs = fails.series?.[0];
    out.failed = isNum(fs?.total) ? fs.total : sum(fs?.values);
    out.recovered = retryRecovered(retry, ctx);
  }
  if (o.metric === 'cache_hit_rate') out.split = await cacheSplit(ctx, f).catch(() => null);
  return out;
}
function deltaHtml(ctx, o, d) {
  if (!o.delta || !isNum(d.value) || !isNum(d.prev)) return '';
  const M = METRICS[o.metric] || {}, rate = !!M.rate && d.unit === 'percent';
  if (!rate && d.prev === 0) return '';
  const ch = rate ? d.value - d.prev : M.rate ? (d.value - d.prev) / d.prev * 100 : (d.value - d.prev) / d.prev * 100;
  const vs = RANGE_VS[rangeKey(ctx)] || 'vs prior period';
  if (Math.abs(ch) < (rate ? .05 : .5)) return `<span class="rc-delta">No change<small>${esc(vs)}</small></span>`;
  const up = ch > 0, tone = M.upBad ? (up ? 'bad' : 'good') : M.upGood ? (up ? 'good' : 'bad') : '';
  return `<span class="rc-delta ${tone}">${up ? ARROW_UP : ARROW_DOWN}${rate ? Math.abs(ch).toFixed(1) + ' pts' : Math.round(Math.abs(ch)) + '%'}<small>${esc(vs)}</small></span>`;
}
const statDef = {
  type: 'stat', name: 'Stat tile', section: 'Numbers', span: 3, min: 2, compact: true, minRows: 3, maxRows: 8,
  defaults: {metric: 'requests', filter: 'all', range: 'board', spark: true, delta: true},
  title(o) { return STAT_TITLES[o.metric] || 'Stat'; },
  fields(o, ctx) {
    const choices = [{value: 'all', label: 'All accounts'}];
    for (const p of providers(ctx)) choices.push({value: 'provider:' + p.id, label: p.name || p.id});
    for (const p of providers(ctx)) for (const a of p.accounts || []) { const id = a.auth_index || a.id; if (id) choices.push({value: `account:${p.id}:${id}`, label: `${p.name || p.id} · ${local(nameOf(ctx, accountRow(p.id, a)))}`}); }
    if (o.filter && !choices.some(c => c.value === o.filter)) choices.push({value: o.filter, label: 'Removed account'});
    const retry = o.metric === 'retry_recoveries';
    return [
      {key: 'metric', label: 'Metric', kind: 'select', choices: metricChoices([...SERIES_METRICS, 'retry_recoveries'])},
      {key: 'filter', label: 'Accounts', kind: 'select', choices, hidden: retry},
      {key: 'range', label: 'Time range', kind: 'range'},
      {key: 'spark', label: 'Sparkline', kind: 'switch', hidden: retry},
      {key: 'delta', label: 'Change vs previous period', kind: 'switch'},
    ];
  },
  normalize(options) {
    const o = {...options};
    if (!METRICS[o.metric] && o.metric !== 'retry_recoveries') o.metric = 'requests';
    if (o.metric === 'retry_recoveries') { o.filter = 'all'; o.spark = false; }
    return {options: o};
  },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options, title = titleFor(this, ctx);
    const root = shell(host, 'stat', 'tile', `<div class="metric-card rc-stat"><div class="rc-stat-head"><span class="rc-stat-label"></span><div class="rc-head-side" data-widget-head-side></div></div><div class="rc-body"></div></div>`);
    const f = statFilter(ctx, o);
    setText(root, '.rc-stat-label', title + (f.label ? ' · ' + f.label : ''));
    const body = root.querySelector('.rc-body');
    if (f.removed) { removedBox(host, ctx, body, 'This account was removed', true); setState(ctx, host, 'ready'); return Promise.resolve(); }
    return load(host, ctx, body, {
      compact: true, skeleton: true,
      fetch: () => statData(ctx, o),
      draw: d => {
        const M = METRICS[o.metric] || {unit: 'count'};
        const noData = !isNum(d.value) || (d.unavailable);
        let detail = '';
        if (noData) detail = `<small class="metric-detail"><span class="rc-wrap">${esc(d.unavailable ? 'Retry evidence isn’t available yet' : `No requests ${phrase(ctx)}`)}</span></small>`;
        else if (o.metric === 'success_rate' && d.failed > 0) detail = `<small class="metric-detail"><button type="button" class="fail-link">${esc(nf.format(d.failed))} failed</button>${d.recovered ? `<span title="Requests recovered after rate limits · from the gateway log">${esc(nf.format(d.recovered))} recovered</span>` : ''}</small>`;
        else if (o.metric === 'cache_hit_rate' && d.split) { const r = cacheAmount(d.split, 'cache_read'), w = cacheAmount(d.split, 'cache_creation'); detail = `<small class="metric-detail"><span title="${esc(r.title)}">Read ${esc(r.text)}</span><span title="${esc(w.title)}">Write ${esc(w.text)}</span></small>`; }
        // Counts in full and tokens compact, as on today's Usage tiles.
        const value = noData ? '—' : d.unit === 'count' ? nf.format(Math.round(d.value)) : d.unit === 'tokens' ? compact.format(d.value) : fmt(d.value, d.unit || M.unit);
        const delta = noData || o.metric==='cache_hit_rate'&&d.split?.shareComplete===false ? '' : deltaHtml(ctx, o, d);
        body.innerHTML = `<strong class="${noData ? 'rc-none' : ''}">${o.metric==='cache_hit_rate'&&d.split?.shareLabel || esc(value)}</strong>${detail}${delta ? `<div class="rc-delta-row">${delta}</div>` : ''}${o.spark && d.spark?.filter(isNum).length > 1 ? '<div class="rc-spark-slot"></div>' : ''}`;
        body.querySelector('.fail-link')?.addEventListener('click', () => ctx.openRequests?.({failed: true, ...(f.provider ? {provider: f.provider} : {}), ...(f.account ? {account: f.account} : {})}));
        const slot = body.querySelector('.rc-spark-slot');
        if (slot) RC.spark(slot, d.spark, {unit: d.unit, current: d.current});
      },
    });
  },
  preview(host) { host.innerHTML = thumb('stat'); },
  async table(ctx) {
    const o = ctx.options, d = await statData(ctx, o);
    const value=o.metric==='cache_hit_rate'&&d.split?.shareLabel ? d.split.shareLabel.replace(/<[^>]*>/g,'') : exact(d.value,d.unit);
    const rows = [[titleFor(this, ctx), value, exact(d.prev, d.unit)]];
    if (isNum(d.failed)) rows.push(['Failed requests', nf.format(d.failed), '']);
    if (d.split) rows.push(['Cache read', cacheAmount(d.split, 'cache_read').text, ''], ['Cache write', cacheAmount(d.split, 'cache_creation').text, '']);
    return {columns: ['Metric', ctx.range?.label || 'Value', 'Previous period'], rows};
  },
  destroy(host) { RC.destroy(host); },
};

/* ---------- allowances (gauge, timeline) ---------- */
const windowSeconds = w => w?.seconds ?? {five_hour: 18000, seven_day: 604800, weekly_model: 604800}[w?.kind];
const windowId = w => String(w?.slot || (w?.kind ? w.kind + (w.model ? '-' + w.model : '') : '') || String(w?.label || 'window').toLowerCase().replace(/[^a-z0-9]+/g, '-')).slice(0, 28);
function limitValue(p, a, w) {
  const v = `${a.key}#${windowId(w)}`;
  return v.length <= 80 ? v : `${a.auth_index || a.id}#${windowId(w)}`.slice(0, 80);
}
function limits(ctx) {
  const out = [];
  for (const p of providers(ctx)) for (const a of p.accounts || []) for (const w of a.windows || []) {
    if (!w || typeof w !== 'object') continue;
    out.push({p, a, w, value: limitValue(p, a, w), row: accountRow(p.id, a)});
  }
  return out;
}
function findLimit(ctx, value) {
  const all = limits(ctx);
  if (!value) return {limit: all[0] || null, auto: true};
  const [acct, wid] = String(value).split('#');
  const same = all.filter(l => l.a.key === acct || l.a.auth_index === acct || l.a.id === acct);
  if (!same.length) return {limit: null, missing: 'account'};
  return {limit: same.find(l => windowId(l.w) === wid) || null, missing: 'limit'};
}
const remainingOf = w => isNum(w?.remaining) ? w.remaining : isNum(w?.used) ? Math.max(0, 100 - w.used) : null;
function limitLabel(ctx, l) { const label = String(l.w.label || 'Allowance'); return `${providerName(ctx, l.p.id)} ${label.charAt(0).toLowerCase()}${label.slice(1)}`; }
function timeLeftShare(w, now = Date.now() / 1000) {
  const s = windowSeconds(w);
  if (!(s > 0) || !isNum(w?.resets_at) || w.resets_at <= now) return null;
  return Math.min(100, (w.resets_at - now) / s * 100);
}
// The same phrases as the account cards' forecast (app.js forecast): a run-out is amber, red within 30 minutes.
function forecastOf(w, remaining, now = Date.now() / 1000) {
  const f = w?.forecast;
  if (!f || typeof f !== 'object' || remaining == null) return null;
  const reset = isNum(w.resets_at) && w.resets_at > now ? w.resets_at : null;
  if (f.state === 'runs_out' && isNum(f.runs_out_at)) {
    const left = f.runs_out_at - now, short = reset == null ? null : reset - f.runs_out_at;
    if (short > 0 && short < 3600) return {tone: 'warn', text: `${duration(short)} short of reset`, state: 'runs_out', at: f.runs_out_at};
    if (left < 60) return {tone: 'bad', text: 'Runs out now', state: 'runs_out', at: f.runs_out_at};
    return {tone: left <= 1800 ? 'bad' : 'warn', text: `Runs out in ${duration(left)}`, state: 'runs_out', at: f.runs_out_at};
  }
  if (f.state === 'lasts') return {tone: '', text: 'Lasts to reset', state: 'lasts'};
  if (f.state === 'idle') return {tone: '', text: 'Idle', state: 'idle'};
  if (f.state === 'out' || remaining <= 0) return {tone: 'bad', text: reset ? `Out · back in ${duration(reset - now)}` : 'Used up', state: 'out'};
  return null;
}
// Same rule as the account cards (app.js quotaWindowFresh): a reading stays fresh while its reset is still ahead (and within
// 8 days of the reading); without a reset, for 15 minutes or until the account's fresh_until.
function allowanceStale(ctx, w, a) {
  if (ctx.overview?.gateway?.online === false) return usageStaleMinutes(ctx) || 1;
  const now = Date.now(), measured = Date.parse(w?.measured_at);
  if (!Number.isFinite(measured) || measured > now) return 0;
  const reset = w.resets_at;
  const fresh = reset != null ? isNum(reset) && reset * 1000 > now && reset * 1000 <= measured + 8 * 86400000
    : now - measured <= 900000 || now <= Date.parse(a?.fresh_until);
  return fresh ? 0 : Math.max(1, Math.round((now - measured) / 60000));
}
const gaugeDef = {
  type: 'gauge', name: 'Allowance gauge', section: 'Limits', span: 3, min: 3, compact: false, minRows: 4, maxRows: 8,
  defaults: {limit: '', style: 'ring'},
  title(o, ctx) { const {limit} = findLimit(ctx || {}, o.limit); return limit ? limitLabel(ctx, limit) : 'Allowance'; },
  fields(o, ctx) {
    const choices = [{value: '', label: 'First limit'}, ...limits(ctx).map(l => ({value: l.value, label: `${local(nameOf(ctx, l.row))} · ${limitLabel(ctx, l)}`}))];
    if (o.limit && !choices.some(c => c.value === o.limit)) choices.push({value: o.limit, label: 'Removed limit'});
    return [
      {key: 'limit', label: 'Limit', kind: 'select', choices},
      {key: 'style', label: 'Style', kind: 'tiles', choices: [{value: 'ring', label: 'Ring', icon: ICONS.ring}, {value: 'bar', label: 'Bar', icon: ICONS.bar}]},
    ];
  },
  normalize(options) { const o = {...options}; if (!['ring', 'bar'].includes(o.style)) o.style = 'ring'; return {options: o}; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options;
    const root = shell(host, 'gauge', o.style, card({cls: 'rc-gauge-card', tight: true, title: titleFor(this, ctx), caption: null}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    const {limit, missing} = findLimit(ctx, o.limit);
    if (!limit) {
      if (missing) removedBox(host, ctx, body, missing === 'account' ? 'This account was removed' : 'This limit isn’t reported anymore');
      else setState(ctx, body, 'empty', {message: 'No allowances reported yet.'});
      setState(ctx, host, 'ready');
      return Promise.resolve();
    }
    const {p, a, w, row} = limit, rem = remainingOf(w), accent = familyColor(accentOf(ctx, p.id));
    const fc = forecastOf(w, rem), share = timeLeftShare(w), now = Date.now() / 1000;
    const reset = isNum(w.resets_at) ? (w.resets_at > now ? duration(w.resets_at - now) : 'Due · refreshing') : 'Not reported';
    const who = `<div class="rc-who">${providerIcon(ctx, p.id)}<b title="${esc(nameOf(ctx, row))}">${esc(local(nameOf(ctx, row)))}</b></div>`;
    const fcHtml = fc ? `<span class="rc-fc ${fc.tone}">${esc(fc.text)}</span>` : '';
    const remText = rem == null ? '—' : String(Math.round(rem));
    const label = `${local(nameOf(ctx, row))}, ${limitLabel(ctx, limit)}: ${rem == null ? 'not reported' : Math.round(rem) + '% left'}${fc ? ', ' + fc.text.toLowerCase() : ''}, resets in ${reset}`;
    if (o.style === 'bar') {
      body.innerHTML = `<div class="rc-gbar" role="img" aria-label="${esc(label)}">${who}<div class="rc-big${rem === 0 ? ' none' : ''}">${esc(remText)}<small>%</small><span>left</span></div><div class="rc-gtrack"><span style="width:${Math.max(0, Math.min(100, rem || 0))}%;background:${accent}"></span>${share != null ? `<i class="rc-pace" style="left:${share.toFixed(1)}%" title="On pace: ${Math.round(share)}% left"></i>` : ''}</div><div class="rc-grow">${fcHtml || '<span></span>'}<span>${isNum(w.used) ? Math.round(w.used) + '% used' : ''}</span></div><div class="rc-grow"><span>Resets in</span><b>${esc(reset)}</b></div></div>`;
    } else {
      body.innerHTML = `<div class="rc-gauge" role="img" aria-label="${esc(label)}"><div class="rc-ring">${RC.ringSVG({remaining: rem, pace: null, color: accent})}<div class="val"><b class="${rem === 0 ? 'none' : ''}">${esc(remText)}<small>%</small></b><span>left</span></div></div><div class="rc-ginfo">${who}${fcHtml ? `<div>${fcHtml}</div>` : ''}<div><span class="gi-l">Resets in</span><b>${esc(reset)}</b></div></div></div>`;
    }
    host.__rcw.drawn = true;
    setState(ctx, host, 'ready');
    const stale = allowanceStale(ctx, w, a);
    if (stale) setState(ctx, host, 'stale', {minutes: stale});
    return Promise.resolve();
  },
  preview(host) { host.innerHTML = thumb('gauge'); },
  async table(ctx) {
    const now = Date.now() / 1000;
    const {limit} = findLimit(ctx, ctx.options.limit);
    const list = limit ? [limit] : [];
    return {columns: ['Account', 'Limit', 'Left', 'Used', 'Resets in', 'Forecast'], rows: list.map(l => { const r = remainingOf(l.w); return [nameOf(ctx, l.row), limitLabel(ctx, l), r == null ? '—' : Math.round(r) + '%', isNum(l.w.used) ? Math.round(l.w.used) + '%' : '—', isNum(l.w.resets_at) && l.w.resets_at > now ? duration(l.w.resets_at - now) : '—', forecastOf(l.w, r)?.text || '—']; })};
  },
};
const TL_HOURS = {'24h': 24, '7d': 168, '30d': 720};
const timelineDef = {
  type: 'timeline', name: 'Reset timeline', section: 'Limits', span: 8, min: 6, compact: false, minRows: 5, maxRows: 16,
  defaults: {window: '7d', forecast: true},
  title() { return 'Resets'; },
  fields() {
    return [
      {key: 'window', label: 'Window', kind: 'segmented', choices: [{value: '24h', label: '24 hours'}, {value: '7d', label: '7 days'}, {value: '30d', label: '30 days'}]},
      {key: 'forecast', label: 'Show when each limit runs out', kind: 'switch'},
    ];
  },
  normalize(options) { const o = {...options}; if (!TL_HOURS[o.window]) o.window = '7d'; return {options: o}; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options, hours = TL_HOURS[o.window] || 168;
    const caption = {24: 'Next 24 hours', 168: 'Next 7 days', 720: 'Next 30 days'}[hours];
    const key = `<div class="rc-tl-key" aria-hidden="true"><span><i></i>Allowance left</span>${o.forecast !== false ? '<span><i class="dry"></i>Forecast to run out</span>' : ''}<span><i class="out"></i>Out now</span><span><i class="dot"></i>Reset</span></div>`;
    const root = shell(host, 'timeline', String(o.forecast !== false), card({cls: 'rc-tl-card', title: titleFor(this, ctx), caption, after: key}));
    setText(root, '.rc-title', titleFor(this, ctx));
    setText(root, '.rc-cap', caption);
    const body = root.querySelector('.rc-body'), now = Date.now() / 1000;
    const list = limits(ctx);
    if (!list.length) { setState(ctx, body, 'empty', {message: 'No allowances reported yet.'}); root.querySelector('.rc-tl-key').hidden = true; host.__rcw.drawn = false; return Promise.resolve(); }
    root.querySelector('.rc-tl-key').hidden = false;
    const lanes = list.map(l => {
      const rem = remainingOf(l.w), fc = forecastOf(l.w, rem, now), name = nameOf(ctx, l.row);
      const state = fc?.state || (rem === 0 ? 'out' : 'lasts');
      const reset = isNum(l.w.resets_at) && l.w.resets_at > now ? l.w.resets_at : null;
      return {name: local(name), sub: `${providerName(ctx, l.p.id)} · ${l.w.label || 'Allowance'}`, color: familyColor(accentOf(ctx, l.p.id)), state, muted: state === 'idle',
        resetAt: reset, runsOutAt: fc?.at, resetText: reset ? (reset - now > hours * 3600 ? 'in ' : '') + duration(reset - now) : '—',
        title: `${name} · ${l.w.label || 'Allowance'} · ${rem == null ? '' : Math.round(rem) + '% left · '}${reset ? 'resets in ' + duration(reset - now) : 'reset not reported'}${fc ? ' · ' + fc.text : ''}`};
    });
    let plot = body.querySelector(':scope > .rc-tl');
    if (!plot) { body.replaceChildren(); plot = document.createElement('div'); body.append(plot); }
    RC.timeline(plot, lanes, {now, hours, forecast: o.forecast !== false, label: `Next reset for each limit, ${caption.toLowerCase()}. ${lanes.map(l => `${l.name}, ${l.sub}: ${l.resetText}`).join('; ')}.`});
    host.__rcw.drawn = true;
    setState(ctx, host, 'ready');
    const stale = Math.max(0, ...list.map(l => allowanceStale(ctx, l.w, l.a)));
    if (stale) setState(ctx, host, 'stale', {minutes: stale});
    return Promise.resolve();
  },
  preview(host) { host.innerHTML = thumb('timeline'); },
  async table(ctx) {
    const now = Date.now() / 1000;
    return {columns: ['Account', 'Limit', 'Left', 'Resets in', 'Forecast'], rows: limits(ctx).map(l => { const r = remainingOf(l.w); return [nameOf(ctx, l.row), limitLabel(ctx, l), r == null ? '—' : Math.round(r) + '%', isNum(l.w.resets_at) && l.w.resets_at > now ? duration(l.w.resets_at - now) : '—', forecastOf(l.w, r, now)?.text || '—']; })};
  },
  destroy(host) { RC.destroy(host); },
};

/* ---------- Breakdown ---------- */
const BY = {model: 'Model', provider: 'Provider', account: 'Account', failure_reason: 'Failure'};
const BREAK_METRICS = ['requests', 'failures', 'tokens', 'input_tokens', 'output_tokens', 'cached_tokens'];
function breakdownFetch(ctx, o) {
  return ctx.api('/api/breakdown', rangeParams(ctx, {metric: o.by === 'failure_reason' ? 'failures' : o.metric, by: o.by, top: o.top}, false));
}
function breakdownParts(ctx, resp, o) {
  const rows = resp.rows || [];
  const parts = rows.map(r => {
    let name = r.label || r.key, color = 'var(--faint)', title = name;
    if (o.by === 'account') {
      const row = {provider: r.provider, auth_index: r.auth_index, email: r.email, account_key: r.account_key};
      const full = nameOf(ctx, row);
      name = `${local(full)} · ${providerName(ctx, r.provider)}`; title = `${providerName(ctx, r.provider)} · ${full}`; color = colorOf(ctx, row);
    } else if (o.by === 'provider') { name = providerName(ctx, r.provider || String(r.key).replace(/^provider:/, '')); color = familyColor(accentOf(ctx, r.provider || String(r.key).replace(/^provider:/, ''))); title = name; }
    else if (o.by === 'model') color = modelColor(accentOf(ctx, r.provider), r.provider + ':' + (r.label || r.key)) || 'var(--faint)';
    else if (o.by === 'failure_reason') color = failColor(kindOf(r.key));
    return {key: r.key, name, title, value: r.value || 0, color, share: r.share};
  });
  if (resp.other && resp.other.value > 0) parts.push({key: 'other', name: `Other (${resp.other.count})`, value: resp.other.value, color: 'var(--faint)', share: resp.other.share});
  return parts;
}
const breakdownDef = {
  type: 'breakdown', name: 'Breakdown', section: 'Charts', span: 4, min: 3, compact: false, minRows: 4, maxRows: 16,
  defaults: {chart: 'donut', by: 'model', metric: 'requests', range: 'board', top: 5},
  presets: [{name: 'Top models', options: {chart: 'bars', by: 'model', metric: 'requests', range: 'board', top: 8}, span: 4}],
  title(o) {
    if (o.by === 'failure_reason') return 'Why requests failed';
    if (o.by === 'model') return o.chart === 'bars' ? 'Top models' : 'Models';
    return o.by === 'provider' ? 'Providers' : 'Accounts';
  },
  fields(o) {
    const fail = o.by === 'failure_reason';
    return [
      {key: 'chart', label: 'Show as', kind: 'tiles', choices: [{value: 'donut', label: 'Donut', icon: ICONS.donut}, {value: 'bars', label: 'Ranked bars', icon: ICONS.ranked}, {value: 'treemap', label: 'Treemap', icon: ICONS.treemap}]},
      {key: 'by', label: 'Split by', kind: 'segmented', choices: Object.entries(BY).map(([value, label]) => ({value, label}))},
      {key: 'metric', label: 'Metric', kind: 'select', choices: metricChoices(BREAK_METRICS), hidden: fail},
      {key: 'range', label: 'Time range', kind: 'range'},
      {key: 'top', label: 'Show top', kind: 'segmented', choices: [5, 8, 10].map(v => ({value: v, label: String(v)}))},
    ];
  },
  normalize(options) {
    const o = {...options};
    if (!['donut', 'bars', 'treemap'].includes(o.chart)) o.chart = 'donut';
    if (!BY[o.by]) o.by = 'model';
    if (!BREAK_METRICS.includes(o.metric)) o.metric = 'requests';
    if (o.by === 'failure_reason') o.metric = 'failures';
    o.top = [5, 8, 10].includes(+o.top) ? +o.top : 5;
    return {options: o};
  },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = {...ctx.options, top: +ctx.options.top || 5};
    const metric = o.by === 'failure_reason' ? 'failures' : o.metric, M = METRICS[metric] || METRICS.requests;
    const caption = `${M.name} · by ${({model: 'model', provider: 'provider', account: 'account', failure_reason: 'reason'})[o.by] || 'model'}`;
    const root = shell(host, 'breakdown', o.chart, card({cls: 'rc-break-card', title: titleFor(this, ctx), caption}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    return load(host, ctx, body, {
      fetch: () => breakdownFetch(ctx, o),
      empty: resp => !(resp.rows || []).some(r => r.value > 0) ? (o.by === 'failure_reason' || metric === 'failures' ? `No failed requests ${phrase(ctx)}.` : M.unit === 'tokens' ? `No ${M.noun} ${phrase(ctx)}.` : `No requests ${phrase(ctx)}.`) : null,
      draw: resp => {
        setText(root, '.rc-cap', caption + (resp.since && Date.parse(resp.since) > (ctx.range?.since?.getTime?.() || 0) + 86400000 ? ` · since ${shortDate(new Date(resp.since))}` : ''));
        const parts = breakdownParts(ctx, resp, o), unit = resp.unit || M.unit;
        RC.destroy(body);
        const el = document.createElement('div');
        body.replaceChildren(el);
        const label = `${titleFor(this, ctx)}. ${parts.map(p => `${p.name} ${fmt(p.value, unit)}`).join(', ')}.`;
        if (o.chart === 'bars') RC.ranked(el, parts, {unit, label});
        else if (o.chart === 'treemap') RC.treemap(el, parts, {unit, label});
        else RC.donut(el, parts, {unit, total: resp.total, centerLabel: unit === 'tokens' ? 'tokens' : metric === 'failures' ? 'failed' : 'requests', label});
      },
    });
  },
  preview(host, options) { host.innerHTML = thumb(options?.chart === 'bars' ? 'ranked' : options?.chart === 'treemap' ? 'treemap' : 'donut'); },
  async table(ctx) {
    const o = ctx.options, resp = await breakdownFetch(ctx, o), parts = breakdownParts(ctx, resp, o), unit = resp.unit;
    const total = resp.total || parts.reduce((s, p) => s + p.value, 0);
    return {columns: [BY[o.by] || 'Name', METRICS[o.by === 'failure_reason' ? 'failures' : o.metric]?.name || 'Value', 'Share'], rows: parts.map(p => [p.title || p.name, exact(p.value, unit), RC.share(p.value, total)])};
  },
  destroy(host) { RC.destroy(host); },
};

/* ---------- Activity heatmap ---------- */
function calendarRange() {
  const end = new Date(); end.setHours(0, 0, 0, 0);
  const start = new Date(end); start.setDate(start.getDate() - (end.getDay() + 6) % 7 - 12 * 7);
  return {start, end};
}
const isoDate = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
function heatFetch(ctx, o) {
  if (o.chart === 'calendar') {
    const {start} = calendarRange();
    return ctx.api('/api/heatmap', {mode: 'calendar', metric: o.metric, since: iso(start), ...(ctx.tz ? {tz: ctx.tz} : {})});
  }
  return ctx.api('/api/heatmap', rangeParams(ctx, {mode: 'weekhour', metric: o.metric}));
}
function calendarDays(resp) {
  const {start, end} = calendarRange(), byDate = new Map((resp.days || []).map(d => [d.date, d.value]));
  const first = resp.first_recorded ? isoDate(new Date(resp.first_recorded)) : null;
  const out = [];
  for (let d = new Date(start), i = 0; i < 91; i++, d.setDate(d.getDate() + 1)) {
    const k = isoDate(d), future = d > end;
    const v = byDate.has(k) ? byDate.get(k) : first && k >= first && !future ? 0 : null;
    out.push({date: k, value: future ? null : v, future});
  }
  return out;
}
const HEAT_WHAT = {requests: 'requests', failures: 'failed requests', tokens: 'tokens'};
const heatmapDef = {
  type: 'heatmap', name: 'Activity heatmap', section: 'Charts', span: 8, min: 4, compact: false, minRows: 5, maxRows: 12,
  defaults: {chart: 'weekhour', metric: 'requests', range: 'all'},
  title(o) { return o.chart === 'calendar' ? ({requests: 'Daily requests', failures: 'Daily failures', tokens: 'Daily tokens'})[o.metric] || 'Daily requests' : 'Activity by hour'; },
  fields(o) {
    return [
      {key: 'chart', label: 'Show as', kind: 'tiles', choices: [{value: 'weekhour', label: 'Week × hour', icon: ICONS.weekhour}, {value: 'calendar', label: 'Calendar', icon: ICONS.calendar}]},
      {key: 'metric', label: 'Metric', kind: 'select', choices: metricChoices(['requests', 'failures', 'tokens'])},
      {key: 'range', label: 'Time range', kind: 'range', hidden: o.chart === 'calendar'},
    ];
  },
  normalize(options) { const o = {...options}; if (!['weekhour', 'calendar'].includes(o.chart)) o.chart = 'weekhour'; if (!HEAT_WHAT[o.metric]) o.metric = 'requests'; return {options: o}; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options, cal = o.chart === 'calendar';
    const what = HEAT_WHAT[o.metric] || 'requests';
    const caption = cal ? 'Last 13 weeks' : `Average ${what} per hour · your local time`;
    const root = shell(host, 'heatmap', o.chart, card({cls: 'rc-heat-card', title: titleFor(this, ctx), caption}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    return load(host, ctx, body, {
      fetch: () => heatFetch(ctx, o),
      empty: resp => cal ? (!(resp.days || []).some(d => d.value > 0) ? `No ${what} in the last 13 weeks.` : null) : (!(resp.cells || []).flat().some(v => v > 0) ? `No ${what} ${phrase(ctx)}.` : null),
      draw: resp => {
        const first = Date.parse(resp.first_recorded);
        const since = Number.isFinite(first) && first > (cal ? calendarRange().start.getTime() : (ctx.range?.since?.getTime?.() || 0)) + 86400000 ? ` · since ${shortDate(new Date(first))}` : '';
        setText(root, '.rc-cap', caption + since);
        let el = body.querySelector(':scope > .rc-heat-host');
        if (!el) { body.replaceChildren(); el = document.createElement('div'); el.className = 'rc-heat-host'; body.append(el); }
        const unit = o.metric === 'tokens' ? 'tokens' : 'count';
        if (cal) RC.calendar(el, calendarDays(resp), {max: resp.max, what, unit, today: isoDate(new Date()), label: `${titleFor(this, ctx)}, last 13 weeks. Use View as table for each day.`});
        else RC.weekHour(el, resp.cells || [], {max: resp.max, what, unit, label: `${titleFor(this, ctx)}: average ${what} for each hour of the week. Use View as table for exact values.`});
      },
    });
  },
  preview(host) { host.innerHTML = thumb('heat'); },
  async table(ctx) {
    const o = ctx.options, resp = await heatFetch(ctx, o), unit = o.metric === 'tokens' ? 'tokens' : 'count';
    if (o.chart === 'calendar') return {columns: ['Date', METRICS[o.metric]?.name || 'Value'], rows: calendarDays(resp).filter(d => !d.future).map(d => [d.date, d.value == null ? '—' : exact(d.value, unit)])};
    return {columns: ['Day', ...Array.from({length: 24}, (_, h) => pad2(h))], rows: (resp.cells || []).map((r, d) => [['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d], ...r.map(v => isNum(v) ? fmt(v >= 10 ? Math.round(v) : Math.round(v * 10) / 10, unit) : '—')])};
  },
  destroy(host) { RC.destroy(host); },
};

/* ---------- Live pulse ---------- */
function pulseFetch(ctx, o) {
  const since = floorMinute(new Date(Date.now() - 59 * 60000));
  return ctx.api('/api/series', {metric: 'requests', group: o.split === 'provider' ? 'provider' : 'none', bucket: 'minute', since: iso(since), ...(ctx.tz ? {tz: ctx.tz} : {})});
}
function lastRequestAt(ctx) {
  const t = Date.parse(ctx.overview?.usage?.recent?.[0]?.timestamp);
  let best = Number.isFinite(t) ? t : null;
  for (const p of providers(ctx)) for (const a of p.accounts || []) { const x = Date.parse(a.today?.last_request_at); if (Number.isFinite(x) && (best == null || x > best)) best = x; }
  return best;
}
const pulseDef = {
  type: 'pulse', name: 'Live pulse', section: 'Charts', span: 4, min: 3, compact: true, minRows: 4, maxRows: 12,
  defaults: {split: 'none'},
  title() { return 'Live pulse'; },
  fields() { return [{key: 'split', label: 'Split by', kind: 'segmented', choices: [{value: 'none', label: 'None'}, {value: 'provider', label: 'Provider'}]}]; },
  normalize(options) { const o = {...options}; if (!['none', 'provider'].includes(o.split)) o.split = 'none'; return {options: o}; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options;
    const root = shell(host, 'pulse', o.split, card({cls: 'rc-pulse-card', tight: true, title: titleFor(this, ctx), caption: null, live: true,
      body: '', after: '<div class="legend rc-legend" hidden></div>'}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body'), lg = root.querySelector('.rc-legend');
    const st = host.__rcw;
    const tick = () => {
      if (!host.isConnected) { clearInterval(st.timer); st.timer = null; return; }
      const at = lastRequestAt(host.__rcCtx), n = root.querySelector('.rc-pulse-ago');
      if (n && at) n.textContent = (root.querySelector('.rc-pulse-hour')?.textContent ? ' · ' : '') + 'last one ' + ago(at);
    };
    if (!st.timer && !ctx.preview) st.timer = setInterval(tick, 1000);
    return load(host, ctx, body, {
      compact: true,
      fetch: () => pulseFetch(ctx, o).then(resp => chartModel(ctx, resp, {metric: 'requests', group: o.split === 'provider' ? 'provider' : 'none'})),
      draw: m => {
        const sums = m.rows.map(r => r ? Object.values(r).reduce((a, b) => a + (isNum(b) ? b : 0), 0) : 0);
        const hour = sums.reduce((a, b) => a + b, 0);
        const full = m.current != null && m.current > 0 ? m.current - 1 : sums.length - 1;
        const perMin = sums[full] ?? 0, at = lastRequestAt(ctx);
        if (!body.querySelector('.rc-pulse-top')) body.innerHTML = '<div class="rc-pulse-top"><b></b><span>requests / min</span></div><div class="rc-pulse-sub"><span class="rc-pulse-hour"></span><span class="rc-pulse-ago"></span></div><div class="rc-pulse-plot"></div><div class="rc-pulse-empty" hidden></div>';
        const top = body.querySelector('.rc-pulse-top b');
        top.textContent = nf.format(perMin);
        top.classList.toggle('none', !hour);
        setText(body, '.rc-pulse-hour', hour ? `${fmt(hour, 'count')} in the last hour` : '');
        setText(body, '.rc-pulse-ago', at ? `${hour ? ' · ' : ''}last one ${ago(at)}` : '');
        const empty = body.querySelector('.rc-pulse-empty'), plotBox = body.querySelector('.rc-pulse-plot');
        empty.hidden = !!hour; plotBox.hidden = !hour;
        if (!hour) { empty.textContent = 'No requests in the last hour.'; lg.hidden = true; return; }
        let plot = plotBox.querySelector('.rc-plot');
        if (!plot) { plot = document.createElement('div'); plotBox.append(plot); }
        plot.style.setProperty('--rc-plot-min', '78px');
        const n = m.rows.length, labels = m.labels.map((l, i) => ({...l, label: i === 0 ? '−60 min' : i === n - 1 ? 'now' : '', tip: i === m.current ? 'This minute · so far' : `${l.tip} · ${Math.max(1, Math.round((Date.now() - l.start) / 60000))} min ago`}));
        RC.time(plot, {kind: o.split === 'provider' ? 'stacked' : 'bars', keys: m.keys, rows: m.rows, labels, xTicks: [0, n - 1], current: m.current, unit: 'count', height: 78, axis: false,
          pickable: !ctx.customizing && !ctx.preview, label: `Requests per minute, last 60 minutes: ${fmt(hour, 'count')} in total.`, onPick: (i, key) => pickRequests(ctx, {...m, labels}, {metric: 'requests'}, i, key)});
        if (o.split === 'provider' && m.keys.length > 1) RC.legend(lg, {keys: m.keys, interactive: false});
        else { lg.replaceChildren(); lg.hidden = true; }
      },
    });
  },
  preview(host) { host.innerHTML = thumb('pulse'); },
  async table(ctx) {
    const o = ctx.options, m = chartModel(ctx, await pulseFetch(ctx, o), {metric: 'requests', group: o.split});
    return {columns: ['Minute', ...m.keys.map(k => k.name)], rows: m.labels.map((l, i) => [l.label, ...m.keys.map(k => fmt(m.rows[i]?.[k.key] ?? 0, 'count'))])};
  },
  destroy(host) { clearInterval(host.__rcw?.timer); RC.destroy(host); },
};

/* ---------- Health ---------- */
const HEALTH_WORDS = {healthy: 'Healthy', down: 'Down', unknown: 'Unknown', stale: 'Stale', failed: 'Failed', warning: 'Needs attention'};
const healthTone = s => s === 'healthy' ? 'ok' : ['down', 'failed'].includes(s) ? 'down' : 'warn';
function healthRows(ctx) {
  const o = ctx.overview || {}, h = o.health || {}, g = o.gateway || {};
  const gw = g.online === false ? 'down' : g.online !== true ? 'unknown' : h.gateway?.status === 'stale' ? 'stale' : 'healthy';
  const rows = [{name: 'Gateway', tone: healthTone(gw), word: {down: 'Unreachable', unknown: 'Connecting', stale: 'Stale', healthy: 'Online'}[gw] + (gw === 'healthy' && g.gateway_version ? ' · ' + g.gateway_version : '')},
    {name: 'Codex server', status: h.codex?.status || 'unknown'}];
  const u = h.usage_collection;
  if (u && typeof u === 'object') rows.push({name: 'Usage collection', status: u.status || 'unknown', when: u.last_success_at});
  rows.push({name: 'Backup', status: h.backup?.status || 'unknown', when: h.backup?.last_success_at});
  const rel = (o.notifications?.items || []).find(n => n?.kind === 'gateway_release');
  if (rel) { const v = /Gateway\s+(\S+)\s+is available/i.exec(rel.message || ''); rows.push({name: 'Gateway releases', tone: 'warn', word: v ? `${v[1]} available` : 'Update available'}); }
  else if (o.notifications?.available) rows.push({name: 'Gateway releases', tone: 'ok', word: 'Up to date'});
  for (const r of rows) {
    if (r.status) { r.tone = healthTone(r.status); r.word = r.status === 'healthy' && r.when ? ago(r.when) : HEALTH_WORDS[r.status] || 'Unknown'; }
    if (r.status && r.status !== 'healthy' && r.when) r.word += ' · ' + ago(r.when);
  }
  return rows;
}
const healthDef = {
  type: 'health', name: 'Health', section: 'Other', span: 4, min: 3, compact: true, minRows: 4, maxRows: 12,
  defaults: {},
  title() { return 'Health'; },
  fields() { return []; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const root = shell(host, 'health', 'card', card({cls: 'rc-health-card', tight: true, title: titleFor(this, ctx), caption: null}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    if (!ctx.overview) { setState(ctx, body, 'loading', {compact: true}); return Promise.resolve(); }
    body.innerHTML = `<div class="rc-hl" role="list">${healthRows(ctx).map(r => `<div role="listitem"><span><i class="${r.tone}" aria-hidden="true"></i>${esc(r.name)}</span><b class="${r.tone}">${esc(r.word)}</b></div>`).join('')}</div>`;
    const st = host.__rcw;
    if (!st.timer && !ctx.preview) st.timer = setInterval(() => {
      if (!host.isConnected) { clearInterval(st.timer); st.timer = null; return; }
      const rows = healthRows(host.__rcCtx);
      root.querySelectorAll('.rc-hl b').forEach((el,i) => { if(rows[i])el.textContent=rows[i].word; });
    }, 1000);
    host.__rcw.drawn = true;
    setState(ctx, host, 'ready');
    return Promise.resolve();
  },
  destroy(host) { clearInterval(host.__rcw?.timer); RC.destroy(host); },
  preview(host) { host.innerHTML = thumb('health'); },
  async table(ctx) { return {columns: ['Item', 'State'], rows: healthRows(ctx).map(r => [r.name, r.word])}; },
};

/* ---------- Retry recovery ---------- */
async function retryData(ctx) {
  const [r, b] = await Promise.all([
    ctx.api('/api/retry', rangeParams(ctx, {}, false)),
    ctx.api('/api/breakdown', rangeParams(ctx, {metric: 'failures', by: 'failure_reason', top: 20}, false)).catch(() => null),
  ]);
  const rate = (b?.rows || []).find(x => kindOf(x.key) === 'rate_limit');
  const since=Date.parse(r?.evidence_since), through=Date.parse(r?.last_scanned_at);
  const partial=!b || !Number.isFinite(since) || !Number.isFinite(through)
    || since > +ctx.range.since || through < +floorMinute(ctx.range.until);
  return {r, partial, recovered: r?.available ? r.recovered_requests || 0 : null, gaveUp: rate ? rate.value || 0 : 0};
}
const retryDef = {
  type: 'retry', name: 'Retry recovery', section: 'Numbers', span: 3, min: 3, compact: true, minRows: 3, maxRows: 8,
  defaults: {range: 'board'},
  title() { return 'Retry recovery'; },
  fields() { return [{key: 'range', label: 'Time range', kind: 'range'}]; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const root = shell(host, 'retry', 'tile', `<div class="metric-card rc-retry"><div class="rc-stat-head"><span class="rc-stat-label"></span><div class="rc-head-side" data-widget-head-side></div></div><div class="rc-body"></div></div>`);
    setText(root, '.rc-stat-label', titleFor(this, ctx));
    const body = root.querySelector('.rc-body');
    return load(host, ctx, body, {
      compact: true, skeleton: true,
      fetch: () => retryData(ctx),
      draw: d => {
        const none = !d.r?.available ? 'Retry evidence isn’t available yet' : !d.partial && d.recovered + d.gaveUp === 0 ? `No rate limits ${phrase(ctx)}` : '';
        if (none) { body.innerHTML = `<strong class="rc-none">—</strong><small class="metric-detail"><span class="rc-wrap">${esc(none)}</span></small>`; return; }
        if(d.partial){body.innerHTML=`<strong class="rc-none">—</strong><small class="metric-detail"><span>Partial journal coverage</span><span>${esc(nf.format(d.recovered))} recovered in journal coverage</span><span>${esc(nf.format(d.gaveUp))} gave up in selected range</span></small>`;return;}
        const total = d.recovered + d.gaveUp, pct = total ? d.recovered / total * 100 : 0;
        const since = Date.parse(d.r.evidence_since), cut = Number.isFinite(since) && since > (ctx.range?.since?.getTime?.() || 0) + 60000;
        body.innerHTML = `<strong>${esc(Math.round(pct) + '%')}</strong><div class="rc-seg" role="img" aria-label="${esc(`${d.recovered} recovered, ${d.gaveUp} gave up`)}"><span style="flex:${d.recovered || 0};background:var(--mint)"></span>${d.gaveUp ? `<span style="flex:${Math.max(d.gaveUp, total * .012)};background:var(--danger)"></span>` : ''}</div><small class="metric-detail"><span>${esc(nf.format(d.recovered))} recovered</span><span>${esc(nf.format(d.gaveUp))} gave up</span>${cut ? `<span>since ${esc(shortDate(new Date(since)))}</span>` : ''}</small>`;
      },
    });
  },
  preview(host) { host.innerHTML = thumb('retry'); },
  async table(ctx) { const d = await retryData(ctx); return {columns: ['Recovered', 'Gave up', 'Recovered share'], rows: [[nf.format(d.recovered || 0), nf.format(d.gaveUp), d.partial ? '— (partial coverage)' : d.r?.available && d.recovered + d.gaveUp ? RC.share(d.recovered, d.recovered + d.gaveUp) : '—']]}; },
};

/* ---------- Notes ---------- */
const NOTE_MAX = 4000;
// Widget IDs are layout-unique. Keep drafts independently of DOM hosts so switching
// boards or a 15-second refresh cannot erase an edit whose write failed.
const noteDrafts = new Map();
const notesDef = {
  type: 'notes', name: 'Notes', section: 'Other', span: 3, min: 2, compact: true, minRows: 3, maxRows: 20,
  defaults: {text: '', edited: null},
  title() { return 'Notes'; },
  fields() { return []; },
  render(host, ctx) {
    host.__rcCtx = ctx;
    const o = ctx.options, editable = !ctx.customizing && !ctx.preview && typeof ctx.saveOptions === 'function';
    const root = shell(host, 'notes', editable ? 'edit' : 'read', card({cls: 'rc-notes-card', tight: true, title: titleFor(this, ctx), caption: null, after: '<div class="rc-note-meta"></div>'}));
    setText(root, '.rc-title', titleFor(this, ctx));
    const body = root.querySelector('.rc-body'), meta = root.querySelector('.rc-note-meta');
    const text = String(o.text || '');
    const edited = Date.parse(o.edited);
    meta.textContent = Number.isFinite(edited) ? 'Edited ' + new Date(edited).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : '';
    meta.hidden = !meta.textContent;
    host.__rcw.drawn = true;
    if (!editable) {
      let n = body.querySelector('.rc-note');
      if (!n) { n = document.createElement('div'); n.className = 'rc-note'; body.replaceChildren(n); }
      n.textContent = text;
      n.dataset.placeholder = 'Write a note';
      return Promise.resolve();
    }
    let ta = body.querySelector('textarea');
    const id = ctx.widget.id;
    const paintMeta = () => {
      const draft = noteDrafts.get(id);
      meta.classList.remove('err');
      if (!draft?.error) return;
      meta.classList.add('err');meta.hidden = false;
      const message = document.createElement('span');message.textContent = 'Couldn’t save your note. ';
      const retry = document.createElement('button');retry.className = 'fail-link';retry.textContent = 'Try again';
      const discard = document.createElement('button');discard.className = 'fail-link';discard.textContent = 'Discard';
      retry.addEventListener('click', save);
      discard.addEventListener('click', () => { noteDrafts.delete(id);ta.value = String(host.__rcCtx.options.text || '');this.render(host,host.__rcCtx);fit(); });
      meta.replaceChildren(message,retry,discard);
    };
    const fit = () => { if (RC.fills?.(host)) { ta.style.height = ''; return; } ta.style.height = 'auto';ta.style.height = Math.max(60,ta.scrollHeight || 60) + 'px'; };
    const save = async () => {
      const draft = noteDrafts.get(id);
      if (!draft || draft.saving) return;
      const value = draft.text, at = new Date().toISOString();
      draft.saving = true;draft.error = false;
      try {
        await host.__rcCtx.saveOptions({text:value,edited:at});
        if (draft.text === value) noteDrafts.delete(id);
      } catch (_) { draft.error = true; }
      finally {
        draft.saving = false;if(host.isConnected)this.render(host,host.__rcCtx);
        if(noteDrafts.get(id)===draft&&!draft.error&&draft.text!==value)save();
      }
    };
    if (!ta) {
      ta = document.createElement('textarea');ta.className = 'rc-note';ta.maxLength = NOTE_MAX;
      ta.placeholder = 'Write a note';ta.spellcheck = true;ta.setAttribute('aria-label',titleFor(this,ctx));body.replaceChildren(ta);
      ta.addEventListener('input', () => {
        const value = ta.value.slice(0,NOTE_MAX), old = noteDrafts.get(id);
        if(value === String(host.__rcCtx.options.text || '') && !old?.saving)noteDrafts.delete(id);
        else if(old)old.text = value;
        else noteDrafts.set(id,{text:value,error:false,saving:false});
        fit();
      });
      ta.addEventListener('blur',save);
    }
    if (document.activeElement !== ta)ta.value = noteDrafts.get(id)?.text ?? text;
    paintMeta();requestAnimationFrame(fit);
    return Promise.resolve();
  },
  preview(host) { host.innerHTML = thumb('notes'); },
  async table(ctx) { return {columns: ['Note'], rows: [[String(ctx.options.text || '')]]}; },
};

/* ---------- picker previews (static, live-coloured) ---------- */
function thumb(kind) {
  const C = 'var(--codex)', L = 'var(--claude)', L2 = 'var(--claude-2)', B = 'var(--blue)', M = 'var(--muted)', T = 'var(--line)';
  const sv = inner => `<svg class="rc-thumb" viewBox="0 0 160 72" preserveAspectRatio="xMidYMid meet" aria-hidden="true">${inner}</svg>`;
  switch (kind) {
    case 'chart': return sv([10, 18, 14, 26, 40, 34, 22, 30, 44, 36, 20, 12].map((h, i) => `<rect x="${8 + i * 12.4}" y="${64 - h}" width="8" height="${(h * .62).toFixed(1)}" rx="1.5" fill="${C}"/><rect x="${8 + i * 12.4}" y="${(64 - h + h * .64).toFixed(1)}" width="8" height="${(h * .36).toFixed(1)}" fill="${L2}"/>`).join('') + `<rect x="4" y="64" width="152" height="1" fill="${T}"/>`);
    case 'donut': return sv(`<g transform="translate(18,8)"><circle cx="28" cy="28" r="23" fill="none" stroke="${C}" stroke-width="9" stroke-dasharray="90 200" transform="rotate(-90 28 28)"/><circle cx="28" cy="28" r="23" fill="none" stroke="${L}" stroke-width="9" stroke-dasharray="40 200" stroke-dashoffset="-92" transform="rotate(-90 28 28)"/><circle cx="28" cy="28" r="23" fill="none" stroke="${L2}" stroke-width="9" stroke-dasharray="8 200" stroke-dashoffset="-134" transform="rotate(-90 28 28)"/></g>${[0, 1, 2, 3].map(i => `<rect x="88" y="${14 + i * 12}" width="6" height="6" rx="1.5" fill="${[C, L, L2, 'var(--faint)'][i]}"/><rect x="99" y="${15 + i * 12}" width="${[44, 36, 30, 22][i]}" height="4" rx="2" fill="${T}"/>`).join('')}`);
    case 'ranked': return sv([118, 70, 34, 18, 10].map((w, i) => `<rect x="10" y="${8 + i * 12.5}" width="${w}" height="7" rx="2" fill="${[C, L, L2, 'var(--codex-2)', 'var(--codex-3)'][i]}"/>`).join(''));
    case 'treemap': return sv(`<rect x="8" y="6" width="78" height="60" rx="4" fill="${C}"/><rect x="88" y="6" width="64" height="34" rx="4" fill="${L}"/><rect x="88" y="42" width="38" height="24" rx="4" fill="${L2}"/><rect x="128" y="42" width="24" height="24" rx="4" fill="var(--faint)"/>`);
    case 'heat': { let h = ''; for (let r = 0; r < 5; r++) for (let c = 0; c < 16; c++) { const v = Math.abs(Math.sin(r * 1.3 + c * .55)) * (c > 3 && c < 7 ? .2 : 1); h += `<rect x="${(6 + c * 9.3).toFixed(1)}" y="${8 + r * 12}" width="7.5" height="9.5" rx="2" fill="color-mix(in srgb,${B} ${Math.round(15 + v * 85)}%,var(--raised))"/>`; } return sv(h); }
    case 'pulse': return sv(Array.from({length: 30}, (_, i) => { const h = 14 + 16 * Math.abs(Math.sin(i / 3.2)) + (i % 4) * 2; return `<rect x="${6 + i * 5}" y="${(62 - h).toFixed(1)}" width="3.4" height="${h.toFixed(1)}" rx="1" fill="${i === 29 ? B : M}" opacity="${i === 29 ? 1 : .5}"/>`; }).join('') + '<circle cx="150" cy="10" r="3" fill="var(--mint)"/>');
    case 'gauge': return sv(`<circle cx="44" cy="36" r="24" fill="none" stroke="var(--track)" stroke-width="6"/><circle cx="44" cy="36" r="24" fill="none" stroke="${C}" stroke-width="6" stroke-linecap="round" stroke-dasharray="${(2 * Math.PI * 24 * .62).toFixed(1)} 999" transform="rotate(-90 44 36)"/><text x="44" y="40" text-anchor="middle" font-family="var(--mono)" font-size="12" fill="var(--text)">62%</text><rect x="84" y="24" width="56" height="5" rx="2.5" fill="${T}"/><rect x="84" y="36" width="40" height="5" rx="2.5" fill="${T}"/><rect x="84" y="48" width="48" height="5" rx="2.5" fill="${T}"/>`);
    case 'timeline': return sv([[L2, 60, 'dry'], [L, 30, 'out'], [C, 86, ''], ['var(--xai)', 100, '']].map(([c, x, k], i) => `${k === 'out' ? `<rect x="8" y="${12 + i * 14}" width="${x}" height="6" rx="2" fill="var(--danger)" opacity=".45"/>` : `<rect x="8" y="${13 + i * 14}" width="${k === 'dry' ? x - 26 : x}" height="3.5" rx="1.75" fill="${c}"/>${k === 'dry' ? `<rect x="${x - 24}" y="${12 + i * 14}" width="24" height="6" rx="2" fill="var(--amber)" opacity=".5"/>` : ''}`}<circle cx="${8 + x}" cy="${15 + i * 14}" r="3.6" fill="var(--text)" stroke="var(--bg)" stroke-width="1.5"/>`).join('') + `<rect x="8" y="6" width="1.5" height="62" fill="${B}"/>`);
    case 'stat': return sv(`<text x="10" y="36" font-family="var(--mono)" font-size="22" fill="var(--text)">8,956</text><text x="10" y="54" font-family="var(--mono)" font-size="9" fill="var(--faint)">7 failed</text><path d="M92 56 L101 50 L110 52 L119 41 L128 44 L138 31 L148 35" fill="none" stroke="${M}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/><circle cx="148" cy="35" r="3" fill="${B}"/>`);
    case 'retry': return sv(`<text x="10" y="36" font-family="var(--mono)" font-size="22" fill="var(--text)">98%</text><rect x="10" y="48" width="128" height="6" rx="3" fill="var(--mint)"/><rect x="140" y="48" width="10" height="6" rx="3" fill="var(--danger)"/>`);
    case 'health': return sv([0, 1, 2, 3].map(i => `<circle cx="12" cy="${14 + i * 14}" r="3" fill="${i === 3 ? 'var(--amber)' : 'var(--mint)'}"/><rect x="22" y="${12 + i * 14}" width="${[60, 48, 70, 54][i]}" height="4" rx="2" fill="${T}"/><rect x="118" y="${12 + i * 14}" width="30" height="4" rx="2" fill="${T}"/>`).join(''));
    case 'notes': return sv(`<text x="12" y="26" font-family="'IBM Plex Serif',Georgia,serif" font-size="11" fill="var(--text)">Fable resets Thursday —</text><text x="12" y="42" font-family="'IBM Plex Serif',Georgia,serif" font-size="11" fill="var(--text)">save long refactors</text><text x="12" y="58" font-family="'IBM Plex Serif',Georgia,serif" font-size="11" fill="var(--muted)">for after.</text>`);
  }
  return '';
}

/* ---------- registration ---------- */
const DEFS = [chartDef, statDef, gaugeDef, timelineDef, breakdownDef, heatmapDef, pulseDef, healthDef, retryDef, notesDef];
window.RouterWidgets = {defs: DEFS, thumb, METRICS, chartModel, seriesKeys, iso, xTicks, forecastOf, limits, findLimit, limitValue, healthRows, stateBox,
  chartEmptyText, statFilter, allowanceStale, rangeParams, kindFor};
function register() {
  if (!window.RouterBoards?.register) return false;
  for (const def of DEFS) window.RouterBoards.register(def);
  return true;
}
if (!register()) document.addEventListener('DOMContentLoaded', register, {once: true});
})();
