'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const elements=new Map(),node=id=>({id,dataset:{},style:{},options:[],classList:{toggle(){},add(){},remove(){}},addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]});
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,
  getElementById(id){if(!elements.has(id))elements.set(id,node(id));return elements.get(id);}};
document.activeElement=document.body;
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../dashboard/static/billing.js'),'utf8'),c);
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const now=Date.now(),stamp=new Date(now).toISOString();
const base={id:'fixture',key:'codex-fixture.json',provider:'codex',email:'fixture@example.test',label:'Fixture',plan:'Pro',
  active:true,disabled:false,auth_state:'authenticated',availability:{state:'ready'},quota_checked_at:stamp,usage_checked_at:stamp,
  windows:[{label:'Weekly',seconds:604800,used:40,remaining:60,resets_at:now/1000+400000}],resets:{},
  billing:{renewal_date:new Date(now+30*86400000).toISOString().slice(0,10),confirmed:true}};
const gateway={online:true,checked_at:stamp,accounts:[],pool:{state:'available'}};
c.fixture={gateway,providers:[{id:'codex',name:'Codex',accounts:[]}]};vm.runInContext('current=fixture;failed=false;',c);
function card(extra={}){gateway.accounts=[{...base,...extra}];c.renderAccountsContent(gateway);return elements.get('account-grid').innerHTML;}
const baseline=card();
assert.doesNotMatch(baseline,/usage-credits|<span>Credits<\/span>/);
for(const usage_credits of [undefined,null,{balance:0},{balance:null},{balance:-1},{balance:NaN}])
  assert.equal(card({usage_credits}),baseline);
const credits={balance:60247.828713,unlimited:false,limit_reached:false};
const html=card({usage_credits:credits});
assert.match(html,/<div class="reset-block usage-credits" title="60,247\.83\. Used after the allowance runs out"><span>Credits<\/span><strong>60,248<\/strong><\/div>/);
assert.match(html,/billing-renewal[^]*?<\/strong><\/div><div class="reset-block usage-credits"/);
assert.equal(html.replace(/<div class="reset-block usage-credits"[^]*?<\/strong><\/div>/,''),baseline);
assert.match(card({usage_credits:{...credits,unlimited:true,balance:null}}),/title="Unlimited\. Used after the allowance runs out"><span>Credits<\/span><strong>Unlimited<\/strong>/);
assert.match(card({usage_credits:{...credits,limit_reached:true}}),/class="reset-block usage-credits renewal-soon" title="Spending limit reached"/);
assert.match(card({usage_credits:{balance:42}}),/title="42\. Used after the allowance runs out"[^]*?<strong>42<\/strong>/);
const stale=card({usage_credits:credits,quota_error:'Quota refresh unavailable'});
assert.equal((stale.match(/last known/g)||[]).length,1);
assert.match(stale,/<span>Credits<\/span><strong>60,248<\/strong>/);
console.log('Usage credits UI checks passed');
