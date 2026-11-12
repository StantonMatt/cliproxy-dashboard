'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const elements=new Map();
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,
  getElementById(id){if(!elements.has(id))elements.set(id,{addEventListener(){},options:[],classList:{toggle(){}},querySelector(){return {before(){}};}});return elements.get(id);}};
document.activeElement=document.body;
const context=vm.createContext({document,Date,JSON,localStorage:{getItem:()=>null}});
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
const A='owner@example.test',B='alternate@example.test';
for(const prefix of ['routing','claude-routing'])document.getElementById(prefix+'-mode').options=
  ['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B].map(value=>({value}));
context.fixture={gateway:{online:true,preference:{policy:'round-robin',status:'active'}},
  watch_accounts:[{id:'claude',email:A},{id:'claude-b',independent_windows:true,email:B}]};
vm.runInContext('current=fixture;failed=false',context);
context.renderRouting(context.fixture.gateway,'claude');
const el=id=>elements.get('claude-routing-'+id);
assert.equal(el('mode').value,'earliest-weekly-reset','Unsaved frontend default must match backend default');
assert.equal(el('help').hidden,true);
assert.equal(el('mode').disabled,false);
assert.equal(el('mode').options[3].textContent,'Prefer '+A);
context.fixture.gateway.claude_preference={policy:'preferred-account',selected_email:B,status:'active'};
context.renderRouting(context.fixture.gateway,'claude');
assert.equal(el('mode').value,B);
context.fixture.gateway.claude_preference={policy:'highest-remaining-quota',status:'held',hold_reason:'stale quota data',held_since:new Date().toISOString()};
context.renderRouting(context.fixture.gateway,'claude');
assert.match(el('state').innerHTML,/<span>Held<\/span>.*data-title="Held for under a minute" hidden>Stale quota data</);
assert.equal(el('state').hidden,false);
context.fixture.gateway.claude_preference={policy:'round-robin',status:'active',drift:true,actual_priorities:{[A]:0,[B]:100}};
context.renderRouting(context.fixture.gateway,'claude');
assert.match(el('state').innerHTML,/<span>Out of sync<\/span>.*data-title="Gateway priorities differ from this selection" hidden>Currently prefers alternate/);
context.fixture.read_only=true;
context.renderRouting(context.fixture.gateway,'claude');
assert.equal(el('mode').disabled,true);
assert.equal(el('trigger').disabled,true,'the routing button follows its row');
assert.equal(el('help').textContent,'Change Claude routing from your computer dashboard');
assert.equal(el('help').hidden,false);
context.fixture.read_only=false;
context.fixture.gateway.online=false;
context.renderRouting(context.fixture.gateway,'claude');
assert.equal(el('mode').disabled,true);
context.fixture.gateway.online=true;
context.load=async()=>{};
let posted;
async function savingChecks(){
  for(const [result,feedback] of [[{status:'active'},'Saved'],[{status:'held'},'Saved · not applied yet']]){
    context.request=async(route,options)=>{posted={route,body:JSON.parse(options.body)};assert.equal(el('feedback').textContent,'Applying…');assert.equal(el('mode').disabled,true);return result;};
    await context.saveRouting(B,'claude');
    assert.equal(posted.route,'/api/claude-routing');
    assert.deepEqual(posted.body,{mode:'preferred-account',preferred_email:B});
    assert.equal(el('feedback').textContent,feedback);
    assert.equal(context.fixture.gateway.preference.policy,'round-robin','Claude save must not change Codex selection');
  }
  context.request=async()=>{throw Error('synthetic');};
  await context.saveRouting('round-robin','claude');
  assert.equal(el('feedback').textContent,'Not confirmed · refresh and try again');
  console.log('Claude routing UI: defaults, options, saving, held, drift, read-only and offline passed');
}
savingChecks().catch(error=>{console.error(error);process.exitCode=1;});
