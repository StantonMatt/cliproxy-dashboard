'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const A='owner@example.test',B='alternate@example.test';

// Each gateway has its own colour in both themes; status colours are not reused for accounts.
assert.match(css,/:root\{[^}]*--codex:#[0-9a-f]{6};--codex-2:#[0-9a-f]{6};--claude:#[0-9a-f]{6};--claude-2:#[0-9a-f]{6}/);
assert.match(css,/\[data-theme=light\]\{[^}]*--codex:#[0-9a-f]{6};--codex-2:#[0-9a-f]{6};--claude:#[0-9a-f]{6};--claude-2:#[0-9a-f]{6}/);
assert.doesNotMatch(css,/\.mint\{background/);
for(const rule of ['tbody tr.codex-row,tbody tr.claude-row{background:var(--surface)'])assert.ok(css.includes(rule),rule);
// Cards and table rows of a gateway share one palette in both themes, so they cannot drift apart.
for(const g of ['codex','claude']){
  assert.ok(css.includes(`.account-card.${g}-card,tbody tr.${g}-row{--accent:var(--${g})`),g);
  assert.ok(css.includes(`[data-theme=light] .account-card.${g}-card,[data-theme=light] tbody tr.${g}-row{--surface:`),g);
}

const elements=new Map();
function element(id){
  return {id,hidden:false,disabled:false,value:'',textContent:'',innerHTML:'',className:'',title:'',checked:false,style:{},
    options:[],addEventListener(){},removeAttribute(){},focus(){},querySelector(){return null;},before(){},classList:{toggle(){}}};
}
const storage=new Map();
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,activeElement:null,
  getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);}};
const $=id=>document.getElementById(id);
const context=vm.createContext({document,Date,JSON,Math,
  localStorage:{getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,v)}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);

const now=Date.now()/1000;
const window=(kind,label,used,offset)=>({kind,label,used,remaining:100-used,resets_at:now+offset,seconds:kind==='five_hour'?18000:604800});
function snapshot(){
  return {read_only:false,health:{},
    gateway:{online:true,accounts:[{id:'a1',label:A,email:A},{id:'a2',label:B,email:B}],
      claude_accounts:[{auth_index:'c0',email:A},{auth_index:'c1',email:B}]},
    watch_accounts:[
      {id:'claude',email:A,status:'ok',checked_at:new Date().toISOString(),routing_id:'c'.repeat(64),routing_enabled:true,runtime_available:true,
        windows:[window('five_hour','5-hour limit',20,9000),window('seven_day','Weekly · all models',97,200000)]},
      {id:'claude-b',independent_windows:true,email:B,status:'ok',checked_at:new Date().toISOString(),routing_id:'d'.repeat(64),routing_enabled:true,runtime_available:true,
        windows:[window('five_hour','5-hour limit',12,14000),window('seven_day','Weekly · all models',2,480000)]}]};
}
// Values from the page's realm compare by content.
const set=data=>{context.fixture=data;vm.runInContext('current=fixture;failed=false',context);};

// Series and palette are tied to account identity and survive reordered inventories.
set(snapshot());
assert.equal(context.series('codex','a1'),'codex:a1');
assert.equal(context.series('codex','a2'),'codex:a2');
assert.equal(context.series('claude','c0'),'claude:claude');
assert.equal(context.series('claude','c1'),'claude:claude-b');
assert.equal(Object.fromEntries([...context.seriesEntries()].map(([k,v])=>[k,v.name]))['codex:a1'],A);
assert.equal(Object.fromEntries([...context.seriesEntries()].map(([k,v])=>[k,v.name]))['codex:a2'],B);
context.fixture.gateway.accounts=[{id:'custom-a',key:'z.json',email:'z@example.test'},{id:'custom-b',key:'a.json',email:'a@example.test'}];
const key=context.series('codex','custom-a'),color=context.seriesColor(key);
context.fixture.gateway.accounts.reverse();
assert.equal(context.series('codex','custom-a'),key);
assert.equal(context.seriesColor(key),color);
assert.equal(Object.fromEntries([...context.seriesEntries()].map(([k,v])=>[k,v.name]))['codex:z.json'],'z@example.test');
assert.equal(context.series('codex','missing'),'codex:removed:missing');
// Accounts sharing a colour family take its four shades in a fixed order: the family's own provider first, then by
// provider and key, with removed history last. Order survives reordered inventories; a provider total takes the first.
set(snapshot());
const shades=()=>['codex:a1','codex:a2','claude:claude','claude:claude-b','codex:total'].map(k=>context.seriesColor(k)).join();
assert.equal(shades(),'var(--codex),var(--codex-2),var(--claude),var(--claude-2),var(--codex)');
context.fixture.gateway.accounts.reverse();context.fixture.watch_accounts.reverse();
assert.equal(shades(),'var(--codex),var(--codex-2),var(--claude),var(--claude-2),var(--codex)');
context.fixture.usage={hours:[{auth_index:'old',requests:3}]};
assert.equal(context.seriesColor('codex:removed:old'),'var(--codex-3)');
assert.equal(context.seriesColor('codex:a2'),'var(--codex-2)');
set({...snapshot(),providers:[
  {id:'claude-api-key',name:'Anthropic API key',accent:'neutral',accounts:[{key:'apikey-a',label:'Key ••••7Qa'}]},
  {id:'claude',name:'Claude',accent:'claude',accounts:[{key:'z.json',email:B}]},
  {id:'zeta',name:'zeta',accent:'neutral',accounts:[{key:'z-1.json',email:'z@example.test'}]},
  {id:'mystery',name:'mystery',accent:'neutral',accounts:[{key:'m-1.json',email:'m@example.test'}]}]});
assert.equal(['claude:z.json','claude-api-key:apikey-a','mystery:m-1.json','zeta:z-1.json'].map(k=>context.seriesColor(k)).join(),
  'var(--claude),var(--claude-2),var(--blue),var(--blue-2)');
// Every family defines all four shades in both themes.
for(const name of ['codex','claude','xai','blue'])for(const n of [3,4])assert.match(css,new RegExp(`:root\\{[^}]*--${name}-${n}:#[0-9a-f]{6}`),name+n);
for(const name of ['codex','claude','xai','blue'])assert.match(css,new RegExp(`\\[data-theme=light\\]\\{[^}]*--${name}-3:#[0-9a-f]{6}`),name);
assert.match(css,/:root\{[^}]*--blue-2:#[0-9a-f]{6}/);assert.match(css,/\[data-theme=light\]\{[^}]*--blue-2:#[0-9a-f]{6}/);
for(const name of ['gemini','meta','antigravity','kimi','devin','compat'])for(const n of [3,4])assert.match(css,new RegExp(`--${name}-${n}:color-mix\\(`),name+n);
// The widget legend receives the same stable account colours and unambiguous labels.
{
  set(snapshot());const {historyKeys}=require('./helpers/board-widgets.cjs');
  const keys=historyKeys(context);assert(keys.some(k=>k.color==='var(--codex)'&&k.title==='Codex · '+A));
  context.fixture.watch_accounts[1].email='owner@elsewhere.test';
  const names=historyKeys(context).filter(k=>k.provider==='claude').map(k=>k.name);
  assert(names.includes(A));assert(names.includes('owner@elsewhere.test'));
}
set(snapshot());
// Before the gateway's first snapshot, routing is unknown: nothing is "off" and no header claims appear.
const loading=snapshot();
loading.gateway={online:null,accounts:[],claude_accounts:[]};
for(const a of loading.watch_accounts){a.routing_id=null;a.routing_enabled=null;a.runtime_available=null;}
set(loading);
context.renderWatch(loading);
assert.equal($('claude-status').innerHTML,'');
assert.doesNotMatch($('watch-grid').innerHTML,/is-off/);
assert.equal(($('watch-grid').innerHTML.match(/class="unknown" aria-checked="mixed"/g)||[]).length,2);
assert.match(context.providerChip('claude','c1',null),/<\/i><span>—<\/span><\/span>$/);
set(snapshot());
assert.match(context.providerChip('claude','missing',null),/Removed account/);
context.fixture.watch_accounts[1].routing_enabled=false;
context.renderWatch(context.fixture);
assert.match($('watch-grid').innerHTML,/claude-card is-off/);

console.log('Gateway colours UI: tokens, account shades and loading state passed');
