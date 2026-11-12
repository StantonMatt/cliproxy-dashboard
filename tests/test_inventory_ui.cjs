'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const nodes=new Map(),generic=new Map();
function node(id='') {return {id,dataset:{},innerHTML:'',hidden:false,value:'',style:{setProperty(k,v){this[k]=v;}},options:[],
  classList:{toggle(){}},addEventListener(){},focus(){},remove(){generic.delete(this.dataset.genericProvider);},
  insertBefore(el){generic.set(el.dataset.genericProvider,el);},prepend(){}};}
const document={documentElement:{dataset:{}},body:{},activeElement:null,
  getElementById(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);},
  createElement:()=>node(),querySelectorAll:selector=>selector==='[data-generic-provider]'?[...generic.values()]:[],
  querySelector:selector=>generic.get(selector.match(/data-generic-provider="([^"]+)"/)?.[1]) || null};
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const account=(provider,i,email)=>({key:provider+'-'+i+'.json',provider,id:provider+'-'+i,auth_index:provider+'-'+i,
  email,label:email || provider+'-'+i+'.json',status:'active',disabled:false,availability:{state:'ready'},windows:[],
  watch_observed:false,routing_id:'a'.repeat(64),routing_enabled:true,runtime_available:true});
const codex=account('codex',0,'shared@example.test'),a=account('claude',0,'shared@example.test'),
  b=account('claude',1,'second@example.test'),d=account('claude',2,'third@example.test'),x=account('xai',0),x2=account('xai',1);
const hours=Array.from({length:24},(_,h)=>h===0?8:0);
x.active=x2.active=true;
x.today={requests:12,success_rate:.75,hours,last_request_at:Date.now()/1000-240};
const usage={available:true,providers:{codex:{accounts:[],hours:[]},claude:{accounts:[],hours:[]},xai:{accounts:[],hours:[]}}};
c.fixture={providers:[{id:'codex',name:'Codex',routable:false,accounts:[codex]},
  {id:'claude',name:'Claude',routable:true,accounts:[a,b,d]},
  {id:'xai',name:'xai',accent:'neutral',routable:false,accounts:[x,x2]}],
  gateway:{online:true,checked_at:new Date().toISOString(),accounts:[codex],claude_accounts:[a,b,d]},usage,watch_accounts:[]};
vm.runInContext('current=fixture;today=fixture;',c);
c.renderProviders(c.fixture);
assert.equal(generic.size,1);
const card=generic.get('xai').innerHTML;
assert.match(card,/data-account-key="xai-0.json"/);
assert.match(card,/Ready to route/);
// Without the add catalog there is no card menu; the only control is the gateway routing switch the account reports
// (the state pill is a button only to open its popover).
assert.equal((card.match(/<button type="button" class="state-text"/g) || []).length,2);
assert.doesNotMatch(card.replace(/<button type="button" class="state-text"/g,''),/<button|<select|Resets|Billing|Sign in|fast mode/);
assert.equal((card.match(/<input [^>]*data-generic-route="xai:xai-0.json"[^>]*checked/g) || []).length,1);
// Layout: each section grows by its account count; xAI takes the monochrome accent and its own mark.
assert.equal(nodes.get('codex-accounts').style['--n'],'1');assert.equal(nodes.get('watch-section').style['--n'],'3');
assert.equal(generic.get('xai').style['--n'],'2');
assert.match(generic.get('xai').className,/^gateway generic-gateway xai-gateway$/);
assert.match(card,/<use href="#xai"\/>/);assert.match(card,/All accounts ready/);assert.match(card,/<strong>2 \/ 2<\/strong>/);
assert.equal((card.match(/class="account-card generic-card xai-card"/g) || []).length,2);
// Basic card: requests today with success and the last request (no per-hour chart); unnamed accounts read "<Provider> account", numbered.
assert.match(card,/<span title="xai account">xai account<\/span>/);assert.match(card,/<span title="xai account 2">xai account 2<\/span>/);
assert.match(card,/<div class="quota-value">12<span class="remaining-word">75% success<\/span>/);
assert.doesNotMatch(card,/hour-chart|hour-bars|hour-axis|Requests by hour/);
assert.match(card,/<span>Last request<\/span><strong>4m ago<\/strong>/);
const signin=c.genericCard(c.fixture.providers[2],{...x2,availability:{state:'auth_required'}},c.fixture.gateway);
assert.match(signin,/generic-card xai-card is-off/);assert.match(signin,/Sign in required/);assert.doesNotMatch(signin,/<button|Ready to route/);
// Catalog providers and API-key channels have their own theme and mark; unknown ones are neutral with their initial.
for(const [id,accent] of [['kimi','kimi'],['kimi-ai','kimi'],['antigravity','antigravity'],['devin','devin'],['meta','meta'],['xai','xai'],
  ['claude-api-key','claude'],['codex-api-key','codex'],['gemini-api-key','gemini'],['xai-api-key','xai'],['openai-compatibility','compat'],['mystery','other']])
  assert.equal(c.providerAccent({id,accent:'neutral'}),accent,id);
assert.match(c.providerIcon({id:'kimi-ai',name:'Kimi'}),/<use href="#kimi"\/>/);
assert.match(c.providerIcon({id:'mystery',name:'mystery'}),/<span class="monogram" aria-hidden="true">M<\/span>/);
assert.equal(vm.runInContext("[gatewayRow('xai'),gatewayRow('mystery'),gatewayRow(undefined),gatewayRow('claude')].join()",c),'xai-row,other-row,codex-row,claude-row');
assert.equal(c.seriesColor(c.series('xai','xai-0')),'var(--xai)');assert.equal(c.seriesColor(c.series('xai','xai-1')),'var(--xai-2)');
assert.match(nodes.get('claude-routing-mode').innerHTML,/<option value="claude-2.json">Prefer third@example.test/);
assert.equal(JSON.stringify(c.routingPayload('claude-2.json','claude')),JSON.stringify({mode:'preferred-account',preferred_key:'claude-2.json'}));
assert.doesNotMatch(nodes.get('routing-all-mode').innerHTML,/second@example.test|third@example.test/);
assert.equal(c.codexRoutable(c.fixture.gateway),false);
assert.equal(c.claudePolicyRoutable(),true);
c.renderWatch(c.fixture);
assert.equal((nodes.get('watch-grid').innerHTML.match(/class="account-card claude-card/g) || []).length,3);
assert.match(nodes.get('watch-grid').innerHTML,/Ready to route · no usage data/);
const key=c.series('claude','claude-2'),color=c.seriesColor(key);
c.fixture.providers[1].accounts.reverse();
assert.equal(c.series('claude','claude-2'),key);assert.equal(c.seriesColor(key),color);
assert.notEqual(c.series('codex','codex-0'),c.series('claude','claude-0'));
c.fixture.providers=[];c.renderProviders(c.fixture);c.renderWatch(c.fixture);
assert.equal(generic.size,0);assert.equal(nodes.get('codex-accounts').hidden,true);assert.equal(nodes.get('watch-section').hidden,true);
assert.equal(nodes.get('inventory-empty').hidden,false);assert.equal(nodes.get('inventory-empty-icons').hidden,false);
assert.match(nodes.get('inventory-empty-text').textContent,/No accounts connected yet/);
assert.equal(nodes.get('inventory-empty-add').hidden,true);
assert.equal(c.codexRoutable(c.fixture.gateway),false);assert.equal(c.claudePolicyRoutable(),false);
console.log('Inventory UI: empty roster, three Claude accounts, filename series, dynamic options and email-less xAI cards passed');
