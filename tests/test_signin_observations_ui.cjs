'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const elements=new Map(), listeners=new Map();
const document={documentElement:{dataset:{}},body:{},activeElement:null,
  addEventListener(){},querySelectorAll:()=>[],querySelector:selector=>selector==='[data-access-focus="a:keep"]'?keep:null,getElementById(id){
    if(!elements.has(id))elements.set(id,{options:[],classList:{toggle(){}},
      addEventListener:(event,callback)=>listeners.set(id+':'+event,callback)});
    return elements.get(id);
  }};
document.activeElement=document.body;
const keep={dataset:{accessFocus:'a:keep'},focus(){document.activeElement=this;}};
const context=vm.createContext({document,console,Date,CSS:{escape:value=>value},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},navigator:{},window:{},
  BillingDates:{describe:()=>({date:'14 Oct 2026'})},setTimeout(){},AbortSignal:{timeout:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
const base={id:'a',access_identity:'a'.repeat(64),email:'one@example.com',label:'one@example.com',plan:'Pro (10x)',
  plan_type:'pro',disabled:false,active:true,windows:[],auth_state:'sign_in_required',
  availability:{state:'ready'},resets:{},signin:{instance:'boot'}};
const session={handle:'synthetic-handle',state:'pending',user_code:'ABCD-EFGH',
  verification_url:'https://auth.openai.com/codex/device',expected_email:base.email,
  started_at:Date.now()/1000,expires_at:Date.now()/1000+900};
const html=()=>elements.get('account-grid').innerHTML;
function reset(anotherReady=false,required=false){
  context.fixture={gateway:{online:true,accounts:[structuredClone(base)],pool:{state:'ready'}},read_only:false};
  if(anotherReady)context.fixture.gateway.accounts.push({...structuredClone(base),id:'b'});
  for(const account of context.fixture.gateway.accounts)account.auth_state=required?'sign_in_required':'authenticated';
  vm.runInContext('current=fixture;failed=false;routeConfirm.clear();routeErrors.clear();signinErrors.clear()',context);
  context.render=()=>context.renderAccounts(context.fixture.gateway);
  context.request=async()=>structuredClone(context.fixture);
  context.render();
}
function staleLoad(){
  let resolveOverview;
  const observation=structuredClone(context.fixture);
  context.request=()=>new Promise(resolve=>{resolveOverview=resolve;});
  const loading=context.load();
  return async()=>{resolveOverview(observation);await loading;};
}
async function freshLoad(){
  context.request=async()=>structuredClone(context.fixture);
  await context.load();
}
async function checks(){
  reset(true);
  const finishStale=staleLoad();
  let posts=0;
  context.fetch=async()=>{posts++;return {ok:false,json:async()=>({error:'last_ready'})};};
  await context.saveAccountRouting('a',false);
  assert.equal(posts,1);
  assert.match(html(),/No other Codex account is ready/);
  assert.equal(document.activeElement,keep);
  assert.doesNotMatch(html(),/Change not confirmed/);
  for(let iteration=0;iteration<3;iteration++){
    context.render();assert.match(html(),/No other Codex account is ready/);
  }
  await finishStale();
  assert.match(html(),/No other Codex account is ready/);
  await freshLoad();
  assert.doesNotMatch(html(),/No other Codex account is ready/);

  reset();
  vm.runInContext('confirmRouting(current.gateway.accounts[0],fastLoadEpoch)',context);
  await freshLoad();
  assert.match(html(),/No other Codex account is ready/);
  context.fixture.gateway.accounts[0].disabled=true;
  await freshLoad();
  assert.doesNotMatch(html(),/No other Codex account is ready/);

  for(const mutation of ['routing','start','cancel']){
    reset(true,mutation!=='routing');
    if(mutation==='cancel')context.fixture.gateway.accounts[0].signin.session=structuredClone(session);
    const finish=staleLoad();
    context.fetch=async()=>{throw new Error('synthetic network error');};
    if(mutation==='routing')await context.saveAccountRouting('a',true);
    else if(mutation==='start')await context.startSignin('a');
    else await context.cancelSignin('a');
    const expected=mutation==='routing'?/Change not confirmed/:mutation==='start'?/Couldn&#39;t reach OpenAI/:/Couldn&#39;t cancel/;
    assert.match(html(),expected);
    context.render();assert.match(html(),expected);
    await finish();assert.match(html(),expected);
    context.request=async()=>{throw new Error('synthetic overview failure');};
    await context.load();
    const map=mutation==='routing'?'routeErrors':'signinErrors';
    assert.equal(vm.runInContext(`${map}.has('a')`,context),true);
    await freshLoad();
    assert.equal(vm.runInContext(`${map}.has('a')`,context),false);
    assert.doesNotMatch(html(),expected);
  }

  reset(true,true);
  const finishRemoved=staleLoad();
  context.fetch=async()=>{throw new Error('synthetic network error');};
  await context.startSignin('a');
  await finishRemoved();
  context.fixture.gateway.accounts=[];
  context.render();
  assert.equal(vm.runInContext("signinErrors.has('a')",context),false);
  console.log('Sign-in observations UI: backend confirmation, stale/in-flight overviews, routing/start/cancel errors, failed/fresh polls and removed-account pruning passed.');
}
checks().catch(error=>{console.error(error);process.exitCode=1;});
