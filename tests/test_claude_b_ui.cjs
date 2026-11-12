'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let clock = Date.parse('2026-10-14T12:00:00Z');
class FakeDate extends Date { static now() { return clock; } }
const elements = new Map();
const document = {documentElement: {dataset: {}}, querySelectorAll: () => [], getElementById(id) {
  if (!elements.has(id)) elements.set(id, {addEventListener() {}});
  return elements.get(id);
}};
const context = vm.createContext({document, fitStatus() {}, Date: FakeDate, localStorage: {getItem: () => null}});
const source = fs.readFileSync(path.join(__dirname, '../dashboard/static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf('function table(')), context);
const windows = [
  {kind: 'five_hour', label: '5-hour limit', used: 4, remaining: 96, resets_at: clock / 1000 + 14000, seconds: 18000},
  {kind: 'seven_day', label: 'Weekly · all models', used: 16, remaining: 84, resets_at: clock / 1000 + 302400, seconds: 604800},
];
const a = {id: 'claude', label: 'Claude A', email: 'a@example.com', plan: 'Max (20x)', status: 'ok',
  checked_at: new FakeDate(clock).toISOString(), windows, extra_usage: {enabled: false},
  resets: {eligible: true, service: 'ok', grants: [{id: 'grant', resets_left: 1}]}};
const b = {...a, id: 'claude-b', independent_windows:true, label: 'Claude B', plan: 'Pro', email: null, resets: null, extra_usage: null};
const render = accounts => {
  context.renderWatch({gateway: {accounts: [{}, {}]}, watch_accounts: accounts});
  return elements.get('watch-grid').innerHTML;
};
const before = render([a]);
const combined = render([a, b]);
assert.equal(combined.slice(0, combined.indexOf('</article>') + 10), before);
assert.equal(elements.get('claude-account-count').textContent, '2 / 2');
assert.equal((combined.match(/class="account-card claude-card"/g) || []).length, 2);
const emailHtml = render([{...b, email: 'alternate@example.test'}]);
assert.match(emailHtml, /<span title="alternate@example.test">alternate@example.test<\/span>/);
// No "Claude A/B" subtitle: the email and plan badge identify the card.
assert.doesNotMatch(emailHtml + before, /class="account-email"|Claude [AB]</);
const bHtml = render([b]);
assert.doesNotMatch(bHtml, /class="account-email"/);
render([]);
assert.equal(elements.get('watch-section').hidden, true);
render([b]);
assert.equal(elements.get('watch-section').hidden, false);
assert.equal(elements.get('claude-account-count').textContent, '1 / 1');
for (const status of ['missing_token', 'error', 'unavailable']) {
  const errorHtml = render([{...b, email: 'alternate@example.test', status,
    checked_at: new FakeDate(clock - 3600000).toISOString(), attempted_at: b.checked_at, error: status}]);
  assert.match(errorHtml, status === 'missing_token' ? /Sign in required/ : /Unavailable/);
  assert.match(errorHtml, /watch-state-down/);
  assert.match(errorHtml, /last known/);
  assert.match(errorHtml, /Last attempt/);
  assert.doesNotMatch(errorHtml, /pace-bar/);
  if (status === 'missing_token') {
    assert.match(errorHtml, /Waiting for usage data/);
    assert.match(errorHtml, /Not reported/);
    assert.doesNotMatch(errorHtml, /aria-valuenow/);
  } else assert.match(errorHtml, /84<span class="percent">%<\/span>/);
}

assert.match(bHtml, /<span title="Claude B">Claude B<\/span>/);
assert.match(bHtml, /<a class="small-badge plan-badge" href="https:\/\/claude\.ai\/settings\/billing"[^>]* aria-label="Pro · Billing">Pro<svg/);
assert.match(bHtml, /84<span class="percent">%<\/span>/);
// The five-hour bar shows pace as the weekly hero does, against its own five hours.
assert.match(bHtml, /title="[^"]*· 78% of the 5 hours left · 96% of the allowance left"><div class="secondary-quota"><span>5-hour limit<\/span><span>96% remaining/);
assert.match(bHtml, /<div class="limit-track has-pace" aria-hidden="true"><span style="width:96%"><\/span><i class="pace-bar" style="width:77.78%" aria-hidden="true"><\/i><\/div>/);
assert.match(bHtml, /quota-track has-pace/);
assert.doesNotMatch(bHtml, /Extra usage|data-reset-open|Resets <span>|Auto-use/);
const unknown = render([{...b, windows: windows.map(w => ({...w, used: null, remaining: null}))}]);
assert.match(unknown, /Waiting for usage data/);
assert.match(unknown, /5-hour limit<\/span><span>—<\/span>/);
assert.doesNotMatch(unknown, /pace-bar|aria-valuenow/);
const measured = {...b, windows: windows.map(w => ({...w, measured_at: b.checked_at}))};
for (const kind of ['five_hour', 'seven_day']) {
  const partial = {...measured, windows: measured.windows.map(w => w.kind === kind
    ? {...w, measured_at: new FakeDate(clock - 901000).toISOString()} : w)};
  const partialHtml = render([partial]);
  assert.doesNotMatch(partialHtml, /Stale · updated 15m ago|last known/);
  assert.match(partialHtml, /quota-track has-pace/);
}
assert.match(render([measured]), /quota-track has-pace/);
for (const value of [null, 'bad']) {
  assert.doesNotMatch(render([{...measured, windows: measured.windows.map(w => w.kind === 'seven_day'
    ? {...w, measured_at: value} : w)}]), /quota-track has-pace/);
}
for (const kind of ['five_hour', 'seven_day']) {
  for (const offset of [0, -1]) {
    const due = {...measured, windows: measured.windows.map(w => w.kind === kind
      ? {...w, resets_at: clock / 1000 + offset} : w)};
    const dueHtml = render([due]);
    if (kind === 'seven_day') {
      assert.match(dueHtml, /Waiting for usage data/);
      assert.doesNotMatch(dueHtml, /pace-bar|aria-valuenow/);
    } else {
      assert.match(dueHtml, /5-hour limit<\/span><span>—<\/span>/);
      assert.doesNotMatch(dueHtml, /limit-track has-pace/);
    }
    assert.equal(due.windows.find(w => w.kind === kind).used, kind === 'five_hour' ? 4 : 16);
  }
}
// A's rendering ignores B-only measurement fields and retains its existing behavior.
assert.equal(render([{...a, windows: windows.map(w => ({...w, measured_at: null}))}]), before);
const expiresOnPage = {...measured, windows: measured.windows.map(w => w.kind === 'seven_day'
  ? {...w, resets_at: clock / 1000 + 1} : w)};
assert.match(render([expiresOnPage]), /quota-track has-pace/);
clock += 1000;
assert.doesNotMatch(render([expiresOnPage]), /quota-track has-pace|aria-valuenow/);
assert.match(render([expiresOnPage]), /Waiting for usage data/);
// Gateway windows expire between polls even though their watch-specific flag is false.
{
  const gateway = {...b, independent_windows: false, usage_source: 'gateway',
    checked_at: new FakeDate(clock).toISOString(), windows: windows.map(w => ({...w,
      usage_source: 'gateway', measured_at: new FakeDate(clock).toISOString(), resets_at: clock / 1000 + 1}))};
  const original = JSON.stringify(gateway);
  assert.match(render([gateway]), /84<span class="percent">%<\/span>/);
  assert.match(render([gateway]), /of the 5 hours left · 96% of the allowance left"><div class="secondary-quota"><span>5-hour limit<\/span><span>96% remaining/);
  clock += 1000;
  for (const offset of [0, 1000]) {
    clock += offset;
    const expired = render([gateway]);
    assert.match(expired, /Waiting for usage data/);
    assert.match(expired, /5-hour limit<\/span><span>—<\/span>/);
    assert.doesNotMatch(expired, /pace-bar|aria-valuenow|84<span class="percent">/);
  }
  assert.equal(JSON.stringify(gateway), original);
}
clock = windows[0].resets_at * 1000;
const stale = render([b]);
assert.match(stale, /last known/);
assert.doesNotMatch(stale, /pace-bar/);
console.log('Claude B UI: A unchanged, per-window reset freshness, full refresh, due resets, pace, unknowns and stale state passed.');
