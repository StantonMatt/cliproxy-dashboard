'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const elements=new Map(),node=id=>({id,dataset:{},style:{},options:[],classList:{toggle(){},add(){},remove(){}},setAttribute(){},removeAttribute(){},addEventListener(){},closest:()=>null,querySelector:()=>null,querySelectorAll:()=>[]});
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
assert.match(html,/<button type="button" class="reset-block usage-credits credits-button"[^]*?title="60,247\.83\. Used after the allowance runs out"><span>Credits<\/span><strong>60,248<\/strong>/);
assert.match(html,/billing-renewal[^]*?<\/strong><\/div><button type="button" class="reset-block usage-credits/);
assert.equal(html.replace(/<button type="button" class="reset-block usage-credits[^]*?<\/button>/,''),baseline);
assert.match(card({usage_credits:{...credits,unlimited:true,balance:null}}),/title="Unlimited\. Used after the allowance runs out"><span>Credits<\/span><strong>Unlimited<\/strong>/);
assert.match(card({usage_credits:{...credits,limit_reached:true}}),/class="reset-block usage-credits credits-button renewal-soon"[^]*?title="Spending limit reached"/);
assert.match(card({usage_credits:{balance:42}}),/title="42\. Used after the allowance runs out"[^]*?<strong>42<\/strong>/);
const stale=card({usage_credits:credits,quota_error:'Quota refresh unavailable'});
assert.equal((stale.match(/last known/g)||[]).length,1);
assert.match(stale,/<span>Credits<\/span><strong>60,248<\/strong>/);
const off={credit_spending:{enabled:false,paused:null},usage_credits:{balance:0}};
assert.match(card(off),/Credits · spending off<\/span><strong>0/);
assert.match(card({...off,usage_credits:null}),/Credits · spending off<\/span><strong>0/);
assert.match(card(off),/Not spent: this account pauses when its allowance runs out/);
vm.runInContext('current.read_only=true',c);
assert.match(card(off),/<div class="reset-block usage-credits"[^]*?<span>Credits · spending off<\/span>/);
assert.doesNotMatch(card(off),/credits-button|data-credits-open/);
vm.runInContext('current.read_only=false',c);
const paused={...off,disabled:true,active:false,routing_enabled:true,availability:{state:'paused'},
  credit_spending:{enabled:false,paused:{windows:['weekly'],resets_at:now/1000+4000}}};
const pausedHtml=card(paused);
assert.match(pausedHtml,/Paused · back in/);
assert.match(pausedHtml,/Credit spending is off · Weekly allowance used up · No other Codex account is ready/);
assert.match(pausedHtml,/watch-state-warn/);
assert.match(pausedHtml,/aria-label="Use for routing" aria-describedby="state-codex-fixture" checked/);
assert.doesNotMatch(pausedHtml,/codex-card is-off/);
assert.equal(c.resetBlockedReason({...base,...paused}),'');
assert.equal(c.resetBlockedReason({...base,disabled:true}),'This account is disabled.');
assert.match(card({...paused,credit_spending:{enabled:false,paused:{windows:['five_hour'],resets_at:now/1000+8040}}}),/5-hour limit used up/);
assert.match(card({...paused,credit_spending:{enabled:false,paused:{windows:['five_hour','weekly'],resets_at:now/1000+8040}}}),/5-hour limit and weekly allowance used up/);
assert.match(card({...paused,credit_spending:{enabled:false,paused:{windows:['weekly'],resets_at:now/1000-601}}}),/Paused · reset due · checking/);
assert.match(card({...paused,credit_spending:{enabled:false,paused:{windows:['weekly'],resets_at:now/1000-601}}}),/Stays paused until a fresh reading shows allowance/);
const a={...base,...paused};gateway.accounts=[a,{...base,id:'other'}];
assert.equal(c.creditsPausedState(a,gateway).tone,'');
a.credit_spending.paused.resets_at=now/1000-30;
assert.equal(c.creditsPausedState(a,gateway).tone,'');
gateway.accounts=[a];
assert.equal(c.creditsPausedState(a,gateway).tone,'warn'); // Only account: requests already fail during the reset grace period.
gateway.accounts=[a,{...base,id:'other'}];
a.credit_spending.paused.resets_at=now/1000-301;
assert.equal(c.creditsPausedState(a,gateway).tone,'warn');
// A split Account widget hides its source grid while the rebuilt button waits
// for distribution. Popover synchronization must see the distributed button.
gateway.accounts=[{...base,usage_credits:credits}];
const button=node('credits-fixture');button.offsetParent=true;
button.getBoundingClientRect=()=>({right:500,top:100,bottom:140});
elements.set(button.id,button);
const pop=document.getElementById('credits-pop');pop.offsetWidth=288;pop.offsetHeight=90;
const err=node('credits-error');err.innerHTML='';pop.querySelector=()=>err;
c.innerWidth=1000;c.innerHeight=800;c.scrollX=0;c.scrollY=0;
vm.runInContext("creditsOpen='fixture'",c);pop.hidden=false;
const grid=elements.get('account-grid');let gridHtml=grid.innerHTML;
Object.defineProperty(grid,'innerHTML',{get:()=>gridHtml,set:html=>{gridHtml=html;button.offsetParent=null;}});
c.distributeCards=()=>{button.offsetParent=true;};c.splitCardFocus=()=>null;c.restoreSplitFocus=()=>{};
c.renderAccounts(gateway);
assert.equal(pop.hidden,false,'The Account widget popover survives a card rebuild');
assert.equal(vm.runInContext('creditsOpen',c),'fixture');
// A failed save's card retry uses the shared focus restoration across re-renders.
vm.runInContext("creditsOpen=null;creditsErrors.set('fixture',{spend:false})",c);
const oldRetry={dataset:{accessFocus:'fixture:credits-retry'}};
const newRetry={focus(){document.activeElement=this;}};document.activeElement=oldRetry;
document.querySelector=selector=>selector==='[data-access-focus="fixture:credits-retry"]'?newRetry:null;
c.renderAccountsContent(gateway);
assert.match(grid.innerHTML,/data-credits-retry="fixture" data-access-focus="fixture:credits-retry"/);
assert.equal(document.activeElement,newRetry,'Card credit retry retains keyboard focus');
vm.runInContext('failed=true',c);assert.equal(c.creditsPausedState(a,gateway).text,'Status stale');
console.log('Usage credits UI checks passed');
