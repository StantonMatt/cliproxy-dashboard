'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const node=()=>({dataset:{},style:{},options:[],classList:{toggle(){}},addEventListener(){}});
const document={documentElement:{dataset:{}},body:{},activeElement:null,getElementById:node,querySelectorAll:()=>[],querySelector:()=>null};
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const now=Date.now()/1000, ago=s=>new Date((now-s)*1000).toISOString();
const hours=Array.from({length:24},(_,h)=>h===0?212:0);
const base={key:'xai-0.json',provider:'xai',email:'owner@example.test',label:'owner@example.test',disabled:false,active:true,
  availability:{state:'ready'},routing_enabled:true,today:{requests:212,success_rate:.995,hours,last_request_at:now-240}};
const p={id:'xai',name:'Grok',accent:'xai',routable:false,accounts:[base]};
const gateway={online:true,checked_at:new Date().toISOString()};
c.fixture={providers:[p]};vm.runInContext('current=fixture;',c);
const card=a=>c.genericCard(p,{...base,...a},gateway);
const monthly={label:'Monthly',used:36,used_pct:36,remaining:64,resets_at:now+12*86400+4*3600+60,measured_at:ago(180),usage_source:'gateway'};

// Without usage windows the card stays the requests-today card.
for(const plain of [card({}),card({windows:[]}),card({windows:[{label:'Monthly',used_pct:null,resets_at:null}]})]) {
  assert.match(plain,/<div class="quota-label">Requests today<\/div>/);
  assert.match(plain,/<div class="quota-value">212<span class="remaining-word">99.5% success<\/span>/);
  assert.match(plain,/<span>Last request<\/span><strong>4m ago<\/strong>/);
  assert.match(plain,/<span class="account-state" title="Ready to route"><svg[^>]*><use href="#check"\/><\/svg><span>Ready to route<\/span>/);
  assert.doesNotMatch(plain,/>remaining<|quota-track|Credits reset|Updated/);
}

// Fresh billing usage: "% remaining" headline from 100 − used_pct, plan badge, credits reset and freshness like Claude cards.
const fresh=card({plan:'SuperGrok',windows:[monthly],usage_checked_at:ago(180),usage_source:'gateway'});
assert.match(fresh,/<span class="small-badge">SuperGrok<\/span>/);
assert.match(fresh,/<div class="quota-label">Monthly credits<\/div>/);
assert.match(fresh,/<div class="quota-value">64<span class="percent">%<\/span><span class="remaining-word">remaining<\/span><\/div>/);
assert.match(fresh,/<div class="quota-track" role="progressbar" aria-label="owner@example.test Monthly credits remaining" aria-valuemin="0" aria-valuemax="100" aria-valuenow="64"><span style="width:64%"><\/span><\/div>/);
assert.match(fresh,/<div class="quota-used"><span>212 requests today<\/span><span>36% used<\/span><\/div>/);
assert.match(fresh,/<span>Credits reset<\/span><strong class="countdown" data-reset="[\d.]+" title="[^"]+">12d 4h<\/strong>/);
assert.match(fresh,/<span>Success today<\/span><strong>99.5%<\/strong>/);
assert.match(fresh,/<span class="account-state watch-state-ok" title="Ready to route · Updated 3m ago"><svg[^>]*><use href="#check"\/><\/svg><span>Updated 3m ago<\/span>/);
assert.match(fresh,/<div class="hour-bars"/);
assert.doesNotMatch(fresh,/Requests today|Last request|last known|limit-rows|usage_source|gateway<\//);
// used_pct wins over a stale "remaining"; secondary windows render as bars under the headline.
const two=card({windows:[{...monthly,used_pct:20,remaining:90},{label:'Weekly · Grok 4',used_pct:75,resets_at:now+3600}],usage_checked_at:ago(30)});
assert.match(two,/aria-valuenow="80"/);assert.match(two,/<span>20% used<\/span>/);
assert.match(two,/<div class="limit-rows"><div class="limit-row" title="Resets [^"]+"><div class="secondary-quota"><span>Weekly · Grok 4<\/span><span>25% remaining<\/span><\/div><div class="limit-track" aria-hidden="true"><span style="width:25%"><\/span><\/div><\/div><\/div>/);
assert.match(two,/<span>Updated 30s ago<\/span>/);
// A label that already names its window is kept, and the facts row says "Resets".
const named=card({windows:[{...monthly,label:'Monthly credits'}],usage_checked_at:ago(30)});
assert.match(named,/<div class="quota-label">Monthly credits<\/div>/);assert.match(named,/<span>Credits reset<\/span>/);
const other=card({windows:[{...monthly,label:'Requests per day',kind:'daily'}],usage_checked_at:ago(30)});
assert.match(other,/<div class="quota-label">Requests per day<\/div>/);assert.match(other,/<span>Resets<\/span>/);

// Stale windows keep the last known numbers, marked as such, with the age in amber.
const stale=card({windows:[monthly],usage_checked_at:ago(2*3600)});
assert.match(stale,/<div class="quota-label">Monthly credits · last known<\/div>/);
assert.match(stale,/aria-valuenow="64"/);
assert.match(stale,/<span class="account-state watch-state-warn" title="Ready to route · Stale · updated 2h ago"><svg[^>]*><use href="#alert"\/><\/svg><span>Stale · updated 2h ago<\/span>/);
assert.match(card({windows:[monthly]}),/<span>Stale<\/span>/);
// A failed latest read marks fresh-looking windows as last known straight away.
assert.match(card({windows:[monthly],usage_checked_at:ago(60),usage_status:'error'}),/Monthly credits · last known/);
assert.match(card({windows:[monthly],usage_checked_at:'not a time'}),/· last known/);
// A window whose reset has passed reads "—" until the next read.
const due=card({windows:[{...monthly,resets_at:now-60}],usage_checked_at:ago(60)});
assert.match(due,/<div class="quota-value">—<span class="remaining-word">remaining<\/span><\/div>/);
assert.doesNotMatch(due,/aria-valuenow/);
assert.match(due,/<span>Waiting for usage data<\/span>/);assert.match(due,/Reset due · refreshing/);

// Other states keep the state spot; freshness moves to the tooltip.
const off=card({disabled:true,availability:{state:'disabled'},windows:[monthly],usage_checked_at:ago(180)});
assert.match(off,/generic-card xai-card is-off/);
assert.match(off,/<span class="account-state" title="Disabled · Updated 3m ago"><span>Disabled<\/span>/);
const signin=card({availability:{state:'auth_required'},windows:[monthly],usage_checked_at:ago(180)});
assert.match(signin,/Sign in required/);assert.doesNotMatch(signin,/account-state/);assert.match(signin,/64<span class="percent">/);
console.log('Generic usage UI: requests-today fallback, billing windows, secondary bars, freshness, stale and reset-due windows passed');
