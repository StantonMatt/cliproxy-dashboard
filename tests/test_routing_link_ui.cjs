'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const A='account-a@example.com',B='account-b@example.com';
const OPTIONS=['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B];
// Just enough DOM for the routing controls: hidden flags, parents and the switch that moves between rows.
const elements=new Map();
function element(id){
  const el={id,hidden:false,disabled:false,value:'',textContent:'',innerHTML:'',checked:false,parentElement:null,children:[],
    addEventListener(){},removeAttribute(){},focus(){document.activeElement=el;},
    options:OPTIONS.map(value=>({value,textContent:value.includes('@')?'Prefer '+value:value})),
    querySelector(){return el.children.find(child=>/state/.test(child.id));},
    before(node){node.parentElement=el.parentElement;}};
  return el;
}
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,
  getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);}};
document.activeElement=document.body;
const $=id=>document.getElementById(id);
for(const [host,state] of [['routing-all','routing-all-state'],['claude-routing','claude-routing-state']]){$(host).children.push($(state));$(state).parentElement=$(host);}
$('routing-split').parentElement=$('routing-all');
const context=vm.createContext({document,Date,JSON,localStorage:{getItem:()=>null}});
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
const active=(policy,email)=>({policy,status:'active',selected_email:email,preferred_email:email});
function snapshot(extra={}){
  return {gateway:{online:true,routing_linked:true,preference:active('preferred-account',B),
    claude_preference:active('preferred-account',B),...extra},
    watch_accounts:[{id:'claude',email:A,routing_id:'c'.repeat(64),routing_enabled:true},{id:'claude-b',email:B,routing_id:'d'.repeat(64),routing_enabled:true}]
      .map(a=>({...a,runtime_available:true,status:'ok',checked_at:new Date().toISOString(),windows:['five_hour','seven_day'].map(kind=>({kind,remaining:70,resets_at:Date.now()/1000+9000}))}))};
}
context.fixture=snapshot();
vm.runInContext('current=fixture;failed=false',context);
const render=()=>{context.renderRouting(context.fixture.gateway);context.renderRouting(context.fixture.gateway,'claude');context.renderRoutingAll(context.fixture.gateway);};

// Linked by default: one select for both gateways, starting from the Codex selection.
render();
assert.equal($('routing-all').hidden,false);
assert.equal($('codex-routing').hidden,true);
assert.equal($('claude-routing').hidden,true);
assert.equal($('routing-all-mode').value,B);
assert.equal($('routing-link').checked,false);
assert.equal($('routing-split').parentElement,$('routing-all'));
assert.equal($('routing-all-state').hidden,true);
assert.equal($('routing-all-help').hidden,true);
// A held gateway stays visible per gateway while linked.
context.fixture.gateway.claude_preference={...active('preferred-account',B),status:'held',hold_reason:'usage is stale',held_since:new Date().toISOString()};
render();
assert.equal($('routing-all-state').hidden,false);
assert.match($('routing-all-state').innerHTML,/<b>Claude<\/b>Held for under a minute · usage is stale/);
assert.doesNotMatch($('routing-all-state').innerHTML,/<b>Codex<\/b>/);
context.fixture.gateway.preference={...active('round-robin'),suspended:true};
render();
assert.match($('routing-all-state').innerHTML,/<b>Codex<\/b>Suspended after a rollback/);
assert.equal($('routing-all-resume').hidden,false);
// Gateways that disagree while linked show the Codex choice, without an extra line.
context.fixture.gateway.preference=active('preferred-account',B);
context.fixture.gateway.claude_preference=active('round-robin');
render();
assert.equal($('routing-all-mode').value,B);
assert.equal($('routing-all-state').hidden,true);
context.fixture.read_only=true;
render();
assert.equal($('routing-all-mode').disabled,true);
assert.equal($('routing-link').disabled,true);
assert.equal($('routing-all-help').textContent,'Change routing from your computer dashboard');
assert.equal($('routing-all-help').hidden,false);
context.fixture.read_only=false;

// Split: each gateway gets its own row, and the switch moves into the Claude row.
context.fixture.gateway.routing_linked=false;
render();
assert.equal($('routing-all').hidden,true);
assert.equal($('codex-routing').hidden,false);
assert.equal($('claude-routing').hidden,false);
assert.equal($('routing-link').checked,true);
assert.equal($('routing-split').parentElement,$('claude-routing'));
// Without Claude accounts there is nothing to link.
$('watch-section').hidden=true;
render();
assert.equal($('routing-all').hidden,true);
assert.equal($('claude-routing').hidden,true);
assert.equal($('routing-split').hidden,true);
$('watch-section').hidden=false;

async function saving(){
  const posts=[];
  context.load=async()=>{};
  context.request=async(route,options)=>{posts.push([route,JSON.parse(options.body)]);
    if(route==='/api/routing-link')return {linked:JSON.parse(options.body).linked};
    return {codex:{status:'active',policy:'earliest-weekly-reset'},claude:{status:'held',policy:'earliest-weekly-reset'}};};
  context.fixture=snapshot();
  vm.runInContext('current=fixture',context);
  await context.saveRoutingAll('earliest-weekly-reset');
  assert.deepEqual(posts.at(-1),['/api/routing-all',{mode:'earliest-weekly-reset'}]);
  assert.equal(context.fixture.gateway.preference.policy,'earliest-weekly-reset');
  assert.equal(context.fixture.gateway.claude_preference.status,'held');
  assert.equal($('routing-all-feedback').textContent,'Saved · not applied yet');
  context.request=async()=>{throw Error('HTTP 503');};
  await context.saveRoutingAll(A);
  assert.equal($('routing-all-feedback').textContent,'Not confirmed · refresh and try again');
  // Linking again applies the Codex choice to both gateways; separating changes nothing else.
  context.request=async(route,options)=>{posts.push([route,JSON.parse(options.body)]);
    return route==='/api/routing-link'?{linked:JSON.parse(options.body).linked}:{codex:{status:'active'},claude:{status:'active'}};};
  context.fixture=snapshot({routing_linked:false});
  vm.runInContext('current=fixture',context);
  render();
  $('routing-mode').value=A;
  posts.length=0;
  await context.saveRoutingLink(true);
  assert.deepEqual(posts,[['/api/routing-link',{linked:true}],['/api/routing-all',{mode:'preferred-account',preferred_email:A}]]);
  assert.equal($('routing-all-feedback').textContent,'Saved');
  posts.length=0;
  await context.saveRoutingLink(false);
  assert.deepEqual(posts,[['/api/routing-link',{linked:false}]]);
  assert.equal(context.fixture.gateway.routing_linked,false);
  context.request=async()=>({linked:true});
  await context.saveRoutingLink(false);
  assert.equal($('claude-routing-feedback').textContent,'');
  assert.equal($('routing-all-feedback').textContent,'Not confirmed · refresh and try again');

  // Claude accounts: confirm before removing the last routable account, then send its routing id.
  const sent=[];
  context.renderWatch=()=>{};
  context.accountRequest=async(route,body)=>{sent.push([route,JSON.parse(JSON.stringify(body))]);return {enabled:body.enabled};};
  context.fixture=snapshot();
  vm.runInContext('current=fixture',context);
  context.fixture.watch_accounts[1].routing_enabled=false;
  await context.saveClaudeAccountRouting('claude',false);
  assert.equal(sent.length,0);
  assert.equal(vm.runInContext("claudeRouteConfirm.has('c'.repeat(64))",context),true);
  await context.saveClaudeAccountRouting('claude',false,true);
  assert.deepEqual(sent.at(-1),['/api/claude/accounts/routing',{account_id:'c'.repeat(64),enabled:false,confirmed:true}]);
  assert.equal(vm.runInContext("claudeRouteConfirm.has('c'.repeat(64))",context),false);
  assert.equal(context.fixture.watch_accounts[0].routing_enabled,false);
  context.accountRequest=async()=>{throw Error('last_ready');};
  context.fixture.watch_accounts[0].routing_enabled=true;
  await context.saveClaudeAccountRouting('claude',false);
  assert.equal(vm.runInContext("claudeRouteConfirm.has('c'.repeat(64))",context),true);
  context.accountRequest=async()=>{throw Error('Account change could not be confirmed. Try again.');};
  context.fixture.watch_accounts[1].routing_enabled=true;
  await context.saveClaudeAccountRouting('claude-b',false);
  assert.equal(vm.runInContext("claudeRouteErrors.get('claude-b')",context),'Change not confirmed. Try again.');
  // Accounts without a verified routing id cannot be switched.
  sent.length=0;
  context.accountRequest=async(route,body)=>{sent.push([route,JSON.parse(JSON.stringify(body))]);return {enabled:body.enabled};};
  context.fixture.watch_accounts[1].routing_id=null;
  await context.saveClaudeAccountRouting('claude-b',false,true);
  assert.equal(sent.length,0);
  assert.match(context.claudeRoutingRow(context.fixture.watch_accounts[1]),/disabled\s+title="Account identity not verified"/);
  console.log('Routing link UI: linked default, per-gateway states, mismatch without a line, split rows, relinking, failures and Claude account switches passed');
}
saving().catch(error=>{console.error(error);process.exitCode=1;});
