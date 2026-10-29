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
  return [usage.since,usage.claude?.since].filter(value=>Number.isFinite(Date.parse(value)))
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
// Claude usage rows carry the gateway auth index; the watch cards carry the email.
function claudeEmail(id) {
  return (current?.gateway.claude_accounts || []).find(a => a.auth_index === id)?.email || null;
}
// Chart and table colours: each gateway has its own hue, with a lighter or darker shade for its second account.
function claudeSeriesEmails() {
  const configured=Array.from($('claude-routing-mode').options || []).map(o=>o.value).filter(v=>v.includes('@'));
  return ['claude','claude-b'].map((id,i)=>(current?.watch_accounts || []).find(a=>a.id===id)?.email || configured[i]);
}
function series(provider, id, email) {
  if (provider === 'claude') {
    const mail = email || claudeEmail(id);
    const [first,second] = claudeSeriesEmails();
    return mail && mail === first ? 'claude-one' : mail && mail === second ? 'claude-two' : 'claude-unknown';
  }
  const accounts = current?.gateway.accounts || [];
  const a = accountFor(id) || accounts.find(x => email && x.email === email);
  return accounts.indexOf(a) === 1 ? 'codex-two' : 'codex-one';
}
// Until the gateway's first snapshot arrives, account routing and identities are unknown, not off.
function gatewayLoading(data=current) { return data?.gateway?.online == null; }
function providerChip(provider, id, email) {
  const name = email || (provider === 'claude' ? claudeEmail(id) : accountFor(id)?.email) || (gatewayLoading() ? '—' : 'Unknown account');
  return `<span class="account-chip" title="${provider === 'claude' ? 'Claude' : 'Codex'} · ${esc(name)}"><i class="${series(provider, id, email)}"></i>${esc(name)}</span>`;
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
  if (a.id === 'claude-b' && a.status === 'missing_token') return {text: 'Sign-in token missing', tone: 'down'};
  if (a.id === 'claude-b' && (a.status === 'error' || a.status === 'unavailable')) return {text: 'Unavailable', tone: 'down'};
  let checked = Date.parse(a.checked_at);
  if (a.id === 'claude-b') {
    const measured = (Array.isArray(a.windows) ? a.windows : [])
      .filter(w => ['five_hour', 'seven_day'].includes(w.kind))
      .map(w => Date.parse(Object.hasOwn(w, 'measured_at') ? w.measured_at : a.checked_at));
    checked = measured.length && measured.every(Number.isFinite) ? Math.min(...measured) : NaN;
  }
  const updated = a.id === 'claude-b' ? (Number.isFinite(checked) ? relative(checked) : '')
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
function watchFor(id) { return (current?.watch_accounts || []).find(a => a.id === id); }
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
  const accounts = Array.isArray(data.watch_accounts) ? data.watch_accounts : [];
  reconcileClaude(accounts);
  $('watch-grid').classList?.toggle('many-accounts',accounts.length>2);
  $('watch-section').hidden = !accounts.length && !data.gateway?.claude_accounts?.length && !data.gateway?.claude_preference?.saved_mode;
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
    const label = a.id === 'claude' ? 'Claude A' : a.label || 'Claude';
    const name = a.email || label;
    const state = watchState(a);
    const windows = (Array.isArray(a.windows) ? a.windows : []).map(w =>
      a.id === 'claude-b' && a.status === 'missing_token'
        ? {...w, used: null, remaining: null, resets_at: null}
        : a.id === 'claude-b' && Number.isFinite(w.resets_at) && w.resets_at <= Date.now()/1000
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
    return `<article class="account-card claude-card${claudeRoutingState(a) === false && !gatewayLoading(data) ? ' is-off' : ''}">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(name)}">${esc(name)}</span></div></div>${a.plan?`<span class="small-badge">${esc(a.plan)}</span>`:''}</div>
      <div class="account-body"><div class="quota-label">${esc(w?.label || 'Subscription allowance')}${stale ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(Math.round(remaining)))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="${esc(label)} · ${esc(name)} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${Math.round(remaining)}"`}${paceValueText(p, remaining)}><span style="width:${Math.min(100,remaining ?? 0)}%"></span>${paceBar(p)}</div>
      <div class="quota-used">${paceLabel(p)}<span>${w?.used == null ? 'Waiting for usage data' : esc(percent(w.used))+' used'}</span></div>
      <div class="limit-rows">${limits.join('')}</div>
      <div class="account-bottom"><div class="account-dates"><div class="reset-block"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div>${a.extra_usage ? `<div class="reset-block"><span>Extra usage</span><strong>${esc(extraUsageText(a.extra_usage))}</strong></div>` : ''}</div><span class="account-state watch-state-${state.tone}" title="${esc(state.text + (note ? ' · ' + note : ''))}">${icon(state.tone==='ok'?'check':'alert')}<span>${esc(state.text)}</span></span></div>
      ${resets}
      ${claudeRoutingRow(a)}</div>
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
  if(valid(value))return value;
  const saved=preference?.saved_mode==='preferred-account' ? preference.saved_preferred_email : preference?.saved_mode;
  return valid(saved) ? saved : provider==='claude' ? 'account-a@example.com' : 'round-robin';
}
function routingLocked() { return routingSaving || claudeRoutingSaving || routingAllSaving || routingLinkSaving; }
// "New conversations only" or "Move all conversations now": an inline choice per routing row.
const routingRows = {codex:{row:'codex-routing',prefix:'routing'}, claude:{row:'claude-routing',prefix:'claude-routing'}, all:{row:'routing-all',prefix:'routing-all'}};
function routingPart(key, part) { return $(routingRows[key].prefix+'-'+part); }
function routingMoveAvailable() { return current?.gateway?.move_available===true; }
function routingApplied(key) {
  const gateway=current?.gateway || {};
  return key==='claude' ? routingSelection(gateway.claude_preference,'claude') : routingSelection(gateway.preference,'codex');
}
function routingRowHidden(key) {
  const both=!$('watch-section').hidden, linked=both && routingLinked(current?.gateway);
  return key==='all' ? !linked : key==='codex' ? linked : !both || linked;
}
function routingRowSaving(key) { return key==='all' ? routingAllSaving : key==='claude' ? claudeRoutingSaving : routingSaving; }
function closeRoutingChoice(revert=true, focus=false) {
  const choice=routingChoice;if(!choice)return;
  routingChoice=null;
  const select=routingPart(choice.key,'mode');
  if(revert)select.value=routingApplied(choice.key);
  select.classList?.remove?.('pending');
  routingPart(choice.key,'choice-host').innerHTML='';
  if(focus)select.focus();
}
function openRoutingChoice(key, value) {
  if(routingLocked())return;
  if(routingChoice && routingChoice.key!==key)closeRoutingChoice(true);
  routingRetry.delete(key);
  routingPart(key,'feedback').textContent='';
  if(value===routingApplied(key)){closeRoutingChoice(true);renderRoutingControls();return;}
  if(routingChoice){routingChoice.value=value;renderRoutingControls();return;}
  routingChoice={key,value,applying:null};
  const line=routingRows[key].prefix+'-choice-line';
  // Built once per open; renders only update attributes, so focus and the alert survive refreshes.
  routingPart(key,'choice-host').innerHTML=`<div class="route-confirm" role="alert"><div>`+
    `<button type="button" class="reset-primary confirm" data-routing-choice="new">New conversations only</button>`+
    `<button type="button" class="reset-primary" data-routing-choice="move" aria-describedby="${line}">Move all conversations now</button>`+
    `<button type="button" class="reset-cancel" data-routing-choice="cancel">Cancel</button></div>`+
    `<span id="${line}">Moving affects both Codex and Claude conversations.</span></div>`;
  renderRoutingControls();
}
// A refresh closes an idle choice silently once it can no longer apply what it shows.
function expireRoutingChoice(key) {
  if(routingChoice?.key!==key || routingChoice.applying)return;
  if(failed || current?.read_only || current?.gateway?.online!==true || !routingMoveAvailable() || routingRowHidden(key) || routingApplied(key)===routingChoice.value)closeRoutingChoice(true);
}
function syncRoutingChoice(key) {
  const select=routingPart(key,'mode'), choice=routingChoice?.key===key ? routingChoice : null;
  select.classList?.toggle?.('pending',!!choice);
  if(!choice)return;
  const disabled=!!choice.applying || routingLocked() || select.disabled;
  for(const button of routingPart(key,'choice-host').querySelectorAll('[data-routing-choice]')) {
    button.disabled=disabled;
    if(choice.applying===button.dataset.routingChoice)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
  }
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
// Focus returns to the select when it was in the row, or was dropped to the body by disabled controls.
function routingRefocus(key) {
  const row=$(routingRows[key].row), select=routingPart(key,'mode');
  return ()=>{if(operationFocus(select.id) || row.contains?.(document.activeElement))select.focus?.();};
}
async function saveRouting(value, provider='codex', move=false) {
  if(routingLocked())return;
  routingEpoch++;
  const claude=provider==='claude', prefix=claude?'claude-routing':'routing', key=claude?'claude_preference':'preference';
  const select=$(prefix+'-mode'), row=$(routingRows[provider].row);
  const refocus=row.contains?.(document.activeElement) || operationFocus(select.id) ? routingRefocus(provider) : null;
  value=routingValue(value,current?.gateway[key],provider); select.value=value;
  const body=value.includes('@') ? {mode:'preferred-account', preferred_email:value} : {mode:value};
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
    $(prefix+'-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key===provider)closeRoutingChoice(false);
    await load(true);
    if(claude)claudeRoutingSaving=false;else routingSaving=false;
    renderRoutingControls();
    refocus?.();
  }
}
for(const provider of ['codex','claude']) {
  const prefix=provider==='claude'?'claude-routing':'routing';
  $(prefix+'-mode').addEventListener('change', ()=>routingMoveAvailable() ? openRoutingChoice(provider,$(prefix+'-mode').value) : saveRouting($(prefix+'-mode').value,provider));
  $(prefix+'-resume').addEventListener('click', ()=>saveRouting($(prefix+'-mode').value,provider));
}
// One routing choice covers both gateways unless the user separates them.
function routingLinked(gateway) { return gateway?.routing_linked!==false; }
function routingSelection(p, provider) { return routingValue(routingSuspended(p) ? null : p?.policy==='preferred-account' ? p.selected_email : p?.policy,p,provider); }
function renderRoutingAll(gateway) {
  const both=!$('watch-section').hidden, linked=both && routingLinked(gateway);
  syncRoutingLayout();expireRoutingChoice('all');
  $('routing-all').hidden=!linked; $('codex-routing').hidden=linked; $('claude-routing').hidden=!both || linked;
  const split=$('routing-split'), toggle=$('routing-link'), host=linked?$('routing-all'):$('claude-routing');
  if(split.parentElement!==host) {
    const focused=document.activeElement===toggle;
    // Keep the switch on the select's line, ahead of the full-width choice and state lines.
    host.querySelector('.routing-choice-host,.routing-state,.routing-states').before(split);
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
  routingEpoch++;
  const select=$('routing-all-mode'), row=$('routing-all');
  const refocus=!relinking && (row.contains?.(document.activeElement) || operationFocus(select.id)) ? routingRefocus('all') : null;
  value=routingValue(value,current?.gateway.preference); select.value=value;
  const body=value.includes('@') ? {mode:'preferred-account', preferred_email:value} : {mode:value};
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
    $('routing-all-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key==='all')closeRoutingChoice(false);
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
  choice.applying=kind;
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
for(const key of Object.keys(routingRows)) {
  const row=$(routingRows[key].row);
  row.addEventListener('click',event=>{
    const button=event.target.closest?.('[data-routing-choice]');
    if(!button || button.disabled || routingChoice?.key!==key)return;
    if(button.dataset.routingChoice==='cancel'){closeRoutingChoice(true,true);renderRoutingControls();}
    else routingChoiceApply(key,button.dataset.routingChoice);
  });
  row.addEventListener('focusout',event=>{
    if(routingChoice?.key===key && !routingChoice.applying && event.relatedTarget && !row.contains(event.relatedTarget)){closeRoutingChoice(true);renderRoutingControls();}
  });
  routingPart(key,'retry').addEventListener('click',()=>retryRoutingMove(key));
}
if(document.addEventListener)document.addEventListener('keydown',event=>{
  const choice=routingChoice;if(!choice || choice.applying)return;
  if(event.target!==document.body && !$(routingRows[choice.key].row).contains(event.target))return;
  if(event.key==='Escape'){event.preventDefault();closeRoutingChoice(true,true);renderRoutingControls();}
});
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
  $('account-grid').classList?.toggle('many-accounts',accounts.length>2);
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
    return `<article class="account-card codex-card${off?' is-off':''}" data-cardid="${esc(a.id)}" data-access-focus="${esc(a.id+':card')}" tabindex="-1">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(accountName(a))}">${esc(accountName(a))}</span></div></div><span class="small-badge">${esc(a.plan)}</span></div>
      <div class="account-body">${session?signinPanel(a,view,gateway):`<div class="quota-label">${esc(w?.label || 'Subscription')} allowance${stale && remaining!=null ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(remaining))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="Codex · ${esc(accountName(a))} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${remaining}"`}${paceValueText(p, remaining)}><span style="width:${remaining ?? 0}%"></span>${paceBar(p)}</div>
      <div class="quota-used">${paceLabel(p)}<span>${w?.used == null ? a.disabled?'Not available while disabled':'Waiting for quota data' : esc(percent(w.used))+' used'}</span></div>
      ${other.length?`<div class="limit-rows">${other.map(o=>limitRow(o.label==='5-hour'?'5-hour limit':o.label,o)).join('')}</div>`:''}
      ${a.quota_error&&!strip?`<div class="quota-warning">${esc(a.quota_error)}</div>`:''}`}
      ${!session&&outcome?.kind==='signed_in'?`<div class="access-result" role="status">${icon('check')}Signed in just now</div>`:''}
      ${!session&&outcome?.kind==='signed_in'&&outcome.enable_failed===true&&a.disabled===true?`<div class="quota-warning" role="status">Couldn't turn on routing</div>`:''}
      ${!session?signinStrip(a,gateway,outcome):''}
      ${a.access_error?`<div class="quota-warning" role="alert">${esc(a.access_error)}</div>`:''}
      <div class="account-bottom"><div class="account-dates"><div class="reset-block"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div><div class="reset-block billing-renewal ${billing.urgent?'renewal-soon':''}" title="${esc(`${billing.badge}. ${billing.note} ${verified}`)}"><span>${esc(billing.ended?billing.label:a.billing?.confirmed?'Renews':billing.label)}</span><strong>${esc(billing.date)}</strong></div></div>${!strip&&!session?`<span class="account-state" title="${esc(state)}">${a.availability?.state==='ready'&&!stale?icon('check'):''}<span>${esc(state)}</span></span>`:''}</div>
      <div class="account-resets"><button type="button" class="resets-button" data-reset-open="${esc(a.id)}" aria-haspopup="dialog">Resets <span>${r.available==null?'—':esc(r.available)}</span></button><span>${r.automatic?'Auto-use before expiry':''}</span></div>
      ${fastRow(a,gateway)}
      ${routingRow(a,gateway)}</div>
    </article>`;
  });
  if(!document.querySelector('[data-signin-handle]'))$('account-grid').innerHTML=cards.join('');
  else {
    for(const card of [...$('account-grid').children])if(!accounts.some(a=>a.id===card.dataset.cardid))card.remove();
    cards.forEach((html,index)=>{
    const template=document.createElement('template');template.innerHTML=html;
    const next=template.content.firstElementChild, old=$('account-grid').children[index];
    const panel=old?.querySelector('.signin-panel'), replacement=next.querySelector('.signin-panel');
    if(old?.dataset.cardid===next.dataset.cardid && panel && replacement &&
        panel.dataset.signinHandle===replacement.dataset.signinHandle &&
        panel.querySelector('code').textContent===replacement.querySelector('code').textContent) {
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
    } else if(old)old.replaceWith(next);else $('account-grid').append(next);
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
// Request flow keeps unmapped Claude history separate from the known accounts.
const SERIES = [['codex','codex-one'],['codex','codex-two'],['claude','claude-one'],['claude','claude-two'],['claude','claude-unknown']];
function seriesNames() {
  const accounts = current?.gateway.accounts || [], [first,second] = claudeSeriesEmails();
  return {'codex-one': accounts[0] && accountName(accounts[0]), 'codex-two': accounts[1] && accountName(accounts[1]),
    'claude-one': first, 'claude-two': second,
    'claude-unknown': 'Unknown account'};
}
function renderLegend() {
  const names = seriesNames();
  const unknown=[current?.usage,today?.usage].some(u=>[...(u?.claude?.accounts || []),...(u?.claude?.hours || []),
    ...(u?.recent || []).filter(r=>r.provider==='claude')].some(r=>series('claude',r.auth_index,r.email)==='claude-unknown'));
  const group = (title, provider) => {
    const items = SERIES.filter(([p, key]) => p === provider && names[key] && (key!=='claude-unknown' || unknown)).map(([, key]) => `<span><i class="${key}"></i>${esc(names[key])}</span>`);
    return items.length ? `<div class="legend-group ${provider}"><b>${title}</b>${items.join('')}</div>` : '';
  };
  for (const legend of document.querySelectorAll('[data-account-legend]')) legend.innerHTML = group('Codex', 'codex') + group('Claude', 'claude');
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
    return {date:d,'codex-one':0,'codex-two':0,'claude-one':0,'claude-two':0,'claude-unknown':0};
  });
  for (const [provider, rows] of [['codex', usage.hours], ['claude', usage.claude?.hours]]) for (const row of rows || []) {
    const d = new Date(row.hour);
    const bin = bins.find(b => daily ? b.date.toDateString()===d.toDateString() : b.date.getHours()===d.getHours() && b.date.toDateString()===d.toDateString());
    if (bin) bin[series(provider, row.auth_index, row.email)] += row.requests;
  }
  const sum = b => SERIES.reduce((n, [, key]) => n + b[key], 0);
  const max = Math.max(1,...bins.map(sum));
  const label = d => daily ? d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) : d.toLocaleTimeString(undefined,{hour:'2-digit',hour12:false});
  const ticks = [...new Set([0,Math.floor((length-1)/3),Math.floor(2*(length-1)/3),length-1])];
  const names = seriesNames();
  const title = b => SERIES.filter(([, key]) => names[key]).map(([provider, key]) => `${provider === 'claude' ? 'Claude' : 'Codex'} ${names[key]} ${b[key]}`).join(' · ');
  $(id).innerHTML = `<div class="bars" role="img" aria-label="Request counts over time. Details are available in the account usage table.">${bins.map(b=>`<div class="bar-slot" title="${esc(label(b.date))} · ${esc(title(b))} requests">${[...SERIES].reverse().map(([, key])=>`<span class="${key}" style="height:${b[key]/max*100}%"></span>`).join('')}</div>`).join('')}</div><div class="chart-labels">${ticks.map(i=>`<span>${esc(label(bins[i].date))}</span>`).join('')}</div>`;
}
function table(headers, rows, empty='No requests recorded in this period.', classes=[]) {
  if (!rows.length) return `<div class="empty-state">${esc(empty)}</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>`<tr${classes[i]?` class="${classes[i]}"`:''}>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const gatewayRow = provider => provider === 'claude' ? 'claude-row' : 'codex-row';
function renderUsage(data) {
  const u = data.usage, claude = u.claude || {}, codexRows = u.accounts || [], claudeRows = claude.accounts || [];
  const t = totals([...codexRows, ...claudeRows]), good = t.requests ? 100*(t.requests-t.failures)/t.requests : null;
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
  const row = provider => r => [`<span class="mono gateway-name">${provider === 'claude' ? 'Claude' : 'Codex'}</span>`,providerChip(provider,r.auth_index,r.email),number(r.requests),number(r.input_tokens),number(r.cached_tokens),cacheAmount(r,'cache_read'),cacheAmount(r,'cache_creation'),number(r.output_tokens),number(r.failures)];
  $('usage-accounts').innerHTML = table(['Gateway','Account','Requests','Input tokens','Cached input','Cache read','Cache write','Output tokens','Failures'],[...codexRows.map(row('codex')),...claudeRows.map(row('claude'))],undefined,[...codexRows.map(()=>'codex-row'),...claudeRows.map(()=>'claude-row')]);
  const models = [...(u.models || []).map(r=>({...r,provider:'codex'})), ...(claude.models || []).map(r=>({...r,provider:'claude'}))].sort((a,b)=>(b.requests||0)-(a.requests||0));
  $('usage-models').innerHTML = table(['Model','Requests','Tokens','Average duration'],models.map(r=>[esc(r.model),number(r.requests),number(r.total_tokens),r.latency_ms==null?'—':`${(r.latency_ms/1000).toFixed(1)}s`]),undefined,models.map(r=>gatewayRow(r.provider)));
  $('history-note').textContent = `History starts ${dateTime(usageSince(u))}. These are best-effort gateway records, refreshed every 15 seconds. Subscription allowances are reported separately by the provider.`;
}
function renderToday(data) {
  const u = data.usage, t = totals(u.accounts), c = u.claude ? totals(u.claude.accounts) : null;
  if (!u.available) {
    for (const id of ['today-requests','today-success','claude-today-requests','claude-today-success','tokens-today','cache-percent','cache-split']) $(id).textContent='—';
    $('token-detail').textContent='Waiting for recorded usage'; $('cache-fill').style.width='0%';
    $('overview-chart').innerHTML='<div class="empty-state">Usage history is temporarily unavailable.</div>';
    $('recent-table').innerHTML='<div class="empty-state">Waiting for recorded requests</div>';
    return;
  }
  const success = x => x?.requests ? percent((x.requests-x.failures)/x.requests*100) : '—';
  $('today-requests').textContent = number(t.requests);
  $('today-success').textContent = success(t);
  $('claude-today-requests').textContent = c ? number(c.requests) : '—';
  $('claude-today-success').textContent = success(c);
  const all = c ? totals([t, c]) : t;
  $('tokens-today').textContent = compact(all.total_tokens);
  $('token-detail').innerHTML = `<span><i class="codex-one"></i>Codex ${esc(compact(t.total_tokens))}</span><span><i class="claude-one"></i>Claude ${c ? esc(compact(c.total_tokens)) : '—'}</span>`;
  $('token-detail').title = `${number(all.input_tokens)} input · ${number(all.output_tokens)} output tokens`;
  const cache = all.input_tokens ? Math.min(100,all.cached_tokens/all.input_tokens*100) : 0;
  $('cache-fill').style.width = `${cache}%`;
  $('cache-percent').textContent = all.input_tokens ? percent(cache) : '—';
  $('cache-split').innerHTML = `${cacheAmount(all,'cache_read',compact)} · ${cacheAmount(all,'cache_creation',compact)}`;
  renderChart('overview-chart',u,'today');
  const recent = u.recent.slice(0,6);
  const recentCache = r => `<span class="mono" title="${esc(`Read ${number(r.cache_read_tokens)} · Write ${number(r.cache_creation_tokens)}`)}">${compact(r.cache_read_tokens)} · ${compact(r.cache_creation_tokens)}</span>`;
  $('recent-table').innerHTML = table(['Account','Model','Reasoning','Mode','Tokens','Cache read · write','Result','Time'],recent.map(r=>[providerChip(r.provider === 'claude' ? 'claude' : 'codex',r.auth_index,r.email),`<span class="mono">${esc(r.model)}</span>`,reasoningCell(r.reasoning_effort),modeCell(r),`<span class="mono" title="${number(r.total_tokens)}">${compact(r.total_tokens)}</span>`,recentCache(r),`<span class="outcome ${r.failed?'failed':''}">${r.failed?'Failed':icon('check')+'Completed'}</span>`,`<span class="mono" title="${esc(dateTime(r.timestamp))}">${esc(new Date(r.timestamp).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'}))}</span>`]),undefined,recent.map(r=>gatewayRow(r.provider)));
}
function renderSystem(data) {
  const g=data.gateway,r=g.routing, tailscale=data.access_transport==='tailscale';
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
  renderOperations(data);renderWatch(data);renderAccounts(g);renderRoutingAll(g);renderLegend();renderResetModal();renderToday(today || data);renderUsage(data);renderSystem(data);
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
  $('reset-dialog-account').textContent=a?`${a.id === 'claude' ? 'Claude A' : a.label || 'Claude'}${a.email?' · '+a.email:''}`:'Account unavailable';
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
  document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==`${view}-view`);
  document.querySelectorAll('[data-view]').forEach(el=>{el.classList.toggle('active',el.dataset.view===view);if(el.dataset.view===view)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
}
window.addEventListener('hashchange',showView);
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
showView();load();setInterval(()=>{if(!document.hidden && current?.gateway.accounts.some(a=>signinView(a).session))load();},3000);setInterval(load,15000);setInterval(()=>{if(current){$('updated').textContent=relative(current.gateway.checked_at);renderWatch(current);renderResetModal();}document.querySelectorAll('[data-reset]').forEach(el=>el.textContent=countdown(Number(el.dataset.reset)));},30000);
setInterval(()=>{if(current && !document.hidden && (routeConfirm.size || current.gateway.accounts.some(a=>signinView(a).session)))renderAccounts(current.gateway);},1000);
