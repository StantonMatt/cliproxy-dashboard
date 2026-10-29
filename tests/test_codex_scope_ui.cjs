'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
const document = {documentElement: {dataset: {}}, body: {}, querySelectorAll: () => [],
  querySelector: () => null, getElementById(id) {
    if (!elements.has(id)) elements.set(id, {addEventListener() {}, options: [],
      classList: {toggle() {}}, open: true});
    return elements.get(id);
  }};
document.activeElement = document.body;
const context = vm.createContext({document, Date, localStorage: {getItem: () => null},
  BillingDates: {describe: () => ({date: '14 Oct 2026'})}});
const source = fs.readFileSync(path.join(__dirname, '../dashboard/static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf("window.addEventListener('hashchange'")), context);
const email = 'account-b@example.com';
const account = {id: 'b', label: email, email, plan: 'Pro (10x)', windows: [], resets: {}};
const gateway = {accounts: [account], online: true, checked_at: new Date().toISOString(),
  pool: {state: 'unavailable'}, preference: {policy: 'round-robin', status: 'active'}};
context.renderAccounts(gateway);
assert.equal(elements.get('pool-availability').textContent, 'No accounts ready');
// Cards carry no subtitle: the gateway column names the provider.
assert.doesNotMatch(elements.get('account-grid').innerHTML, /ChatGPT subscription|account-email/);
// Card names keep one line and truncate, with the full email in the tooltip.
assert.match(elements.get('account-grid').innerHTML, /<span title="account-b@example\.com">account-b@example\.com<\/span>/);
assert.match(elements.get('account-grid').innerHTML, /aria-label="Codex · account-b@example\.com /);
// Help text appears only when routing can't be changed here.
assert.equal(elements.get('routing-help').hidden, true);
gateway.preference.policy = 'highest-remaining-quota';
context.renderRouting(gateway);
assert.equal(elements.get('routing-help').hidden, true);
vm.runInContext('current={read_only:true}', context);
context.renderRouting(gateway);
assert.equal(elements.get('routing-help').textContent, 'Change Codex routing from your computer dashboard');
assert.equal(elements.get('routing-help').hidden, false);
assert.equal(context.routingState({...gateway, preference: {drift: true, actual_priorities: {[email]: 100}}}),
  `Gateway priorities differ from this selection · currently prefers ${email}`);
context.fixture = {gateway, watch_accounts: [{id: 'claude', label: 'Claude A', email,
  windows: [], resets: {grants: []}}]};
vm.runInContext("current=fixture;resetModalAccount='b';renderResetModal()", context);
assert.equal(elements.get('reset-dialog-account').textContent, `Codex · ${email}`);
vm.runInContext("resetModalAccount='claude';renderWatchResetModal()", context);
assert.equal(elements.get('reset-dialog-account').textContent, `Claude A · ${email}`);
console.log('Codex scope UI: routing wording, email drift, card headers and reset subtitles passed.');
