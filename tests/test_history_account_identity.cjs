'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8'),nodes=new Map();
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,
  getElementById(id){if(!nodes.has(id))nodes.set(id,{options:[],classList:{toggle(){}},addEventListener(){},style:{}});return nodes.get(id);}};
const c=vm.createContext({document,Date,JSON,Math,localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const a={key:'a.json',provider:'claude',id:null,auth_index:null,email:'a@example.test'},b={...a,key:'b.json',email:'b@example.test'};
const row={key:b.key,auth_index:null,requests:7,hour:new Date().toISOString()};
c.fixture={providers:[{id:'claude',name:'Claude',accounts:[a,b]}],gateway:{online:true,claude_accounts:[a,b]},
  usage:{available:true,providers:{claude:{accounts:[row],hours:[row,{...row,key:'removed.json',requests:3}]}}}};
vm.runInContext('current=fixture;today=fixture',c);
assert.equal(c.providerAccount('claude',null,undefined,b.key),b);
assert.equal(c.providerAccount('claude',null),undefined,'missing runtime IDs must not resolve to the first account');
for(const id of [undefined,'','   '])assert.equal(c.providerAccount('claude',id),undefined);
assert.equal(c.providerAccount('claude',null,b.email),b,'explicit email remains a valid fallback');
assert.equal(c.providerAccount('claude','runtime',a.email,b.key),b,'filename wins over conflicting identifiers');
a.id=b.key;assert.equal(c.providerAccount('claude',b.key),b,'filename wins over another account runtime ID');
assert.equal(c.providerAccount('claude',null,a.email,'removed.json'),undefined,'removed filename stays removed');
assert.equal(c.series('claude',null,undefined,b.key),'claude:b.json');
assert.equal(c.series('claude',null),'claude:removed:unknown');
const names=Object.fromEntries([...c.seriesEntries()].map(([k,v])=>[k,v.name]));assert.equal(names['claude:b.json'],b.email);assert.equal(names['claude:removed:removed.json'],'Removed account');
const {install,context:widgetContext,historyKeys}=require('./helpers/board-widgets.cjs');
install(c);const keys=historyKeys(c);assert(keys.some(k=>k.name===b.email&&k.series.total===7));assert(keys.some(k=>k.name==='Removed account'&&k.series.total===3));
(async()=>{
 const host={classList:{add(){},remove(){}},innerHTML:''};await c.window.boardTest.defs.get('usage-accounts').render(host,widgetContext(c,'usage-accounts'));
 assert.match(host.innerHTML,/data-series="claude:b.json"[^]*b@example.test/);assert.doesNotMatch(host.innerHTML,/a@example.test/);
 const html=c.requestTable([{...row,provider:'claude',timestamp:new Date().toISOString()}],false);assert.match(html,/data-series="claude:b.json"[^]*b@example.test/);
 console.log('Widget account identity: filename-first, missing IDs, removed series and table attribution passed');
})().catch(e=>{console.error(e);process.exitCode=1});
