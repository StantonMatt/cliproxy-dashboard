'use strict';
// Allowance forecast on account cards: wording per state, thresholds, hover text, gating and card wiring.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/static/app.js'), 'utf8');
let clock = Date.parse('2026-11-01T12:00:00Z');
class FakeDate extends Date { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } }
const elements = new Map();
const document = {documentElement: {dataset: {}}, body: {}, querySelector: () => null, querySelectorAll: () => [],
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {addEventListener() {}});
    return elements.get(id);
  }};
document.activeElement = document.body;
const context = vm.createContext({document, fitStatus() {}, Date: FakeDate, localStorage: {getItem: () => null},
  BillingDates: {describe: () => ({date: '14 Oct 2026'})}});
vm.runInContext(source.slice(0, source.indexOf('function table(')), context);
vm.runInContext(source.slice(source.indexOf('function providerStatus('), source.indexOf('function renderProviders(')), context);
context.renderRouting = () => {};
const now = () => clock / 1000;
const H = 3600, D = 86400;
const fc = (state, extra = {}) => ({state, reason: 'at_current_pace', runs_out_at: null, rate_per_hour: null, basis_minutes: 0, observed_at: now(), ...extra});
const win = (forecast, resetIn = 5 * D, extra = {}) => ({label: 'Weekly', seconds: 604800, remaining: 40, used: 60, resets_at: now() + resetIn, forecast, ...extra});
const f = (w, remaining = w.remaining, shown = true) => context.forecast(w, remaining, shown);
const pick = r => r && {tone: r.tone, long: r.long, short: r.short, quiet: !!r.quiet};

// Formatting: duration as the reset countdown, rate with one decimal under 10, basis like a countdown.
assert.equal(context.duration(2 * D + 4 * H), '2d 4h');
assert.equal(context.duration(H + 40 * 60), '1h 40m');
assert.equal(context.duration(14 * 60), '14m');
assert.equal(context.duration(59 * 60 + 30), '1h 0m');
assert.equal(context.duration(4 * H + 59 * 60 + 30), '5h 0m');
assert.equal(context.duration(23 * H + 59 * 60 + 30), '1d 0h');
assert.equal(context.countdown(now() + 14 * 60), '14m');
assert.equal(context.countdown(now() + 4 * H + 59 * 60 + 30), '5h 0m');
assert.equal(context.countdown(now() + 23 * H + 59 * 60 + 30), '1d 0h');
assert.equal(vm.runInContext('forecastRate', context)({rate_per_hour: 0.011}), '1.1%/h');
assert.equal(vm.runInContext('forecastRate', context)({rate_per_hour: 0.13}), '13%/h');
assert.equal(vm.runInContext('forecastBasis', context)(45), '45m');
assert.equal(vm.runInContext('forecastBasis', context)(120), '2h');
assert.equal(vm.runInContext('forecastBasis', context)(150), '2h 30m');

// runs_out: plain amber, red within 30 minutes, "now" under a minute, and short of reset (under an hour) first.
const run = (inSec, resetIn = 5 * D) => f(win(fc('runs_out', {runs_out_at: now() + inSec, rate_per_hour: 0.008, basis_minutes: 180}), resetIn));
assert.deepEqual(pick(run(2 * D + 4 * H)), {tone: 'out', long: 'Runs out in 2d 4h', short: 'Out in 2d 4h', quiet: false});
assert.deepEqual(pick(run(31 * 60)), {tone: 'out', long: 'Runs out in 31m', short: 'Out in 31m', quiet: false});
assert.deepEqual(pick(run(30 * 60)), {tone: 'soon', long: 'Runs out in 30m', short: 'Out in 30m', quiet: false});
assert.deepEqual(pick(run(14 * 60)), {tone: 'soon', long: 'Runs out in 14m', short: 'Out in 14m', quiet: false});
assert.deepEqual(pick(run(30)), {tone: 'soon', long: 'Runs out now', short: 'Out now', quiet: false});
assert.deepEqual(pick(run(-120)), {tone: 'soon', long: 'Runs out now', short: 'Out now', quiet: false});
assert.deepEqual(pick(run(2 * H - 12 * 60, 2 * H)), {tone: 'out', long: '12m short of reset', short: '12m short', quiet: false});
// Short of reset wins over within 30 minutes and over now.
assert.deepEqual(pick(run(10 * 60, 50 * 60)), {tone: 'out', long: '40m short of reset', short: '40m short', quiet: false});
assert.deepEqual(pick(run(-60, 20 * 60)).long, '21m short of reset');
assert.equal(run(2 * H - 61 * 60, 2 * H).long, 'Runs out in 59m');
// Unknown reset: the plain form.
assert.equal(f({...win(fc('runs_out', {runs_out_at: now() + 3 * H, rate_per_hour: 0.2, basis_minutes: 60})), resets_at: null}).long, 'Runs out in 3h 0m');
// Hover: "At this pace", the time, how long before the reset, rate and basis.
assert.match(run(2 * D + 4 * H).title, /^At this pace, runs out about [A-Z][a-z]{2} \d+, \d+:\d\d [AP]M, 2d 20h before the reset · 0\.8%\/h over the last 3h$/);
assert.match(run(30).title, /^At this pace, runs out about now, 5d 0h before the reset · 0\.8%\/h over the last 3h$/);
assert.match(f(win(fc('runs_out', {runs_out_at: now() + 3 * H, rate_per_hour: 0.2, basis_minutes: 0}), 30 * D)).title, /^At this pace, runs out about \d+:\d\d [AP]M, 29d 21h before the reset · 20%\/h$/);

// lasts: muted, never a deadline; the hover estimates what's left at the reset, never below 0.
const lasts = f(win(fc('lasts', {runs_out_at: now() + 30 * D, rate_per_hour: 0.011, basis_minutes: 120, observed_at: now() - 3600}), 24 * H));
assert.deepEqual(pick(lasts), {tone: 'lasts', long: 'Lasts to reset', short: 'Lasts', quiet: true});
assert.match(lasts.title, /^At this pace, lasts until the reset [A-Z][a-z]{2} \d+, \d+:\d\d [AP]M, with about 13% left · 1\.1%\/h over the last 2h$/);
assert.match(f(win(fc('lasts', {runs_out_at: now() + 30 * D, rate_per_hour: 0.5}), 24 * H)).title, /with about 0% left/);
assert.equal(f(win(fc('lasts', {runs_out_at: now() + 30 * D, rate_per_hour: 0.01}), null, {resets_at: null})).long, undefined);

// idle, out (back in / used up), and the states that show nothing.
assert.deepEqual(pick(f(win(fc('idle', {rate_per_hour: 0, basis_minutes: 40})))), {tone: 'idle', long: 'Idle', short: 'Idle', quiet: true});
assert.equal(f(win(fc('idle', {rate_per_hour: 0}))).title, 'No recent usage');
const back = f(win(fc('out'), D + 8 * H, {remaining: 0, used: 100}));
assert.deepEqual(pick(back), {tone: 'back', long: 'Back in 1d 8h', short: 'Back in 1d 8h', quiet: true});
assert.match(back.title, /^Allowance used up · back [A-Z][a-z]{2} \d+, \d+:\d\d [AP]M$/);
assert.deepEqual(pick(f({...win(fc('out')), resets_at: null, remaining: 0})), {tone: 'back', long: 'Used up', short: 'Used up', quiet: true});
for (const [forecast, title] of [
  [fc('learning', {reason: 'insufficient_history'}), 'Learning your pace'],
  [fc('learning', {reason: 'superseded_observation'}), 'Waiting for a newer reading'],
  [fc('stale', {reason: 'projection_elapsed'}), 'Waiting for updated usage'],
  [fc('unavailable', {reason: 'invalid_reset'}), 'Forecast unavailable'],
  [fc('surprise'), 'Forecast unavailable'],
  [fc('runs_out', {runs_out_at: null}), 'Forecast unavailable'],
  [undefined, 'Forecast unavailable'],
]) {
  const r = f(win(forecast));
  assert.equal(r.long, undefined); assert.equal(r.title, title);
  assert.equal(context.forecastHtml(r), '');
}
// Hidden with the card's freshness, without a figure, and while the connection is lost.
assert.equal(f(win(fc('idle')), 40, false), null);
assert.equal(f(win(fc('idle')), null), null);
vm.runInContext('failed=true', context);
assert.equal(f(win(fc('idle'))), null);
vm.runInContext('failed=false', context);

// Markup: wide and short forms when they differ, the quiet class, no title on the phrase itself.
assert.equal(context.forecastHtml(run(14 * 60)), '<span class="forecast fc-soon"><span class="fc-long">Runs out in 14m</span><span class="fc-short">Out in 14m</span></span>');
assert.equal(context.forecastHtml(f(win(fc('idle')))), '<span class="forecast fc-idle fc-quiet">Idle</span>');
assert.equal(context.paceValueText(null, 22, run(H + 40 * 60)), ' aria-valuetext="22% remaining, runs out in 1h 40m"');
assert.equal(context.paceValueText({time: 63, period: 'the week'}, 22, back), ' aria-valuetext="22% remaining, back in 1d 8h, 63% of the week left"');

// Cards. Claude: the hero's foot line and limit rows; an out account mutes its other limits and their forecasts.
const iso = t => new FakeDate(t).toISOString();
function claude(windows, extra = {}) {
  context.renderWatch({gateway: {accounts: [], online: true}, watch_accounts: [{id: 'c', email: 'c@example.com', status: 'ok',
    checked_at: iso(clock), windows, ...extra}]});
  return elements.get('watch-grid').innerHTML;
}
const weekly = (forecast, remaining = 41, resetIn = 5 * D) => ({kind: 'seven_day', label: 'Weekly · all models', seconds: 604800, remaining, used: 100 - remaining,
  resets_at: now() + resetIn, measured_at: iso(clock), forecast});
const fable = (forecast, remaining = 83) => ({kind: 'weekly_model', model: 'Fable', label: 'Weekly · Fable', seconds: 604800, remaining, used: 100 - remaining,
  resets_at: now() + 5 * D, measured_at: iso(clock), forecast});
const routable = {routing_id: 'r', routing_enabled: true, runtime_available: true};
let html = claude([weekly(fc('runs_out', {runs_out_at: now() + 52 * H, rate_per_hour: 0.008, basis_minutes: 180})), fable(fc('lasts', {runs_out_at: now() + 40 * D, rate_per_hour: 0.002}))], routable);
assert.match(html, /<div class="quota-used" title="At this pace, runs out about [^"]+ · 0\.8%\/h over the last 3h · \d+% of the week left · 41% of the allowance left"><span class="forecast fc-out"><span class="fc-long">Runs out in 2d 4h<\/span><span class="fc-short">Out in 2d 4h<\/span><\/span><span class="used-figure">59% used<\/span><span class="foot-reset"[^>]*>Resets in 5d 0h<\/span><\/div>/);
assert.match(html, /aria-valuetext="41% remaining, runs out in 2d 4h, \d+% of the week left"/);
assert.match(html, /<div class="limit-row" title="Resets [^"]+ · As of [^"]+ · At this pace, lasts until the reset [^"]+"><div class="secondary-quota"><span>Weekly · Fable<\/span><span class="forecast fc-lasts fc-quiet"><span class="fc-long">Lasts to reset<\/span><span class="fc-short">Lasts<\/span><\/span><span>83% remaining<\/span>/);
assert.match(html, /<span>Ready<\/span><\/button>/);
// "Ready" needs the gateway to be able to route to it; otherwise the freshness or "Unavailable" shows.
assert.doesNotMatch(claude([weekly(fc('lasts'))]), /<span>Ready<\/span>/);
assert.match(claude([weekly(fc('lasts'))], {...routable, runtime_available: false}), /<span>Unavailable<\/span><\/button>/);
html = claude([weekly(fc('out'), 0), fable(fc('idle', {rate_per_hour: 0}))]);
assert.match(html, /class="account-card claude-card is-out"/);
assert.match(html, /<span class="forecast fc-back fc-quiet">Back in 5d 0h<\/span>/);
assert.match(html, /<div class="limit-row muted"[^>]*><div class="secondary-quota"><span>Weekly · Fable<\/span><span>83% remaining<\/span><\/div><div class="limit-track" aria-hidden="true"><span style="width:83%"><\/span><\/div>/);
assert.match(html, /<span>Out · back in 5d 0h · [^<]+<\/span>/);
// Out on the 5-hour limit: the weekly hero shows no pace or forecast.
const fiveOut = {kind: 'five_hour', label: '5-hour limit', seconds: 18000, remaining: 0, used: 100, resets_at: now() + 2 * H, measured_at: iso(clock), forecast: fc('out')};
html = claude([weekly(fc('runs_out', {runs_out_at: now() + 3 * H, rate_per_hour: 0.1})), fiveOut], routable);
assert.match(html, /class="account-card claude-card is-out"/);
assert.doesNotMatch(html, /<div class="quota-used"[^>]*><span class="forecast/);
// Routing off reads "Disabled", as on Codex and other cards.
html = claude([weekly(fc('lasts', {runs_out_at: now() + 40 * D, rate_per_hour: 0.002}))], {routing_id: 'r', routing_enabled: false});
assert.match(html, /<span>Disabled<\/span><\/button>/);
// A stale reading shows no forecast anywhere.
html = claude([weekly(fc('runs_out', {runs_out_at: now() + 3 * H, rate_per_hour: 0.1})), fable(fc('idle'))], {status: 'stale'});
assert.doesNotMatch(html, /class="forecast/);

// Codex reads its forecast from the provider view (authoritative), matched by slot.
const codexWindow = {slot: 'secondary_window', label: '5-hour', seconds: 18000, remaining: 22, used: 78, resets_at: now() + 3 * H, measured_at: iso(clock)};
const hero = {slot: 'primary_window', label: 'Weekly', seconds: 604800, remaining: 64, used: 36, resets_at: now() + 3 * D, measured_at: iso(clock)};
vm.runInContext(`current={providers:[{id:'codex',accounts:[{key:'k',email:'one@example.com',windows:${JSON.stringify([
  {...hero, forecast: fc('lasts', {runs_out_at: now() + 9 * D, rate_per_hour: 0.011, basis_minutes: 120})},
  {...codexWindow, forecast: fc('runs_out', {runs_out_at: now() + 100 * 60, rate_per_hour: 0.13, basis_minutes: 45})}])}}]}],gateway:{accounts:[]}}`, context);
context.renderAccounts({online: true, checked_at: iso(clock), accounts: [{id: 'one', key: 'k', email: 'one@example.com', plan: 'Pro', quota_checked_at: iso(clock),
  availability: {state: 'ready'}, windows: [{...hero, forecast: fc('learning')}, {...codexWindow, forecast: fc('learning')}]}]});
html = elements.get('account-grid').innerHTML;
assert.match(html, /<span class="forecast fc-lasts fc-quiet"><span class="fc-long">Lasts to reset<\/span>/);
assert.match(html, /<span>5-hour limit<\/span><span class="forecast fc-out"><span class="fc-long">Runs out in 1h 40m<\/span><span class="fc-short">Out in 1h 40m<\/span><\/span><span>22% remaining<\/span>/);
// Shared domain: cards show the part before it; the title keeps the address.
assert.match(html, /<span title="one@example.com">one<\/span>/);
assert.equal(context.shortName('two@example.com'), 'two');
assert.equal(context.shortName('two@other.com'), 'two@other.com');
assert.equal(context.shortName('Claude B'), 'Claude B');
assert.equal(context.shortName('x@example.com', ''), 'x@example.com');
// A Codex account out on its 5-hour limit: the weekly hero recedes too, with no forecast.
context.renderAccounts({online: true, checked_at: iso(clock), accounts: [{id: 'one', key: 'k', email: 'one@example.com', plan: 'Pro', quota_checked_at: iso(clock),
  availability: {state: 'ready'}, windows: [hero, {...codexWindow, remaining: 0, used: 100}]}]});
html = elements.get('account-grid').innerHTML;
assert.match(html, /class="account-card codex-card is-out"/);
assert.doesNotMatch(html, /<div class="quota-used"[^>]*><span class="forecast/);
// A Codex account that is out mutes its other limits too.
context.renderAccounts({online: true, checked_at: iso(clock), accounts: [{id: 'one', key: 'k', email: 'one@example.com', plan: 'Pro', quota_checked_at: iso(clock),
  availability: {state: 'ready'}, windows: [{...hero, remaining: 0, used: 100}, codexWindow]}]});
html = elements.get('account-grid').innerHTML;
assert.match(html, /<div class="limit-row muted"[^>]*><div class="secondary-quota"><span>5-hour limit<\/span><span>22% remaining<\/span>/);
vm.runInContext('current=null', context);

// Other providers: the forecast takes the requests-today spot only when it has something to say.
const p = {id: 'xai', name: 'Grok', accounts: []};
const grok = forecast => context.genericCard(p, {key: 'x', email: 'x@example.com', plan: 'Premium', usage_checked_at: iso(clock), availability: {state: 'ready'},
  today: {requests: 3}, windows: [{label: 'Weekly', used: 6, remaining: 94, resets_at: now() + 4 * D, forecast}]}, {online: true, checked_at: iso(clock)});
assert.match(grok(fc('runs_out', {runs_out_at: now() - 30, rate_per_hour: 0.18, basis_minutes: 30})), /<div class="quota-used" title="At this pace, runs out about now[^"]*"><span class="forecast fc-soon"><span class="fc-long">Runs out now<\/span><span class="fc-short">Out now<\/span><\/span><span class="used-figure">6% used<\/span>/);
assert.match(grok(fc('learning')), /<div class="quota-used" title="Learning your pace"><span class="requests-today">3 requests today<\/span>/);

// CSS: the forecast's colours and the narrow-card rules.
const css = fs.readFileSync(path.join(__dirname, '../dashboard/static/style.css'), 'utf8');
assert.match(css, /\.forecast\.fc-out\{color:var\(--amber\)\}/);
assert.match(css, /\.forecast\.fc-soon\{color:var\(--danger\)\}/);
assert.match(css, /@container card-body \(max-width:300px\)\{\.limit-row \.forecast \.fc-long\{display:none\}\.limit-row \.forecast \.fc-short\{display:inline\}\}/);
assert.match(css, /@container card-body \(max-width:224px\)\{\.limit-row \.forecast\.fc-quiet\{display:none\}\}/);
console.log('Allowance forecast UI: formatting, run-out thresholds and order, lasts estimate, idle, out, hidden states, gating, markup, aria, Claude/Codex/other cards, muted sub-limits and shared-domain names passed.');
