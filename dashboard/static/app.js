'use strict';
const ACCOUNT_MUTATION_DEADLINE_MS = 45000;
const ACCOUNT_PROXY_TIMEOUT_MS = ACCOUNT_MUTATION_DEADLINE_MS + 5000;
const ACCOUNT_BROWSER_TIMEOUT_MS = ACCOUNT_PROXY_TIMEOUT_MS + 5000;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = id => `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
const compact = n => n == null ? '—' : new Intl.NumberFormat(undefined, {notation:'compact', maximumFractionDigits:1}).format(n);
const number = n => n == null ? '—' : new Intl.NumberFormat().format(n);
const percent = n => n == null ? '—' : `${new Intl.NumberFormat(undefined,{maximumFractionDigits:1}).format(n)}%`;
const dateTime = value => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '—';
function reasoningCell(value) {
  if (typeof value !== 'string' || !value.trim()) return '<span class="mono" title="Reasoning effort was not recorded" aria-label="Reasoning effort not recorded">—</span>';
  const effort = value.trim();
  const label = effort === 'xhigh' ? 'Extra high' : effort.charAt(0).toUpperCase() + effort.slice(1);
  return `<span class="mono" title="Reasoning effort: ${esc(effort)}">${esc(label)}</span>`;
}
function modeCell(row) {
  const asked = row.service_tier, ran = row.response_service_tier;
  const forwarded = row.forwarded_service_tier;
  const fast = tier => tier === 'priority' || tier === 'ultrafast';
  const off = row.fast_mode_allowed === false;
  if (!['normal','priority','ultrafast'].includes(asked)) return '<span class="mono" title="Service tier was not recorded">—</span>';
  let text = '—', title = 'Service tier was not recorded', tone = '';
  if (fast(ran) && off) {
    text = 'Still fast'; title = `Fast mode off · still ran ${ran}`; tone = ' tier-warn';
  } else if (fast(asked) && off) {
    text = 'Downgraded'; title = `Asked for ${asked} · downgraded to normal`;
  } else if (forwarded === 'ultrafast') {
    text = 'Ultrafast'; title = 'Service tier: ultrafast'; tone = ' tier-fast';
  } else if (forwarded === 'priority') {
    text = 'Fast'; tone = ' tier-fast';
    title = asked === 'ultrafast' ? 'Asked for ultrafast · ran priority on this plan' : 'Service tier: priority';
  } else if (forwarded === 'normal') {
    text = 'Normal'; title = 'Service tier: normal';
  }
  return `<span class="mono${tone}" title="${esc(title)}">${text}</span>`;
}
let range = 'today', current = null, today = null, busy = false, failed = false;
const resetBusy = new Set();
const resetPendingCredits = new Map();
const fastPending = new Map(), fastErrors = new Map();
let fastFocus = null, fastLoadEpoch = 0;
let routingSaving = false;
let claudeRoutingSaving = false;
let routingAllSaving = false, routingLinkSaving = false, routingEpoch = 0, loadDone = Promise.resolve();
let routingChoice = null;
const routingRetry = new Map();
const claudeRoutePending = new Set(), claudeRouteErrors = new Map(), claudeRouteConfirm = new Set();
const claudeRouteErrorState = new Map();
let claudeNextFocus = '';
let resetModalAccount = null, resetConfirmCredit = null, resetModalMessage = '', resetModalKey = '', resetModalWatch = false;
const watchPendingGrants = new Map();
try { document.documentElement.dataset.theme = localStorage.getItem('router-theme') || 'dark'; } catch (_) {}

function startFor(value) {
  if (value === 'all') return '1970-01-01T00:00:00.000Z';
  const d = new Date(); d.setHours(0,0,0,0);
  if (value === 'week') d.setDate(d.getDate()-6);
  return d.toISOString();
}
function usageSince(usage) {
  return Object.values(usageProviders(usage)).map(s=>s.since).filter(value=>Number.isFinite(Date.parse(value)))
    .sort((a,b)=>Date.parse(a)-Date.parse(b))[0] || null;
}
function totals(rows) {
  const t = {requests:0,failures:0,input_tokens:0,output_tokens:0,cached_tokens:0,total_tokens:0};
  for (const r of rows || []) for (const k of Object.keys(t)) t[k] += r[k] || 0;
  // Cache read/write sums cover known values only; with none known they stay null, never zero.
  for (const kind of ['cache_read','cache_creation']) {
    const known = (rows || []).filter(r => r[`${kind}_requests`] > 0 && r[`${kind}_tokens`] != null);
    t[`${kind}_requests`] = known.reduce((n, r) => n + r[`${kind}_requests`], 0);
    t[`${kind}_tokens`] = known.length ? known.reduce((n, r) => n + r[`${kind}_tokens`], 0) : null;
  }
  return t;
}
// Unknown reads "—"; a split recorded for only some requests is a lower bound.
function cacheAmount(r, kind, format=number) {
  const count = r[`${kind}_requests`] || 0, value = r[`${kind}_tokens`];
  if (!count || value == null) return '<span title="Not recorded">—</span>';
  if (count < (r.requests || 0)) return `<span class="partial" title="Recorded for ${esc(number(count))} of ${esc(number(r.requests))} requests">≥${esc(format(value))}</span>`;
  return esc(format(value));
}
function accountFor(id) { return current?.gateway.accounts.find(a => a.id === id); }
function accountName(account) { return account?.email || 'Unknown account'; }
// Card names may wrap in the four-across row; break emails before the @, never mid-word.
const cardName = name => esc(name).replace('@', '<wbr>@');
function providerData(data=current) {
  return data?.providers || [
    {id:'codex',name:'Codex',accent:'codex',routable:codexRoutable(data?.gateway),accounts:data?.gateway?.accounts || []},
    {id:'claude',name:'Claude',accent:'claude',routable:true,accounts:data?.watch_accounts || []}
  ];
}
function usageProviders(u) {
  return u?.providers || {codex:u || {}, ...(u?.claude ? {claude:u.claude} : {})};
}
function providerName(id) { return providerData().find(p=>p.id===id)?.name || id; }
// Codex violet, Claude terracotta, Grok monochrome; any other provider takes the page's navy and blue with its initial.
const providerMarks = {codex:'openai', claude:'claude', xai:'xai'};
function providerAccent(p) { return [p?.accent,p?.id].find(id=>Object.hasOwn(providerMarks,id || '')) || 'other'; }
function providerIcon(p, marked=false) {
  const mark=providerMarks[providerAccent(p)];
  return `<span class="provider-icon${marked ? ' '+providerAccent(p)+'-mark' : ''}">${mark ? icon(mark) : `<span class="monogram" aria-hidden="true">${esc(String(p.name || p.id || '?').charAt(0).toUpperCase())}</span>`}</span>`;
}
const accentOf = id => providerAccent(providerData().find(p=>p.id===id) || {id});
// Email, then the gateway's own label, then "<Provider> account" numbered from the second unnamed account.
function accountLabel(a) {
  if (a?.email) return a.email;
  if (a?.label && a.label!==a.key) return a.label;
  const p=providerData().find(p=>p.id===a?.provider), unnamed=(p?.accounts || []).filter(x=>!x.email && (!x.label || x.label===x.key));
  const n=unnamed.findIndex(x=>x.key===a?.key)+1;
  return `${p?.name || providerName(a?.provider)} account${n>1 ? ' '+n : ''}`;
}
// Every card's account actions sit behind one ⋯ button; the menu is a single popover (openCardMenu).
let cardMenu=null;
const accountActions = () => Array.isArray(current?.add_providers);
const menuId = (provider, key) => `menu-${provider}-${key}`;
function cardMenuButton(provider, a, name, attrs='') {
  if(!accountActions() || !a?.key)return '';
  const open=cardMenu?.provider===provider && cardMenu.key===a.key;
  return `<button type="button" class="card-menu" id="${esc(menuId(provider,a.key))}" data-menu-provider="${esc(provider)}" data-menu-key="${esc(a.key)}" aria-label="Account actions · ${esc(name)}" aria-haspopup="menu" aria-controls="card-menu" aria-expanded="${open}"${attrs ? ' '+attrs : ''}>${icon('more')}</button>`;
}
const hasRuntimeId = value => value!=null && String(value).trim()!=='';
function providerAccount(provider, id, email, key) {
  const p=providerData().find(p=>p.id===provider);
  // A recorded filename is authoritative, including when that credential has been removed.
  if(key)return p?.accounts.find(a=>a.key===key);
  const named=hasRuntimeId(id) && p?.accounts.find(a=>a.key===id);
  if(named)return named;
  const runtime=provider==='claude' && hasRuntimeId(id) ? (current?.gateway.claude_accounts || []).find(a=>a.auth_index===id) : null;
  email=email || runtime?.email;
  return (hasRuntimeId(id) && p?.accounts.find(a=>a.id===id || a.auth_index===id)) ||
    (email && p?.accounts.find(a=>a.email===email));
}
function claudeEmail(id) { return providerAccount('claude',id)?.email; }
function series(provider, id, email, key) {
  const a=providerAccount(provider,id,email,key);
  return provider+':'+(a?.key || a?.id || a?.email || 'removed:'+(key || (hasRuntimeId(id) ? id : email || 'unknown')));
}
function seriesColor(key) {
  const palettes={codex:['var(--codex)','var(--codex-2)','#b497d8','#afa0ea'],claude:['var(--claude)','var(--claude-2)','#e9a77e','#b9836f'],
    xai:['var(--xai)','var(--xai-2)','#b4b7bd','#7d8087'],other:['var(--blue)','#5f9bd6','#a9d2ff','#4a7cb0']};
  const palette=palettes[accentOf(key.split(':')[0])];
  let hash=0;for(const char of key)hash=(Math.imul(hash,31)+char.charCodeAt(0))>>>0;
  return palette[hash%palette.length];
}
function seriesDot(key) { return `<i data-series="${esc(key)}" style="background:${seriesColor(key)}"></i>`; }
// Until the gateway's first snapshot arrives, account routing and identities are unknown, not off.
function gatewayLoading(data=current) { return data?.gateway?.online == null; }
function providerChip(provider, id, email, key) {
  const a=providerAccount(provider,id,email,key);
  const name=a ? accountLabel(a) : email || (gatewayLoading() ? '—' : 'Removed account');
  return `<span class="account-chip" title="${esc(providerName(provider))} · ${esc(name)}">${seriesDot(series(provider,id,email,key))}${esc(name)}</span>`;
}
function countdown(epoch) {
  if (!epoch) return 'Not reported';
  const seconds = Math.ceil(epoch - Date.now()/1000);
  if (seconds <= 0) return 'Reset due · refreshing';
  const hours = Math.floor(seconds/3600), days = Math.floor(hours/24);
  return days ? `${days}d ${hours%24}h` : hours ? `${hours}h ${Math.ceil((seconds%3600)/60)}m` : `${Math.ceil(seconds/60)}m`;
}
function pace(w, remaining) {
  if (remaining == null || !(w?.seconds > 0) || !Number.isFinite(w.resets_at)) return null;
  const left = w.resets_at - Date.now()/1000;
  if (left <= 0) return null;
  const time = Math.min(100, left / w.seconds * 100);
  const n = Math.round(Math.abs(remaining - time)), over = remaining < time;
  return {
    time,
    text: n < 5 ? 'On pace' : `${n}% ${over ? 'over' : 'under'} pace`,
    warn: over && n >= 15,
    under: remaining > time,
    title: `${Math.round(time)}% of the week left · ${Math.round(remaining)}% of the allowance left`
  };
}
function paceBar(p) { return p ? `<i class="pace-bar${p.under ? '' : ' pace-behind'}" style="width:${p.time.toFixed(2)}%" aria-hidden="true"></i>` : ''; }
function paceLabel(p) { return p ? `<span class="pace${p.warn ? ' pace-warn' : ''}" title="${esc(p.title)}">${esc(p.text)}</span>` : ''; }
function paceValueText(p, remaining) { return p ? ` aria-valuetext="${Math.round(remaining)}% remaining, ${Math.round(p.time)}% of the week left"` : ''; }

// Secondary allowance windows sit under the weekly hero as slim bars; a missing window reads "—".
function limitRow(label, w) {
  const left = w ? w.remaining ?? (w.used == null ? null : Math.max(0, 100 - w.used)) : null;
  return `<div class="limit-row"${w?.resets_at ? ` title="Resets ${esc(dateTime(w.resets_at*1000))}"` : ''}><div class="secondary-quota"><span>${esc(label)}</span><span>${left == null ? '—' : esc(percent(left)) + ' remaining'}</span></div><div class="limit-track${left == null ? ' empty' : ''}" aria-hidden="true"><span style="width:${Math.min(100, left ?? 0)}%"></span></div></div>`;
}
function relative(value) {
  if (!value) return 'Waiting for first refresh';
  const diff = Math.max(0,Math.round((Date.now()-new Date(value).getTime())/1000));
  return diff < 15 ? 'Updated just now' : diff < 60 ? `Updated ${diff}s ago` : diff < 3600 ? `Updated ${Math.floor(diff/60)}m ago` : diff < 86400 ? `Updated ${Math.floor(diff/3600)}h ago` : `Updated ${Math.floor(diff/86400)}d ago`;
}
function currency(amount, code) {
  if (amount == null) return '—';
  try { return new Intl.NumberFormat(undefined,{style:'currency',currency:code || 'USD',maximumFractionDigits:2}).format(amount); }
  catch (_) { return `${number(amount)} ${code || ''}`.trim(); }
}
function watchState(a) {
  if (failed) return {text: 'Status stale', tone: 'warn'};
  if(a.watch_observed===false)return {text:a.disabled?'Disabled':a.availability?.state==='ready'?'Ready to route · no usage data':availabilityLabel(a,current?.gateway || {}),tone:'warn'};
  if (a.independent_windows===true && a.status === 'missing_token') return {text: 'Sign-in token missing', tone: 'down'};
  if (a.independent_windows===true && (a.status === 'error' || a.status === 'unavailable')) return {text: 'Unavailable', tone: 'down'};
  let checked = Date.parse(a.checked_at);
  if (a.independent_windows===true) {
    const measured = (Array.isArray(a.windows) ? a.windows : [])
      .filter(w => ['five_hour', 'seven_day'].includes(w.kind))
      .map(w => Date.parse(Object.hasOwn(w, 'measured_at') ? w.measured_at : a.checked_at));
    checked = measured.length && measured.every(Number.isFinite) ? Math.min(...measured) : NaN;
  }
  const updated = a.independent_windows===true ? (Number.isFinite(checked) ? relative(checked) : '')
    : (a.checked_at ? relative(a.checked_at) : '');
  if (a.status === 'stale' || (Number.isFinite(checked) && Date.now()-checked > 15*60*1000) ||
      (a.status === 'ok' && !Number.isFinite(checked))) return {text: 'Stale' + (updated ? ' · ' + updated.charAt(0).toLowerCase() + updated.slice(1) : ''), tone: 'warn'};
  if (a.status === 'ok') return {text: updated || 'Waiting for first read', tone: 'ok'};
  if (a.status === 'expired_token') return {text: 'Waiting for Claude Code to refresh sign-in', tone: 'warn'};
  return {text: 'Unavailable', tone: 'down'};
}
function extraUsageText(extra) {
  if (!extra?.enabled) return 'Off';
  if (extra.used != null && extra.limit != null) return `${currency(extra.used,extra.currency)} of ${currency(extra.limit,extra.currency)}`;
  return extra.used != null ? `${currency(extra.used,extra.currency)} used` : 'On';
}
function watchFor(id) { return (current?.watch_accounts || []).find(a => a.id === id) || providerData().find(p=>p.id==='claude')?.accounts.map(a=>({...a,id:a.watch_id || a.key})).find(a=>a.id===id); }
function watchResets(a) { return a?.resets && typeof a.resets === 'object' ? a.resets : null; }
function watchGrants(r) { return Array.isArray(r?.grants) ? r.grants.filter(g => g && typeof g === 'object') : []; }
function grantExpired(g, now=Date.now()/1000) { return Number.isFinite(g.ends_at) && g.ends_at<=now; }
function watchAvailable(r) {
  return watchGrants(r).filter(g=>g.paused!==true && !grantExpired(g)).reduce((n,g)=>n+(g.resets_left || 0),0);
}
function grantLabel(g) { return g?.label || {full:'Full reset', five_hour:'5-hour reset'}[g?.kind] || 'Reset'; }
function watchPending(a) {
  const r = watchResets(a);
  if (r?.pending?.grant_id) return r.pending;
  const local = watchPendingGrants.get(a?.id);
  if (local && r?.last_event?.grant_id === local && r.last_event.outcome && r.last_event.outcome !== 'uncertain') { watchPendingGrants.delete(a.id); return null; }
  return local ? {grant_id: local, state: 'uncertain'} : null;
}
function cooldownActive(r) { return Number.isFinite(r?.cooldown_until) && r.cooldown_until*1000 > Date.now(); }
function watchResetSurfaceReason(r) {
  return r?.eligible===false && r.ineligible_reason==='surface' ? 'Use the Claude app for resets' : '';
}
function watchResetsNote(a, r) {
  const surface=watchResetSurfaceReason(r);
  if (surface) return surface;
  const pending = watchPending(a);
  if (r.service === 'unavailable') return 'Reset service unavailable';
  if (pending) return pending.state === 'uncertain' ? 'Reset not confirmed' : 'Reset in progress';
  if (r.eligible === false) return 'Not eligible';
  if (cooldownActive(r)) return `Cooldown until ${dateTime(r.cooldown_until*1000)}`;
  return r.automatic ? 'Auto-use before expiry' : '';
}
function claudeUsageFresh(a, now=Date.now()) {
  const checked = Date.parse(a.checked_at);
  if (a.status !== 'ok' || a.error || !Number.isFinite(checked) || now-checked < -30000 || now-checked > 900000) return false;
  const windows = Array.isArray(a.windows) ? a.windows : [];
  return ['five_hour','seven_day'].every(kind => {
    const primary = windows.filter(w => w.kind === kind);
    if (primary.length !== 1) return false;
    const measured = Date.parse(Object.hasOwn(primary[0],'measured_at') ? primary[0].measured_at : a.checked_at);
    return Number.isFinite(measured) && now-measured >= -30000 && now-measured <= 900000;
  }) && windows.every(w => {
    if (w.invalid_reset) return false;
    const left = w.remaining ?? (w.used === 0 ? 100 : null);
    return Number.isFinite(left) && left >= 0 && left <= 100 &&
      (w.resets_at == null && left === 100 || Number.isFinite(w.resets_at) && w.resets_at*1000 > now && w.resets_at*1000 <= now+8*86400000);
  });
}
function claudeRoutingState(a) {
  return a.routing_id && typeof a.routing_enabled === 'boolean' ? a.routing_enabled : null;
}
function claudeReadiness(a) {
  const enabled = claudeRoutingState(a);
  if (enabled == null) return 'unknown';
  if (!enabled) return 'off';
  if (a.runtime_available === false) return 'unavailable';
  if (a.runtime_available !== true || !claudeUsageFresh(a)) return 'unknown';
  return a.windows.every(w => (w.remaining ?? (w.used === 0 ? 100 : 0)) > 0) ? 'ready' : 'limited';
}
function claudeRoutable(a) {
  return claudeReadiness(a) === 'ready';
}
function reconcileClaude(accounts, generation=null) {
  const ids = new Set(accounts.map(a => a.id)), identities = new Set(accounts.filter(a => claudeRoutingState(a) != null).map(a => a.routing_id));
  for (const id of claudeRouteConfirm) if (!identities.has(id)) claudeRouteConfirm.delete(id);
  for (const [id, state] of claudeRouteErrorState) {
    const a = accounts.find(a => a.id === id);
    if (!a || a.routing_id !== state.identity || generation > state.after && claudeRoutingState(a) === state.enabled) {
      claudeRouteErrors.delete(id);claudeRouteErrorState.delete(id);
    }
  }
  for (const id of claudeRouteErrors.keys()) if (!ids.has(id)) claudeRouteErrors.delete(id);
}
function operationFocus(id) {
  const active = document.activeElement;
  return active === document.body || active?.id === id || active?.id === id.replace('claude-route-','claude-route-off-');
}
function claudeRoutingRow(a) {
  const enabled = claudeRoutingState(a), pending = claudeRoutePending.has(a.id), known = enabled != null, loading = gatewayLoading();
  const offline = failed || current?.gateway?.online !== true;
  const disabled = !known || current?.read_only || offline || pending;
  const title = loading ? 'Checking routing' : !known ? 'Account identity not verified' : current?.read_only ? 'Change routing from your computer dashboard' : offline ? 'Gateway unreachable' : '';
  return `<div class="account-route"><label for="claude-route-${esc(a.id)}">Use for routing</label><input id="claude-route-${esc(a.id)}" data-claude-route="${esc(a.id)}" type="checkbox" role="switch" ${loading || !known ? 'class="unknown" aria-checked="mixed"' : enabled ? 'checked' : ''} ${disabled ? 'disabled' : ''} ${pending ? 'aria-busy="true"' : ''} ${title ? `title="${title}"` : ''}></div>
    ${claudeRouteConfirm.has(a.routing_id) ? `<div class="route-confirm" role="alert"><span>No other Claude account is ready.</span><div><button type="button" class="reset-primary confirm" id="claude-route-off-${esc(a.id)}" data-claude-route-off="${esc(a.id)}">Turn off</button><button type="button" class="reset-cancel" id="claude-route-keep-${esc(a.id)}" data-claude-route-keep="${esc(a.id)}">Keep on</button></div></div>` : ''}
    ${claudeRouteErrors.has(a.id) ? `<div class="fast-usage error" role="status">${esc(claudeRouteErrors.get(a.id))}</div>` : ''}`;
}
function renderWatch(data) {
  const accounts = data.providers ? (data.providers.find(p=>p.id==='claude')?.accounts || []).map(a=>({...a,id:a.watch_id || a.key})) : Array.isArray(data.watch_accounts) ? data.watch_accounts : [];
  reconcileClaude(accounts);
  $('watch-section').hidden = data.providers ? !data.providers.some(p=>p.id==='claude') : !accounts.length && !data.gateway?.claude_accounts?.length && !data.gateway?.claude_preference?.saved_mode;
  const routes = data.gateway?.claude_accounts;
  $('claude-account-count').textContent = Array.isArray(routes) && routes.length
    ? `${routes.filter(r => !r.disabled && r.status !== 'disabled').length} / ${routes.length}`
    : accounts.length ? `${accounts.filter(a => a.status === 'ok').length} / ${accounts.length}` : '—';
  const ready = accounts.filter(claudeRoutable).length;
  const checked = Date.parse(data.gateway?.checked_at);
  const unknown = failed || data.gateway?.online !== true || !Number.isFinite(checked) || Date.now()-checked < -30000 || Date.now()-checked > 180000 ||
    accounts.some(a => claudeReadiness(a) === 'unknown');
  const note = !accounts.length || gatewayLoading(data) ? '' : unknown ? 'Status unavailable · checking' : ready === 0 ? 'No accounts ready' : ready < accounts.length ? 'One account ready' : 'All accounts ready';
  $('claude-status').innerHTML = note ? `<span class="status-item ${unknown ? 'warn' : ready === accounts.length ? 'ok' : ready ? 'warn' : 'down'}">${esc(note)}</span>` : '';
  if (!accounts.length) { $('watch-grid').innerHTML = ''; return; }
  const focused = claudeNextFocus || (document.activeElement?.closest?.('#watch-grid') ? document.activeElement.id : '');
  claudeNextFocus = '';
  const hasResets = a => { const r = watchResets(a); return !!(r && r.eligible===true && watchGrants(r).length); };
  // Rows below the facts line up across cards: a card without resets keeps an empty row when another card shows them.
  const resetsRow = accounts.some(hasResets);
  $('watch-grid').innerHTML = accounts.map(a => {
    const label = a.label || a.email || a.key || 'Claude';
    const name = a.email || label;
    const state = watchState(a);
    const windows = (Array.isArray(a.windows) ? a.windows : []).map(w =>
      a.independent_windows===true && a.status === 'missing_token'
        ? {...w, used: null, remaining: null, resets_at: null}
        : (a.independent_windows===true || w.usage_source==='gateway') && Number.isFinite(w.resets_at) && w.resets_at <= Date.now()/1000
          ? {...w, used: null, remaining: null} : w);
    const note = a.attempted_at ? `Last attempt ${dateTime(a.attempted_at)}${a.error ? ' · ' + a.error : ''}` : '';
    const r = watchResets(a);
    if(r)r.available=watchAvailable(r);
    const resets = !hasResets(a) ? (resetsRow ? '<div class="account-resets reserved" aria-hidden="true"><span class="resets-button">Resets</span></div>' : '') : `<div class="account-resets"><button type="button" class="resets-button" data-reset-open="${esc(a.id)}" aria-haspopup="dialog">Resets <span>${r.available==null?'—':esc(r.available)}</span></button><span>${esc(watchResetsNote(a,r))}</span></div>`;
    const w = windows.find(x => x.kind === 'seven_day') || windows[0];
    const five = windows.find(x => x.kind === 'five_hour');
    const fable = windows.find(x => x.kind === 'weekly_model' && /fable/i.test(x.model || x.label || ''));
    // Both secondary limits always show, in the same order, so the cards line up.
    const limits = [['5-hour limit', five], ['Weekly · Fable', fable]].filter(([, x]) => !x || x !== w).map(([fallback, x]) => limitRow(x?.label || fallback, x))
      .concat(windows.filter(x => x !== w && x !== five && x !== fable).map(x => limitRow(x.label || 'Usage window', x)));
    const remaining = w?.remaining ?? (w?.used == null ? null : Math.max(0, 100 - w.used));
    const stale = state.tone !== 'ok';
    const p = !stale && w?.kind === 'seven_day' ? pace({...w, seconds: w.seconds ?? 604800}, remaining) : null;
    return `<article data-provider="claude" data-account-key="${esc(a.key || a.id)}" class="account-card claude-card${claudeRoutingState(a) === false && !gatewayLoading(data) ? ' is-off' : ''}">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(name)}">${esc(name)}</span></div></div>${a.plan?`<span class="small-badge">${esc(a.plan)}</span>`:''}${cardMenuButton('claude',a,name)}</div>
      <div class="account-body"><div class="account-hero"><div class="quota-label">${esc(w?.label || 'Subscription allowance')}${stale ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(Math.round(remaining)))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="${esc(label)} · ${esc(name)} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${Math.round(remaining)}"`}${paceValueText(p, remaining)}><span style="width:${Math.min(100,remaining ?? 0)}%"></span>${paceBar(p)}</div>
      <div class="quota-used">${paceLabel(p)}<span>${w?.used == null ? 'Waiting for usage data' : esc(percent(w.used))+' used'}</span></div></div>
      <div class="account-side"><div class="limit-rows">${limits.join('')}</div>
      <div class="account-bottom"><div class="account-dates"><div class="reset-block"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div>${a.extra_usage ? `<div class="reset-block"><span>Extra usage</span><strong>${esc(extraUsageText(a.extra_usage))}</strong></div>` : ''}</div><span class="account-state watch-state-${state.tone}" title="${esc(state.text + (note ? ' · ' + note : ''))}">${icon(state.tone==='ok'?'check':'alert')}<span>${esc(state.text)}</span></span></div>
      ${resets}
      ${claudeRoutingRow(a)}</div></div>
    </article>`;
  }).join('');
  if (focused) document.getElementById(focused)?.focus();
}
function availabilityLabel(account, gateway) {
  const checked = Date.parse(gateway.checked_at);
  if (failed || !gateway.online || !Number.isFinite(checked) || Date.now()-checked>180000) return 'Status stale';
  if (account.access_state==='disable_unconfirmed') return 'Disable unconfirmed';
  if (account.disabled && account.access_reason) return account.access_reason;
  const labels = {ready:'Ready to route', quota_exhausted:'Quota exhausted', rate_limited:'Temporary rate limit', provider_unavailable:'Provider temporarily unavailable', auth_required:'Sign in required', disabled:'Disabled', unknown:'Status unknown'};
  const a = account.availability || {};
  return (labels[a.state] || 'Status unknown') + (a.scope==='model' ? ' · model cooldown' : '');
}
function routingSuspended(p) { return !!p && (p.suspended===true || p.status==='disabled'); }
function routingLabel(gateway) {
  const p=gateway.preference;
  if (!p) return gateway.routing?.strategy==='round-robin' ? 'Round-robin' : 'Routing unknown';
  if (routingSuspended(p)) return 'Routing · suspended';
  if (p.status==='held') return 'Routing · held';
  if (p.status==='fallback') return 'Routing · neutral fallback';
  if (p.status!=='active') return 'Routing · checking';
  return {'round-robin':'Round-robin','preferred-account':'Preferred account','earliest-weekly-reset':'Earliest reset','highest-remaining-quota':'Higher remaining quota'}[p.policy] || 'Routing unknown';
}
function elapsed(value) {
  const minutes=Math.floor((Date.now()-Date.parse(value))/60000);
  if (!Number.isFinite(minutes) || minutes<0) return '';
  return minutes<1 ? 'under a minute' : minutes<60 ? `${minutes}m` : `${Math.floor(minutes/60)}h ${minutes%60}m`;
}
function routingState(gateway) {
  const p=gateway.preference; if (!p) return '';
  if (routingSuspended(p)) return 'Suspended after a rollback · resume or choose a routing to continue';
  const reason=p.hold_reason || p.reason || '';
  if (p.status==='held') { const age=p.held_since ? elapsed(p.held_since) : ''; return 'Held' + (age ? ' for ' + age : '') + ' · ' + (reason || 'waiting for fresh quota data'); }
  if (p.status==='fallback') { const age=p.held_since ? elapsed(p.held_since) : ''; return 'Neutral fallback · ' + (reason || 'quota data held' + (age ? ' for ' + age : '')); }
  if (p.drift===true) {
    const actual=p.actual_priorities || {}, top=Math.max(0,...Object.values(actual).filter(Number.isFinite));
    const leaders=Object.keys(actual).filter(e=>actual[e]===top && top>0);
    const who=leaders.length===1 ? leaders[0] : '';
    return 'Gateway priorities differ from this selection' + (who ? ` · currently prefers ${who}` : '');
  }
  return '';
}
function routingValue(value, preference, provider='codex') {
  const prefix=provider==='claude'?'claude-routing':'routing';
  const valid=value=>!!value && Array.from($(prefix+'-mode').options).some(option=>option.value===value);
  const key=value=>current?.providers?.find(p=>p.id===provider)?.accounts.find(a=>a.email===value)?.key || value;
  if(valid(key(value)))return key(value);
  const saved=preference?.saved_mode==='preferred-account' ? preference.saved_preferred_email : preference?.saved_mode;
  return valid(key(saved)) ? key(saved) : 'earliest-weekly-reset';
}
function routingPayload(value, provider) {
  const p=current?.providers?.find(p=>p.id===(provider==='all'?'codex':provider));
  const account=p?.accounts.find(a=>a.key===value);
  if(account && provider==='all') {
    return {mode:'preferred-account', preferred_keys:Object.fromEntries(current.providers.filter(p=>p.routable).map(p=>[p.id,p.accounts.find(a=>a.email===account.email)?.key]))};
  }
  if(account)return {mode:'preferred-account',preferred_key:account.key};
  return value.includes('@') ? {mode:'preferred-account',preferred_email:value} : {mode:value};
}
function routingLocked() { return routingSaving || claudeRoutingSaving || routingAllSaving || routingLinkSaving; }
// "New conversations only" or "Move all conversations now": one small modal, opened by whichever routing row changed.
const routingRows = {codex:{row:'codex-routing',prefix:'routing'}, claude:{row:'claude-routing',prefix:'claude-routing'}, all:{row:'routing-all',prefix:'routing-all'}};
function routingPart(key, part) { return $(routingRows[key].prefix+'-'+part); }
function routingMoveAvailable() { return current?.gateway?.move_available===true; }
function routingTarget(key, value) {
  if(!value || !Array.from(routingPart(key,'mode').options).some(o=>o.value===value && !o.disabled))return null;
  if(['round-robin','earliest-weekly-reset','highest-remaining-quota'].includes(value))return value;
  const providers=providerData().filter(p=>key==='all' ? ['codex','claude'].includes(p.id) : p.id===key).sort((a,b)=>a.id.localeCompare(b.id));
  const account=providers.find(p=>p.id===(key==='all'?'codex':key))?.accounts.find(a=>a.key===value || a.email===value);
  if(!account)return null;
  const targets=providers.map(p=>p.id===(key==='all'?'codex':key) ? account : p.accounts.find(a=>a.email===account.email));
  if(targets.some(a=>!a))return null;
  return JSON.stringify(targets.map(a=>a.key || a.id || a.email));
}
function rejectRoutingChoice(key, value) {
  const target=routingTarget(key,value);
  if(target && (routingChoice?.key!==key || routingChoice.value===value && routingChoice.target===target))return false;
  closeRoutingChoice(true);
  routingPart(key,'mode').value=routingApplied(key);
  routingPart(key,'feedback').textContent='Choice unavailable · choose again';
  return true;
}
function routingApplied(key) {
  const gateway=current?.gateway || {};
  return key==='claude' ? routingSelection(gateway.claude_preference,'claude') : routingSelection(gateway.preference,'codex');
}
// With one configured Codex account there is nothing to route between, so only Claude keeps a routing choice.
// The count, not the account list: a retired credential stays listed until its file is removed. Older backends omit it.
function codexRoutable(gateway) { return current?.providers ? current.providers.some(p=>p.id==='codex' && p.routable) : typeof gateway?.codex_account_count!=='number' || gateway.codex_account_count>1; }
function claudePolicyRoutable() { return current?.providers ? current.providers.some(p=>p.id==='claude' && p.routable) : !$('watch-section').hidden; }
function routingRowHidden(key, gateway=current?.gateway) {
  const claude=claudePolicyRoutable(), codex=codexRoutable(gateway), linked=claude && codex && routingLinked(gateway);
  return key==='all' ? !linked : key==='codex' ? !codex || linked : !claude || linked;
}
function routingRowSaving(key) { return key==='all' ? routingAllSaving : key==='claude' ? claudeRoutingSaving : routingSaving; }
function routingChoiceTitle(key, value) {
  const option=Array.from(routingPart(key,'mode').options).find(option=>option.value===value);
  const account=current?.providers?.find(p=>p.id===(key==='all'?'codex':key))?.accounts.find(a=>a.key===value);
  return `Switch ${key==='all' ? 'routing' : key==='claude' ? 'Claude routing' : 'Codex routing'} to ${account ? 'prefer '+account.label : value.includes('@') ? 'prefer '+value : option?.textContent || value}?`;
}
// A move drops every pinned conversation on the gateway, so one gateway's move reaches the other's conversations too.
function routingMoveNote(key) {
  if(key==='codex' && !$('watch-section').hidden)return 'Also moves Claude conversations.';
  return key==='claude' && codexRoutable(current?.gateway) ? 'Also moves Codex conversations.' : '';
}
function closeRoutingChoice(revert=true, focus=false) {
  const choice=routingChoice;if(!choice)return;
  routingChoice=null;
  const select=routingPart(choice.key,'mode');
  if(revert)select.value=routingApplied(choice.key);
  select.classList?.remove?.('pending');
  if($('routing-dialog').open)$('routing-dialog').close();
  if(focus)select.focus();
}
function openRoutingChoice(key, value) {
  if(routingLocked())return;
  if(routingChoice && routingChoice.key!==key)closeRoutingChoice(true);
  const target=routingTarget(key,value);
  if(!target){rejectRoutingChoice(key,value);return;}
  routingRetry.delete(key);
  routingPart(key,'feedback').textContent='';
  if(value===routingApplied(key)){closeRoutingChoice(true);renderRoutingControls();return;}
  if(routingChoice){routingChoice.value=value;routingChoice.target=target;routingChoice.error=false;renderRoutingControls();return;}
  routingChoice={key,value,target,applying:null,error:false,focus:'new'};
  renderRoutingControls();
  if(!$('routing-dialog').open)$('routing-dialog').showModal();
  routingChoiceButton('new').focus();
}
function routingChoiceButton(kind) { return $('routing-dialog').querySelector(`[data-routing-choice="${kind}"]`); }
// A refresh closes an idle choice once it can no longer apply what it shows; an unconfirmed attempt's error moves to the row.
function expireRoutingChoice(key) {
  if(routingChoice?.key!==key || routingChoice.applying)return;
  if(failed || current?.read_only || current?.gateway?.online!==true || !routingMoveAvailable() || routingRowHidden(key) || routingApplied(key)===routingChoice.value ||
      !routingTarget(key,routingChoice.value) || routingTarget(key,routingChoice.value)!==routingChoice.target) {
    const error=routingChoice.error;
    closeRoutingChoice(true);
    if(error)routingPart(key,'feedback').textContent='Not confirmed · refresh and try again';
  }
}
// The modal's text only changes when its content does, so a refresh never re-announces the error.
function syncRoutingChoice(key) {
  const select=routingPart(key,'mode'), choice=routingChoice?.key===key ? routingChoice : null;
  select.classList?.toggle?.('pending',!!choice);
  if(!choice)return;
  const disabled=!!choice.applying || routingLocked() || select.disabled, note=routingMoveNote(key);
  const text=(id,value)=>{if($(id).textContent!==value)$(id).textContent=value;};
  text('routing-dialog-title',routingChoiceTitle(key,choice.value));
  text('routing-dialog-error',choice.error ? 'Change not confirmed. Try again.' : '');
  text('routing-dialog-note',note);$('routing-dialog-note').hidden=!note;
  for(const button of $('routing-dialog').querySelectorAll('[data-routing-choice]')) {
    button.disabled=disabled;
    if(choice.applying===button.dataset.routingChoice)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
  }
}
// An unconfirmed save keeps its open modal, with the error inside and the choices available again.
function routingChoiceFailed(key) {
  const choice=routingChoice;
  if(choice?.key!==key || !$('routing-dialog').open)return false;
  choice.error=true;choice.focus=choice.applying;choice.applying=null;
  routingPart(key,'feedback').textContent='';
  return true;
}
// A retry stays valid only while every preference the row posts for is live and still shows the retried value.
// A suspended routing shows its saved value, but re-posting it would resume routing, not retry a move.
function routingRetryValid(key) {
  const gateway=current?.gateway || {};
  const parts=key==='all' ? [gateway.preference,gateway.claude_preference] : [key==='claude' ? gateway.claude_preference : gateway.preference];
  return routingRetry.has(key) && routingApplied(key)===routingRetry.get(key) &&
    !parts.some(p=>routingSuspended(p) || ['failed','unconfirmed','error'].includes(p?.status));
}
function syncRoutingRetry(key) {
  const button=routingPart(key,'retry');
  if(routingRetry.has(key) && !routingRowSaving(key) && !routingRetryValid(key)){routingRetry.delete(key);routingPart(key,'feedback').textContent='';}
  button.hidden=!routingRetry.has(key) || !!current?.read_only;
  button.disabled=routingLocked() || !!routingChoice || routingPart(key,'mode').disabled;
}
// Like the move gate, only an active status means the selection is in effect; held, pending and fallback are not.
function routingNotApplied(part) { return !part || part.status!=='active' || part.applied===false || part.pending===true; }
function routingFeedback(key, value, move, pending, moved) {
  const feedback=routingPart(key,'feedback');
  if(!move){feedback.textContent=pending ? 'Saved · not applied yet' : 'Saved';return;}
  let text='Saved · move not confirmed';
  if(moved?.outcome==='moved')text='Saved · conversations moved';
  else if(moved?.outcome==='not_moved' || (!moved && pending))
    text=moved?.code==='not_applied' || !moved ? 'Saved · not applied yet · conversations not moved' : 'Saved · conversations not moved';
  // Try again re-runs every server-side check, so a retry that can't succeed just fails fast.
  const retry=moved?.outcome!=='moved' && moved?.code!=='unavailable';
  feedback.textContent=text;
  if(retry)routingRetry.set(key,value);else routingRetry.delete(key);
}
// Rows that appear or disappear (a link change here or elsewhere) drop their old Try again and move feedback;
// a row still applying keeps its own.
let routingLayout=null;
function syncRoutingLayout() {
  const layout=[routingLinked(current?.gateway),...Object.keys(routingRows).filter(key=>!routingRowHidden(key))].join();
  if(routingLayout!==null && layout!==routingLayout) {
    for(const key of Object.keys(routingRows))if(!routingRowSaving(key)){routingRetry.delete(key);routingPart(key,'feedback').textContent='';}
  }
  routingLayout=layout;
}
function renderRoutingControls() {
  if (!current) return;
  renderRouting(current.gateway);renderRouting(current.gateway,'claude');
  if(current.watch_accounts || current.gateway.claude_accounts)renderRoutingAll(current.gateway);
}
function renderRouting(gateway, provider='codex') {
  const claude=provider==='claude', prefix=claude?'claude-routing':'routing';
  const p=claude?gateway.claude_preference:gateway.preference, select=$(prefix+'-mode');
  const saving=claude?claudeRoutingSaving:routingSaving;
  syncRoutingLayout();
  if (claude) {
    for(const account of current?.watch_accounts || []) {
      const option=Array.from(select.options).find(option=>option.value===account.email);
      if(option)option.textContent=`Prefer ${account.email || account.label}`;
    }
  }
  expireRoutingChoice(provider);
  if (!saving && routingChoice?.key!==provider) {
    const value=routingValue(routingSuspended(p) ? null : p?.policy==='preferred-account' ? p.selected_email : p?.policy,p,provider);
    if (document.activeElement!==select || !select.value) select.value=value;
  }
  const locked=routingLocked() || failed || current?.read_only || !gateway.online;
  select.disabled=locked;
  const state=routingState({preference:p});
  $(prefix+'-state').hidden=!state; $(prefix+'-state').textContent=state;
  $(prefix+'-resume').hidden=!routingSuspended(p) || !!current?.read_only; $(prefix+'-resume').disabled=locked || !!routingChoice;
  const name=claude?'Claude':'Codex';
  $(prefix+'-help').hidden=!current?.read_only; $(prefix+'-help').textContent=current?.read_only ? `Change ${name} routing from your computer dashboard` : '';
  syncRoutingRetry(provider);syncRoutingChoice(provider);
}
// Focus returns to the select when it was in the row or the modal, or was dropped to the body by disabled controls;
// a modal kept open by an error gets it back on the choice that failed.
function routingFocused(key) {
  const active=document.activeElement;
  return operationFocus(routingPart(key,'mode').id) || $(routingRows[key].row).contains?.(active) || $('routing-dialog').contains?.(active);
}
function routingRefocus(key) {
  return ()=>{
    if(routingChoice?.key===key && $('routing-dialog').open)routingChoiceButton(routingChoice.focus).focus?.();
    else if(routingFocused(key))routingPart(key,'mode').focus?.();
  };
}
async function saveRouting(value, provider='codex', move=false) {
  if(routingLocked())return;
  if(rejectRoutingChoice(provider,value))return;
  routingEpoch++;
  const claude=provider==='claude', prefix=claude?'claude-routing':'routing', key=claude?'claude_preference':'preference';
  const select=$(prefix+'-mode'), refocus=routingFocused(provider) ? routingRefocus(provider) : null;
  select.value=value;
  const body=routingPayload(value,provider);
  if(move)body.move=true;
  routingRetry.delete(provider);
  if(claude)claudeRoutingSaving=true;else routingSaving=true;
  renderRoutingControls(); $(prefix+'-feedback').textContent='Applying…';
  try {
    const result=await request(claude?'/api/claude-routing':'/api/routing',{method:'POST',body:JSON.stringify(body)});
    const {move:moved,...part}=result && typeof result==='object' ? result : {};
    if(current && result && typeof result==='object')current.gateway[key]={...current.gateway[key],...part};
    routingFeedback(provider,value,move,routingNotApplied(result),moved);
  } catch (_) {
    if(!routingChoiceFailed(provider))$(prefix+'-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key===provider && !routingChoice.error)closeRoutingChoice(false);
    await load(true);
    if(claude)claudeRoutingSaving=false;else routingSaving=false;
    renderRoutingControls();
    refocus?.();
  }
}
for(const [provider,{prefix}] of Object.entries(routingRows).filter(([key])=>key!=='all')) {
    $(prefix+'-mode').addEventListener('change', ()=>routingMoveAvailable() ? openRoutingChoice(provider,$(prefix+'-mode').value) : saveRouting($(prefix+'-mode').value,provider));
  $(prefix+'-resume').addEventListener('click', ()=>saveRouting($(prefix+'-mode').value,provider));
}
// One routing choice covers both gateways unless the user separates them.
function routingLinked(gateway) { return gateway?.routing_linked!==false; }
function routingSelection(p, provider) { return routingValue(routingSuspended(p) ? null : p?.policy==='preferred-account' ? p.selected_email : p?.policy,p,provider); }
function renderRoutingAll(gateway) {
  const both=claudePolicyRoutable() && codexRoutable(gateway), linked=!routingRowHidden('all',gateway);
  syncRoutingLayout();expireRoutingChoice('all');
  for(const key of Object.keys(routingRows))$(routingRows[key].row).hidden=routingRowHidden(key,gateway);
  const split=$('routing-split'), toggle=$('routing-link'), host=linked?$('routing-all'):$('claude-routing');
  if(split.parentElement!==host) {
    const focused=document.activeElement===toggle;
    // Keep the switch on the select's line, ahead of the full-width state lines.
    host.querySelector('.routing-state,.routing-states').before(split);
    if(focused)toggle.focus();
  }
  split.hidden=!both;
  const locked=routingLocked() || failed || current?.read_only || !gateway.online;
  toggle.checked=!routingLinked(gateway);
  toggle.disabled=locked || !!routingChoice || routingLinkSaving || routingAllSaving || routingSaving || claudeRoutingSaving;
  if(current?.read_only)toggle.title='Change routing from your computer dashboard';else toggle.removeAttribute?.('title');
  $('routing-all-mode').disabled=locked; $('routing-all-resume').disabled=locked || !!routingChoice;
  if(!linked)return;
  const select=$('routing-all-mode'), codex=gateway.preference, claude=gateway.claude_preference;
  if(!routingAllSaving && routingChoice?.key!=='all' && (document.activeElement!==select || !select.value))select.value=routingSelection(codex,'codex');
  select.disabled=locked || routingAllSaving || routingLinkSaving;
  const states=[['Codex',routingState({preference:codex})],['Claude',routingState({preference:claude})]].filter(([,text])=>text);
  $('routing-all-state').hidden=!states.length;
  $('routing-all-state').innerHTML=states.map(([name,text])=>`<span class="routing-state"><b>${name}</b>${esc(text)}</span>`).join('');
  $('routing-all-resume').hidden=!(routingSuspended(codex) || routingSuspended(claude)) || !!current?.read_only; $('routing-all-resume').disabled=select.disabled || !!routingChoice;
  $('routing-all-help').hidden=!current?.read_only; $('routing-all-help').textContent=current?.read_only ? 'Change routing from your computer dashboard' : '';
  syncRoutingRetry('all');syncRoutingChoice('all');
}
async function saveRoutingAll(value, relinking=false, move=false) {
  if(routingLocked() && !(relinking && routingLinkSaving && !routingAllSaving))return;
  if(rejectRoutingChoice('all',value))return;
  routingEpoch++;
  const select=$('routing-all-mode'), refocus=!relinking && routingFocused('all') ? routingRefocus('all') : null;
  select.value=value;
  const body=routingPayload(value,'all');
  if(move)body.move=true;
  routingRetry.delete('all');
  routingAllSaving=true; renderRoutingControls(); $('routing-all-feedback').textContent='Applying…';
  try {
    const result=await request('/api/routing-all',{method:'POST',body:JSON.stringify(body)});
    const pending=[];
    for(const [name,key] of [['codex','preference'],['claude','claude_preference']]) {
      const part=result?.[name];
      if(current && part && typeof part==='object')current.gateway[key]={...current.gateway[key],...part};
      pending.push(routingNotApplied(part));
    }
    routingFeedback('all',value,move,pending.some(Boolean),result?.move);
  } catch (_) {
    if(!routingChoiceFailed('all'))$('routing-all-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key==='all' && !routingChoice.error)closeRoutingChoice(false);
    await load(true);
    routingAllSaving=false;
    renderRoutingControls();
    refocus?.();
  }
}
async function saveRoutingLink(linked) {
  if(routingLocked())return;
  routingEpoch++;
  const feedback=$(linked?'claude-routing-feedback':'routing-all-feedback');
  const start=$('routing-mode').value, refocus=document.activeElement===$('routing-link');
  if(linked && !routingTarget('all',start)) {
    renderRoutingControls();
    feedback.textContent='Choose a policy or account available to both providers';
    return;
  }
  routingLinkSaving=true; renderRoutingControls(); feedback.textContent='Applying…';
  try {
    const result=await request('/api/routing-link',{method:'POST',body:JSON.stringify({linked})});
    if(result?.linked!==linked)throw new Error('Not confirmed');
    if(current)current.gateway.routing_linked=linked;
    feedback.textContent='';
    // Keep every routing control locked until applying both providers and reading them back.
    if(linked)await saveRoutingAll(start,true);
    else {routingEpoch++;await load(true);}
  } catch (_) {
    feedback.textContent='Not confirmed · refresh and try again';
    routingEpoch++;await load(true);
  } finally {
    routingLinkSaving=false;renderRoutingControls();
    if(refocus && operationFocus('routing-link'))$('routing-link').focus();
  }
}
$('routing-all-mode').addEventListener('change', ()=>routingMoveAvailable() ? openRoutingChoice('all',$('routing-all-mode').value) : saveRoutingAll($('routing-all-mode').value));
$('routing-all-resume').addEventListener('click', ()=>saveRoutingAll($('routing-all-mode').value));
$('routing-link').addEventListener('change', ()=>saveRoutingLink(!$('routing-link').checked));
function routingChoiceApply(key, kind) {
  const choice=routingChoice;
  if(!choice || choice.key!==key || choice.applying || routingLocked())return;
  choice.applying=kind;choice.error=false;
  const move=kind==='move';
  return key==='all' ? saveRoutingAll(choice.value,false,move) : saveRouting(choice.value,key,move);
}
// Try again re-applies the row's current selection with a move, through the normal save and lock.
function retryRoutingMove(key) {
  if(routingLocked() || routingChoice || !routingRetry.has(key))return;
  if(!routingRetryValid(key)){renderRoutingControls();return;}
  const value=routingRetry.get(key);
  return key==='all' ? saveRoutingAll(value,false,true) : saveRouting(value,key,true);
}
for(const key of Object.keys(routingRows))routingPart(key,'retry').addEventListener('click',()=>retryRoutingMove(key));
// Cancel, Escape and a backdrop click revert the select, except while a choice is applying.
function cancelRoutingChoice() {
  if(!routingChoice || routingChoice.applying)return;
  closeRoutingChoice(true,true);renderRoutingControls();
}
$('routing-dialog').addEventListener('click',event=>{
  const dialog=$('routing-dialog'), button=event.target.closest?.('[data-routing-choice]');
  if(!routingChoice)return;
  if(!button){
    const r=dialog.getBoundingClientRect();
    if(event.target===dialog && (event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom))cancelRoutingChoice();
    return;
  }
  if(button.disabled)return;
  if(button.dataset.routingChoice==='cancel')cancelRoutingChoice();
  else routingChoiceApply(routingChoice.key,button.dataset.routingChoice);
});
// Tab wraps inside the modal instead of leaving for the browser's own controls.
$('routing-dialog').addEventListener('keydown',event=>{
  const buttons=[...$('routing-dialog').querySelectorAll('[data-routing-choice]')].filter(button=>!button.disabled);
  const edge=event.shiftKey ? buttons[0] : buttons.at(-1);
  if(event.key==='Tab' && edge && document.activeElement===edge){event.preventDefault();(event.shiftKey ? buttons.at(-1) : buttons[0]).focus();}
});
$('routing-dialog').addEventListener('cancel',event=>{if(routingChoice?.applying)event.preventDefault();});
// The browser can still close it (a repeated Escape); an applying choice then reports in its row.
$('routing-dialog').addEventListener('close',()=>{if(!$('routing-dialog').open)cancelRoutingChoice();});
const signinBusy = new Set(), signinErrors = new Map(), copiedCodes = new Map();
const routePending = new Set(), routeErrors = new Map(), routeConfirm = new Map(), signinPanels = new Map();
let accessFocus = null, accessNextFocus = null;
const outcomeText = {
  start_failed:"Couldn't reach OpenAI.", provider_error:'OpenAI sign-in failed.', expired:'Code expired.',
  declined:'Sign-in was declined at OpenAI.', save_failed:"Couldn't save the new sign-in.",
  wrong_workspace:'Wrong ChatGPT workspace. Nothing was changed.',
  invalid_identity:'Could not verify the sign-in identity. Nothing was changed.',
  identity_unavailable:'Saved account identity unavailable. Nothing was changed.',
  saved_unconfirmed:"Saved, but the gateway hasn't confirmed it yet.",
  gateway_untested:'Gateway version not validated for sign-in. Nothing was changed.',
  save_unavailable:"Dashboard can't save sign-ins. Nothing was changed."
};
// Another click cannot succeed for these until the outcome expires.
const signinBlocked = ['save_unavailable','gateway_untested','identity_unavailable'];
// A confirmed sign-in newer than the account's last check makes its sign_in_required stale.
function signinSuperseded(a) { const outcome=signinView(a).last_outcome; return outcome?.kind==='signed_in' && Date.parse(a.quota_attempted_at)/1000<outcome.at; }
function signinNeeded(a) { return (a.auth_state==='sign_in_required' || a.availability?.state==='auth_required') && !signinSuperseded(a); }
function signinRetry(a) { return signinNeeded(a) || signinView(a).retry_allowed===true; }
function signinDetail(a, outcome) {
  return signinErrors.get(a.id)?.message || (outcome?.kind==='wrong_account' ? `Wrong account: ${outcome.approved_email}. Nothing was changed.` :
    outcomeText[outcome?.kind]);
}
// The sign-in button's label, or null when the card offers none.
function signinButton(a) {
  const outcome=signinView(a).last_outcome;
  if(!signinRetry(a) || signinBlocked.includes(outcome?.kind))return null;
  return signinBusy.has(a.id) ? 'Starting…' :
    signinErrors.has(a.id) || outcome?.kind!=='saved_unconfirmed' && signinDetail(a,outcome) ? 'Try again' : 'Sign in';
}
function accessBlocked(gateway) { return current?.read_only || failed || gateway.online!==true; }
function confirmRouting(a, after=null) { routeConfirm.set(a.id,after); }
function accountError(errors, id, message) { errors.set(id,{message,after:fastLoadEpoch}); }
function reconcileAccess(gateway, previous=null, generation=null) {
  const accounts=gateway.accounts || [], known=new Set(accounts.map(a=>a.id));
  for(const errors of [signinErrors,routeErrors]) {
    for(const [id,error] of errors)if(generation>error.after)errors.delete(id);
  }
  for(const map of [signinErrors,routePending,routeErrors,routeConfirm,signinPanels]) {
    for(const id of map.keys())if(!known.has(id))map.delete(id);
  }
  const handles=new Set();
  for(const a of accounts) {
    const session=signinView(a).session;
    if(session)handles.add(session.handle);
    const old=previous?.accounts?.find(other=>other.id===a.id);
    const anotherReady=accounts.some(other=>other.id!==a.id && other.active && !other.disabled && other.availability?.state==='ready');
    if(routeConfirm.has(a.id) && (routeConfirm.get(a.id)===null || generation>routeConfirm.get(a.id)) &&
        (accessBlocked(gateway) || signinNeeded(a) || a.disabled ||
        anotherReady || previous && JSON.stringify(old)!==JSON.stringify(a)))routeConfirm.delete(a.id);
  }
  for(const [handle,until] of copiedCodes)if(until<=Date.now() || !handles.has(handle))copiedCodes.delete(handle);
}
function signinView(a) {
  const view=a.signin || {};
  return view.last_outcome && Date.now()/1000-view.last_outcome.at>=600 ? {...view,last_outcome:null,retry_allowed:false}:view;
}
function signinPanel(a, view, gateway) {
  const s=view.session, finishing=s.state==='finishing';
  const remaining=Math.max(0,Math.ceil(s.expires_at-Date.now()/1000));
  const error=signinErrors.get(a.id)?.message;
  const status=failed ? 'Connection lost · retrying' : error || (finishing ? 'Approved · finishing' :
    remaining===0?'Code expired · checking':`Waiting for approval · ${remaining<120 ? remaining+' s' : Math.ceil(remaining/60)+' min'} left`);
  const disabled=finishing || remaining===0 || current?.read_only;
  const focus=action=>`data-access-focus="${esc(a.id+':'+action)}"`;
  const dis=disabled?' aria-disabled="true" tabindex="-1"':'';
  return `<div class="signin-panel${finishing?' finishing':''}" data-signin-handle="${esc(s.handle)}" role="group" aria-label="Sign in to ${esc(a.email)}">
    <div class="signin-as">Sign in as <b>${cardName(s.expected_email)}</b></div>
    <div class="signin-code"><code tabindex="0" ${focus('code')} data-signin-code="${esc(a.id)}" aria-label="Code ${esc(s.user_code.split('').join(' '))}">${esc(s.user_code)}</code><button type="button" class="resets-button" data-signin-copy="${esc(a.id)}" ${focus('copy')} ${disabled?'disabled':''}>${copiedCodes.get(s.handle)>Date.now()?icon('check')+'Copied':'Copy'}</button></div>
    <div class="signin-actions"><a class="signin-open" data-signin-open="${esc(a.id)}" ${focus('open')} href="${esc(s.verification_url)}" target="_blank" rel="noreferrer"${dis}>Open sign-in page ${icon('arrow')}</a>${finishing?'':`<button type="button" class="reset-cancel" data-signin-cancel="${esc(a.id)}" ${focus('cancel')} ${signinBusy.has(a.id)||current?.read_only?'disabled':''}>Cancel</button>`}</div>
    <div class="signin-status${failed||error||(!finishing&&remaining<120)?' warn':''}" role="status"><i aria-hidden="true"></i>${esc(status)}</div>
  </div>`;
}
function signinStrip(a, gateway, outcome) {
  const terminal=outcome?.kind!=='saved_unconfirmed' && (outcomeText[outcome?.kind] || outcome?.kind==='wrong_account');
  if(!signinNeeded(a) && !terminal)return '';
  const starting=signinBusy.has(a.id), label=signinButton(a), detail=signinDetail(a,outcome);
  const title=current?.read_only ? 'Sign in from your computer dashboard' : failed||gateway.online!==true ? 'Gateway unreachable' : '';
  return `<div class="account-access critical">
    <div class="account-access-row"><span>${icon('alert')}${signinNeeded(a)?'Sign in required':'Sign-in not completed'}</span>${label?`<button type="button" class="resets-button" data-signin-start="${esc(a.id)}" data-access-focus="${esc(a.id+':start')}" ${starting?'aria-busy="true"':''} ${starting||accessBlocked(gateway)?'disabled':''} ${title?`title="${title}"`:''}>${label}</button>`:''}</div>
    ${detail?`<span class="account-access-detail" role="status">${esc(detail)}</span>`:''}
  </div>`;
}
function routingRow(a, gateway) {
  const pending=routePending.has(a.id), required=signinNeeded(a);
  const enabled=!required && !a.disabled;
  const disabled=required || accessBlocked(gateway) || pending;
  const title=required?'Sign in to use for routing':current?.read_only?'Change routing from your computer dashboard':failed||gateway.online!==true?'Gateway unreachable':'';
  return `<div class="account-route"><label for="route-${esc(a.id)}">Use for routing</label><input id="route-${esc(a.id)}" data-route-account="${esc(a.id)}" data-access-focus="${esc(a.id+':route')}" type="checkbox" role="switch" ${enabled?'checked':''} ${disabled?'disabled':''} ${pending?'aria-busy="true"':''} ${title?`title="${title}"`:''}></div>
    ${routeConfirm.has(a.id)?`<div class="route-confirm" role="alert"><span>No other Codex account is ready.</span><div><button type="button" class="reset-primary confirm" data-route-off="${esc(a.id)}" data-access-focus="${esc(a.id+':off')}">Turn off</button><button type="button" class="reset-cancel" data-route-keep="${esc(a.id)}" data-access-focus="${esc(a.id+':keep')}">Keep on</button></div></div>`:''}
    ${routeErrors.has(a.id)?`<div class="fast-usage error" role="status">${esc(routeErrors.get(a.id).message)}</div>`:''}`;
}
async function accountRequest(path, data) {
  const response=await fetch(path,{method:'POST',body:JSON.stringify(data),headers:{'X-Router-Request':'1','Content-Type':'application/json'},cache:'no-store',signal:AbortSignal.timeout(ACCOUNT_BROWSER_TIMEOUT_MS)});
  const result=await response.json();
  if(!response.ok)throw new Error(result.error==='last_ready'?'last_ready':'Account change could not be confirmed. Try again.');
  return result;
}
async function startSignin(id) {
  const a=accountFor(id);if(!a || !signinButton(a) || accessBlocked(current.gateway) || signinBusy.has(id))return;
  signinBusy.add(id);signinErrors.delete(id);accessFocus=id+':start';renderAccounts(current.gateway);
  try {
    await accountRequest('/api/accounts/signin/start',{account_id:id,identity:a.access_identity});
  } catch (_) { accountError(signinErrors,id,"Couldn't reach OpenAI."); }
  finally { signinBusy.delete(id);renderAccounts(current.gateway);await load(); }
}
async function cancelSignin(id) {
  const a=accountFor(id), s=a&&signinView(a).session;
  if(!s || s.state==='finishing' || current?.read_only || signinBusy.has(id))return;
  signinBusy.add(id);signinErrors.delete(id);
  try {
    await accountRequest('/api/accounts/signin/cancel',{account_id:id,identity:a.access_identity,handle:s.handle});
  } catch (_) { accountError(signinErrors,id,"Couldn't cancel. Try again."); }
  finally { signinBusy.delete(id);renderAccounts(current.gateway);await load(); }
}
async function copySignin(id) {
  const a=accountFor(id), s=a&&signinView(a).session;
  if(!s || s.state==='finishing')return;
  let copied=false;
  try { if(navigator.clipboard?.writeText) {await navigator.clipboard.writeText(s.user_code);copied=true;} } catch (_) {}
  if(!copied) {
    const input=document.createElement('textarea');input.value=s.user_code;input.setAttribute('readonly','');
    input.style.cssText='position:fixed;left:-9999px;top:0';document.body.append(input);
    input.select();try {copied=document.execCommand('copy');} catch (_) {}input.remove();
  }
  if(copied) {
    for(const [handle,until] of copiedCodes)if(until<=Date.now())copiedCodes.delete(handle);
    copiedCodes.set(s.handle,Date.now()+2000);
    const button=document.querySelector(`[data-signin-copy="${CSS.escape(id)}"]`);
    if(button)button.innerHTML=icon('check')+'Copied';
    // Avoid re-rendering the link while the browser is opening its new tab. Render just after the
    // deadline: timers can fire a millisecond before Date.now() reaches it, which would leave "Copied" up.
    setTimeout(()=>{if(current)renderAccounts(current.gateway);},2050);
    document.querySelector(`[data-access-focus="${CSS.escape(id+':'+(accessFocus?.endsWith(':open')?'open':'copy'))}"]`)?.focus();
  } else {
    const code=document.querySelector(`[data-signin-code="${CSS.escape(id)}"]`);
    if(code) {code.focus();const range=document.createRange();range.selectNodeContents(code);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
  }
}
async function saveAccountRouting(id, enabled, confirmed=false) {
  const a=accountFor(id);if(!a || accessBlocked(current.gateway) || signinNeeded(a) || routePending.has(id))return;
  const others=current.gateway.accounts.some(b=>b.id!==id&&!b.disabled&&b.availability?.state==='ready');
  if(!enabled&&!confirmed&&!a.disabled&&!others) {confirmRouting(a);accessNextFocus=id+':keep';renderAccounts(current.gateway);return;}
  routeConfirm.delete(id);routeErrors.delete(id);accessFocus=id+':route';
  routePending.add(id);renderAccounts(current.gateway);
  try {
    const result=await accountRequest('/api/accounts/routing',{account_id:id,identity:a.access_identity,enabled,confirmed});
    if(result.enabled!==enabled)throw new Error();
  } catch (error) {
    if(error.message==='last_ready'){confirmRouting(a,fastLoadEpoch);accessNextFocus=id+':keep';}
    else accountError(routeErrors,id,'Change not confirmed. Try again.');
  }
  routePending.delete(id);renderAccounts(current.gateway);await load();setTimeout(load,3000);
}
// Claude accounts use the same confirm-before-removing-the-last-routable-account flow as Codex.
async function saveClaudeAccountRouting(id, enabled, confirmed=false) {
  const a=watchFor(id);
  if(!a || claudeRoutingState(a)==null || current?.read_only || failed || current?.gateway.online!==true || claudeRoutePending.has(id))return;
  const identity=a.routing_id, focus='claude-route-'+id;
  if(confirmed && !claudeRouteConfirm.has(identity))return;
  const others=(current.watch_accounts || []).some(b=>b.id!==id && claudeRoutable(b));
  if(!enabled && !confirmed && !others){claudeRouteConfirm.add(identity);claudeNextFocus='claude-route-keep-'+id;renderWatch(current);return;}
  claudeRouteConfirm.delete(identity);claudeRouteErrors.delete(id);claudeRouteErrorState.delete(id);
  if(operationFocus(focus))claudeNextFocus=focus;
  claudeRoutePending.add(id);renderWatch(current);
  try {
    const result=await accountRequest('/api/claude/accounts/routing',{account_id:identity,enabled,confirmed});
    if(result.enabled!==enabled)throw new Error();
    if(watchFor(id)?.routing_id===identity)watchFor(id).routing_enabled=enabled;
  } catch (error) {
    if(watchFor(id)?.routing_id===identity) {
      if(error.message==='last_ready')claudeRouteConfirm.add(identity);
      else {claudeRouteErrors.set(id,'Change not confirmed. Try again.');claudeRouteErrorState.set(id,{identity,enabled,after:fastLoadEpoch});}
    }
  }
  claudeRoutePending.delete(id);
  if(operationFocus(focus))claudeNextFocus=claudeRouteConfirm.has(identity)?'claude-route-keep-'+id:focus;
  renderWatch(current);await load(true);
}
$('watch-grid').addEventListener('change',event=>{
  const input=event.target.closest('[data-claude-route]');
  if(input&&!input.disabled)saveClaudeAccountRouting(input.dataset.claudeRoute,input.checked);
});
$('watch-grid').addEventListener('click',event=>{
  const off=event.target.closest('[data-claude-route-off]'), keep=event.target.closest('[data-claude-route-keep]');
  if(off)saveClaudeAccountRouting(off.dataset.claudeRouteOff,false,true);
  if(keep){claudeRouteConfirm.delete(watchFor(keep.dataset.claudeRouteKeep)?.routing_id);claudeNextFocus='claude-route-'+keep.dataset.claudeRouteKeep;renderWatch(current);}
});
$('account-grid').addEventListener('change',event=>{
  const input=event.target.closest('[data-route-account]');
  if(input&&!input.disabled)saveAccountRouting(input.dataset.routeAccount,input.checked);
});
$('account-grid').addEventListener('click',event=>{
  const target=event.target.closest('[data-access-focus]');if(!target)return;
  accessFocus=target.dataset.accessFocus;
  if(target.dataset.signinStart)startSignin(target.dataset.signinStart);
  if(target.dataset.signinCancel)cancelSignin(target.dataset.signinCancel);
  if(target.dataset.signinCopy)copySignin(target.dataset.signinCopy);
  if(target.dataset.signinOpen){if(target.getAttribute('aria-disabled')==='true'){event.preventDefault();return;}copySignin(target.dataset.signinOpen);}
  if(target.dataset.signinCode){const range=document.createRange();range.selectNodeContents(target);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
  if(target.dataset.routeOff)saveAccountRouting(target.dataset.routeOff,false,true);
  if(target.dataset.routeKeep){routeConfirm.delete(target.dataset.routeKeep);accessNextFocus=target.dataset.routeKeep+':route';renderAccounts(current.gateway);}
});
$('account-grid').addEventListener('keydown',event=>{
  const card=event.target.closest('[data-cardid]');
  if(event.key==='Escape' && card && routeConfirm.has(card.dataset.cardid)) {
    event.preventDefault();routeConfirm.delete(card.dataset.cardid);accessNextFocus=card.dataset.cardid+':route';renderAccounts(current.gateway);
  }
});
$('account-grid').addEventListener('focusout',event=>{
  const card=event.target.closest('[data-cardid]');
  if(card && event.relatedTarget && !card.contains(event.relatedTarget)) {routeConfirm.delete(card.dataset.cardid);card.querySelector('.route-confirm')?.remove();}
});
$('account-grid').addEventListener('pointerout',event=>{
  const card=event.target.closest('[data-cardid]');
  if(card && event.relatedTarget && !card.contains(event.relatedTarget)) {routeConfirm.delete(card.dataset.cardid);card.querySelector('.route-confirm')?.remove();}
});

function fastRow(a, gateway) {
  const pending = fastPending.get(a.id), error = fastErrors.get(a.id);
  const mode = pending?.mode || a.fast_mode || {allowed:true};
  // Only save feedback and an "off" the gateway cannot enforce show under the switch.
  const status = pending?.saving ? {line:'Saving…',tone:''} : error && error.until > Date.now() ? {line:'Not saved · try again',tone:'error'}
    : mode.allowed === false && mode.enforcement !== 'active' ? {line:'Not enforced · gateway plugin not loaded',tone:'warn'} : null;
  const disabled = current?.read_only || failed || gateway.online !== true || pending?.saving;
  return `<div class="account-fast"><div><label for="fast-${esc(a.id)}">Allow fast mode</label>${status?`<span class="fast-usage ${status.tone}" id="fast-${esc(a.id)}-status">${status.line}</span>`:''}</div><input id="fast-${esc(a.id)}" data-fast-account="${esc(a.id)}" type="checkbox" role="switch" ${status?`aria-describedby="fast-${esc(a.id)}-status"`:''} ${mode.allowed!==false?'checked':''} ${disabled?'disabled':''} ${pending?.saving?'aria-busy="true"':''} ${current?.read_only?'title="Change fast mode from your computer dashboard"':''}></div>`;
}
async function saveFastMode(id, allowed) {
  if (current?.read_only || failed || current?.gateway.online !== true || fastPending.get(id)?.saving) return;
  const previous = {...(fastPending.get(id)?.mode || accountFor(id)?.fast_mode || {allowed:true})};
  fastErrors.delete(id);fastFocus=id;
  fastPending.set(id,{saving:true,mode:{...previous,allowed}});
  if(accountFor(id))accountFor(id).fast_mode={...previous,allowed};
  renderAccounts(current.gateway);
  try {
    const result = await request('/api/fast-mode',{method:'POST',body:JSON.stringify({account_id:id,allowed})});
    if (result.allowed !== allowed || !['active','unavailable','unknown'].includes(result.enforcement) ||
        !(result.changed_at === null || (typeof result.changed_at === 'string' && Number.isFinite(Date.parse(result.changed_at))))) throw new Error('Not confirmed');
    const mode = {...result};
    fastPending.set(id,{saving:false,mode,after:fastLoadEpoch});
    if(accountFor(id))accountFor(id).fast_mode=mode;
  } catch (_) {
    fastPending.set(id,{saving:false,mode:previous,after:fastLoadEpoch});
    if(accountFor(id))accountFor(id).fast_mode=previous;
    const error = {until:Date.now()+10000};fastErrors.set(id,error);
    setTimeout(()=>{if(fastErrors.get(id)===error){fastErrors.delete(id);if(current)renderAccounts(current.gateway);}},10000);
  }
  if(current)renderAccounts(current.gateway);
  await load();
}
$('account-grid').addEventListener('change',event=>{
  const input=event.target.closest('[data-fast-account]');
  if(input && !input.disabled)saveFastMode(input.dataset.fastAccount,input.checked);
});
function renderAccounts(gateway) {
  reconcileAccess(gateway);
  const activeAccess=document.activeElement?.dataset?.accessFocus;
  if(accessNextFocus){accessFocus=accessNextFocus;accessNextFocus=null;}
  else if(activeAccess)accessFocus=activeAccess;
  else if(document.activeElement!==document.body)accessFocus=null;
  const focus = document.activeElement?.dataset?.fastAccount || (document.activeElement === document.body ? fastFocus : null);
  if(document.activeElement !== document.body && !document.activeElement?.dataset?.fastAccount)fastFocus=null;
  const accounts = gateway.accounts || [];
  for(const a of accounts) {
    const session=signinView(a).session;
    if(session && signinPanels.get(a.id)!==session.handle &&
        (document.activeElement===document.body || activeAccess?.startsWith(a.id+':')))accessFocus=a.id+':open';
    if(session)signinPanels.set(a.id,session.handle);else signinPanels.delete(a.id);
  }
  const accessBefore=accessFocus;
  const pool = gateway.pool || {};
  const checked = Date.parse(gateway.checked_at);
  const stalePool = failed || !gateway.online || !Number.isFinite(checked) || Date.now()-checked>180000;
  const poolState = stalePool ? 'unknown' : pool.state;
  const note = poolState==='reduced' ? 'One account ready' : poolState==='unavailable' ? 'No accounts ready' : poolState==='unknown' ? 'Status unavailable · checking' : poolState==='available' ? 'All accounts ready' : '';
  $('pool-availability').hidden = !note;
  $('pool-availability').className = `status-item ${poolState==='available'?'ok':poolState==='unavailable'?'down':'warn'}`;
  $('pool-availability').textContent = note;
  $('pool-availability').title = 'Latest account snapshot. Model-specific cooldowns may affect fewer routes. Brief provider errors can occur between refreshes.';
  const preference = gateway.preference;
  renderRouting(gateway);
  renderRouting(gateway,'claude');
  $('account-count').textContent = `${accounts.filter(a=>a.active).length} / ${accounts.length}`;
  if (!accounts.length) {
    $('account-grid').innerHTML = `<div class="account-card empty-state">${gateway.online === null ? 'Connecting to your gateway…' : gateway.online ? 'No Codex subscription accounts are connected.' : 'Your desktop is temporarily unreachable. Retrying automatically.'}</div>`;
    return;
  }
  const cards = accounts.map(a => {
    const w = a.windows.find(w=>w.seconds===604800) || a.windows[0];
    const view=signinView(a), session=view.session, outcome=view.last_outcome;
    const off=a.disabled || a.auth_state==='sign_in_required' && !signinSuperseded(a);
    const strip=!session && (signinNeeded(a) || outcome?.kind!=='saved_unconfirmed' && (outcomeText[outcome?.kind] || outcome?.kind==='wrong_account'));
    const stale = !!a.quota_error || gateway.online === false;
    const other = a.windows.filter(x=>x!==w);
    const remaining = w?.remaining;
    const p = !off && !stale && w?.seconds === 604800 ? pace(w, remaining) : null;
    const preferred = preference?.status === 'active' && preference.preferred_email === a.email;
    const billing = BillingDates.describe(a.billing,new Date(),a.plan_type);
    const r = a.resets || {}, credits = r.credits || [];
    const until = a.availability?.retry_at && !failed && gateway.online && ['quota_exhausted','rate_limited','provider_unavailable'].includes(a.availability.state);
    const state = (a.disabled?'Disabled':availabilityLabel(a,gateway)) + (until ? ` · until ${dateTime(a.availability.retry_at*1000)}` : '');
    const verified = a.billing?.verified_at ? `Checked ${dateTime(a.billing.verified_at)} against ${a.billing.source==='user_confirmed'?'your confirmation':'ChatGPT billing'}. Recheck billing if you cancel or change plans.` : 'The account reports access through this date; the next charge has not been confirmed.';
    return `<article data-provider="codex" data-account-key="${esc(a.key || a.id)}" class="account-card codex-card${off?' is-off':''}" data-cardid="${esc(a.id)}" data-access-focus="${esc(a.id+':card')}" tabindex="-1">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(accountName(a))}">${esc(accountName(a))}</span></div></div><span class="small-badge">${esc(a.plan)}</span>${cardMenuButton('codex',a,accountName(a),`data-access-focus="${esc(a.id+':menu')}"`)}</div>
      <div class="account-body">${session?signinPanel(a,view,gateway):`<div class="account-hero"><div class="quota-label">${esc(w?.label || 'Subscription')} allowance${stale && remaining!=null ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(remaining))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="Codex · ${esc(accountName(a))} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${remaining}"`}${paceValueText(p, remaining)}><span style="width:${remaining ?? 0}%"></span>${paceBar(p)}</div>
      <div class="quota-used">${paceLabel(p)}<span>${w?.used == null ? a.disabled?'Not available while disabled':'Waiting for quota data' : esc(percent(w.used))+' used'}</span></div></div>`}
      <div class="account-side">${session?'':`${other.length?`<div class="limit-rows">${other.map(o=>limitRow(o.label==='5-hour'?'5-hour limit':o.label,o)).join('')}</div>`:''}
      ${a.quota_error&&!strip?`<div class="quota-warning">${esc(a.quota_error)}</div>`:''}`}
      ${!session&&outcome?.kind==='signed_in'?`<div class="access-result" role="status">${icon('check')}Signed in just now</div>`:''}
      ${!session&&outcome?.kind==='signed_in'&&outcome.enable_failed===true&&a.disabled===true?`<div class="quota-warning" role="status">Couldn't turn on routing</div>`:''}
      ${!session?signinStrip(a,gateway,outcome):''}
      ${a.access_error?`<div class="quota-warning" role="alert">${esc(a.access_error)}</div>`:''}
      <div class="account-bottom"><div class="account-dates"><div class="reset-block"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div><div class="reset-block billing-renewal ${billing.urgent?'renewal-soon':''}" title="${esc(`${billing.badge}. ${billing.note} ${verified}`)}"><span>${esc(billing.ended?billing.label:a.billing?.confirmed?'Renews':billing.label)}</span><strong>${esc(billing.date)}</strong></div></div>${!strip&&!session?`<span class="account-state" title="${esc(state)}">${a.availability?.state==='ready'&&!stale?icon('check'):''}<span>${esc(state)}</span></span>`:''}</div>
      <div class="account-resets"><button type="button" class="resets-button" data-reset-open="${esc(a.id)}" aria-haspopup="dialog">Resets <span>${r.available==null?'—':esc(r.available)}</span></button><span>${r.automatic?'Auto-use before expiry':''}</span></div>
      ${fastRow(a,gateway)}
      ${routingRow(a,gateway)}</div></div>
    </article>`;
  });
  if(!document.querySelector('[data-signin-handle]'))$('account-grid').innerHTML=cards.join('');
  else {
    const grid=$('account-grid'), existing=new Map([...grid.children].map(card=>[card.dataset.accountKey || card.dataset.cardid,card]));
    const keys=new Set(accounts.map(a=>a.key || a.id));
    for(const [key,card] of existing)if(!keys.has(key))card.remove();
    cards.forEach((html,index)=>{
    const template=document.createElement('template');template.innerHTML=html;
    const next=template.content.firstElementChild, old=existing.get(next.dataset.accountKey || next.dataset.cardid);
    let card=next;
    const panel=old?.querySelector('.signin-panel'), replacement=next.querySelector('.signin-panel');
    if(old && panel && replacement &&
        panel.dataset.signinHandle===replacement.dataset.signinHandle &&
        panel.querySelector('code').textContent===replacement.querySelector('code').textContent) {
      card=old;
      old.className=next.className;panel.className=replacement.className;
      panel.querySelector('.signin-status').replaceWith(replacement.querySelector('.signin-status'));
      const copy=panel.querySelector('[data-signin-copy]'), newCopy=replacement.querySelector('[data-signin-copy]');
      copy.disabled=newCopy.disabled;copy.innerHTML=newCopy.innerHTML;
      const open=panel.querySelector('a'), newOpen=replacement.querySelector('a');
      open.setAttribute('aria-disabled',newOpen.getAttribute('aria-disabled') || 'false');open.tabIndex=newOpen.tabIndex;
      const cancel=panel.querySelector('[data-signin-cancel]'), newCancel=replacement.querySelector('[data-signin-cancel]');
      if(cancel){cancel.hidden=!newCancel;cancel.disabled=!newCancel || newCancel.disabled;}
      old.firstElementChild.replaceWith(next.firstElementChild);
      for(const child of [...panel.parentElement.children])if(child!==panel)child.remove();
      for(const child of [...replacement.parentElement.children].reverse())if(child!==replacement)panel.after(child);
    } else if(old)old.replaceWith(next);
    const before=grid.children[index] || null;
    if(card!==before) {
      if(grid.moveBefore && card.parentElement===grid)grid.moveBefore(card,before);
      else {
        // Older browsers reset a selection inside a node moved with insertBefore.
        const selection=typeof window!=='undefined' ? window.getSelection?.() : null;
        const selected=selection && card.contains(selection.anchorNode) && card.contains(selection.focusNode)
          ? [selection.anchorNode,selection.anchorOffset,selection.focusNode,selection.focusOffset] : null;
        grid.insertBefore(card,before);
        if(selected)selection.setBaseAndExtent(...selected);
      }
    }
    });
  }
  if(accessBefore) {
    const target=document.querySelector(`[data-access-focus="${CSS.escape(accessBefore)}"]`);
    const cardId=accessBefore.slice(0,accessBefore.lastIndexOf(':'));
    const next=target || document.querySelector(`[data-signin-start="${CSS.escape(cardId)}"]`) || document.querySelector(`[data-cardid="${CSS.escape(cardId)}"]`);
    if(next && next!==document.activeElement)next.focus({preventScroll:true});
  }
  if(focus)document.querySelector(`[data-fast-account="${CSS.escape(focus)}"]`)?.focus();

}
function seriesEntries() {
  const entries=new Map();
  for(const p of providerData())for(const a of p.accounts)entries.set(series(p.id,a.auth_index || a.id,a.email,a.key),{provider:p.id,name:accountLabel(a)});
  for(const u of [current?.usage,today?.usage])for(const [provider, summary] of Object.entries(usageProviders(u)))for(const row of [...(summary.hours || []),...(summary.accounts || [])].filter(r=>r.requests>0)) {
    const key=series(provider,row.auth_index,row.email,row.key);
    if(!entries.has(key))entries.set(key,{provider,name:'Removed account'});
  }
  return entries;
}
function seriesNames() { return Object.fromEntries([...seriesEntries()].map(([key,value])=>[key,value.name])); }
function renderLegend() {
  const groups=new Map();
  for(const [key,value] of seriesEntries()) {
    if(!groups.has(value.provider))groups.set(value.provider,[]);
    groups.get(value.provider).push(`<span>${seriesDot(key)}${esc(value.name)}</span>`);
  }
  for(const legend of document.querySelectorAll('[data-account-legend]'))legend.innerHTML=[...groups].map(([provider,items])=>`<span class="legend-group ${accentOf(provider)}" data-provider="${esc(provider)}"><b>${esc(providerName(provider))}</b>${items.join('')}</span>`).join('');
}
function renderChart(id, usage, selectedRange) {
  const now = new Date(), start = new Date(startFor(selectedRange));
  const daily = selectedRange !== 'today';
  let length = 24;
  if (daily) {
    const since = usageSince(usage);
    const first = selectedRange==='all' && since ? new Date(since) : start;
    first.setHours(0,0,0,0);
    const min = new Date(now); min.setHours(0,0,0,0); min.setDate(min.getDate()-29);
    start.setTime(Math.max(first.getTime(),min.getTime()));
    length = Math.max(1,Math.min(30,Math.round((new Date(now).setHours(0,0,0,0)-start.getTime())/86400000)+1));
  }
  const bins = Array.from({length},(_,i)=>{
    const d = new Date(start); if(daily)d.setDate(d.getDate()+i);else d.setHours(i);
    return Object.assign({date:d},Object.fromEntries([...seriesEntries().keys()].map(key=>[key,0])));
  });
  for (const [provider, summary] of Object.entries(usageProviders(usage))) for (const row of summary.hours || []) {
    const d = new Date(row.hour);
    const bin = bins.find(b => daily ? b.date.toDateString()===d.toDateString() : b.date.getHours()===d.getHours() && b.date.toDateString()===d.toDateString());
    if (bin) { const key=series(provider,row.auth_index,row.email,row.key);bin[key]=(bin[key] || 0)+row.requests; }
  }
  const SERIES=[...seriesEntries()].map(([key,value])=>[value.provider,key]);
  const sum = b => SERIES.reduce((n, [, key]) => n + (b[key] || 0), 0);
  const max = Math.max(1,...bins.map(sum));
  const label = d => daily ? d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) : d.toLocaleTimeString(undefined,{hour:'2-digit',hour12:false});
  const ticks = [...new Set([0,Math.floor((length-1)/3),Math.floor(2*(length-1)/3),length-1])];
  const names = seriesNames();
  const title = b => SERIES.filter(([, key]) => names[key]).map(([provider, key]) => `${providerName(provider)} ${names[key]} ${b[key]}`).join(' · ');
  $(id).innerHTML = `<div class="bars" role="img" aria-label="Request counts over time. Details are available in the account usage table.">${bins.map(b=>`<div class="bar-slot" title="${esc(label(b.date))} · ${esc(title(b))} requests">${[...SERIES].reverse().map(([, key])=>`<span data-series="${esc(key)}" style="background:${seriesColor(key)};height:${(b[key] || 0)/max*100}%"></span>`).join('')}</div>`).join('')}</div><div class="chart-labels">${ticks.map(i=>`<span>${esc(label(bins[i].date))}</span>`).join('')}</div>`;
}
function table(headers, rows, empty='No requests recorded in this period.', classes=[]) {
  if (!rows.length) return `<div class="empty-state">${esc(empty)}</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>`<tr${classes[i]?` class="${classes[i]}"`:''}>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const gatewayRow = provider => (!provider || provider==='codex' ? 'codex' : provider==='claude' ? 'claude' : accentOf(provider))+'-row';
function renderUsage(data) {
  const u=data.usage, summaries=Object.entries(usageProviders(u)), rows=summaries.flatMap(([provider,s])=>(s.accounts || []).map(r=>({...r,provider})));
  const t = totals(rows), good = t.requests ? 100*(t.requests-t.failures)/t.requests : null;
  if (!u.available) {
    $('usage-summary').innerHTML = '<div class="metric-card"><span>Usage history</span><strong>—</strong></div>';
    $('usage-chart').innerHTML = '<div class="empty-state">Waiting for usage history</div>';
    $('usage-accounts').innerHTML = $('usage-models').innerHTML = '<div class="empty-state">Usage data is currently unavailable.</div>';
    $('history-note').textContent = 'Retrying automatically. Unavailable data is not counted as zero usage.';
    return;
  }
  const split = `<small class="metric-detail"><span>Read ${cacheAmount(t,'cache_read',compact)}</span><span>Write ${cacheAmount(t,'cache_creation',compact)}</span></small>`;
  $('usage-summary').innerHTML = [['Requests',number(t.requests)],['Tokens',compact(t.total_tokens)],['Success rate',percent(good)],['Cached input',t.input_tokens?percent(100*t.cached_tokens/t.input_tokens):'—',split]].map(([label,value,detail=''])=>`<div class="metric-card"><span>${label}</span><strong>${value}</strong>${detail}</div>`).join('');
  renderChart('usage-chart',u,range);
  $('chart-caption').textContent = range==='today'?'By hour · your local time':range==='week'?'By day · your local time':'By day · up to the latest 30 days';
  const row = provider => r => [`<span class="mono gateway-name">${esc(providerName(provider))}</span>`,providerChip(provider,r.auth_index,r.email,r.key),number(r.requests),number(r.input_tokens),number(r.cached_tokens),cacheAmount(r,'cache_read'),cacheAmount(r,'cache_creation'),number(r.output_tokens),number(r.failures)];
  $('usage-accounts').innerHTML = table(['Gateway','Account','Requests','Input tokens','Cached input','Cache read','Cache write','Output tokens','Failures'],rows.map(r=>row(r.provider)(r)),undefined,rows.map(r=>gatewayRow(r.provider)));
  const models = summaries.flatMap(([provider,s])=>(s.models || []).map(r=>({...r,provider}))).sort((a,b)=>(b.requests||0)-(a.requests||0));
  $('usage-models').innerHTML = table(['Model','Requests','Tokens','Average duration'],models.map(r=>[esc(r.model),number(r.requests),number(r.total_tokens),r.latency_ms==null?'—':`${(r.latency_ms/1000).toFixed(1)}s`]),undefined,models.map(r=>gatewayRow(r.provider)));
  $('history-note').textContent = `History starts ${dateTime(usageSince(u))}. These are best-effort gateway records, refreshed every 15 seconds. Subscription allowances are reported separately by the provider.`;
}
function renderToday(data) {
  const u=data.usage, summaries=Object.entries(usageProviders(u));
  if (!u.available) {
    for (const id of ['today-requests','today-success','claude-today-requests','claude-today-success','tokens-today','cache-percent','cache-split']) $(id).textContent='—';
    $('token-detail').textContent='Waiting for recorded usage'; $('cache-fill').style.width='0%';
    $('overview-chart').innerHTML='<div class="empty-state">Usage history is temporarily unavailable.</div>';
    $('recent-table').innerHTML='<div class="empty-state">Waiting for recorded requests</div>';
    return;
  }
  const success=x=>x?.requests ? percent((x.requests-x.failures)/x.requests*100) : '—';
  for(const p of providerData()) {
    const t=totals(usageProviders(u)[p.id]?.accounts || []);
    const prefix=p.id==='codex'?'':p.id+'-';
    const requests=$(prefix+'today-requests'), ok=$(prefix+'today-success');
    if(requests)requests.textContent=number(t.requests);if(ok)ok.textContent=success(t);
  }
  const all=totals(summaries.flatMap(([,s])=>s.accounts || []));
  $('tokens-today').textContent=compact(all.total_tokens);
  $('token-detail').innerHTML=summaries.map(([provider,s])=>`<span>${seriesDot(provider+':total')}${esc(providerName(provider))} ${esc(compact(totals(s.accounts || []).total_tokens))}</span>`).join('');
  $('token-detail').title = `${number(all.input_tokens)} input · ${number(all.output_tokens)} output tokens`;
  const cache = all.input_tokens ? Math.min(100,all.cached_tokens/all.input_tokens*100) : 0;
  $('cache-fill').style.width = `${cache}%`;
  $('cache-percent').textContent = all.input_tokens ? percent(cache) : '—';
  $('cache-split').innerHTML = `${cacheAmount(all,'cache_read',compact)} · ${cacheAmount(all,'cache_creation',compact)}`;
  renderChart('overview-chart',u,'today');
  const recent = u.recent.slice(0,6);
  const recentCache = r => `<span class="mono" title="${esc(`Read ${number(r.cache_read_tokens)} · Write ${number(r.cache_creation_tokens)}`)}">${compact(r.cache_read_tokens)} · ${compact(r.cache_creation_tokens)}</span>`;
  $('recent-table').innerHTML = table(['Account','Model','Reasoning','Mode','Tokens','Cache read · write','Result','Time'],recent.map(r=>[providerChip(r.provider || 'codex',r.auth_index,r.email,r.key),`<span class="mono">${esc(r.model)}</span>`,reasoningCell(r.reasoning_effort),modeCell(r),`<span class="mono" title="${number(r.total_tokens)}">${compact(r.total_tokens)}</span>`,recentCache(r),`<span class="outcome ${r.failed?'failed':''}">${r.failed?'Failed':icon('check')+'Completed'}</span>`,`<span class="mono" title="${esc(dateTime(r.timestamp))}">${esc(new Date(r.timestamp).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'}))}</span>`]),undefined,recent.map(r=>gatewayRow(r.provider)));
}
function renderSystem(data) {
  const g=data.gateway,r=g.routing, tailscale=data.access_transport==='tailscale';
  if($('machine-name'))$('machine-name').textContent=data.machine || '';
  $('advanced-link').closest('.advanced').hidden=!!data.read_only;
  $('system-routing-label').textContent = routingLabel(g);
  const ttl=['1h0m0s','1h0m1s'].includes(r['session-affinity-ttl'])?'1h':r['session-affinity-ttl'];
  const values=[['Codex routing',routingLabel(g)],['Claude routing',routingLabel({preference:g.claude_preference})],['Session affinity',r['session-affinity'] ? `Enabled · ${ttl || ''}` : 'Not reported'],['Subagent affinity',r['session-affinity-subagents'] ? 'Enabled' : 'Not reported'],['Usage history',data.usage.available?'Recording locally':'Waiting for records'],['Latest recorded request',dateTime(data.usage.last_collected_at)],['Codex quota refresh','Every minute'],['Dashboard access',tailscale?'Private Tailscale connection':'Private SSH connection']];
  document.querySelector('.connection-flow .flow-line').textContent=tailscale?'Tailscale':'SSH · Tailscale';
  document.querySelector('#system-view>.footnote').textContent=tailscale?'Account controls are available to your approved Tailscale devices. Your management key stays on the Linux desktop.':'Account controls stay private. Access stays on your computer and its private SSH connection. Your management key stays on the Linux desktop.';
  $('system-details').innerHTML=values.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  $('system-status').textContent=g.online?'Online':g.online===null?'Connecting':'Unreachable';
}
function renderOperations(data) {
  const h=data.health || {}, labels={healthy:'Healthy',down:'Down',unknown:'Unknown',stale:'Stale',failed:'Failed'};
  // The Codex server belongs to the Codex column; account health is on each card; the gateway is in the top bar.
  let server=h.codex?.status || 'unknown';
  if(failed && server==='healthy')server='stale';
  $('codex-server').hidden=false;
  $('codex-server').className=`status-item ${server==='healthy'?'ok':server==='down'?'down':'warn'}`;
  $('codex-server').textContent=`Codex server ${(labels[server] || 'Unknown').toLowerCase()}`;
  $('codex-server').title='Managed Linux Codex server for Phone Remote and Mac SSH';
  let backup=h.backup?.status || 'unknown';
  if(failed && backup==='healthy')backup='stale';
  $('health-panel').innerHTML=`<span class="health-item health-${esc(backup)}" title="${esc(`Last successful backup: ${dateTime(h.backup?.last_success_at)} · same disk`)}"><i aria-hidden="true"></i><span>Backup</span><b>${h.backup?.last_success_at ? esc(relative(h.backup.last_success_at).replace('Updated ',''))+' · ' : ''}${esc(labels[backup] || 'Unknown')}</b></span>`;
}
// Sections grow in proportion to their account count, so every card on a line gets the same width (see .gateway in style.css).
function sectionSize(section, count) { section.style.setProperty?.('--n',String(Math.max(1,count))); }
function providerStatus(p, gateway) {
  const checked=Date.parse(gateway.checked_at), ready=p.accounts.filter(a=>!a.disabled && a.availability?.state==='ready').length;
  const [tone,text]=failed || gateway.online!==true || !Number.isFinite(checked) || Date.now()-checked>180000 ? ['warn','Status unavailable · checking']
    : ready===p.accounts.length ? ['ok','All accounts ready'] : !ready ? ['down','No accounts ready']
    : ['warn',ready===1 ? 'One account ready' : `${ready} of ${p.accounts.length} ready`];
  return `<span class="status-item ${tone}">${esc(text)}</span>`;
}
function lastRequest(epoch) {
  if(!Number.isFinite(epoch))return '—';
  const text=relative(epoch*1000).replace('Updated ','');
  return text.charAt(0).toUpperCase()+text.slice(1);
}
// Today's requests per local hour; hours still to come stay blank.
function hourChart(hours) {
  const now=new Date().getHours(), values=Array.from({length:24},(_,h)=>Math.max(0,Number(hours?.[h]) || 0)), peak=Math.max(1,...values);
  const bars=values.map((v,h)=>!v ? `<span class="${h>now ? 'later' : 'none'}"></span>` :
    `<span style="height:${Math.max(4,v/peak*100).toFixed(1)}%" title="${String(h).padStart(2,'0')}:00 · ${esc(number(v))} ${v===1 ? 'request' : 'requests'}"></span>`).join('');
  return `<div class="hour-chart"><div class="hour-bars" role="img" aria-label="Requests by hour today">${bars}</div><div class="hour-axis" aria-hidden="true"><span>00</span><span>12</span><span>23</span></div></div>`;
}
// "Use for routing" on other providers turns the gateway's disabled flag off and on; turning off the last ready account asks first.
const genericRoutePending=new Set(), genericRouteConfirm=new Set(), genericRouteErrors=new Map();
let genericNextFocus='';
const accountReady = a => !a.disabled && a.availability?.state==='ready';
function genericRoutingRow(p, a, gateway) {
  if(typeof a.routing_enabled!=='boolean')return '';
  const id=p.id+':'+a.key, input=`generic-route-${p.id}-${a.key}`, pending=genericRoutePending.has(id), required=a.availability?.state==='auth_required';
  const disabled=required || accessBlocked(gateway) || pending;
  const title=required?'Sign in to use for routing':current?.read_only?'Change routing from your computer dashboard':failed||gateway.online!==true?'Gateway unreachable':'';
  return `<div class="account-route"><label for="${esc(input)}">Use for routing</label><input id="${esc(input)}" data-generic-route="${esc(id)}" type="checkbox" role="switch" ${a.routing_enabled && !required?'checked':''} ${disabled?'disabled':''} ${pending?'aria-busy="true"':''} ${title?`title="${title}"`:''}></div>
    ${genericRouteConfirm.has(id)?`<div class="route-confirm" role="alert"><span>No other ${esc(p.name)} account is ready.</span><div><button type="button" class="reset-primary confirm" id="${esc(input)}-off" data-generic-route-off="${esc(id)}">Turn off</button><button type="button" class="reset-cancel" id="${esc(input)}-keep" data-generic-route-keep="${esc(id)}">Keep on</button></div></div>`:''}
    ${genericRouteErrors.get(id)>Date.now()?'<div class="fast-usage error" role="status">Change not confirmed. Try again.</div>':''}`;
}
// Usage windows a provider reports through the gateway (Grok's billing credits). The first is the headline; a window
// whose reset has passed reads "—" until the next read. Bare period labels ("Monthly") are credit windows.
function genericUsage(a, now=Date.now()) {
  const windows=(Array.isArray(a.windows) ? a.windows : []).filter(w=>Number.isFinite(w?.used_pct ?? w?.used)).map(w=>{
    const used=Math.min(100,Math.max(0,w.used_pct ?? w.used)), due=Number.isFinite(w.resets_at) && w.resets_at*1000<=now;
    return {...w,used:due ? null : used,remaining:due ? null : 100-used};
  });
  if(!windows.length)return null;
  const [w,...others]=windows, label=/^(daily|weekly|monthly)$/i.test(w.label || '') ? w.label+' credits' : w.label || 'Usage';
  const checked=Date.parse(a.usage_checked_at), updated=Number.isFinite(checked) ? relative(checked) : '';
  const stale=!Number.isFinite(checked) || now-checked>15*60*1000 || a.usage_status==='error';
  return {w,others,label,credits:/credit/i.test(label),stale,
    freshness:stale ? 'Stale'+(updated ? ' · '+updated.charAt(0).toLowerCase()+updated.slice(1) : '') : updated};
}
// Providers without allowance data: today's requests are the headline. With usage windows, "% remaining" leads instead.
function genericCard(p, a, gateway) {
  const t=a.today || {}, requests=t.requests ?? a.requests ?? 0, name=accountLabel(a), u=genericUsage(a);
  const signin=a.availability?.state==='auth_required' && !a.disabled, reauth=signin && !current?.read_only && !!addProvider(p.id);
  const until=a.availability?.retry_at && !failed && gateway.online && ['quota_exhausted','rate_limited','provider_unavailable'].includes(a.availability.state);
  const state=availabilityLabel(a,gateway)+(until ? ` · until ${dateTime(a.availability.retry_at*1000)}` : '');
  const success=t.success_rate==null ? null : percent(t.success_rate*100), ready=state==='Ready to route';
  // A ready account with usage shows its freshness in the state spot, as Claude cards do; other states keep priority.
  const shown=u && ready ? u.freshness : state, tone=u && ready ? u.stale ? 'warn' : 'ok' : '';
  const remaining=u?.w.remaining;
  const hero=u ? `<div class="account-hero"><div class="quota-label">${esc(u.label)}${u.stale ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining==null ? '—' : esc(number(Math.round(remaining)))+'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track" role="progressbar" aria-label="${esc(name)} ${esc(u.label)} remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null ? '' : `aria-valuenow="${Math.round(remaining)}"`}><span style="width:${remaining ?? 0}%"></span></div>
      <div class="quota-used"><span>${esc(number(requests))} ${requests===1 ? 'request' : 'requests'} today</span><span>${u.w.used==null ? 'Waiting for usage data' : esc(percent(u.w.used))+' used'}</span></div></div>`
    : `<div class="account-hero"><div class="quota-label">Requests today</div>
      <div class="quota-value">${esc(number(requests))}<span class="remaining-word">${success==null ? '' : esc(success)+' success'}</span></div></div>`;
  const facts=u ? `<div class="reset-block"><span>${u.credits ? 'Credits reset' : 'Resets'}</span><strong class="countdown" data-reset="${u.w.resets_at || ''}" title="${u.w.resets_at ? esc(dateTime(u.w.resets_at*1000)) : ''}">${countdown(u.w.resets_at)}</strong></div><div class="reset-block"><span>Success today</span><strong>${esc(success ?? '—')}</strong></div>`
    : `<div class="reset-block"><span>Last request</span><strong>${esc(lastRequest(t.last_request_at))}</strong></div>`;
  return `<article class="account-card generic-card ${providerAccent(p)}-card${a.disabled || signin ? ' is-off' : ''}" data-provider="${esc(p.id)}" data-account-key="${esc(a.key)}">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(name)}">${esc(name)}</span></div></div>${a.plan ? `<span class="small-badge">${esc(a.plan)}</span>` : ''}${cardMenuButton(p.id,a,name)}</div>
      <div class="account-body">${hero}
      <div class="account-side">${u?.others.length ? `<div class="limit-rows">${u.others.map(w=>limitRow(w.label || 'Usage window',w)).join('')}</div>` : ''}${hourChart(t.hours)}
      ${signin ? `<div class="account-access critical"><div class="account-access-row"><span>${icon('alert')}Sign in required</span>${reauth ? `<button type="button" class="resets-button" id="${esc(`signin-${p.id}-${a.key}`)}" data-add-provider="${esc(p.id)}" data-add-key="${esc(a.key)}" aria-haspopup="dialog">Sign in</button>` : ''}</div></div>` : ''}
      <div class="account-bottom"><div class="account-dates">${facts}</div>${signin ? '' : `<span class="account-state${tone ? ' watch-state-'+tone : ''}" title="${esc(u ? state+' · '+u.freshness : state)}">${tone ? icon(tone==='ok' ? 'check' : 'alert') : ready ? icon('check') : ''}<span>${esc(shown)}</span></span>`}</div>
      ${genericRoutingRow(p,a,gateway)}</div></div>
    </article>`;
}
function renderProviders(data) {
  if(!data.providers)return;
  const list=data.providers, count=id=>list.find(p=>p.id===id)?.accounts.length || 0;
  const active=document.activeElement, focusId=genericNextFocus || (active?.closest?.('[data-generic-provider]') ? active.id : '');
  for(const id of [...genericRoutePending,...genericRouteConfirm,...genericRouteErrors.keys()]) {
    const p=list.find(p=>p.id===id.slice(0,id.indexOf(':'))), a=p?.accounts.find(a=>p.id+':'+a.key===id);
    if(!a)for(const store of [genericRoutePending,genericRouteConfirm,genericRouteErrors])store.delete(id);
    else if(genericRouteConfirm.has(id) && (!a.routing_enabled || !accountReady(a) || p.accounts.some(b=>b!==a && accountReady(b))))genericRouteConfirm.delete(id);
  }
  $('codex-accounts').hidden=!list.some(p=>p.id==='codex');
  sectionSize($('codex-accounts'),count('codex'));sectionSize($('watch-section'),count('claude'));
  const known=new Set(list.map(p=>p.id));
  document.querySelectorAll('[data-generic-provider]').forEach(el=>{if(!known.has(el.dataset.genericProvider))el.remove();});
  for(const p of list) {
    if(p.id==='codex' || p.id==='claude')continue;
    let section=document.querySelector(`[data-generic-provider="${CSS.escape(p.id)}"]`);
    if(!section){section=document.createElement('section');section.dataset.genericProvider=p.id;}
    $('gateways').insertBefore(section,$('routing-all'));
    const accent=providerAccent(p), stats=totals(usageProviders((today || data).usage)[p.id]?.accounts || []);
    section.className=`gateway generic-gateway ${accent}-gateway`;section.dataset.provider=p.id;section.dataset.accent=accent;
    section.setAttribute?.('aria-labelledby',p.id+'-heading');
    sectionSize(section,p.accounts.length);
    section.innerHTML=`<header class="gateway-head"><div class="pool-name">${providerIcon(p)}<div><div class="gateway-title"><h2 id="${esc(p.id)}-heading">${esc(p.name)}</h2></div><div class="gateway-status">${providerStatus(p,data.gateway)}</div></div></div><div class="gateway-stats"><div class="pool-stat"><span>Accounts</span><strong>${p.accounts.filter(a=>a.active).length} / ${p.accounts.length}</strong></div><div class="pool-stat"><span>Requests today</span><strong id="${esc(p.id)}-today-requests">${number(stats.requests)}</strong></div><div class="pool-stat"><span>Success today</span><strong id="${esc(p.id)}-today-success">${stats.requests?percent(100*(stats.requests-stats.failures)/stats.requests):'—'}</strong></div></div></header><div class="account-grid">${p.accounts.map(a=>genericCard(p,a,data.gateway)).join('')}</div><div class="routing-slot" aria-hidden="true" hidden></div>`;
  }
  if(focusId) {
    const target=document.getElementById(focusId);
    if(target && !target.disabled){if(target!==document.activeElement)target.focus?.({preventScroll:true});genericNextFocus='';}
  }
  const online=data.gateway.online, adding=Array.isArray(data.add_providers);
  $('inventory-empty').hidden=!!list.length;$('inventory-empty-icons').hidden=online!==true;
  $('inventory-empty-text').textContent=online===true ? adding ? 'Add your first account' : 'No accounts connected yet' : online===null ? 'Connecting to your gateway…' : 'Gateway inventory is unavailable';
  $('inventory-empty-add').hidden=online!==true || !adding || !!data.read_only;
  const modes=[['round-robin','Round-robin'],['earliest-weekly-reset','Earliest reset'],['highest-remaining-quota','Higher remaining quota']];
  for(const key of Object.keys(routingRows)) {
    const select=routingPart(key,'mode'), selected=select.value;
    const accounts=key==='all' ? (list.find(p=>p.id==='codex')?.accounts || []).filter(a=>list.find(p=>p.id==='claude')?.accounts.some(b=>b.email===a.email)) : list.find(p=>p.id===key)?.accounts || [];
    const options=[...modes,...accounts.filter(a=>a.email).map(a=>[a.key,'Prefer '+a.label])];
    const markup=options.map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
    if(select.innerHTML!==markup){select.innerHTML=markup;select.value=selected;}
  }
}
// Sections on one line end their cards level: one without its own routing row reserves the height of the tallest beside it.
function reserveRouting() {
  const sections=[...document.querySelectorAll('#gateways>.gateway:not([hidden])')], lines=new Map();
  const row=s=>s.querySelector(':scope>.routing-control:not([hidden])'), slot=s=>s.querySelector(':scope>.routing-slot');
  for(const s of sections)if(slot(s))slot(s).hidden=true;
  for(const s of sections)lines.set(s.offsetTop,[...(lines.get(s.offsetTop) || []),s]);
  for(const line of lines.values()) {
    const tallest=Math.max(0,...line.map(s=>row(s)?.offsetHeight || 0));
    for(const s of line)if(slot(s) && !row(s) && tallest){slot(s).hidden=false;slot(s).style.height=tallest+'px';}
  }
}
// Adding and disconnecting accounts. One dialog, shaped like the routing choice, serves both; the gateway owns sign-in sessions.
const addProviders = () => Array.isArray(current?.add_providers) ? current.add_providers : [];
const addProvider = id => addProviders().find(p=>p.id===id);
// Disconnect is refused through the Tailscale address and in read-only views. Adding works over Tailscale too.
const disconnectBlocked = () => current?.access_transport==='tailscale' || !!current?.read_only;
let accountFlow=null, accountOpener=null, accountTimers=[];
async function accountCall(path, body) {
  const response=await fetch(path,{method:body?'POST':'GET',...(body?{body:JSON.stringify(body)}:{}),headers:{'X-Router-Request':'1',...(body?{'Content-Type':'application/json'}:{})},cache:'no-store',signal:AbortSignal.timeout(ACCOUNT_BROWSER_TIMEOUT_MS)});
  let data=null;try{data=await response.json();}catch(_){}
  return {status:response.status,ok:response.ok,data:data && typeof data==='object'?data:{}};
}
function accountTimer(fn, ms) { const id=setTimeout(()=>{accountTimers=accountTimers.filter(t=>t!==id);fn();},ms);accountTimers.push(id); }
function clearAccountTimers() { accountTimers.forEach(clearTimeout);accountTimers=[]; }
// A session is live from start until the gateway reports a result; only a live one is cancelled on close.
const addLive = f => f?.kind==='add' && ['signin','finishing'].includes(f.view);
const addLocked = f => f?.kind==='add' ? f.view==='cancelling' : f?.view==='working';
const sameAddAttempt = (f, attempt, session) => accountFlow===f && f.attempt===attempt && f.session===session;
function addStatus(f) {
  if(f.view==='finishing')return {text:'Finishing sign-in…',tone:''};
  if(f.offline)return {text:f.offline==='gateway'?'Gateway unreachable · retrying':'Connection lost · retrying',tone:'warn'};
  const waiting=f.flow==='device' ? 'Waiting for approval' : 'Sign-in open';
  if(!Number.isFinite(f.expires))return {text:waiting,tone:''};
  const left=Math.max(0,Math.ceil((f.expires-Date.now())/1000));
  if(!left)return {text:(f.flow==='device' ? 'Code expired' : 'Sign-in expired')+' · checking',tone:'warn'};
  return {text:`${waiting} · ${left<120 ? left+' s' : Math.ceil(left/60)+' min'} left`,tone:left<120 ? 'warn' : ''};
}
// The dashboard only checks the address's shape; the server checks it belongs to this sign-in.
function callbackShape(value) {
  try { const url=new URL(value);return ['http:','https:'].includes(url.protocol) && ['localhost','127.0.0.1'].includes(url.hostname) && url.searchParams.has('code'); }
  catch (_) { return false; }
}
const dialogHead = (p, title) => `<div class="dialog-head">${p ? providerIcon(p,true) : ''}<h2 id="account-dialog-title">${title}</h2></div>`;
const dialogCancel = (label='Cancel', attrs='') => `<button type="button" class="reset-cancel" data-dialog-cancel ${attrs}>${label}</button>`;
const codeBox = (f, dead) => `<div class="code-box${dead ? ' dim' : ''}"><code aria-label="Code ${esc(f.code.split('').join(' '))}">${esc(f.code)}</code><button type="button" class="resets-button" data-add-copy ${dead ? 'tabindex="-1" aria-hidden="true"' : ''}>${f.copiedUntil>Date.now() ? icon('check')+'Copied' : 'Copy'}</button></div>`;
function accountDialogHtml(f) {
  if(f.kind==='disconnect') {
    const p=providerData().find(p=>p.id===f.provider) || {id:f.provider,name:providerName(f.provider)}, working=f.view==='working';
    return `${dialogHead(p,`Disconnect ${esc(f.name)} from the gateway?`)}
      <p class="dialog-note">Requests stop using it. Your ${esc(p.name)} subscription and other sign-ins stay as they are.</p>
      ${f.last ? `<p class="dialog-warn">${icon('alert')}<span>It's your only ready ${esc(p.name)} account. ${esc(p.name)} requests will fail until you add another.</span></p>` : ''}
      ${f.error ? `<p class="dialog-error" role="alert">${esc(f.error)}</p>` : ''}
      <div class="dialog-actions"><button type="button" class="reset-primary danger" data-disconnect ${working ? 'disabled aria-busy="true"' : f.view==='failed' ? 'data-autofocus' : ''}>${working ? 'Disconnecting…' : f.view==='failed' ? 'Try again' : 'Disconnect'}</button></div>
      ${dialogCancel('Cancel',working ? 'disabled' : f.view==='failed' ? '' : 'data-autofocus')}`;
  }
  if(f.view==='pick') {
    const list=addProviders();
    if(!list.length)return `${dialogHead(null,'Add account')}<p class="dialog-error" role="alert">${current?.gateway?.online===true ? "Adding accounts isn't available on this gateway version." : "Couldn't load providers · gateway unreachable"}</p>${dialogCancel('Close','data-autofocus')}`;
    // Two providers can share a display name (Kimi has two sign-in services); the id tells them apart.
    const shared=c=>list.filter(o=>o.name===c.name).length>1;
    return `${dialogHead(null,'Add account')}<div class="provider-list">${list.map((c,i)=>`<button type="button" class="provider-option" data-add-pick="${esc(c.id)}" ${i ? '' : 'data-autofocus'}>${providerIcon(c,true)}<span>${esc(c.name)}</span><small>${c.login==='device' ? 'Code' : 'Browser'}${shared(c) ? ' · '+esc(c.id) : ''}</small></button>`).join('')}</div>${dialogCancel()}`;
  }
  if(f.view==='busy')return `${dialogHead(null,'A sign-in is in progress')}<p class="dialog-note">${esc(f.message || 'Finish or cancel the current sign-in first.')}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>Try again</button></div>${dialogCancel('Close')}`;
  const p=addProvider(f.provider) || {id:f.provider,name:providerName(f.provider)}, name=esc(p.name);
  const title=f.reauth ? `Sign in to ${name} again` : `Add ${name} account`;
  if(f.view==='cancelling')return `${dialogHead(p,title)}<div class="signin-status" role="status"><i aria-hidden="true"></i><span>Cancelling sign-in…</span></div>${dialogCancel('Cancel','disabled')}`;
  if(f.view==='cancel-failed')return `${dialogHead(p,title)}<p class="dialog-error" role="alert">${esc(f.message)}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-cancel-retry data-autofocus>Retry cancel</button></div>${dialogCancel('Close')}`;
  if(f.view==='starting')return `${dialogHead(p,title)}<div class="signin-status" role="status"><i aria-hidden="true"></i><span>Starting sign-in…</span></div>${dialogCancel('Cancel','data-autofocus')}`;
  if(f.view==='failed')return `${dialogHead(p,title)}<p class="dialog-error" role="alert">${esc(f.message)}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>Try again</button></div>${dialogCancel()}`;
  if(f.view==='done') {
    const a=f.account, found=a?.key ? providerAccount(f.provider,null,null,a.key) : null, replaced=a?.replaced===true;
    const label=found ? accountLabel(found) : a?.label && a.label!==a.key ? a.label : '';
    const state=!a ? f.message : replaced ? 'Already connected · sign-in replaced' : !found ? '' : accountReady(found) ? 'Ready to route' : availabilityLabel(found,current.gateway);
    return `<div class="dialog-result" role="status"><span class="result-icon">${icon('check')}</span><h2 id="account-dialog-title">${a ? `${name} account ${replaced ? 'updated' : 'added'}` : `Signed in to ${name}`}</h2>${label && !label.startsWith(p.name+' account') ? `<p class="result-name">${esc(label)}</p>` : ''}${state ? `<p>${esc(state)}</p>` : ''}</div><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-done data-autofocus>Done</button></div>`;
  }
  if(f.view==='ended') {
    const text=f.ended==='expired' ? (f.flow==='device' ? 'Code expired' : 'Sign-in expired') : f.ended==='cancelled' ? 'Sign-in was cancelled' : f.message || "Sign-in didn't finish";
    const again=f.ended==='expired' ? f.flow==='device' ? 'Get a new code' : 'Start again' : 'Try again';
    return `${dialogHead(p,title)}${f.code ? codeBox(f,true) : ''}<div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>${again}</button></div><div class="signin-status bad" role="status"><i aria-hidden="true"></i><span>${esc(text)}</span></div>${dialogCancel('Close')}`;
  }
  const status=addStatus(f), line=`<div class="signin-status${status.tone ? ' '+status.tone : ''}" role="status" data-add-status><i aria-hidden="true"></i><span>${esc(status.text)}</span></div>`;
  if(f.flow==='device')return `${dialogHead(p,title)}${f.code ? codeBox(f,false) : ''}<a class="signin-open" href="${esc(f.url)}" target="_blank" rel="noreferrer" data-add-open data-autofocus>Open sign-in page ${icon('arrow')}</a>${line}${dialogCancel()}`;
  const finishing=f.view==='finishing', opened=f.opened || !!f.pasted;
  return `${dialogHead(p,title)}
    <a class="signin-open${opened ? ' secondary' : ''}" href="${esc(f.url)}" target="_blank" rel="noreferrer" data-add-open ${finishing ? 'aria-disabled="true" tabindex="-1"' : opened ? '' : 'data-autofocus'}>${opened ? 'Open sign-in page again' : 'Open sign-in page'} ${icon('arrow')}</a>
    <div class="paste-step"><label for="add-callback">After approving, copy the address of the page that can't load and paste it here.</label>
    <input id="add-callback" class="paste-input" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="${esc(p.callback_example || 'http://localhost/…')}" value="${esc(f.pasted)}" ${f.error ? 'aria-invalid="true" aria-describedby="add-callback-error"' : ''} ${finishing ? 'readonly' : opened ? 'data-autofocus' : ''}>
    <p class="paste-error" id="add-callback-error" role="alert"${f.error ? '' : ' hidden'}>${esc(f.error)}</p>
    <button type="button" class="reset-primary${opened ? ' confirm' : ''}" data-add-continue ${finishing ? 'disabled aria-busy="true"' : f.pasted ? '' : 'disabled'}>${finishing ? 'Finishing sign-in…' : 'Continue'}</button></div>
    ${line}${dialogCancel()}`;
}
function renderAccountDialog() {
  const dialog=$('account-dialog');if(!accountFlow)return;
  dialog.innerHTML=accountDialogHtml(accountFlow);
  if(!dialog.open)dialog.showModal();
  const target=dialog.querySelector('[data-autofocus]');
  (target && !target.disabled ? target : dialog).focus();
}
function openAccountDialog(f, opener) {
  if(accountFlow)return false;
  closeCardMenu();accountFlow=f;accountOpener=opener || null;
  return true;
}
function openAdd(provider=null, reauth=null, opener=null) {
  if(!accountActions() || current?.read_only || provider && !addProvider(provider))return;
  // A failed cancellation still owns the gateway session, even after its dialog was hidden.
  if(accountFlow?.kind==='add' && (accountFlow.view==='cancel-failed' || accountFlow.closeRequested)) {
    accountOpener=opener || accountOpener;renderAccountDialog();return;
  }
  const f={kind:'add',provider,reauth,view:'pick'};
  if(!openAccountDialog(f,opener))return;
  if(provider)startAdd(f);else renderAccountDialog();
}
async function startAdd(f, provider=f.provider) {
  if(accountFlow!==f)return;
  clearAccountTimers();
  const attempt=(f.attempt || 0)+1;
  Object.assign(f,{attempt,closeRequested:false,provider,view:'starting',session:null,flow:addProvider(provider)?.login,url:'',code:null,expires:NaN,opened:false,pasted:'',error:'',ended:null,message:'',offline:false,copiedUntil:0,account:null});
  renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/start',{provider});}catch(_){}
  const d=result?.data || {};
  const valid=result?.ok && typeof d.session==='string' && !!d.session && ['device','browser'].includes(d.flow) && /^https:\/\//i.test(d.url || '');
  if(!sameAddAttempt(f,attempt,null) || f.view!=='starting')return;
  if(valid) {
    Object.assign(f,{view:'signin',session:d.session,flow:d.flow,url:d.url,code:typeof d.user_code==='string' && d.user_code ? d.user_code : null,
      expires:typeof d.expires_at==='number' ? d.expires_at*1000 : Date.parse(d.expires_at)});
    if(f.closeRequested){cancelAddSession(f);return;}
    renderAccountDialog();tickAdd(f);pollAdd(f);
    return;
  }
  if(f.closeRequested){finishAccountDialog(f);return;}
  if(result?.status===409 && d.error==='busy')Object.assign(f,{view:'busy',message:d.message || ''});
  else Object.assign(f,{view:'failed',message:!result ? "Couldn't start sign-in · gateway unreachable" : d.message || "Couldn't start sign-in"});
  renderAccountDialog();
}
async function cancelAddSession(f) {
  if(accountFlow!==f || !f.session || f.view==='cancelling')return;
  clearAccountTimers();
  // Invalidate callback and status requests that were sent before cancellation.
  const attempt=(f.attempt || 0)+1, session=f.session;
  Object.assign(f,{attempt,view:'cancelling'});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/cancel',{session});}catch(_){}
  if(!sameAddAttempt(f,attempt,session) || f.view!=='cancelling')return;
  if(result?.ok && result.data.cancelled===true){finishAccountDialog(f);return;}
  if(result?.ok && result.data.cancelled===false && result.data.state==='error')return endAdd(f,'error',result.data.message || 'Sign-in failed. Start again.');
  if(result?.ok && result.data.cancelled===false && ['ok','finishing'].includes(result.data.state)) {
    // A completed session still needs its status account details before the fresh inventory read.
    Object.assign(f,{view:'finishing',closeRequested:false});renderAccountDialog();tickAdd(f);pollAdd(f);return;
  }
  Object.assign(f,{view:'cancel-failed',message:result?.data.message || "Couldn't cancel sign-in. Try again."});
  renderAccountDialog();
}
function updateAddStatus(f) {
  const line=$('account-dialog').querySelector('[data-add-status]'), status=addStatus(f);if(!line)return;
  line.className='signin-status'+(status.tone ? ' '+status.tone : '');
  if(line.lastElementChild.textContent!==status.text)line.lastElementChild.textContent=status.text;
}
function tickAdd(f, attempt=f.attempt, session=f.session) { accountTimer(()=>{if(sameAddAttempt(f,attempt,session) && addLive(f)){updateAddStatus(f);tickAdd(f,attempt,session);}},1000); }
// The dashboard polls on its own; a code sign-in needs nothing else from the user.
function pollAdd(f, attempt=f.attempt, session=f.session) {
  accountTimer(async()=>{
    if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
    let result=null;try{result=await accountCall('/api/accounts/add/status?session='+encodeURIComponent(session));}catch(_){}
    if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
    const d=result?.data || {};
    if(result?.ok && d.state==='ok')return addSucceeded(f,d,attempt,session);
    if(result?.ok && ['expired','error','cancelled'].includes(d.state))return endAdd(f,d.state,d.message);
    if(result?.ok && d.state==='finishing') {
      const changed=f.view!=='finishing';f.view='finishing';f.offline=false;
      if(changed)renderAccountDialog();else updateAddStatus(f);
      pollAdd(f,attempt,session);return;
    }
    if(result?.status===409)return endAdd(f,'error',d.message || 'This sign-in is no longer available.');
    // A waiting reply with a message means the dashboard could not read the gateway this time.
    const offline=!result ? 'connection' : !result.ok || d.message ? 'gateway' : false;
    if(offline!==f.offline){f.offline=offline;updateAddStatus(f);}
    pollAdd(f,attempt,session);
  },f.flow==='device' ? 3000 : 2000);
}
function endAdd(f, state, message) {
  clearAccountTimers();
  Object.assign(f,{view:'ended',ended:state,message:message || ''});
  renderAccountDialog();
}
async function addSucceeded(f, d, attempt=f.attempt, session=f.session) {
  clearAccountTimers();
  // Read the new inventory first so the result names the account and its real state.
  try {await load(true);} catch (_) {}
  if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
  Object.assign(f,{view:'done',account:d.account && typeof d.account==='object' && typeof d.account.key==='string' ? d.account : null,message:d.message || ''});
  renderAccountDialog();
  accountTimer(()=>{if(sameAddAttempt(f,attempt,session))closeAccountDialog();},3200);
}
function addOpened() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || f.opened)return;
  const attempt=f.attempt, session=f.session;
  f.opened=true;
  // Like the Codex card, opening a code sign-in also copies the code for pasting there.
  if(f.flow!=='browser'){copyAddCode();return;}
  // Change the link in place after the click: the browser is still opening its new tab from it.
  setTimeout(()=>{
    if(!sameAddAttempt(f,attempt,session) || f.view!=='signin')return;
    const dialog=$('account-dialog'), link=dialog.querySelector('[data-add-open]');
    if(link){link.classList.add('secondary');link.innerHTML=`Open sign-in page again ${icon('arrow')}`;}
    dialog.querySelector('[data-add-continue]')?.classList.add('confirm');
    dialog.querySelector('#add-callback')?.focus();
  },0);
}
async function copyText(text) {
  try { if(navigator.clipboard?.writeText) {await navigator.clipboard.writeText(text);return true;} } catch (_) {}
  const input=document.createElement('textarea');input.value=text;input.setAttribute('readonly','');
  input.style.cssText='position:fixed;left:-9999px;top:0';document.body.append(input);
  input.select();let copied=false;try {copied=document.execCommand('copy');} catch (_) {}input.remove();
  return copied;
}
async function copyAddCode() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || !f.code)return;
  const attempt=f.attempt, session=f.session, dialog=$('account-dialog'), button=()=>dialog.querySelector('[data-add-copy]');
  if(!await copyText(f.code)) {
    if(!sameAddAttempt(f,attempt,session))return;
    const code=dialog.querySelector('.code-box code');
    if(code){const range=document.createRange();range.selectNodeContents(code);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
    return;
  }
  if(!sameAddAttempt(f,attempt,session))return;
  f.copiedUntil=Date.now()+2000;
  if(button())button().innerHTML=icon('check')+'Copied';
  setTimeout(()=>{if(sameAddAttempt(f,attempt,session) && f.copiedUntil<=Date.now() && button())button().textContent='Copy';},2050);
}
function showCallbackError(message) {
  const f=accountFlow, dialog=$('account-dialog'), input=dialog.querySelector('#add-callback'), error=dialog.querySelector('#add-callback-error');
  f.error=message;
  if(!input || !error){renderAccountDialog();return;}
  error.textContent=message;error.hidden=!message;
  if(message){input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby','add-callback-error');input.focus();}
  else {input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');}
}
async function submitAddCallback() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || f.flow!=='browser')return;
  const value=($('account-dialog').querySelector('#add-callback')?.value ?? f.pasted).trim();
  f.pasted=value;if(!value)return;
  if(!callbackShape(value)){showCallbackError("That isn't the sign-in address. It starts with http://localhost.");return;}
  const attempt=f.attempt, session=f.session;
  Object.assign(f,{view:'finishing',error:''});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/callback',{session,redirect_url:value});}catch(_){}
  if(!sameAddAttempt(f,attempt,session) || f.view!=='finishing')return;
  // Accepted: the status poll reports the result.
  if(result?.ok && result.data.accepted===true)return;
  if(result?.status===409)return endAdd(f,'error',result.data.message || 'This sign-in is no longer available.');
  Object.assign(f,{view:'signin',error:!result ? "Couldn't reach your desktop. Try again." : result.data.message ||
    (result.status===400 ? "That isn't the sign-in address. It starts with http://localhost." : "Couldn't finish sign-in. Try again.")});
  renderAccountDialog();
}
function closeAccountDialog(force=false) {
  const f=accountFlow;if(!f || !force && addLocked(f))return;
  if(f.kind==='add' && f.view==='starting') {
    // Keep ownership until start returns, then cancel the returned session.
    f.closeRequested=true;hideAccountDialog();return;
  }
  if(addLive(f)){cancelAddSession(f);return;}
  if(f.kind==='add' && f.view==='cancel-failed'){hideAccountDialog();return;}
  finishAccountDialog(f);
}
function restoreAccountFocus(added=false) {
  // Cards are periodically replaced, so the original node may already be detached.
  const opener=accountOpener, target=opener?.isConnected ? opener : opener?.id ? document.getElementById(opener.id) : null;
  const focus=target?.isConnected && !target.disabled ? target : $('add-account');
  focus?.focus({preventScroll:!!added});
}
function hideAccountDialog() {
  if($('account-dialog').open)$('account-dialog').close();
  restoreAccountFocus();
}
function finishAccountDialog(f) {
  if(accountFlow!==f)return;
  const added=f.kind==='add' && f.view==='done' && f.account && !f.account.replaced ? f.account.key : null;
  accountFlow=null;clearAccountTimers();
  if($('account-dialog').open)$('account-dialog').close();
  restoreAccountFocus(added);accountOpener=null;
  if(added)highlightCard(f.provider,added);
}
// Native Escape/close also retains ownership until the gateway acknowledges cancellation.
function accountDialogClosed() {
  if($('account-dialog').open || !accountFlow)return;
  closeAccountDialog();
}
function accountCard(provider, key) { return document.querySelector(`.account-card[data-provider="${CSS.escape(provider)}"][data-account-key="${CSS.escape(key)}"]`); }
function highlightCard(provider, key) {
  const card=accountCard(provider,key);if(!card)return;
  card.classList.add('card-new');card.scrollIntoView({block:'nearest',behavior:'smooth'});
  setTimeout(()=>card.classList.remove('card-new'),2600);
}
function openDisconnect(provider, key, opener) {
  const p=providerData().find(p=>p.id===provider), a=p?.accounts.find(a=>a.key===key);
  if(!a || disconnectBlocked())return;
  // Warn, but allow: the owner may be replacing the account or retiring the provider.
  const last=accountReady(a) && !p.accounts.some(b=>b!==a && accountReady(b));
  if(openAccountDialog({kind:'disconnect',provider,key,auth_index:a.auth_index,name:accountLabel(a),last,view:'confirm',error:''},opener))renderAccountDialog();
}
async function confirmDisconnect() {
  const f=accountFlow;if(f?.kind!=='disconnect' || f.view==='working')return;
  Object.assign(f,{view:'working',error:''});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/disconnect',{key:f.key,auth_index:f.auth_index,confirmed:true});}catch(_){}
  if(result?.ok && result.data.disconnected===true) {
    // Focus moves to a neighbouring card's menu, or Add account, before this card goes.
    const card=accountCard(f.provider,f.key), next=[...(card?.parentElement?.querySelectorAll('.card-menu') || [])].find(b=>!card.contains(b));
    if(accountFlow===f){accountOpener=next || $('add-account');closeAccountDialog(true);}
    $('account-announce').textContent=`${f.name} disconnected`;
    card?.classList.add('card-leaving');
    setTimeout(()=>load(true),360);
    return;
  }
  if(accountFlow!==f){load(true);return;}
  Object.assign(f,{view:'failed',error:!result ? "Couldn't disconnect · gateway unreachable" : result.status===403 ? 'Disconnect works from the desktop or over SSH only.' :
    result.data.message || "Couldn't disconnect · gateway unreachable"});
  renderAccountDialog();
}
function cardMenuOutside(event) {
  if(!event.target.closest?.('#card-menu,.card-menu[aria-expanded="true"]'))closeCardMenu();
}
// Phones resize while scrolling (the address bar); only a width change moves the cards.
let cardMenuWidth=0;
function cardMenuResized() { if(innerWidth!==cardMenuWidth)closeCardMenu(); }
function closeCardMenu(focus=false) {
  const menu=cardMenu, pop=$('card-menu');if(!menu)return;
  cardMenu=null;
  try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;
  document.removeEventListener('pointerdown',cardMenuOutside,true);window.removeEventListener('resize',cardMenuResized);
  const button=document.getElementById(menuId(menu.provider,menu.key));
  button?.setAttribute('aria-expanded','false');
  if(focus)button?.focus();
}
function openCardMenu(button) {
  const {menuProvider:provider,menuKey:key}=button.dataset, a=providerAccount(provider,null,null,key), pop=$('card-menu');
  if(cardMenu?.provider===provider && cardMenu.key===key){closeCardMenu(true);return;}
  closeCardMenu();if(!a)return;
  const signin=!current?.read_only && !!addProvider(provider), remote=disconnectBlocked();
  pop.innerHTML=`<button type="button" role="menuitem" data-menu-signin${signin ? '' : ' aria-disabled="true"'}>Sign in again${signin ? '' : `<small>${current?.read_only ? 'Desktop or SSH only' : 'Not available for this provider'}</small>`}</button><hr><button type="button" role="menuitem" class="danger" data-menu-disconnect${remote ? ' aria-disabled="true"' : ''}>Disconnect…${remote ? '<small>Desktop or SSH only</small>' : ''}</button>`;
  pop.setAttribute('aria-label',`Account actions · ${accountLabel(a)}`);
  cardMenu={provider,key};button.setAttribute('aria-expanded','true');
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  // The popover sits in the top layer at page coordinates, so it scrolls with the card.
  const r=button.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight, below=r.bottom+6+h<=innerHeight-8 || r.top-6-h<8;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+scrollX+'px';
  pop.style.top=(below ? r.bottom+6 : r.top-6-h)+scrollY+'px';
  pop.querySelector('[role=menuitem]').focus();
  cardMenuWidth=innerWidth;document.addEventListener('pointerdown',cardMenuOutside,true);window.addEventListener('resize',cardMenuResized);
}
async function saveGenericRouting(id, enabled, confirmed=false) {
  const p=providerData().find(p=>p.id===id.slice(0,id.indexOf(':'))), a=p?.accounts.find(a=>p.id+':'+a.key===id);
  if(!a || typeof a.routing_enabled!=='boolean' || accessBlocked(current.gateway) || genericRoutePending.has(id))return;
  const input=`generic-route-${p.id}-${a.key}`, focused=document.activeElement?.closest?.('[data-generic-provider]');
  if(!enabled && !confirmed && accountReady(a) && !p.accounts.some(b=>b!==a && accountReady(b))) {
    genericRouteConfirm.add(id);if(focused)genericNextFocus=input+'-keep';render();return;
  }
  genericRouteConfirm.delete(id);genericRouteErrors.delete(id);genericRoutePending.add(id);
  if(focused)genericNextFocus=input;
  render();
  try {
    const result=await accountCall('/api/accounts/routing',{key:a.key,auth_index:a.auth_index,enabled});
    if(!result.ok || result.data.enabled!==enabled)throw new Error('Not confirmed');
    a.routing_enabled=enabled;
  } catch (_) { genericRouteErrors.set(id,Date.now()+10000); }
  genericRoutePending.delete(id);render();await load(true);
}
// Add account shows once the backend offers a sign-in catalog; read-only views can't change accounts.
function renderAccountEntry(data) {
  const add=$('add-account');add.hidden=!Array.isArray(data.add_providers);add.disabled=!!data.read_only;
  add.title=data.read_only ? 'Add accounts from your computer dashboard' : '';
  if(cardMenu && !providerAccount(cardMenu.provider,null,null,cardMenu.key))closeCardMenu();
}
$('add-account').addEventListener('click',()=>openAdd(null,null,$('add-account')));
$('gateways').addEventListener('click',event=>{
  const target=event.target.closest('[data-menu-key],[data-add-provider],[data-generic-route-off],[data-generic-route-keep],#inventory-empty-add');
  if(!target || target.disabled)return;
  const {menuKey,addProvider:provider,addKey,genericRouteOff:off,genericRouteKeep:keep}=target.dataset;
  if(menuKey)openCardMenu(target);
  else if(provider)openAdd(provider,addKey,target);
  else if(off)saveGenericRouting(off,false,true);
  else if(keep){genericRouteConfirm.delete(keep);genericNextFocus=target.id.replace(/-keep$/,'');render();}
  else openAdd(null,null,target);
});
$('gateways').addEventListener('change',event=>{
  const input=event.target.closest('[data-generic-route]');
  if(input && !input.disabled)saveGenericRouting(input.dataset.genericRoute,input.checked);
});
$('card-menu').addEventListener('click',event=>{
  const item=event.target.closest('[role=menuitem]');if(!item || !cardMenu || item.getAttribute('aria-disabled')==='true')return;
  const {provider,key}=cardMenu, opener=document.getElementById(menuId(provider,key));
  closeCardMenu();
  if(item.hasAttribute('data-menu-signin'))openAdd(provider,key,opener);else openDisconnect(provider,key,opener);
});
$('card-menu').addEventListener('keydown',event=>{
  const items=[...$('card-menu').querySelectorAll('[role=menuitem]')], at=items.indexOf(document.activeElement);
  if(event.key==='Escape'){event.preventDefault();closeCardMenu(true);}
  // Tab leaves from the ⋯ button, as if the menu had not been open.
  else if(event.key==='Tab')closeCardMenu(true);
  else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    items[event.key==='Home' ? 0 : event.key==='End' ? items.length-1 : (at+(event.key==='ArrowDown' ? 1 : -1)+items.length)%items.length]?.focus();
  }
});
$('account-dialog').addEventListener('click',event=>{
  const dialog=$('account-dialog'), f=accountFlow, target=event.target.closest('button,a');if(!f)return;
  if(!target) {
    const r=dialog.getBoundingClientRect();
    if(event.target===dialog && (event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom))closeAccountDialog();
    return;
  }
  if(target.disabled)return;
  if(target.matches('[data-dialog-cancel],[data-add-done]'))closeAccountDialog();
  else if(target.dataset.addPick)startAdd(f,target.dataset.addPick);
  else if(target.matches('[data-add-restart]'))startAdd(f);
  else if(target.matches('[data-add-cancel-retry]'))cancelAddSession(f);
  else if(target.matches('[data-add-copy]'))copyAddCode();
  else if(target.matches('[data-add-open]')){if(target.getAttribute('aria-disabled')==='true')event.preventDefault();else addOpened();}
  else if(target.matches('[data-add-continue]'))submitAddCallback();
  else if(target.matches('[data-disconnect]'))confirmDisconnect();
});
$('account-dialog').addEventListener('input',event=>{
  const f=accountFlow;if(event.target.id!=='add-callback' || f?.kind!=='add')return;
  f.pasted=event.target.value.trim();
  const next=$('account-dialog').querySelector('[data-add-continue]');if(next)next.disabled=!f.pasted;
  if(f.error)showCallbackError('');
});
// Pasting a complete address moves focus to Continue; Enter submits.
$('account-dialog').addEventListener('paste',event=>{
  if(event.target.id==='add-callback')setTimeout(()=>{if(accountFlow?.kind==='add' && callbackShape(event.target.value.trim()))$('account-dialog').querySelector('[data-add-continue]:not(:disabled)')?.focus();},0);
});
$('account-dialog').addEventListener('keydown',event=>{
  if(event.key==='Enter' && event.target.id==='add-callback'){event.preventDefault();submitAddCallback();return;}
  if(event.key!=='Tab')return;
  const items=[...$('account-dialog').querySelectorAll('button,a[href],input')].filter(el=>!el.disabled && el.tabIndex>=0 && el.getAttribute('aria-hidden')!=='true');
  const edge=event.shiftKey ? items[0] : items.at(-1);
  if(edge && document.activeElement===edge){event.preventDefault();(event.shiftKey ? items.at(-1) : items[0]).focus();}
});
// Disconnect and an in-flight cancellation must finish before another close action.
$('account-dialog').addEventListener('cancel',event=>{if(addLocked(accountFlow))event.preventDefault();});
$('account-dialog').addEventListener('close',accountDialogClosed);
function render() {
  const data=current;if(!data)return;
  const g=data.gateway;
  const state=$('gateway-state');state.className=`status ${failed || g.online===false?'offline':g.online===null?'connecting':''}`;
  const stale=!failed && g.online && data.health?.gateway?.status==='stale';
  if(stale)state.className='status connecting';
  state.innerHTML=`<i></i>${failed?'Connection lost':stale?'Gateway status stale':g.online?'Gateway online':g.online===null?'Connecting':'Gateway unreachable'}`;
  $('updated').textContent=relative(g.checked_at);
  $('refresh').classList.toggle('refreshing',g.refreshing);
  $('refresh').disabled=!!g.refreshing;
  const warning = failed ? 'Cannot reach your desktop. Check Tailscale; the dashboard will reconnect automatically. The values below were last saved in this tab.' : g.error || data.usage.error;
  $('notice').hidden=!warning;$('notice').textContent=warning || '';
  renderAccountEntry(data);renderProviders(data);renderOperations(data);renderWatch(data);renderAccounts(g);renderRoutingAll(g);reserveRouting();renderLegend();renderResetModal();renderToday(today || data);renderUsage(data);renderSystem(data);
  const since=usageSince(data.usage);
  $('footer-note').textContent=`Codex quotas refresh every minute · history since ${since?new Date(since).toLocaleDateString(undefined,{month:'short',day:'numeric'}):'setup'}`;
}
async function request(path,options={}) {
  const timeout=['/api/routing','/api/claude-routing','/api/routing-all'].includes(path)?ACCOUNT_BROWSER_TIMEOUT_MS:options.body?75000:15000;
  const response=await fetch(path,{...options,headers:{'X-Router-Request':'1',...(options.body?{'Content-Type':'application/json'}:{})},cache:'no-store',signal:AbortSignal.timeout(timeout)});
  if(!response.ok)throw new Error(`HTTP ${response.status}`);
  return response.json();
}
function resetExpiry(value) {
  return value ? new Date(value*1000).toLocaleDateString(undefined,{month:'long',day:'numeric'}) : 'Expiry not reported';
}
function stepsFailed(steps) { return !!steps && typeof steps==='object' && Object.values(steps).includes('failed'); }
function resetBlockedReason(a) {
  if(!a)return 'Account unavailable.';
  const state=a.availability?.state;
  if(state==='auth_required' || a.auth_state==='sign_in_required')return 'Sign in again to use resets.';
  if(a.disabled || state==='disabled')return 'This account is disabled.';
  return '';
}
function resetCredits(a) {
  const r=a?.resets || {},credits=r.credits || [];
  const pending=resetPendingCredits.get(a?.id) || r.pending_credit_id;
  return pending ? [credits.find(c=>c.id===pending) || {id:pending,expires_at:null},...credits.filter(c=>c.id!==pending)] : credits;
}
function watchLastEvent(r) {
  const e=r.last_event; if(!e || typeof e!=='object')return '';
  const label=grantLabel(watchGrants(r).find(g=>g.id===e.grant_id) || {kind:e.kind});
  const code=e.code?' · '+e.code:'';
  const text={reset:`${label} applied`,rejected:`${label} rejected${code}`,uncertain:`${label} not confirmed`,error:`${label} failed${code}`}[e.outcome] || `${label} · ${e.outcome || 'checked'}`;
  return e.at ? `${text} · ${dateTime(typeof e.at==='number'?e.at*1000:e.at)}` : text;
}
function renderWatchResetModal() {
  const id=resetModalAccount,a=watchFor(id),r=watchResets(a) || {},grants=watchGrants(r);
  const pending=watchPending(a);
  const working=resetBusy.has('watch:'+id);
  const readOnly=!!current?.read_only;
  const serviceDown=r.service==='unavailable';
  const surface=watchResetSurfaceReason(r);
  const cooldown=cooldownActive(r);
  const unavailable=readOnly || serviceDown || failed || !a || r.eligible===false || cooldown;
  const windows=Array.isArray(a?.windows)?a.windows:[];
  const soon=(kind,limit)=>{const w=windows.find(w=>w.kind===kind);const left=w?.resets_at?w.resets_at-Date.now()/1000:null;return left!==null && left>0 && left<limit ? w : null;};
  const next=typeof r.next_grant_id==='string' && grants.some(g=>g.id===r.next_grant_id) ? r.next_grant_id : null;
  const now=Date.now()/1000;
  const expired=g=>grantExpired(g,now);
  r.available=watchAvailable(r);
  const later=g=>!expired(g) && !!next && g.id!==next && !(pending?.grant_id===g.id);
  const live=g=>!expired(g) && g.usable===true;
  const open=g=>live(g) && !unavailable && (!pending || pending.grant_id===g.id && pending.state==='uncertain');
  const actionable=g=>!working && open(g);
  if(resetConfirmCredit && !grants.some(g=>g.id===resetConfirmCredit && open(g)))resetConfirmCredit=null;
  const weekly=grants.some(g=>g.kind==='full' && live(g)) ? soon('seven_day',6*3600) : null;
  const five=grants.some(g=>g.kind==='five_hour' && live(g)) ? soon('five_hour',3600) : null;
  const key=JSON.stringify([a?.label,a?.plan,r,grants.map(expired),pending,working,unavailable,weekly?.resets_at,five?.resets_at,resetConfirmCredit,resetModalMessage]);
  if(key===resetModalKey)return;
  resetModalKey=key;
  const focus=document.activeElement?.dataset?.grantAction;
  const autoFocus=document.activeElement?.id==='reset-auto-toggle';
  const row=g=>{
    const can=actionable(g),confirm=can && resetConfirmCredit===g.id,mine=pending?.grant_id===g.id;
    const state=expired(g)?'Expired':g.paused?'Paused':g.resets_left===0?'Used':mine?(pending.state==='uncertain'?'Not confirmed':'In progress'):g.usable_now===false?(g.use_requires_limit?'Usable at the limit':'Not usable now'):'';
    const count=g.resets_left!=null && (g.resets_total ?? 0)>1 ? `${g.resets_left} of ${g.resets_total} left` : '';
    const detail=[count,g.ends_at?'Expires '+resetExpiry(g.ends_at):'Expiry not reported',state].filter(Boolean).join(' · ');
    const label=working && resetConfirmCredit===g.id?'Working…':confirm?'Confirm':mine && pending.state==='uncertain'?'Try again':'Use reset';
    const action=later(g)?'<span class="reset-wait">Use next reset first</span>':`<button type="button" class="reset-primary ${confirm?'confirm':''}" data-grant-action="${esc(g.id)}" ${can?'':'disabled'} ${surface?`title="${esc(surface)}"`:''}>${label}</button>${confirm&&!working?'<button type="button" class="reset-cancel" data-reset-cancel>Cancel</button>':''}`;
    return `<div class="reset-row ${can?'':later(g)?'reset-row-later':'reset-row-off'}"><div><strong>${esc(grantLabel(g))}</strong><p title="${g.ends_at?esc(dateTime(g.ends_at*1000)):''}">${esc(detail)}</p></div><div class="reset-row-action">${action}</div></div>`;
  };
  const entries=[];
  for(const kind of ['full','five_hour']){
    const of=grants.filter(g=>g.kind===kind);
    if(of.length)entries.push(...of);else entries.push({kind,placeholder:true});
  }
  entries.push(...grants.filter(g=>!['full','five_hour'].includes(g.kind)));
  entries.sort((x,y)=>(y.id===next)-(x.id===next));
  const rows=entries.map(g=>g.placeholder?`<div class="reset-row reset-row-none"><div><strong>${esc(grantLabel(g))}</strong><p>None right now</p></div></div>`:row(g));
  const status=surface || (readOnly?'Manage resets on your computer.':resetModalMessage || (serviceDown?(grants.length?'Reset service unavailable.':''):!a?'Account unavailable.':pending?(pending.state==='uncertain'?'The earlier reset was not confirmed. Other resets wait until it is settled.':'A reset is in progress. Other resets wait until it finishes.'):r.eligible===false?'This account is not eligible for resets.':cooldown?`Cooldown until ${dateTime(r.cooldown_until*1000)}.`:resetConfirmCredit?'Confirm uses this reset now.':''));
  const locked=r.automatic_locked===true;
  $('reset-dialog-account').textContent=a?(a.email || a.label || 'Claude'):'Account unavailable';
  $('reset-dialog-content').innerHTML=`
    <div class="reset-list-heading">Available <span>${r.available==null?'—':esc(r.available)}</span></div>
    <div class="reset-list">${serviceDown && !grants.length?'<p class="reset-empty">Reset service unavailable.</p>':rows.join('')}</div>
    ${weekly?`<p class="reset-warning">Weekly limit resets on its own in ${esc(countdown(weekly.resets_at))}. A full reset now would mostly be wasted.</p>`:''}
    ${five?`<p class="reset-warning">5-hour limit resets on its own in ${esc(countdown(five.resets_at))}. A 5-hour reset now would mostly be wasted.</p>`:''}
    <p class="reset-modal-status" role="status">${esc(status)}</p>
    <div class="reset-auto-setting"><label><span>Auto-use before expiry<small>${surface || (locked?'Locked on this desktop.':serviceDown?'Reset service unavailable.':'Used in a grant’s final five minutes.')}</small></span><input id="reset-auto-toggle" type="checkbox" role="switch" aria-label="Auto-use before expiry" ${r.automatic?'checked':''} ${readOnly||working||locked||serviceDown||!a||surface?'disabled':''} ${surface?`title="${esc(surface)}"`:''}></label></div>
    ${r.last_event?`<p class="reset-last-event">${esc(watchLastEvent(r))}</p>`:''}`;
  if(focus)$('reset-dialog-content').querySelector(`[data-grant-action="${CSS.escape(focus)}"]`)?.focus();
  if(autoFocus)$('reset-auto-toggle')?.focus();
}
function renderResetModal() {
  if(!resetModalAccount || !$('reset-dialog').open)return;
  if(resetModalWatch){renderWatchResetModal();return;}
  const a=accountFor(resetModalAccount),r=a?.resets || {},credits=resetCredits(a);
  const pending=resetPendingCredits.get(resetModalAccount) || r.pending_credit_id;
  const working=resetBusy.has(resetModalAccount);
  const readOnly=!!current?.read_only;
  const blocked=resetBlockedReason(a);
  const unavailable=readOnly || !!blocked || !!r.error || failed || current?.gateway.online!==true;
  const weekly=(a?.windows || []).find(w=>w.seconds===604800);
  const untilReset=weekly?.resets_at ? weekly.resets_at-Date.now()/1000 : null;
  const resetSoon=credits.length>0 && untilReset!==null && untilReset>0 && untilReset<6*3600;
  if(resetConfirmCredit && resetConfirmCredit!==credits[0]?.id)resetConfirmCredit=null;
  const key=JSON.stringify([a?.email,r,working,unavailable,blocked,resetSoon,resetConfirmCredit,resetModalMessage]);
  if(key===resetModalKey)return;
  resetModalKey=key;
  const focus=document.activeElement?.dataset?.creditAction;
  const autoFocus=document.activeElement?.id==='reset-auto-toggle';
  $('reset-dialog-account').textContent=a?.email ? `Codex · ${a.email}` : 'Account unavailable';
  $('reset-dialog-content').innerHTML=`
    <div class="reset-list-heading">Available <span>${r.available==null?'—':esc(r.available)}</span></div>
    <div class="reset-list">${credits.length?credits.map((c,i)=>{
      const first=i===0,confirm=first && resetConfirmCredit===c.id;
      return `<div class="reset-row ${first?'':'reset-row-later'}"><div><strong>Full reset</strong><p title="${c.expires_at?esc(dateTime(c.expires_at*1000)):''}">${c.expires_at?'Expires '+esc(resetExpiry(c.expires_at)):'Expiry not reported'}</p></div><div class="reset-row-action">${first?`<button type="button" class="reset-primary ${confirm?'confirm':''}" data-credit-action="${esc(c.id)}" ${unavailable||working?'disabled':''}>${working?'Working…':confirm?'Confirm':'Use reset'}</button>${confirm&&!working?'<button type="button" class="reset-cancel" data-reset-cancel>Cancel</button>':''}`:'<span class="reset-wait">Use earlier reset first</span>'}</div></div>`;
    }).join(''):'<p class="reset-empty">'+esc(r.error || 'No available resets.')+'</p>'}</div>
    ${resetSoon?`<p class="reset-warning">Weekly allowance resets on its own in ${esc(countdown(weekly.resets_at))}. A reset now would mostly be wasted.</p>`:''}
    <p class="reset-modal-status" role="status">${esc(readOnly?'Manage resets on your computer.':resetModalMessage || r.error || blocked || (pending?'Retrying the earlier reset.':resetConfirmCredit?'Confirm spends one reset credit for this account.':''))}</p>
    <div class="reset-auto-setting"><label><span>Auto-use before expiry<small>Try five minutes before expiry, with automatic retries.</small></span><input id="reset-auto-toggle" type="checkbox" role="switch" aria-label="Auto-use before expiry" ${r.automatic?'checked':''} ${readOnly||working||blocked?'disabled':''}></label></div>
    ${r.last_event?`<p class="reset-last-event">${esc(r.last_event.message)} · ${esc(dateTime(r.last_event.at*1000))}${stepsFailed(r.last_event.steps)?' · <span class="reset-step-warning">Account may not be usable yet</span>':''}</p>`:''}`;
  if(focus)$('reset-dialog-content').querySelector(`[data-credit-action="${CSS.escape(focus)}"]`)?.focus();
  if(autoFocus)$('reset-auto-toggle')?.focus();
}
function closeResetModal() { $('reset-dialog').close(); }
let resetModalOpener=null;
function openResetModal(id,opener,watch=false) {
  resetModalAccount=id;resetModalWatch=watch;resetModalOpener=opener || null;resetConfirmCredit=null;resetModalMessage='';resetModalKey='';
  $('reset-dialog-description').textContent=watch?'Restore your 5-hour limit or all your limits.':'Restore your 5-hour limit, weekly limit, or both.';
  $('reset-dialog').showModal();renderResetModal();$('reset-dialog-close').focus();
}
$('reset-dialog').addEventListener('close',()=>{
  const old=resetModalAccount,opener=resetModalOpener;resetModalAccount=null;resetModalWatch=false;resetModalOpener=null;resetConfirmCredit=null;resetModalMessage='';resetModalKey='';
  (opener?.isConnected ? opener : document.querySelector(`[data-reset-open="${CSS.escape(old || '')}"]`))?.focus();
});
$('watch-grid').addEventListener('click',event=>{
  const button=event.target.closest('[data-reset-open]');if(!button)return;
  openResetModal(button.dataset.resetOpen,button,true);
});
$('reset-dialog-close').addEventListener('click',closeResetModal);
$('reset-dialog').addEventListener('click',event=>{if(event.target===$('reset-dialog')){const r=$('reset-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeResetModal();}});
$('account-grid').addEventListener('click',event=>{
  const button=event.target.closest('[data-reset-open]');if(!button)return;
  openResetModal(button.dataset.resetOpen,button);
});
async function resetAction(id,path,payload) {
  if(current?.read_only || resetBusy.has(id))return;
  const previousAutomatic=accountFor(id)?.resets?.automatic;
  if(path.endsWith('/policy') && accountFor(id)?.resets)accountFor(id).resets.automatic=payload.enabled;
  resetBusy.add(id);renderAccounts(current.gateway);renderResetModal();
  let message='';
  try {
    const result=await request(path,{method:'POST',body:JSON.stringify({account_id:id,...payload})});
    if(path.endsWith('/policy') && accountFor(id))accountFor(id).resets=result;
    if(!path.endsWith('/policy')) {
      if(result.outcome==='uncertain' && result.credit_id)resetPendingCredits.set(id,result.credit_id);
      else if(result.outcome!=='uncertain')resetPendingCredits.delete(id);
    }
    const messages={reset:'Reset applied.',already_redeemed:'This reset was already used.',nothing_to_reset:'No reset spent. The provider reports no eligible allowance to restore.',no_credit:'This reset is no longer available.',earliest_required:'Use the reset that expires first. The list has been refreshed.'};
    message=path.endsWith('/policy')?`Automatic expiry resets ${payload.enabled?'enabled':'disabled'}.`:(messages[result.outcome]||'Reset checked. Refreshing allowance.');
    if(result.outcome==='uncertain')message='Retrying the earlier reset. Confirm again to retry the same request.';
    if(!path.endsWith('/policy') && ['reset','already_redeemed'].includes(result.outcome) && stepsFailed(result.steps))message='Reset applied. The account may not be usable yet.';
  } catch (_) {
    if(path.endsWith('/policy') && accountFor(id)?.resets)accountFor(id).resets.automatic=previousAutomatic;
    message='The request could not be confirmed. Refresh before retrying the same reset.';
  } finally {
    resetBusy.delete(id);
    if(resetModalAccount===id){resetModalMessage=message;resetConfirmCredit=null;}
    await load();if(current)renderAccounts(current.gateway);renderResetModal();
  }
}
async function postOutcome(path,body) {
  const response=await fetch(path,{method:'POST',body:JSON.stringify(body),headers:{'X-Router-Request':'1','Content-Type':'application/json'},cache:'no-store',signal:AbortSignal.timeout(75000)});
  let data=null;try{data=await response.json();}catch(_){}
  return {status:response.status,ok:response.ok,data:data && typeof data==='object'?data:{}};
}
async function watchResetAction(id,path,payload) {
  const key='watch:'+id;
  if(current?.read_only || resetBusy.has(key))return;
  const policy=path.endsWith('/policy'),r=watchResets(watchFor(id));
  const previousAutomatic=r?.automatic;
  if(policy && r)r.automatic=payload.enabled;
  resetBusy.add(key);renderWatch(current);renderResetModal();
  let message='';
  try {
    const {status,ok,data}=await postOutcome(path,payload);
    if(policy){
      if(ok && typeof data.automatic==='boolean'){if(r)r.automatic=data.automatic;message=`Automatic expiry resets ${data.automatic?'enabled':'disabled'}.`;}
      else {if(r){r.automatic=previousAutomatic;if(status===423)r.automatic_locked=true;}message=status===423?'Automatic resets are locked on this desktop.':status===503?'Reset service unavailable.':'The request could not be confirmed.';}
    } else {
      // Only an explicit outcome confirms a reset; unreadable replies stay unconfirmed.
      const outcome=['reset','uncertain','rejected','unavailable'].includes(data.outcome)?data.outcome:status===503?'unavailable':status===409?'rejected':'uncertain';
      if(outcome==='uncertain')watchPendingGrants.set(id,data.grant_id || payload.grant_id);else watchPendingGrants.delete(id);
      const rejected={surface:'Use the Claude app for resets',not_usable:'This reset cannot be used right now.',pending:'Another reset is still in progress.',budget:'No reset budget left.',not_eligible:'This account is not eligible for resets.',cooldown:'Resets are in cooldown.',account_changed:'Claude account changed. Check the active account.',account_unbound:'Older reset state needs local resolution or re-enabling.'};
      message={reset:'Reset applied.',uncertain:'Not confirmed. Try again to retry the same request.',unavailable:'Reset service unavailable. Try again later.'}[outcome] || (outcome==='rejected'?rejected[data.code] || `Reset rejected${data.code?' · '+data.code:''}.`:'Reset checked. Refreshing usage.');
    }
  } catch (_) {
    if(policy && r)r.automatic=previousAutomatic;
    message='The request could not be confirmed. Refresh before retrying the same reset.';
  } finally {
    resetBusy.delete(key);
    if(resetModalAccount===id && resetModalWatch){resetModalMessage=message;resetConfirmCredit=null;}
    await load();if(current)renderWatch(current);renderResetModal();
  }
}
$('reset-dialog-content').addEventListener('change',event=>{
  if(event.target.id!=='reset-auto-toggle')return;
  if(resetModalWatch)watchResetAction(resetModalAccount,'/api/claude/resets/policy',{enabled:event.target.checked});
  else resetAction(resetModalAccount,'/api/resets/policy',{enabled:event.target.checked});
});
$('reset-dialog-content').addEventListener('click',event=>{
  if(event.target.closest('[data-reset-cancel]')){resetConfirmCredit=null;resetModalMessage='';renderResetModal();return;}
  const grant=event.target.closest('[data-grant-action]');
  if(grant){
    if(grant.disabled || !resetModalWatch || resetBusy.has('watch:'+resetModalAccount))return;
    const gid=grant.dataset.grantAction;
    if(resetConfirmCredit!==gid){resetConfirmCredit=gid;resetModalMessage='';renderResetModal();return;}
    watchResetAction(resetModalAccount,'/api/claude/resets/consume',{grant_id:gid,confirmed:true});return;
  }
  const button=event.target.closest('[data-credit-action]');if(!button || button.disabled)return;
  const id=resetModalAccount,a=accountFor(id),credit=resetCredits(a)[0];
  if(!credit || button.dataset.creditAction!==credit.id || resetBusy.has(id))return;
  if(resetConfirmCredit!==credit.id){resetConfirmCredit=credit.id;resetModalMessage='';renderResetModal();return;}
  resetAction(id,'/api/resets/consume',{credit_id:credit.id,confirmed:true});
});
async function load(fresh=false) {
  if(busy){if(fresh){await loadDone;return load(true);}return;}busy=true;
  let finish;loadDone=new Promise(resolve=>finish=resolve);
  const selected=range, generation=++fastLoadEpoch, epoch=routingEpoch;
  try {
    const requests=[request('/api/overview?since='+encodeURIComponent(startFor(selected)))];
    if(selected!=='today')requests.push(request('/api/overview?since='+encodeURIComponent(startFor('today'))));
    const results=await Promise.all(requests);
    if(selected!==range)return;
    const previous=current?.gateway;
    if(epoch!==routingEpoch && current)for(const key of ['preference','claude_preference','routing_linked'])results[0].gateway[key]=current.gateway[key];
    current=results[0];today=selected==='today'?current:results[1];
    reconcileClaude(current.watch_accounts || [],generation);
    reconcileAccess(current.gateway,previous,generation);
    for(const a of current.gateway.accounts || []) {
      const pending=fastPending.get(a.id);
      if(pending && !pending.saving && generation>pending.after)fastPending.delete(a.id);
    }
    failed=false;render();
  } catch (_) {
    failed=true;
    if(current)render();else{$('notice').hidden=false;$('notice').textContent='Connecting to your Linux desktop. Make sure Tailscale is connected; retrying automatically.';}
  } finally {busy=false;finish();if(selected!==range)load();}
}
function showView() {
  const view=['overview','usage','system'].includes(location.hash.slice(1))?location.hash.slice(1):'overview';
  if(view!=='overview' && routingChoice && !routingChoice.applying){closeRoutingChoice(true);renderRoutingControls();}
  if(view!=='overview')closeCardMenu();
  document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==`${view}-view`);
  document.querySelectorAll('[data-view]').forEach(el=>{el.classList.toggle('active',el.dataset.view===view);if(el.dataset.view===view)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
}
window.addEventListener('hashchange',showView);
window.addEventListener('pagehide',()=>{
  if(accountFlow?.kind==='add' && accountFlow.session && (addLive(accountFlow) || ['cancelling','cancel-failed'].includes(accountFlow.view)))fetch('/api/accounts/add/cancel',{method:'POST',keepalive:true,body:JSON.stringify({session:accountFlow.session}),headers:{'X-Router-Request':'1','Content-Type':'application/json'}}).catch(()=>{});
});
document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{
  range=button.dataset.range;document.querySelectorAll('[data-range]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});load();
}));
$('theme').addEventListener('click',()=>{const value=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=value;try{localStorage.setItem('router-theme',value);}catch(_){};});
$('refresh').addEventListener('click',async()=>{
  $('refresh').disabled=true;$('refresh').classList.add('refreshing');
  try {await request('/api/refresh',{method:'POST'});await load();setTimeout(load,3000);}catch(_){failed=true;render();}finally{$('refresh').disabled=false;}
});
if(location.port==='8320' || navigator.platform.toLowerCase().includes('linux'))$('advanced-link').href='http://127.0.0.1:8317/management.html#/quota';
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
window.addEventListener('resize',reserveRouting);document.fonts?.ready.then(reserveRouting);
if(window.ResizeObserver){const rows=new ResizeObserver(reserveRouting);for(const id of ['codex-routing','claude-routing'])rows.observe($(id));}
showView();load();setInterval(()=>{if(!document.hidden && current?.gateway.accounts.some(a=>signinView(a).session))load();},3000);setInterval(load,15000);setInterval(()=>{if(current){$('updated').textContent=relative(current.gateway.checked_at);renderWatch(current);renderResetModal();}document.querySelectorAll('[data-reset]').forEach(el=>el.textContent=countdown(Number(el.dataset.reset)));},30000);
setInterval(()=>{if(current && !document.hidden && (routeConfirm.size || current.gateway.accounts.some(a=>signinView(a).session)))renderAccounts(current.gateway);},1000);
