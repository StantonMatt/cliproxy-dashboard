'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
const A='a@example.test',B='b@example.test',C='c@example.test';
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const tick=()=>new Promise(r=>setImmediate(r));
const copy=value=>JSON.parse(JSON.stringify(value));
function fixture(){
  const now=Date.now()/1000;
  const account=(id,email,routing_id)=>({id,email,routing_id,routing_enabled:true,runtime_available:true,status:'ok',checked_at:new Date().toISOString(),
    windows:[{kind:'five_hour',remaining:70,resets_at:now+9000},{kind:'seven_day',remaining:70,resets_at:now+200000}]});
  return {gateway:{online:true,checked_at:new Date().toISOString(),routing_linked:false,accounts:[{id:'a',email:A,windows:[]},{id:'b',email:B,windows:[]}],
    claude_accounts:[{auth_index:'ca',email:A},{auth_index:'cb',email:B}],
    preference:{policy:'round-robin',status:'active'},claude_preference:{policy:'round-robin',status:'active'}},
    watch_accounts:[account('claude',A,'c'.repeat(64)),account('claude-b',B,'d'.repeat(64))],health:{},notifications:{available:true,items:[]}};
}
function harness(data=fixture()){
  const elements=new Map(),storage=new Map();
  const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null};
  document.activeElement=document.body;
  function element(id){
    const classes=new Set(),listeners={};
    const el={id,hidden:false,disabled:false,value:'',textContent:'',checked:false,children:[],dataset:{},parentElement:null,
      classList:{toggle(k,on){if(on)classes.add(k);else classes.delete(k);},contains:k=>classes.has(k)},
      options:['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B].map(value=>({value})),
      addEventListener(type,fn){listeners[type]=fn;},listeners,removeAttribute(){},setAttribute(){},
      focus(){document.activeElement=el;},closest(selector){return selector==='#watch-grid'&&id.startsWith('claude-route-')?get('watch-grid'):null;},
      querySelector(){return el.children.find(c=>/state/.test(c.id)) || null;},
      before(node){node.parentElement=el.parentElement;}};
    let html='';
    Object.defineProperty(el,'innerHTML',{get:()=>html,set(value){
      if(id==='watch-grid'&&document.activeElement?.id?.startsWith('claude-route-'))document.activeElement=document.body;
      html=value;
    }});
    return el;
  }
  function get(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);}
  document.getElementById=get;
  for(const [host,state] of [['routing-all','routing-all-state'],['claude-routing','claude-routing-state']]){get(host).children.push(get(state));get(state).parentElement=get(host);}
  get('routing-split').parentElement=get('claude-routing');
  const context=vm.createContext({document,Date,JSON,Math,CSS:{escape:v=>v},setTimeout:()=>0,BillingDates:{describe:()=>({})},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)}});
  vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
  context.fixture=data;vm.runInContext('current=fixture;failed=false',context);
  return {context,get,document,storage,run:s=>vm.runInContext(s,context),renderRouting(){context.renderRouting(data.gateway);context.renderRouting(data.gateway,'claude');context.renderRoutingAll(data.gateway);}};
}
const tests=[];
const test=(name,fn)=>tests.push([name,fn]);
test('F1: relink locks all routing controls through both deferred requests and readback',async()=>{
  const h=harness(),{context:c,get}=h,link=deferred(),save=deferred(),read=deferred();let calls=0;
  c.load=()=>read.promise;c.request=()=>++calls===1?link.promise:save.promise;
  h.renderRouting();const pending=c.saveRoutingLink(true);
  const locked=()=>{h.renderRouting();for(const id of ['routing-mode','claude-routing-mode','routing-all-mode','routing-link','routing-resume','claude-routing-resume','routing-all-resume'])assert.equal(get(id).disabled,true,id);};
  locked();await c.saveRouting(B);assert.equal(calls,1,'programmatic conflicting save must be ignored');
  link.resolve({linked:true});await tick();locked();save.resolve({codex:{status:'active'},claude:{status:'active'}});await tick();locked();
  read.resolve();await pending;h.renderRouting();assert.equal(get('routing-link').disabled,false);
});
test('F2: stale poll cannot replace saved routing and mutation queues fresh readback',async()=>{
  const h=harness(),{context:c,run}=h,old=deferred(),fresh=deferred();let reads=0;
  c.render=()=>{};c.request=(url)=>url.startsWith('/api/overview')?(++reads===1?old.promise:fresh.promise):Promise.resolve({policy:'preferred-account',selected_email:B,status:'active'});
  const poll=c.load(),save=c.saveRouting(B);await tick();old.resolve(copy(fixture()));await poll;await tick();
  assert.equal(run('current.gateway.preference.selected_email'),B);assert.equal(reads,2);
  const next=fixture();next.gateway.preference={policy:'preferred-account',selected_email:B,status:'active'};fresh.resolve(next);await save;
  assert.equal(run('current.gateway.preference.selected_email'),B);
});
test('Overview renders health without reading notification items or creating notice controls',()=>{
  const h=harness();
  Object.defineProperty(h.context.fixture,'notifications',{get(){throw Error('UI must not read the desktop outbox');}});
  const get=h.document.getElementById;
  h.document.getElementById=id=>{assert.doesNotMatch(id,/^(?:codex|claude|account)-notices$/);return get(id);};
  h.context.renderOperations(h.context.fixture);
  assert.match(h.get('codex-server').textContent,/Codex server/);
  assert.match(h.get('health-panel').innerHTML,/Backup/);
  assert.doesNotMatch(source,/claudeNotices|router-dismissed-notices|data-notice-|data-dismiss-|api\/notifications\/dismiss/);
  assert.doesNotMatch(css,/account-notice|notice-actions|notification-warning|codex-notice|claude-notice/);
});
test('F4: third-account grid fallback',()=>{
  const h=harness();h.context.fixture.watch_accounts.push({...h.context.fixture.watch_accounts[0],id:'third',email:C});
  h.context.renderWatch(h.context.fixture);assert.equal(h.get('watch-grid').classList.contains('many-accounts'),true);
  h.context.fixture.gateway.accounts.push({id:'third',email:C,windows:[]});h.context.renderAccounts(h.context.fixture.gateway);
  assert.equal(h.get('account-grid').classList.contains('many-accounts'),true);
  h.context.fixture.watch_accounts.pop();h.context.renderWatch(h.context.fixture);assert.equal(h.get('watch-grid').classList.contains('many-accounts'),false);
  assert.match(css,/\.gateway>[^}]*many-accounts[^}]*grid-template-rows:auto/);
});
test('F7/S6: readiness requires online gateway and fresh positive core windows',async()=>{
  const h=harness(),c=h.context,a=c.fixture.watch_accounts[0];assert.equal(c.claudeRoutable(a),true);
  for(const kind of ['five_hour','seven_day']){a.windows.find(w=>w.kind===kind).remaining=0;assert.equal(c.claudeRoutable(a),false);a.windows.find(w=>w.kind===kind).remaining=70;}
  for(const value of [null,NaN,Infinity]){a.windows[0].remaining=value;assert.equal(c.claudeRoutable(a),false);}a.windows[0].remaining=70;
  a.windows[0].measured_at=new Date(Date.now()-16*60000).toISOString();assert.equal(c.claudeRoutable(a),false);delete a.windows[0].measured_at;
  a.checked_at=new Date(Date.now()-16*60000).toISOString();assert.equal(c.claudeRoutable(a),false);c.renderWatch(c.fixture);assert.match(h.get('claude-status').innerHTML,/Status unavailable · checking/);
  a.checked_at=new Date().toISOString();c.fixture.gateway.online=false;c.renderWatch(c.fixture);assert.match(h.get('claude-status').innerHTML,/Status unavailable · checking/);assert.doesNotMatch(h.get('claude-status').innerHTML,/status-item ok/);
  c.fixture.gateway.online=true;c.fixture.watch_accounts[1].windows[0].remaining=0;c.accountRequest=async()=>{throw Error('should confirm first');};await c.saveClaudeAccountRouting('claude',false);assert.equal(h.run("claudeRouteConfirm.has('c'.repeat(64))"),true);
  c.fixture.gateway.checked_at=new Date(Date.now()-4*60000).toISOString();c.renderWatch(c.fixture);assert.match(h.get('claude-status').innerHTML,/Status unavailable · checking/);
});
test('F9: Claude and relink completion preserve a newer focus choice',async()=>{
  const h=harness(),c=h.context,save=deferred();c.load=async()=>{};c.accountRequest=()=>save.promise;
  h.get('claude-route-claude').focus();const pending=c.saveClaudeAccountRouting('claude',false);h.get('theme').focus();save.resolve({enabled:false});await pending;assert.equal(h.document.activeElement,h.get('theme'));
  const link=deferred();c.request=()=>link.promise;h.get('routing-link').focus();const relink=c.saveRoutingLink(false);h.get('theme').focus();link.resolve({linked:false});await relink;assert.equal(h.document.activeElement,h.get('theme'));
});
test('F11: later authoritative recovery clears Claude errors and removed identities',async()=>{
  const h=harness(),c=h.context,load=c.load;c.load=async()=>{};c.accountRequest=async()=>{throw Error('uncertain');};await c.saveClaudeAccountRouting('claude',false);
  assert.equal(h.run('claudeRouteErrors.size'),1);const reload=fixture();reload.watch_accounts[0].routing_enabled=false;c.fixture=reload;c.request=async()=>reload;c.render=()=>{};
  c.load=load;await c.load();
  assert.equal(h.run('claudeRouteErrors.size'),0);
  h.run("claudeRouteErrors.set('removed',{message:'test',after:0})");await c.load();assert.equal(h.run('claudeRouteErrors.size'),0);
});
test('S3: confirmation cannot transfer to a replacement routing identity',async()=>{
  const h=harness(),c=h.context;c.fixture.watch_accounts[1].routing_enabled=false;let calls=0;c.load=async()=>{};c.accountRequest=async()=>{calls++;return {enabled:false};};
  await c.saveClaudeAccountRouting('claude',false);const a=c.fixture.watch_accounts[0];a.routing_id='e'.repeat(64);
  await c.saveClaudeAccountRouting('claude',false,true);assert.equal(calls,0);
  c.renderWatch(c.fixture);assert.doesNotMatch(h.get('watch-grid').innerHTML,/data-claude-route-off/);
});
test('S7: row email identifies Claude first and unknown history has its own series and legend',()=>{
  const h=harness(),c=h.context;assert.equal(c.series('claude','ca',B),'claude-two');
  c.fixture.watch_accounts.pop();assert.equal(c.series('claude','cb',B),'claude-two');assert.equal(c.series('claude','missing'),'claude-unknown');assert.equal(c.series('claude','ca',C),'claude-unknown');
  c.fixture.gateway.claude_accounts=[{auth_index:'cb',email:B}];assert.equal(c.series('claude','cb'),'claude-two');
  const legend={};h.document.querySelectorAll=()=>[legend];c.renderLegend();assert.doesNotMatch(legend.innerHTML,/Unknown account/);
  c.fixture.usage={claude:{accounts:[{auth_index:'missing',requests:7}]}};c.renderLegend();assert.match(legend.innerHTML,/claude-unknown[^]*Unknown account/);
  assert.equal(c.seriesNames()['claude-unknown'],'Unknown account');assert.match(css,/\.claude-unknown\{background:var\(--faint\)\}/);
  c.renderChart('usage-chart',{hours:[],claude:{hours:[{hour:new Date().toISOString(),auth_index:'missing',requests:7}]}},'today');assert.match(h.get('usage-chart').innerHTML,/Unknown account 7/);assert.doesNotMatch(h.get('usage-chart').innerHTML,/NaN/);
});
(async()=>{let failed=0;for(const [name,fn] of tests){try{await fn();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}if(failed)process.exitCode=1;})();
