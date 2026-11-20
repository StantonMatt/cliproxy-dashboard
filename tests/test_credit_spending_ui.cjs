'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const nodes=new Map();
const listeners=new Map(), document={activeElement:null,addEventListener(type,handler){listeners.set(type,handler);}};
function node(id) {
  const n={id,attrs:{},dataset:{},style:{},hidden:false,offsetParent:true,offsetWidth:288,offsetHeight:90,innerHTML:'',textContent:'',
    classList:{toggle(){}},focus(){document.activeElement=n;},setAttribute(k,v){n.attrs[k]=v;},removeAttribute(k){delete n.attrs[k];},
    getBoundingClientRect(){return {right:500,top:300,bottom:340};},querySelector(){return null;},querySelectorAll(){return [];},closest(){return null;}};
  nodes.set(id,n);return n;
}
const pop=node('credits-pop'),input=node('credits-switch'),note=node('credits-pop-note'),error=node('error');
const fact=node('credits-a'),state=node('state-codex-a');pop.querySelector=()=>error;
const retry=node('retry');pop.querySelectorAll=()=>error.hidden?[input]:[input,retry];
let inserted='';const bottom={parentElement:{querySelector:()=>null},insertAdjacentHTML:(where,html)=>{inserted=html;}};
fact.closest=()=>({querySelector:s=>s==='.account-bottom'?bottom:null});
const a={id:'a',email:'fixture@example.test',access_identity:'identity',credit_spending:{enabled:true,paused:null},usage_credits:{balance:12}};
const gateway={online:true,checked_at:new Date().toISOString(),accounts:[a]};
let post,loads=0,renders=0;
const c=vm.createContext({document,Date,Math,Number,Set,Map,console,innerWidth:1000,innerHeight:800,scrollX:0,scrollY:0,
  current:{gateway},failed:false,fastLoadEpoch:0,$:id=>nodes.get(id),accountFor:id=>c.current.gateway.accounts.find(a=>a.id===id),esc:String,icon:id=>'<svg>'+id+'</svg>',
  renderAccounts(){renders++;},accountRequest:async(...args)=>post(...args),load:async()=>{loads++;vm.runInContext('fastLoadEpoch++;for(const [id,p] of creditsPending)if(!p.saving && fastLoadEpoch>p.after)creditsPending.delete(id)',c);},
  allowanceCountdown:()=> '2h 14m',allowanceClock:()=> '10:21 PM',relative:()=> 'Updated 39s ago'});
vm.runInContext(source.slice(source.indexOf('const creditsPending='),source.indexOf('// A check when ready')),c);
const pageClicks=[];c.accountEvents={addEventListener(type,handler){if(type==='click')pageClicks.push(handler);}};
for(const n of nodes.values())n.addEventListener=(type,handler)=>{n[type]=handler;};
vm.runInContext(source.slice(source.indexOf("$('credits-pop').addEventListener('change'"),source.indexOf("$('speed-confirm').addEventListener('click'")),c);
vm.runInContext(source.split('\n').find(line=>line.startsWith("document.addEventListener('keydown'") && line.includes('creditsKey(event)')),c);
const run=code=>vm.runInContext(code,c);
(async()=>{
  c.creditsShow('a');assert.equal(pop.hidden,false);assert.equal(document.activeElement,input);
  assert.equal(fact.attrs['aria-expanded'],'true');assert.equal(pop.attrs['aria-label'],'Credits · fixture@example.test');
  assert.match(note.textContent,/No other Codex account is ready/);
  const focused=document.activeElement;c.syncCredits();assert.equal(document.activeElement,focused);
  let prevented=0;const event=(key,shiftKey=false)=>({key,shiftKey,preventDefault(){prevented++;},stopPropagation(){}});
  c.creditsKey(event('Escape'));assert.equal(pop.hidden,true);assert.equal(document.activeElement,fact);
  c.creditsShow('a');c.creditsKey(event('Tab',true));assert.equal(document.activeElement,fact);assert.equal(pop.hidden,true);
  c.creditsShow('a');c.creditsKey(event('Tab'));assert.equal(document.activeElement,state);assert.equal(pop.hidden,true);
  c.creditsShow('a');input.focus();c.current.gateway.online=false;c.syncCredits();assert.equal(input.disabled,true);assert.equal(input.title,'Gateway unreachable');
  c.current.gateway.online=true;c.syncCredits();assert.equal(input.disabled,false);
  // An unreachable popover owns focus even though its only input is disabled.
  c.creditsClose();c.current.gateway.online=false;c.creditsShow('a');
  assert.equal(document.activeElement,pop);assert.equal(input.disabled,true);
  c.creditsKey(event('Tab'));assert.equal(pop.hidden,true);assert.equal(document.activeElement,state);
  c.creditsShow('a');c.creditsKey(event('Tab',true));assert.equal(pop.hidden,true);assert.equal(document.activeElement,fact);
  c.creditsShow('a');fact.focus();listeners.get('keydown')(event('Escape'));
  assert.equal(pop.hidden,true);assert.equal(document.activeElement,fact);
  assert.match(fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8'),/id="credits-pop"[^>]*tabindex="-1"/);
  c.current.gateway.online=true;c.creditsShow('a');
  let resolve;let calls=0;post=()=>{calls++;return new Promise(r=>resolve=r);};
  const save=c.saveCredits('a',false);assert.equal(calls,1);assert.equal(input.checked,false);assert.equal(input.attrs['aria-busy'],'true');
  assert.match(c.usageCreditsBlock(a),/spending off/);assert.match(c.usageCreditsBlock(a),/aria-busy="true"/);
  assert.equal(input.disabled,false);assert.equal(document.activeElement,input);
  await c.saveCredits('a',true);assert.equal(calls,1);assert.equal(input.checked,false);
  resolve({enabled:false,paused:null});await save;assert.equal(pop.hidden,false);assert.equal(a.credit_spending.enabled,false);assert.equal(document.activeElement,input);
  post=async()=>{throw Error('synthetic');};await c.saveCredits('a',true);
  assert.equal(input.checked,false);assert.match(error.innerHTML,/Couldn't turn spending on · .*Try again/);assert.equal(error.hidden,false);
  // Simulate the same retry click bubbling from the dialog to the page.
  retry.dataset.creditsRetry='a';retry.closest=selector=>selector==='#credits-pop'?pop:selector==='[data-credits-retry]'?retry:null;
  post=async()=>{throw Error('synthetic');};
  const click={target:retry,stopped:false,stopPropagation(){this.stopped=true;}};
  pop.click(click);if(!click.stopped)for(const handler of pageClicks)handler(click);
  assert.equal(document.activeElement,input);assert.equal(pop.hidden,false);
  await new Promise(resolve=>setImmediate(resolve));
  c.creditsClose();assert.match(inserted,/role="status"[^]*?Couldn't turn spending on[^]*?Try again/);
  post=async()=>({enabled:true,paused:null});await c.saveCredits('a',true);assert.equal(c.creditsStatus(a),'');
  c.creditsShow('a');await c.saveCredits('a',false); // Result deliberately fails verification.
  assert.match(error.innerHTML,/Couldn't turn spending off/);
  input.focus();c.creditsKey(event('Tab'));assert.equal(pop.hidden,false);retry.focus();c.creditsKey(event('Tab'));assert.equal(pop.hidden,true);assert.equal(document.activeElement,state);
  // Closing on an outside pointer does not rebuild the target card before its click.
  c.creditsShow('a');const before=renders;c.creditsClose();assert.equal(renders,before);
  c.current.read_only=true;c.creditsShow('a');assert.equal(pop.hidden,true);
  c.current.read_only=false;c.creditsShow('a');nodes.delete('credits-a');c.syncCredits();assert.equal(pop.hidden,true);
  assert(loads>=4);assert(prevented>=4);
  // Run the real overview loader: a read begun before save completion must not
  // restore the old switch value, and saving must queue a new overview.
  nodes.set('credits-a',fact);c.current={gateway};c.failed=false;
  c.busy=false;c.loadDone=Promise.resolve();c.today=null;c.routingEpoch=0;c.layoutSaved=null;
  c.fastPending=new Map();c.reconcileClaude=()=>{};c.reconcileAccess=()=>{};c.startFor=()=>'';
  const reads=[],shown=[];
  c.request=()=>new Promise(resolve=>reads.push(resolve));
  c.render=()=>shown.push(c.creditsEnabled(c.current.gateway.accounts[0]));
  vm.runInContext(source.slice(source.indexOf('async function load('),source.indexOf('function showView()')),c);
  const oldRead=c.load();assert.equal(reads.length,1);
  post=()=>new Promise(r=>resolve=r);
  a.routing_enabled=true;a.disabled=false;a.active=true;a.availability={state:'ready'};
  const race=c.saveCredits('a',false);
  // A routing OFF response arrives while the credit save is in flight.
  a.routing_enabled=false;a.disabled=true;a.active=false;a.availability={state:'disabled'};
  const routingBefore=JSON.stringify([a.routing_enabled,a.disabled,a.active,a.availability]);
  resolve({enabled:false,paused:{windows:['weekly'],resets_at:Date.now()/1000+600}});
  await new Promise(r=>setImmediate(r));
  assert.equal(JSON.stringify([a.routing_enabled,a.disabled,a.active,a.availability]),routingBefore,
    'Credit save must never overwrite routing state');
  reads[0]({gateway:{...gateway,accounts:[{...a,credit_spending:{enabled:true,paused:null}}]}});
  await oldRead;await new Promise(r=>setImmediate(r));
  assert.equal(reads.length,2,'A post-save overview is requested even when load was busy');
  assert.equal(c.creditsEnabled(c.current.gateway.accounts[0]),false);
  assert.equal(c.current.gateway.accounts[0].credit_spending.enabled,false);
  assert.deepEqual(shown,[false]);
  reads[1]({gateway:{...gateway,accounts:[{...a,credit_spending:{enabled:false,paused:null}}]}});
  await race;
  assert.equal(run('creditsPending.size'),0,'Only the post-save overview retires the overlay');
  assert.deepEqual(shown,[false,false]);
  console.log('Credit spending interaction checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
