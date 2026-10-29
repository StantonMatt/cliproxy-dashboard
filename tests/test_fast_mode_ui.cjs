'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../dashboard/static/app.js'),'utf8');
const css = fs.readFileSync(require('node:path').join(__dirname,'../dashboard/static/style.css'),'utf8');
const lightWarnings = css.match(/([^{}]+)\{color:#8a6100\}/)[1].split(',').map(s=>s.trim());
for (const selector of ['.fast-usage.warn','.mono.tier-warn']) {
  assert.ok(lightWarnings.includes(`[data-theme=light] ${selector}`),`${selector} has a light-theme override`);
}
function luminance(hex) {
  const rgb = hex.match(/[a-f\d]{2}/gi).map(v=>parseInt(v,16)/255)
    .map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);
  return rgb[0]*0.2126+rgb[1]*0.7152+rgb[2]*0.0722;
}
for (const color of ['#8a6100','#b8452c']) {
  assert.ok((luminance('#ffffff')+0.05)/(luminance(color)+0.05)>=4.5,`${color} is readable on white`);
}
assert.match(css,/\[data-theme=light\]\{--danger:#b8452c;/);
assert.match(css,/\.fast-usage\.error\{color:var\(--danger\)\}/);
for (const savedTheme of ['dark','light',null]) {
  const document = {documentElement:{dataset:{}}};
  vm.runInNewContext(source.slice(source.indexOf('try { document.documentElement.dataset.theme'),source.indexOf('function startFor(')),
    {document,localStorage:{getItem:()=>savedTheme}});
  assert.equal(document.documentElement.dataset.theme,savedTheme||'dark');
}
const block = source.slice(source.indexOf('function fastRow('),source.indexOf('function renderAccounts('));
const loading = source.slice(source.indexOf('async function load('),source.indexOf('function showView()'));
const account = {id:'one',fast_mode:{allowed:true,changed_at:null,enforcement:'active'}};
const timers = [], rows = [];
let clock = 100000;
class FakeDate extends Date {static now(){return clock;}}
const context = vm.createContext({Date:FakeDate,console,JSON,Map,Set,Error,Number,
  fastPending:new Map(),fastErrors:new Map(),fastFocus:null,fastLoadEpoch:0,
  routeErrors:new Map(),routePending:new Set(),signinErrors:new Map(),reconcileAccess(){},reconcileClaude(){},routingEpoch:0,loadDone:Promise.resolve(),
  current:{gateway:{online:true,accounts:[account]}},failed:false,busy:false,range:'today',today:null,
  esc:s=>String(s ?? '').replaceAll('&','&amp;').replaceAll('"','&quot;'),
  $:()=>({addEventListener(){}}),accountFor:()=>context.current.gateway.accounts[0],
  setTimeout:(fn,delay)=>{timers.push({fn,delay});},
  renderAccounts:()=>rows.push(context.fastRow(context.current.gateway.accounts[0],context.current.gateway)),
  render:()=>context.renderAccounts(),startFor:()=> '2026-10-02T00:00:00Z'});
vm.runInContext(block+loading,context);
vm.runInContext(source.slice(source.indexOf('function modeCell('),source.indexOf('let range =')),context);
const modeCases = [
  [null,null,true,'unknown','—','Service tier was not recorded','mono'],
  ['priority',null,false,'normal','Downgraded','Asked for priority · downgraded to normal','mono'],
  ['normal','unknown',false,'normal','Normal','Service tier: normal','mono'],
  ['priority','priority',false,'normal','Still fast','Fast mode off · still ran priority','mono tier-warn'],
  ['ultrafast','ultrafast',false,'normal','Still fast','Fast mode off · still ran ultrafast','mono tier-warn'],
  ['ultrafast','ultrafast',true,'ultrafast','Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['ultrafast','priority',true,'priority','Fast','Asked for ultrafast · ran priority on this plan','mono tier-fast'],
  ['priority','priority',true,'priority','Fast','Service tier: priority','mono tier-fast'],
  ['priority','normal',false,'normal','Downgraded','Asked for priority · downgraded to normal','mono'],
  ['ultrafast','normal',false,'normal','Downgraded','Asked for ultrafast · downgraded to normal','mono'],
  ['priority','normal',true,'priority','Fast','Service tier: priority','mono tier-fast'],
  ['priority','default',true,'priority','Fast','Service tier: priority','mono tier-fast'],
  ['priority','auto',true,'priority','Fast','Service tier: priority','mono tier-fast'],
  ['priority',null,true,'priority','Fast','Service tier: priority','mono tier-fast'],
  ['ultrafast','normal',true,'priority','Fast','Asked for ultrafast · ran priority on this plan','mono tier-fast'],
  ['ultrafast','default',true,'ultrafast','Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['normal','normal',true,'normal','Normal','Service tier: normal','mono'],
  ['normal','priority',true,'normal','Normal','Service tier: normal','mono'],
  ['normal','priority',false,'normal','Still fast','Fast mode off · still ran priority','mono tier-warn'],
  ['normal','normal',false,'normal','Normal','Service tier: normal','mono'],
  [null,'priority',true,'unknown','—','Service tier was not recorded','mono'],
  [null,'ultrafast',true,'unknown','—','Service tier was not recorded','mono'],
  [null,'normal',false,'unknown','—','Service tier was not recorded','mono'],
  ['ultrafast','unknown',true,'ultrafast','Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['ultrafast',null,true,'priority','Fast','Asked for ultrafast · ran priority on this plan','mono tier-fast'],
  ['unknown',null,true,'unknown','—','Service tier was not recorded','mono'],
  [null,'priority',false,'unknown','—','Service tier was not recorded','mono'],
];
for (const [asked,ran,allowed,forwarded,text,title,css] of modeCases) {
  assert.equal(context.modeCell({service_tier:asked,response_service_tier:ran,fast_mode_allowed:allowed,forwarded_service_tier:forwarded}),
    `<span class="${css}" title="${title}">${text}</span>`);
}
assert.equal(context.modeCell({service_tier:'<script>',response_service_tier:'<script>'}),
  '<span class="mono" title="Service tier was not recorded">—</span>');
const elements = new Map();
const recentContext = vm.createContext({
  document:{getElementById(id){
    if (!elements.has(id)) elements.set(id,{style:{}});
    return elements.get(id);
  }},providerChip:(provider,id)=>id,renderChart(){},
});
vm.runInContext(source.slice(0,source.indexOf('let range ='))+
  source.slice(source.indexOf('function totals('),source.indexOf('function accountFor('))+
  source.slice(source.indexOf('function table('),source.indexOf('function renderUsage('))+
  source.slice(source.indexOf('function renderToday('),source.indexOf('function renderSystem(')),recentContext);
recentContext.renderToday({usage:{available:true,accounts:[],recent:modeCases.map(([asked,ran,allowed,forwarded])=>({
  auth_index:'one',model:'gpt-5.4',reasoning_effort:'xhigh',service_tier:asked,
  response_service_tier:ran,fast_mode_allowed:allowed,forwarded_service_tier:forwarded,total_tokens:100,timestamp:'2026-10-02T18:12:00Z',
}))}});
const recentHTML = elements.get('recent-table').innerHTML;
assert.match(recentHTML,/<th>Reasoning<\/th><th>Mode<\/th><th>Tokens<\/th>/);
assert.equal((recentHTML.match(/<tr[ >]/g)||[]).length,7); // Header and six requests.
assert.equal((recentHTML.match(/<tr class="codex-row">/g)||[]).length,6); // Rows without a provider are Codex rows.
assert.match(recentHTML,/<span class="mono tier-warn" title="Fast mode off · still ran priority">Still fast<\/span>/);
function mode({allowed=true,enforcement='active',changed=false}={}) {
  return {allowed,enforcement,changed_at:changed?'2026-10-02T19:40:00Z':null};
}
// The row shows no usage line: only save feedback and an "off" the gateway cannot enforce.
const cases = [
  [mode(),null],
  [mode({enforcement:'unavailable'}),null],
  [mode({allowed:false}),null],
  [mode({allowed:false,changed:true}),null],
  [mode({enforcement:'unknown'}),null],
  [mode({allowed:false,enforcement:'unavailable'}),'warn'],
  [mode({allowed:false,enforcement:'unknown'}),'warn'],
];
for(const [data,tone] of cases) {
  const html=context.fastRow({id:'one',fast_mode:data},context.current.gateway);
  assert.match(html,/<label for="fast-one">Allow fast mode<\/label>/);
  assert.doesNotMatch(html,/ran fast|downgraded|recent requests|not recorded|Last \d+ requests/);
  if(tone) {
    assert.match(html,/<span class="fast-usage warn" id="fast-one-status">Not enforced · gateway plugin not loaded<\/span>/);
    assert.match(html,/aria-describedby="fast-one-status"/);
  } else assert.doesNotMatch(html,/fast-usage|aria-describedby/);
}
context.current.read_only=true;
assert.match(context.fastRow(account,context.current.gateway),/disabled.*title="Change fast mode from your computer dashboard"/);
context.current.read_only=false;context.failed=true;
assert.match(context.fastRow(account,context.current.gateway),/disabled/);
assert.doesNotMatch(context.fastRow(account,context.current.gateway),/Change fast mode from/);
context.failed=false;context.current.gateway.online=false;
assert.match(context.fastRow(account,context.current.gateway),/disabled/);
context.current.gateway.online=true;
assert.doesNotMatch(context.fastRow(account,context.current.gateway),/disabled/);
function snapshot(allowed=true,changed_at=null){return {gateway:{online:true,accounts:[{id:'one',fast_mode:{allowed,changed_at,enforcement:'active'}}]}};}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};}
async function checkSave() {
  let post=deferred(), refreshed=snapshot(false,'2026-10-02T20:00:00Z');
  context.request=(path)=>path==='/api/fast-mode'?post.promise:Promise.resolve(refreshed);
  const saving=context.saveFastMode('one',false);
  assert.equal(context.current.gateway.accounts[0].fast_mode.allowed,false);
  assert.match(rows.at(-1),/Saving…/);assert.match(rows.at(-1),/disabled.*aria-busy="true"/);
  // A 15-second refresh that finishes during the POST cannot overwrite the switch.
  refreshed=snapshot(true);
  await context.load();
  assert.match(rows.at(-1),/Saving…/);assert.doesNotMatch(rows.at(-1),/ checked/);
  refreshed=snapshot(false,'2026-10-02T20:00:00Z');
  post.resolve(refreshed.gateway.accounts[0].fast_mode);
  await saving;
  assert.equal(context.fastPending.size,0);assert.equal(context.current.gateway.accounts[0].fast_mode.allowed,false);
  assert.doesNotMatch(rows.at(-1),/Saving…|disabled/);
  // Non-confirming responses and network failures restore the previous value.
  for(const reply of [Promise.resolve({allowed:true}),Promise.reject(new Error('fixture'))]) {
    context.current=snapshot(true);context.request=path=>path==='/api/fast-mode'?reply:Promise.resolve(snapshot(true));
    await context.saveFastMode('one',false);
    assert.equal(context.current.gateway.accounts[0].fast_mode.allowed,true);
    assert.match(rows.at(-1),/fast-usage error/);assert.match(rows.at(-1),/Not saved · try again/);
    assert.match(rows.at(-1),/ checked/);assert.doesNotMatch(rows.at(-1),/disabled/);
    await context.load();assert.match(rows.at(-1),/Not saved · try again/);
  }
  assert.equal(timers.at(-1).delay,10000);
  clock+=10000;timers.at(-1).fn();assert.doesNotMatch(rows.at(-1),/Not saved/);
  // An overview started before the save settles must not replace its returned state.
  context.current=snapshot(true);post=deferred();const old=deferred();
  let calls=0;
  context.request=path=>path==='/api/fast-mode'?post.promise:(++calls===1?old.promise:Promise.resolve(snapshot(false,'2026-10-02T21:00:00Z')));
  const oldLoad=context.load(),savingAgain=context.saveFastMode('one',false);
  post.resolve({allowed:false,changed_at:'2026-10-02T21:00:00Z',enforcement:'active'});
  await savingAgain;old.resolve(snapshot(true));await oldLoad;
  assert.doesNotMatch(rows.at(-1),/ checked/);assert.equal(context.fastPending.size,1);
  await context.load();assert.equal(context.fastPending.size,0);
  console.log(`Fast mode UI: ${cases.length} row states, ${modeCases.length} Mode cells, six-row table, tooltips, disabled states, save/rollback, and refresh races passed.`);
}
checkSave().catch(error=>{console.error(error);process.exitCode=1;});
