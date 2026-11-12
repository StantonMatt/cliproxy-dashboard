'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
// Stub elements record listeners so a click can go through the real delegated handler.
const elements=new Map(),listeners={};
const element=id=>({id,dataset:{},style:{},options:[],classList:{toggle(){},add(){},remove(){}},addEventListener(type,fn){(listeners[id+':'+type] ||= []).push(fn);},
  setAttribute(){},removeAttribute(){},querySelector:()=>null,querySelectorAll:()=>[]});
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,addEventListener(type,fn){(listeners['document:'+type] ||= []).push(fn);},removeEventListener(){},
  getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);}};
document.activeElement=document.body;
// Timers run only when the test advances them; each fetch waits until the test replies.
let timers=[],timerId=0,clock=Date.parse('2026-10-30T12:00:00Z');
class FakeDate extends Date { constructor(...a){super(...(a.length?a:[clock]));} static now(){return clock;} }
const calls=[];let respond=null;
function fetch(url,options={}){
  calls.push({url,method:options.method,headers:JSON.parse(JSON.stringify(options.headers)),body:options.body?JSON.parse(options.body):undefined});
  return new Promise((resolve,reject)=>{respond=(status,data)=>status==='throw'?reject(new TypeError('Failed to fetch')):resolve({status,ok:status>=200 && status<300,json:async()=>data});});
}
const c=vm.createContext({document,Date:FakeDate,JSON,Math,URL,AbortSignal,fetch,CSS:{escape:v=>v},localStorage:{getItem:()=>null},
  window:{addEventListener(){},removeEventListener(){}},BillingDates:{describe:()=>({date:'Nov 14'})},innerWidth:1440,
  setTimeout:(fn,ms)=>{timers.push({id:++timerId,at:clock+ms,fn});return timerId;},clearTimeout:id=>{timers=timers.filter(t=>t.id!==id);}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const flush=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
async function advance(ms){
  const until=clock+ms;
  for(;;){timers.sort((a,b)=>a.at-b.at);const t=timers[0];if(!t || t.at>until)break;timers.shift();clock=t.at;t.fn();await flush();}
  clock=until;
}
const loads=[];let renders=0;
c.load=async fresh=>{loads.push(fresh);};c.render=()=>{renders++;};

const iso=s=>new FakeDate(clock+s*1000).toISOString();
const open={supported:true,available_at:null};
const windows=[{kind:'five_hour',label:'5-hour limit',used:10,remaining:90,resets_at:clock/1000+9000,seconds:18000},
  {kind:'seven_day',label:'Weekly · all models',used:40,remaining:60,resets_at:clock/1000+300000,seconds:604800}];
let xai,claude,codex,gateway;
function reset(){
  xai={key:'xai-0.json',auth_index:'xai-0',provider:'xai',email:'owner@example.test',label:'owner@example.test',disabled:false,active:true,
    availability:{state:'ready'},routing_enabled:true,today:{requests:3,hours:[]},usage_refresh:{...open}};
  claude={key:'claude-0.json',auth_index:'claude-0',provider:'claude',email:'a@example.test',label:'a@example.test',status:'ok',
    checked_at:iso(-60),windows,usage_refresh:{...open},routing_id:'r',routing_enabled:true,runtime_available:true};
  codex={key:'codex-0.json',provider:'codex',email:'c@example.test',label:'c@example.test',usage_refresh:{...open}};
  gateway={online:true,checked_at:iso(0),pool:{state:'available'},claude_accounts:[],
    accounts:[{id:'codex-0',key:'codex-0.json',email:'c@example.test',plan:'Pro',windows:[],resets:{},availability:{state:'ready'},active:true}]};
  c.fixture={providers:[{id:'codex',name:'Codex',accent:'codex',routable:false,accounts:[codex]},
    {id:'claude',name:'Claude',accent:'claude',routable:true,accounts:[claude]},{id:'xai',name:'Grok',accent:'xai',routable:false,accounts:[xai]}],
    gateway,usage:{available:true,providers:{}},read_only:false};
  vm.runInContext('current=fixture;failed=false;usageRefresh.clear();',c);
  calls.length=0;loads.length=0;timers=[];renders=0;document.getElementById('account-announce').textContent='';
}
const cards={
  xai:()=>c.genericCard(c.fixture.providers[2],xai,gateway),
  claude:()=>{c.renderWatch(c.fixture);return elements.get('watch-grid').innerHTML;},
  codex:()=>{c.renderAccounts(gateway);return elements.get('account-grid').innerHTML;}
};
const button=(html,id)=>html.match(new RegExp(`<button type="button" class="usage-refresh[^"]*" id="usage-refresh-${id.replace('.','\\.')}"[^>]*>`))?.[0];
// The handler on #gateways sees the button as the click target, as a browser would through closest().
function click(id,html=cards.xai()){
  const tag=button(html,id.replace(':','-'));assert(tag,'missing refresh button '+id);
  const attrs=Object.fromEntries([...tag.matchAll(/\s([\w-]+)="([^"]*)"/g)].map(m=>[m[1],m[2]]));
  const target={dataset:{usageRefresh:attrs['data-usage-refresh'].replace(/&amp;/g,'&')},disabled:false,getAttribute:k=>attrs[k] ?? null,closest(selector){return selector.includes('[data-usage-refresh]') ? this : null;}};
  for(const fn of listeners['document:click'])fn({target});
}

(async()=>{
  // Hidden unless the account says the read is supported; a sign-in card has no status, so no button either.
  reset();
  for(const value of [undefined,null,{supported:false,available_at:null},{available_at:null}]) {
    xai.usage_refresh=value;assert.doesNotMatch(cards.xai(),/usage-refresh/);
  }
  xai.usage_refresh={...open};xai.availability={state:'auth_required'};
  assert.doesNotMatch(cards.xai(),/usage-refresh/);
  claude.usage_refresh={supported:false};assert.doesNotMatch(cards.claude(),/usage-refresh/);
  codex.usage_refresh=undefined;assert.doesNotMatch(cards.codex(),/usage-refresh/);

  // Available: an icon button after the status on every card kind, named for its account.
  reset();
  assert.equal(button(cards.xai(),'xai-xai-0.json'),
    '<button type="button" class="usage-refresh" id="usage-refresh-xai-xai-0.json" data-usage-refresh="xai:xai-0.json" aria-label="Refresh usage · owner@example.test" title="Refresh usage">');
  assert.match(cards.xai(),/<span>Ready<\/span><\/button><span class="state-detail"[^>]*>Ready to route<\/span><button type="button" class="usage-refresh"[^>]*><svg aria-hidden="true"><use href="#refresh"\/><\/svg><\/button><\/span><\/div>/);
  assert.match(button(cards.claude(),'claude-claude-0.json'),/aria-label="Refresh usage · a@example\.test" title="Refresh usage">$/);
  assert.match(cards.claude(),/<span>Ready<\/span><\/button><span class="state-detail"[^>]*>Updated 1m ago<\/span><button type="button" class="usage-refresh"/);
  // Codex cards read usage_refresh from the provider inventory and keep focus through their access-focus restore.
  assert.match(button(cards.codex(),'codex-codex-0.json'),/title="Refresh usage" data-access-focus="codex-0:usage">$/);

  // A future available_at shows it disabled, with the wait in the tooltip; a click does nothing.
  xai.usage_refresh={supported:true,available_at:iso(90)};
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Available in 2 min" aria-disabled="true">$/);
  click('xai:xai-0.json');await flush();assert.equal(calls.length,0);
  xai.usage_refresh={supported:true,available_at:iso(2*3600+300)};
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Available in 2 h 5 min"/);
  xai.usage_refresh={supported:true,available_at:iso(-5)};
  assert.doesNotMatch(button(cards.xai(),'xai-xai-0.json'),/aria-disabled/);
  // Without a dashboard connection there is nothing to send.
  vm.runInContext('failed=true',c);
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Connection lost" aria-disabled="true">$/);
  vm.runInContext('failed=false',c);

  // Click: one POST with the account's provider and key and the app's request headers; spinning, busy and disabled meanwhile.
  reset();
  click('xai:xai-0.json');await flush();
  assert.deepEqual(calls,[{url:'/api/accounts/usage/refresh',method:'POST',headers:{'X-Router-Request':'1','Content-Type':'application/json'},
    body:{provider:'xai',key:'xai-0.json'}}]);
  assert.equal(renders,1);
  const busy=button(cards.xai(),'xai-xai-0.json');
  assert.match(busy,/class="usage-refresh refreshing"/);assert.match(busy,/title="Refreshing usage" aria-disabled="true" aria-busy="true">$/);
  // The state lives outside the DOM, so a poll's re-render keeps it; a second click sends nothing.
  assert.equal(button(cards.xai(),'xai-xai-0.json'),busy);
  click('xai:xai-0.json');await flush();assert.equal(calls.length,1);
  // Only the clicked account is busy.
  assert.doesNotMatch(button(cards.claude(),'claude-claude-0.json'),/aria-busy/);
  // 200: the overview reloads, then the spinner stops and the result is announced.
  respond(200,{ok:true});await flush();
  assert.deepEqual(loads,[true]);
  assert.equal(button(cards.xai(),'xai-xai-0.json'),
    '<button type="button" class="usage-refresh" id="usage-refresh-xai-xai-0.json" data-usage-refresh="xai:xai-0.json" aria-label="Refresh usage · owner@example.test" title="Refresh usage">');
  assert.equal(elements.get('account-announce').textContent,'Usage refreshed · owner@example.test');
  assert.doesNotMatch(cards.xai(),/fast-usage/);

  // Busy state survives Claude and Codex re-renders too.
  reset();
  click('claude:claude-0.json',cards.claude());await flush();
  assert.match(button(cards.claude(),'claude-claude-0.json'),/aria-busy="true"/);
  assert.match(button(cards.claude(),'claude-claude-0.json'),/aria-busy="true"/);
  respond(200,{ok:true});await flush();
  click('codex:codex-0.json',cards.codex());await flush();
  assert.deepEqual(calls.at(-1).body,{provider:'codex',key:'codex-0.json'});
  assert.match(button(cards.codex(),'codex-codex-0.json'),/class="usage-refresh refreshing"[^>]*aria-busy="true" data-access-focus="codex-0:usage">$/);
  respond(200,{ok:true});await flush();
  assert.doesNotMatch(button(cards.codex(),'codex-codex-0.json'),/aria-busy/);

  // 429: disabled until retry_at, with the wait in the tooltip and a brief note under the status.
  reset();
  click('xai:xai-0.json');await flush();
  respond(429,{error:'rate_limited',retry_at:iso(12*60)});await flush();
  assert.deepEqual(loads,[]);
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Rate-limited · try again in 12 min" aria-disabled="true">$/);
  assert.match(cards.xai(),/<div class="fast-usage warn usage-refresh-note" role="status">Rate-limited · try again in 12 min<\/div>/);
  click('xai:xai-0.json');await flush();assert.equal(calls.length,1);
  await advance(10000);
  assert.doesNotMatch(cards.xai(),/fast-usage/);
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Rate-limited · try again in 12 min" aria-disabled="true">$/);
  // A later server wait wins and reads as the account's availability.
  xai.usage_refresh={supported:true,available_at:iso(20*60)};
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Available in 20 min"/);
  xai.usage_refresh={...open};
  await advance(12*60*1000);
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Refresh usage">$/);
  click('xai:xai-0.json');await flush();assert.equal(calls.length,2);
  respond(429,{error:'rate_limited'});await flush();
  assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Rate-limited · try again in 1 min" aria-disabled="true">$/);

  // Claude's combined availability can shorten an older local usage-only 429 wait.
  reset();
  claude.usage_refresh={supported:true,available_at:null,usage_available_at:iso(7200)};
  click('claude:claude-0.json',cards.claude());await flush();
  claude.usage_refresh.available_at=iso(60);
  respond(429,{error:'rate_limited',retry_at:iso(7200)});await flush();
  assert.deepEqual(loads,[true]);
  assert.match(button(cards.claude(),'claude-claude-0.json'),/aria-disabled/);
  await advance(60000);
  assert.doesNotMatch(button(cards.claude(),'claude-claude-0.json'),/aria-disabled/);
  click('claude:claude-0.json',cards.claude());await flush();
  assert.equal(calls.length,2);respond(200,{ok:true});await flush();

  // 409: a read is already running, so the spinner stays briefly and the overview reloads.
  reset();
  click('xai:xai-0.json');await flush();
  respond(409,{error:'busy'});await flush();
  assert.deepEqual(loads,[]);assert.match(button(cards.xai(),'xai-xai-0.json'),/aria-busy="true"/);
  await advance(2500);
  assert.deepEqual(loads,[true]);assert.doesNotMatch(button(cards.xai(),'xai-xai-0.json'),/aria-busy|aria-disabled/);
  assert.equal(elements.get('account-announce').textContent,'');

  // Other failures: a brief error note; the button stays usable.
  for(const status of [500,'throw']) {
    reset();
    click('xai:xai-0.json');await flush();
    respond(status,{error:'read_failed'});await flush();
    assert.deepEqual(loads,[]);
    assert.match(cards.xai(),/<div class="fast-usage error usage-refresh-note" role="status">Couldn't refresh usage\. Try again\.<\/div>/);
    assert.match(button(cards.xai(),'xai-xai-0.json'),/title="Refresh usage">$/);
    assert.equal(elements.get('account-announce').textContent,'');
    const before=renders;await advance(10000);
    assert.doesNotMatch(cards.xai(),/fast-usage/);assert.equal(renders,before+1);
  }
  // Endpoint-read Codex allowance uses its reset, with no header-only age exemption.
  reset();
  Object.assign(gateway.accounts[0],{quota_checked_at:iso(-7200),usage_source:'gateway',windows:windows.map(w=>({...w}))});
  assert.doesNotMatch(cards.codex(),/allowance · last known/);
  gateway.accounts[0].windows[0].resets_at=clock/1000;
  assert.match(cards.codex(),/allowance · last known/);

  // Deliberately deferred exhaustion keeps aged cards current without changing their timestamps.
  reset();
  for(const independent of [false,true]) {
    claude.checked_at=iso(-1800);claude.independent_windows=independent;claude.fresh_until=iso(60);
    claude.windows=windows.map(w=>({...w,used:100,remaining:0,measured_at:iso(-1800)}));
    assert.equal(c.watchState(claude).tone,'ok');
    assert.equal(c.claudeUsageFresh(claude),true);
    assert.doesNotMatch(cards.claude(),/ · last known/);
    claude.fresh_until=iso(-1);
    assert.equal(c.claudeUsageFresh(claude),true);
    claude.windows[0].resets_at=clock/1000;
    assert.equal(c.watchState(claude).tone,'warn');
    assert.equal(c.claudeUsageFresh(claude),false);
    assert.match(cards.claude(),/ · last known/);
  }
  xai.windows=windows;xai.usage_checked_at=iso(-1800);xai.fresh_until=iso(60);
  assert.equal(c.genericUsage(xai).stale,false);
  xai.fresh_until=iso(-1);assert.equal(c.genericUsage(xai).stale,true);
  console.log('Usage refresh UI: hidden when unsupported, wait and offline states, POST, busy across re-renders, 200 reload, 429 wait, 409 and failures passed');
})().catch(error=>{console.error(error);process.exit(1);});
