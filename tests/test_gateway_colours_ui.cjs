'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const A='account-a@example.com',B='account-b@example.com';

// Each gateway has its own colour in both themes; status colours are not reused for accounts.
assert.match(css,/:root\{[^}]*--codex:#[0-9a-f]{6};--codex-2:#[0-9a-f]{6};--claude:#[0-9a-f]{6};--claude-2:#[0-9a-f]{6}/);
assert.match(css,/\[data-theme=light\]\{[^}]*--codex:#[0-9a-f]{6};--codex-2:#[0-9a-f]{6};--claude:#[0-9a-f]{6};--claude-2:#[0-9a-f]{6}/);
assert.doesNotMatch(css,/\.mint\{background/);
for(const rule of ['.codex-one{background:var(--codex)}','.claude-two{background:var(--claude-2)}','tbody tr.codex-row,tbody tr.claude-row{background:var(--surface)'])assert.ok(css.includes(rule),rule);
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
      {id:'claude-b',email:B,status:'ok',checked_at:new Date().toISOString(),routing_id:'d'.repeat(64),routing_enabled:true,runtime_available:true,
        windows:[window('five_hour','5-hour limit',12,14000),window('seven_day','Weekly · all models',2,480000)]}]};
}
// Values from the page's realm compare by content.
const set=data=>{context.fixture=data;vm.runInContext('current=fixture;failed=false',context);};

// Two shades per gateway: the second account of each gateway gets the second shade.
set(snapshot());
assert.equal(context.series('codex','a1'),'codex-one');
assert.equal(context.series('codex','a2'),'codex-two');
assert.equal(context.series('claude','c0'),'claude-one');
assert.equal(context.series('claude','c1'),'claude-two');
assert.equal(context.series('codex','missing',B),'codex-two');
assert.equal(context.seriesNames()['codex-one'],A);
assert.equal(context.seriesNames()['codex-two'],B);
// Position determines the series even without a label or a configured email.
context.fixture.gateway.accounts=[{id:'custom-a',email:'z@example.test'},{id:'custom-b',email:'a@example.test'}];
assert.equal(context.series('codex','custom-a'),'codex-one');
assert.equal(context.series('codex','custom-b'),'codex-two');
assert.equal(context.seriesNames()['codex-one'],'z@example.test');
assert.equal(context.seriesNames()['codex-two'],'a@example.test');
context.fixture.gateway.accounts.reverse();
assert.equal(context.series('codex','custom-a'),'codex-two');
assert.equal(context.series('codex','custom-b'),'codex-one');
assert.equal(context.seriesNames()['codex-one'],'a@example.test');
assert.equal(context.seriesNames()['codex-two'],'z@example.test');
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
assert.match(context.providerChip('claude','c1',null),/<\/i>—<\/span>$/);
set(snapshot());
assert.match(context.providerChip('claude','missing',null),/Unknown account/);
context.fixture.watch_accounts[1].routing_enabled=false;
context.renderWatch(context.fixture);
assert.match($('watch-grid').innerHTML,/claude-card is-off/);

console.log('Gateway colours UI: tokens, account shades and loading state passed');
