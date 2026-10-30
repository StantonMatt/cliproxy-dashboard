'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const clock=Date.parse('2026-10-16T12:00:00Z'),A='a@example.test',B='b@example.test';
class FakeDate extends Date { static now(){return clock;} }
const tests=[];
const test=(name,fn)=>tests.push([name,fn]);
function harness(){
  const elements=new Map(),document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null};
  document.activeElement=document.body;
  const get=id=>{
    if(!elements.has(id))elements.set(id,{id,innerHTML:'',style:{},options:[],addEventListener(){},classList:{toggle(){}},focus(){}});
    return elements.get(id);
  };
  document.getElementById=get;
  const context=vm.createContext({document,Date:FakeDate,JSON,Math,localStorage:{getItem:()=>null}});
  vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
  const account=(id,email,routing_id)=>({id,email,routing_id,routing_enabled:true,runtime_available:true,status:'ok',
    checked_at:new FakeDate(clock).toISOString(),windows:[
      {kind:'five_hour',remaining:70,resets_at:clock/1000+9000},
      {kind:'seven_day',remaining:70,resets_at:clock/1000+200000}]});
  const data={gateway:{online:true,checked_at:new FakeDate(clock).toISOString(),accounts:[],
    claude_accounts:[{email:A,auth_index:'ca'},{email:B,auth_index:'cb'}]},
    watch_accounts:[account('claude',A,'c'.repeat(64)),account('claude-b',B,'d'.repeat(64))]};
  context.fixture=data;vm.runInContext('current=fixture;failed=false',context);
  context.load=async()=>{};
  const sent=[];
  context.accountRequest=async(route,body)=>{sent.push({route,body});return {enabled:body.enabled};};
  return {c:context,data,get,sent,run:s=>vm.runInContext(s,context),render:()=>context.renderWatch(data)};
}
const input=(h,id)=>h.get('watch-grid').innerHTML.match(new RegExp(`<input id="claude-route-${id}"[^>]*>`))[0];
const faultCases=[
  ['gateway unavailable',a=>{a.runtime_available=false;},/One account ready/],
  ['gateway unknown',a=>{a.runtime_available=null;},/Status unavailable · checking/],
  ['gateway missing',a=>{delete a.runtime_available;},/Status unavailable · checking/],
  ['invalid unused reset',a=>{a.windows[0]={kind:'five_hour',used:0,remaining:100,resets_at:null,invalid_reset:true};},/Status unavailable · checking/],
  ['unverified identity',a=>{a.routing_enabled=null;a.routing_id=null;},/Status unavailable · checking/],
  ['missing identity on enabled record',a=>{a.routing_id=null;},/Status unavailable · checking/],
  ['missing identity on disabled record',a=>{a.routing_enabled=false;a.routing_id=null;},/Status unavailable · checking/],
  ['null routing with an identity',a=>{a.routing_enabled=null;},/Status unavailable · checking/],
];
for(const [name,change,header] of faultCases){
  test(`${name}: header, card and switch`,()=>{
    const h=harness(),a=h.data.watch_accounts[1];change(a);
    assert.equal(h.c.claudeRoutable(a),false);
    h.render();assert.match(h.get('claude-status').innerHTML,header);
    assert.doesNotMatch(h.get('watch-grid').innerHTML,/is-off/);
    const unknown=typeof a.routing_enabled!=='boolean'||!a.routing_id,control=input(h,'claude-b');
    if(unknown){
      assert.match(control,/disabled/);assert.match(control,/title="Account identity not verified"/);
      assert.match(control,/class="unknown" aria-checked="mixed"/);assert.doesNotMatch(control,/\schecked(?:\s|>)/);
    }else{assert.match(control,/\schecked(?:\s|>)/);assert.doesNotMatch(control,/disabled/);}

  });
  test(`${name}: fallback cannot suppress last-ready confirmation`,async()=>{
    const h=harness();change(h.data.watch_accounts[1]);
    h.data.watch_accounts[0].windows[1].remaining=0;
    await h.c.saveClaudeAccountRouting('claude',false);
    assert.equal(h.sent.length,0);assert.equal(h.run("claudeRouteConfirm.has('c'.repeat(64))"),true);
    assert.match(h.get('watch-grid').innerHTML,/No other Claude account is ready/);
  });
}
test('verified off differs from unknown, and unavailable credentials need no fresh usage',()=>{
  const h=harness(),a=h.data.watch_accounts[1];a.routing_enabled=false;a.checked_at=null;h.render();
  assert.match(h.get('claude-status').innerHTML,/One account ready/);assert.match(h.get('watch-grid').innerHTML,/claude-card is-off/);
  assert.doesNotMatch(input(h,'claude-b'),/\schecked(?:\s|>)|disabled|unknown/);
  a.routing_enabled=true;a.runtime_available=false;h.render();assert.match(h.get('claude-status').innerHTML,/One account ready/);
  h.data.watch_accounts[0].runtime_available=false;h.render();assert.match(h.get('claude-status').innerHTML,/No accounts ready/);
});
test('valid unused allowances are ready; every invalid reset marker is rejected',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.windows=a.windows.map(w=>({...w,remaining:100,used:0,resets_at:null}));
  assert.equal(h.c.claudeRoutable(a),true);h.render();assert.match(h.get('claude-status').innerHTML,/All accounts ready/);
  for(const kind of ['five_hour','seven_day','weekly_model']){
    const b={...a,windows:a.windows.map(w=>({...w}))};
    if(kind==='weekly_model')b.windows.push({kind,used:0,remaining:100,resets_at:null,invalid_reset:true});
    else b.windows.find(w=>w.kind===kind).invalid_reset=true;
    assert.equal(h.c.claudeUsageFresh(b),false,kind);assert.equal(h.c.claudeRoutable(b),false,kind);
  }
});
test('secondary exhausted or invalid allowances cannot count as a fallback',async()=>{
  const h=harness(),b=h.data.watch_accounts[1];
  b.windows.push({kind:'weekly_model',remaining:0,resets_at:clock/1000+9000});
  assert.equal(h.c.claudeRoutable(b),false);h.render();assert.match(h.get('claude-status').innerHTML,/One account ready/);
  await h.c.saveClaudeAccountRouting('claude',false);assert.equal(h.sent.length,0);
  b.windows[2].remaining=70;b.windows[2].resets_at=null;
  assert.equal(h.c.claudeUsageFresh(b),false);
});
test('fresh fallback allows disabling',async()=>{
  const h=harness();h.data.watch_accounts[0].windows[1].remaining=0;
  await h.c.saveClaudeAccountRouting('claude',false);
  assert.equal(h.sent.length,1);assert.equal(h.sent[0].body.confirmed,false);
});
test('unverified identity blocks mutation and clears an old confirmation',async()=>{
  const h=harness();h.data.watch_accounts[1].routing_enabled=false;
  await h.c.saveClaudeAccountRouting('claude',false);assert.equal(h.run('claudeRouteConfirm.size'),1);
  h.data.watch_accounts[0].routing_enabled=null;h.render();
  assert.equal(h.run('claudeRouteConfirm.size'),0);assert.doesNotMatch(h.get('watch-grid').innerHTML,/data-claude-route-off/);
  await h.c.saveClaudeAccountRouting('claude',false,true);assert.equal(h.sent.length,0);
});
test('Recent account chips and chart identities survive unknown or disabled routing',()=>{
  const h=harness();
  const before=h.c.providerChip('claude','cb'),name=h.c.seriesNames()['claude:claude-b'];
  for(const change of [a=>{a.routing_enabled=false;},a=>{a.routing_enabled=null;a.routing_id=null;},a=>{a.runtime_available=false;}]){
    change(h.data.watch_accounts[1]);
    assert.equal(h.c.providerChip('claude','cb'),before);assert.equal(h.c.series('claude','cb'),'claude:claude-b');
    assert.equal(h.c.seriesNames()['claude:claude-b'],name);
  }
});
(async()=>{let failures=0;for(const [name,fn] of tests){try{await fn();}catch(error){failures++;console.error(name,error);}}
  console.log(`Claude readiness UI: ${tests.length-failures}/${tests.length} passed`);if(failures)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
