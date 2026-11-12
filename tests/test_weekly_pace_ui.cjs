'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/static/app.js'), 'utf8');
const WEEK = 604800;
let clock = Date.parse('2026-10-09T12:00:00Z');
class FakeDate extends Date { static now() { return clock; } }
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
context.renderRouting = () => {};
const weekly = (time = 50) => ({seconds: WEEK, resets_at: clock / 1000 + WEEK * time / 100});
for (const [remaining, time, text, warn, under] of [
  [58, 50, '8% under pace', false, true], [40, 50, '10% over pace', false, false],
  [54, 50, 'On pace', false, true], [55, 50, '5% under pace', false, true],
  [46, 50, 'On pace', false, false], [45, 50, '5% over pace', false, false],
  [36, 50, '14% over pace', false, false], [35, 50, '15% over pace', true, false],
  [54.49, 50, 'On pace', false, true], [54.5, 50, '5% under pace', false, true],
  [0, 50, '50% over pace', true, false], [100, 120, 'On pace', false, false],
  [50, 50, 'On pace', false, false],
]) {
  const p = context.pace(weekly(time), remaining);
  assert.equal(p.time, Math.min(100, time));
  assert.equal(p.text, text); assert.equal(p.warn, warn); assert.equal(p.under, under);
}
for (const [w, remaining] of [
  [weekly(), null], [weekly(), undefined], [{resets_at: clock / 1000 + 100}, 50],
  [{...weekly(), seconds: 0}, 50], [{seconds: WEEK}, 50],
  [{...weekly(), resets_at: null}, 50], [{...weekly(), resets_at: NaN}, 50],
  [{...weekly(), resets_at: '2026-10-10T00:00:00Z'}, 50],
  [{...weekly(), resets_at: clock / 1000 - 1}, 50], [weekly(0), 50],
]) assert.equal(context.pace(w, remaining), null);
assert.equal(context.paceBar(null), '');
assert.equal(context.paceMark, undefined);
assert.equal(context.paceLabel, undefined);
assert.equal(context.paceValueText(null, 50), '');

function render(provider, windows, extra = {}) {
  if (provider === 'codex') {
    context.renderAccounts({online: true, checked_at: new FakeDate(clock).toISOString(),
      accounts: [{id: 'one', email: 'one@example.com', plan: 'Pro', quota_checked_at: new FakeDate(clock).toISOString(), windows}]});
    return elements.get('account-grid').innerHTML;
  }
  context.renderWatch({gateway: {accounts: []}, watch_accounts: [{id: 'claude',
    email: 'claude@example.com', status: 'ok', checked_at: new FakeDate(clock).toISOString(), windows, ...extra}]});
  return elements.get('watch-grid').innerHTML;
}
const w = {...weekly(63.1), kind: 'seven_day', label: 'Weekly', remaining: 17, used: 83};
const short = {seconds: 18000, kind: 'five_hour', label: '5-hour limit', remaining: 89,
  used: 11, resets_at: clock / 1000 + 3600};
for (const provider of ['codex', 'claude']) {
  const html = render(provider, [short, w]);
  // The weekly hero and the five-hour bar each show pace, the five-hour one against its own five hours.
  assert.equal((html.match(/class="pace-bar/g) || []).length, 2);
  assert.doesNotMatch(html, /pace-mark|--pace-at/);
  assert.match(html, /class="quota-track has-pace"/);
  assert.match(html, /aria-valuetext="17% remaining, 63% of the week left"/);
  assert.match(html, /<span style="width:17%"><\/span><i class="pace-bar pace-behind" style="width:63.10%" aria-hidden="true"><\/i>/);
  // The forecast took the pace text's place (none sent here); the pace figures are in the hover.
  assert.match(html, /<div class="quota-used" title="Forecast unavailable · 63% of the week left · 17% of the allowance left"><span class="used-figure">83% used<\/span><span class="foot-reset" data-reset-in="[\d.]+">Resets in 4d 10h<\/span><\/div>/);
  assert.match(html, /<div class="limit-row" title="Resets [^"]+ · Forecast unavailable · 20% of the 5 hours left · 89% of the allowance left"><div class="secondary-quota"><span>5-hour limit<\/span><span>89% remaining<\/span>/);
  assert.doesNotMatch(html, /class="pace[" ]|pace-word|over pace|under pace|On pace/);
  assert.match(html, /<div class="limit-track has-pace" aria-hidden="true"><span style="width:89%"><\/span><i class="pace-bar" style="width:20.00%" aria-hidden="true"><\/i><\/div>/);
  assert.match(render(provider, [{...short, remaining: 5, used: 95}, w]), /title="Resets [^"]+ · Forecast unavailable · 20% of the 5 hours left · 5% of the allowance left"/);
  for (const windows of [[], [{...w, remaining: null, used: null}],
    [{...w, resets_at: undefined}], [{...w, resets_at: clock / 1000 - 1}]]) {
    const without = render(provider, windows);
    // No pace value text on the allowance bar (the Codex speed slider has its own).
    assert.doesNotMatch(without, /has-pace|pace-bar|class="pace|role="progressbar"[^>]*aria-valuetext/);
    assert.match(without, /<div class="quota-used"[^>]*><span class="used-figure">[^<]+<\/span>(<span class="foot-reset"[^>]*>[^<]+<\/span>)?<\/div>/);
  }
  // A five-hour-only hero paces against its five hours.
  const fiveOnly = render(provider, [short]);
  assert.match(fiveOnly, /<div class="quota-track has-pace"/);
  assert.match(fiveOnly, /<div class="quota-used" title="Forecast unavailable · 20% of the 5 hours left · 89% of the allowance left">/);
  const empty = render(provider, [{...w, remaining: 0, used: 100}]);
  assert.match(empty, /aria-valuetext="0% remaining, 63% of the week left"/);
  assert.match(empty, /<span style="width:0%"><\/span><i class="pace-bar pace-behind" style="width:63.10%"/);
  assert.match(render(provider, [{...w, ...weekly(110)}]), /class="pace-bar pace-behind" style="width:100.00%"/);
  assert.match(render(provider, [{...w, ...weekly(110), remaining: 100, used: 0}]), /<span style="width:100%"><\/span><i class="pace-bar pace-behind" style="width:100.00%"/);
  assert.match(render(provider, [{...w, ...weekly(10), remaining: 32, used: 68}]), /<span style="width:32%"><\/span><i class="pace-bar" style="width:10.00%" aria-hidden="true"><\/i>/);
  const before = render(provider, [w]);
  clock += WEEK * 0.1 * 1000;
  assert.match(render(provider, [w]), /class="pace-bar pace-behind" style="width:53.10%"/);
  clock -= WEEK * 0.1 * 1000;
  assert.match(before, /style="width:63.10%"/);
}
const legacy = {...w}; delete legacy.seconds;
assert.match(render('claude', [legacy]), /class="pace-bar pace-behind" style="width:63.10%"/);
assert.doesNotMatch(render('codex', [legacy]), /pace-bar/);
assert.doesNotMatch(render('claude', [{...w, kind: 'weekly_model', model: 'Fable'}]), /pace-bar/);
// The weekly Fable bar shows pace under the hero; a Fable reading older than 15 minutes is last known and shows none.
const fable = {...weekly(63.1), kind: 'weekly_model', model: 'Fable', label: 'Weekly · Fable', remaining: 80, used: 20,
  measured_at: new FakeDate(clock).toISOString()};
const withFable = render('claude', [short, w, fable]);
assert.equal((withFable.match(/class="pace-bar/g) || []).length, 3);
assert.match(withFable, /title="[^"]*63% of the week left · 80% of the allowance left"><div class="secondary-quota"><span>Weekly · Fable<\/span><span>80% remaining<\/span>/);
const oldFable = render('claude', [short, w, {...fable, measured_at: new FakeDate(clock - 901000).toISOString()}]);
assert.match(oldFable, /<span>Weekly · Fable · last known<\/span><span>80% remaining<\/span>/);
assert.equal((oldFable.match(/class="pace-bar/g) || []).length, 2);
// A read the server deferred (fresh_until still ahead, e.g. until an exhausted window resets) stays current with pace.
const oldMeasured = new FakeDate(clock - 901000).toISOString();
const deferred = render('claude', [short, w, {...fable, measured_at: oldMeasured}], {fresh_until: new FakeDate(clock + 3600000).toISOString()});
assert.match(deferred, /title="[^"]*63% of the week left · 80% of the allowance left"><div class="secondary-quota"><span>Weekly · Fable<\/span>/);
assert.equal((deferred.match(/class="pace-bar/g) || []).length, 3);
const lapsed = render('claude', [short, w, {...fable, measured_at: oldMeasured}], {fresh_until: new FakeDate(clock - 1000).toISOString()});
assert.match(lapsed, /<span>Weekly · Fable · last known<\/span>/);
// A model reset expires independently of fresh primary readings and their deferred schedule.
for(const model of ['Fable','Sonnet']) {
  for(const extra of [{resets_at:clock/1000-1},{resets_at:clock/1000},
    {resets_at:null},{resets_at:null,invalid_reset:true}]) {
    const html=render('claude',[short,w,{...fable,model,label:'Weekly · '+model,...extra}],
      {fresh_until:new FakeDate(clock+3600000).toISOString()});
    assert.match(html,new RegExp(`<span>Weekly · ${model}<\\/span><span>—<\\/span>`));
    assert.doesNotMatch(html,/80% remaining|width:80%/);
  }
}
const idleFable=render('claude',[short,w,{...fable,resets_at:null,used:0,remaining:100}]);
assert.match(idleFable,/<span>Weekly · Fable<\/span><span>100% remaining<\/span>/);
const unknownFable=render('claude',[short,w,{...fable,resets_at:null,measured_at:oldMeasured}],
  {fresh_until:new FakeDate(clock+3600000).toISOString()});
assert.match(unknownFable,/<span>Weekly · Fable · last known<\/span><span>—<\/span>/);
assert.doesNotMatch(render('claude', [{...legacy, seconds: 0}]), /pace-bar/);
const fractional = {...w, ...weekly(50.4), remaining: 54.6, used: 45.4};
assert.match(render('claude', [fractional]), /title="Forecast unavailable · 50% of the week left · 55% of the allowance left"/);
assert.match(render('codex', [fractional]), /title="Forecast unavailable · 50% of the week left · 55% of the allowance left"/);
const under = {...w, ...weekly(50), remaining: 58, used: 42};
assert.match(render('codex', [under]), /aria-valuetext="58% remaining, 50% of the week left"/);
assert.match(render('codex', [under]), /<span style="width:58%"><\/span><i class="pace-bar" style="width:50.00%"/);
assert.match(render('claude', [under]), /<span style="width:58%"><\/span><i class="pace-bar" style="width:50.00%"/);
context.renderAccounts({online: true, checked_at: new FakeDate(clock).toISOString(),
  accounts: [{id: 'one', email: 'one@example.com', plan: 'Pro', windows: [short, w], quota_error: 'Quota check failed'}]});
assert.doesNotMatch(elements.get('account-grid').innerHTML, /pace-bar|class="pace/);
context.renderAccounts({online: false, checked_at: new FakeDate(clock).toISOString(),
  accounts: [{id: 'one', email: 'one@example.com', plan: 'Pro', windows: [short, w]}]});
assert.doesNotMatch(elements.get('account-grid').innerHTML, /pace-bar|class="pace/);
context.renderAccounts({online: true, checked_at: new FakeDate(clock).toISOString(),
  accounts: [{id: 'one', email: 'one@example.com', plan: 'Pro', disabled: true, quota_checked_at: new FakeDate(clock).toISOString(), windows: [short, w]}]});
assert.doesNotMatch(elements.get('account-grid').innerHTML, /pace-bar|class="pace/);
for (const status of ['expired_token', 'error']) {
  context.renderWatch({gateway: {accounts: []}, watch_accounts: [{id: 'claude', email: 'claude@example.com',
    status, checked_at: new FakeDate(clock).toISOString(), windows: [short, w, fable]}]});
  assert.doesNotMatch(elements.get('watch-grid').innerHTML, /pace-bar|class="pace/);
}
const css = fs.readFileSync(path.join(__dirname, '../dashboard/static/style.css'), 'utf8');
assert.match(css, /\.quota-used>:is\(\.used-figure,\.foot-reset\)\{margin-left:auto;white-space:nowrap\}/);
assert.doesNotMatch(css, /\.pace\{|\.pace-word|\.pace\.pace-warn/);
assert.doesNotMatch(css, /pace-mark|\.quota-track\.has-pace\{[^}]*overflow:visible/);
assert.match(css, /\.pace-bar\{[^}]*z-index:2[^}]*\}/);
assert.match(css, /\.pace-bar\.pace-behind\{z-index:0\}/);
assert.match(css, /\.quota-track\.has-pace>span\{[^}]*z-index:1[^}]*\}/);
console.log('Weekly pace UI: calculation boundaries, missing data, weekly hero selection, five-hour and Fable limit rows, legacy Claude fallback, pace bar stacking, markup, and render updates passed.');
