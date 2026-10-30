'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
// A small tree DOM runs the real dialog markup and its delegated handlers.
let documentRef;
class Node {
  constructor(tag='div',attrs={}){this.tag=tag;this.attrs=attrs;this.children=[];this.parentElement=null;this.text='';this.listeners={};this.style={};
    this.classList={add:c=>this.toggleClass(c,true),remove:c=>this.toggleClass(c,false),toggle:(c,on)=>this.toggleClass(c,on),contains:c=>this.className.split(/\s+/).includes(c)};}
  toggleClass(name,on){const list=this.className.split(/\s+/).filter(Boolean).filter(c=>c!==name);if(on!==false)list.push(name);this.attrs.class=list.join(' ');}
  get className(){return this.attrs.class || '';} set className(v){this.attrs.class=v;}
  get id(){return this.attrs.id || '';}
  get dataset(){return Object.fromEntries(Object.entries(this.attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));}
  get disabled(){return Object.hasOwn(this.attrs,'disabled');} set disabled(v){if(v)this.attrs.disabled='';else delete this.attrs.disabled;}
  get hidden(){return Object.hasOwn(this.attrs,'hidden');} set hidden(v){if(v)this.attrs.hidden='';else delete this.attrs.hidden;}
  get value(){return this.attrs.value ?? '';} set value(v){this.attrs.value=v;}
  get tabIndex(){return Number(this.attrs.tabindex ?? 0);}
  get isConnected(){return this===documentRef?.body || !!this.parentElement?.isConnected;}
  get lastElementChild(){return this.children.at(-1) || null;}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(v){for(const child of this.children)child.parentElement=null;this.text=String(v);this.children=[];}
  get innerHTML(){return this.markup || '';}
  set innerHTML(v){for(const child of this.children)child.parentElement=null;this.markup=v;const root=parse(v);this.text=root.text;this.children=root.children;for(const child of this.children)child.parentElement=this;}
  addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);} removeEventListener(){}
  dispatch(type,event={}){let prevented=false;const e={type,key:'',target:this,preventDefault(){prevented=true;},...event};for(const fn of this.listeners[type] || [])fn(e);return prevented;}
  focus(){if(this.isConnected)documentRef.activeElement=this;}
  setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k] ?? null;} removeAttribute(k){delete this.attrs[k];} hasAttribute(k){return Object.hasOwn(this.attrs,k);}
  contains(node){return node===this || this.children.some(c=>c.contains(node));}
  matches(selector){return selector.split(',').some(one=>{
    const s=one.trim().replace(/:not\(:disabled\)$/,''), notDisabled=one.trim().endsWith(':not(:disabled)');
    if(notDisabled && this.disabled)return false;
    const tag=s.match(/^[a-z]+/)?.[0], rest=s.slice(tag?.length || 0);
    if(tag && this.tag!==tag)return false;
    for(const [,kind,name,value] of rest.matchAll(/([.#])([\w-]+)|\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g).map(m=>[m[0],m[1] || '[',m[2] || m[3],m[4]])){
      if(kind==='.' && !this.classList.contains(name))return false;
      if(kind==='#' && this.id!==name)return false;
      if(kind==='[' && (!Object.hasOwn(this.attrs,name) || value!==undefined && this.attrs[name]!==value))return false;
    }
    return true;
  });}
  closest(selector){for(let n=this;n;n=n.parentElement)if(n.matches?.(selector))return n;return null;}
  querySelectorAll(selector){const out=[];const walk=n=>{for(const c of n.children){if(c.matches(selector))out.push(c);walk(c);}};walk(this);return out;}
  querySelector(selector){if(/^\.\S+ \S+$/.test(selector)){const [outer,inner]=selector.split(' ');return this.querySelector(outer)?.querySelector(inner) || null;}return this.querySelectorAll(selector)[0] || null;}
}
function parse(html){const root=new Node(),stack=[root];
  for(const token of html.match(/<[^>]+>|[^<]+/g) || []){
    if(token.startsWith('</')){stack.pop();continue;}
    if(!token.startsWith('<')){stack.at(-1).text+=token.replace(/&amp;/g,'&').replace(/&#39;/g,"'").replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>');continue;}
    const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const attrs={};
    for(const [,key,value] of token.matchAll(/\s([\w-]+)(?:="([^"]*)")?/g))attrs[key]=(value ?? '').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
    const node=new Node(tag,attrs);node.parentElement=stack.at(-1);node.parentElement.children.push(node);
    if(!['input','wbr','br','hr','img','use'].includes(tag) && !token.endsWith('/>'))stack.push(node);
  }return root;
}
const elements=new Map();
const document=documentRef={documentElement:{dataset:{}},body:new Node('body'),activeElement:null,addEventListener(){},removeEventListener(){},
  getElementById(id){
    const found=this.body.querySelector(`[id="${id}"]`);if(found)return found;
    if(elements.has(id))return null;
    const node=new Node('div',{id});elements.set(id,node);node.parentElement=this.body;this.body.children.push(node);return node;
  },
  querySelector:()=>null,querySelectorAll:()=>[],createElement:tag=>new Node(tag)};
document.activeElement=document.body;
const dialog=document.getElementById('account-dialog');
dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};
const menu=document.getElementById('card-menu');
Object.assign(menu,{showPopover(){},hidePopover(){},offsetWidth:208,offsetHeight:90});
// Timers run only when a test advances them; fetch replies come from a per-route queue.
let timers=[],timerId=0,clock=Date.parse('2026-10-30T12:00:00Z');
class FakeDate extends Date { constructor(...a){super(...(a.length?a:[clock]));} static now(){return clock;} }
const calls=[],replies=[];
const reply=(status,data)=>replies.push({status,data});
async function fetch(url,options={}){
  calls.push({url,method:options.method,body:options.body?JSON.parse(options.body):undefined});
  const next=replies.shift();assert(next,'unexpected request '+url);
  if(next.status==='throw')throw new TypeError('Failed to fetch');
  if(next.status==='hold')return next.data;
  return {status:next.status,ok:next.status>=200 && next.status<300,json:async()=>next.data};
}
let copied=[];
const windowEvents={};
const c=vm.createContext({document,Date:FakeDate,JSON,Math,URL,AbortSignal,fetch,CSS:{escape:v=>v},localStorage:{getItem:()=>null},
  navigator:{clipboard:{writeText:async text=>{copied.push(text);}}},window:{addEventListener(type,fn){windowEvents[type]=fn;},removeEventListener(){}},
  innerWidth:1440,innerHeight:900,scrollX:0,scrollY:0,BillingDates:{describe:()=>({})},
  setTimeout:(fn,ms)=>{timers.push({id:++timerId,at:clock+ms,fn});return timerId;},clearTimeout:id=>{timers=timers.filter(t=>t.id!==id);}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
vm.runInContext(source.slice(source.indexOf("window.addEventListener('pagehide'"),source.indexOf("document.querySelectorAll('[data-range]')")),c);
const flush=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
async function advance(ms){
  const until=clock+ms;
  for(;;){timers.sort((a,b)=>a.at-b.at);const t=timers[0];if(!t || t.at>until)break;timers.shift();clock=t.at;t.fn();await flush();}
  clock=until;
}
const button=selector=>dialog.querySelector(selector);
const click=async(target,options={})=>{assert(target,'missing click target');dialog.dispatch('click',{target,...options});await flush();};
const html=()=>dialog.textContent.replace(/\s+/g,' ');
let loads=0,onLoad=()=>{};
c.load=async()=>{loads++;onLoad();};
let renders=0;c.render=()=>{renders++;};

const ready={state:'ready',scope:'account'};
const grok=(i,extra={})=>({key:`xai-${i}.json`,auth_index:`xai-${i}`,provider:'xai',label:`xai-${i}.json`,status:'active',disabled:false,active:true,
  availability:ready,routing_enabled:true,today:{requests:0,hours:[]},...extra});
const claude=(i,email)=>({key:`claude-${i}.json`,auth_index:`claude-${i}`,provider:'claude',email,label:email,status:'active',disabled:false,active:true,availability:ready});
const catalog=[{id:'claude',name:'Claude',login:'browser',callback_example:'http://localhost:54545/callback?code=…&state=…'},
  {id:'xai',name:'Grok',login:'device'},{id:'kimi',name:'Kimi',login:'device'},{id:'kimi-ai',name:'Kimi',login:'device'}];
function reset(){
  c.fixture={providers:[{id:'claude',name:'Claude',routable:true,accounts:[claude(0,'a@example.test'),claude(1,'b@example.test')]},
    {id:'xai',name:'Grok',accent:'xai',routable:false,accounts:[grok(0)]}],
    gateway:{online:true,checked_at:new FakeDate().toISOString(),accounts:[],claude_accounts:[]},usage:{available:true,providers:{}},watch_accounts:[],
    add_providers:catalog,access_transport:'local',read_only:false};
  vm.runInContext('current=fixture;failed=false;',c);
  calls.length=0;replies.length=0;timers=[];copied=[];loads=0;onLoad=()=>{};
}
function mount(node){if(node.id)elements.set(node.id,node);node.parentElement=document.body;document.body.children.push(node);return node;}
function unmount(node){node.parentElement.children=node.parentElement.children.filter(child=>child!==node);node.parentElement=null;}
const opener=mount(new Node('button',{id:'opener'}));
const expires=minutes=>new FakeDate(clock+minutes*60000).toISOString();

(async()=>{
  // The top-bar entry follows the catalog; read-only views show it disabled; a stale card menu closes.
  reset();
  c.renderAccountEntry(c.fixture);
  assert.equal(document.getElementById('add-account').hidden,false);assert.equal(document.getElementById('add-account').disabled,false);
  c.renderAccountEntry({...c.fixture,read_only:true});assert.equal(document.getElementById('add-account').disabled,true);
  c.renderAccountEntry({...c.fixture,add_providers:undefined});assert.equal(document.getElementById('add-account').hidden,true);
  assert.equal(vm.runInContext("cardMenuButton('xai',{key:'xai-0.json'},'Grok account')",c).includes('aria-label="Account actions · Grok account"'),true);
  vm.runInContext('current={...fixture,add_providers:undefined}',c);
  assert.equal(c.cardMenuButton('xai',{key:'xai-0.json'},'Grok account'),'');

  // Picker: catalog order, Code or Browser, and duplicate names told apart by id. The first option takes focus.
  reset();
  c.openAdd(null,null,opener);
  assert.equal(dialog.open,true);
  const options=dialog.querySelectorAll('.provider-option');
  assert.deepEqual(options.map(o=>[o.children[1].textContent,o.querySelector('small').textContent]),[['Claude','Browser'],['Grok','Code'],['Kimi','Code · kimi'],['Kimi','Code · kimi-ai']]);
  assert.equal(document.activeElement,options[0]);

  // Code path: picking starts at once; the code shows large with Copy; opening the page copies it; polling finishes on its own.
  reply(200,{session:'s-grok',provider:'xai',flow:'device',url:'https://auth.x.ai/device?user_code=WDJB-MJHT',user_code:'WDJB-MJHT',expires_at:expires(15)});
  await click(options[1]);
  assert.deepEqual(calls[0],{url:'/api/accounts/add/start',method:'POST',body:{provider:'xai'}});
  assert.match(html(),/Add Grok account/);assert.equal(button('.code-box code').textContent,'WDJB-MJHT');
  assert.equal(button('[data-add-open]').getAttribute('href'),'https://auth.x.ai/device?user_code=WDJB-MJHT');
  assert.equal(document.activeElement,button('[data-add-open]'));
  assert.match(button('[data-add-status]').textContent,/Waiting for approval · 15 min left/);
  await click(button('[data-add-open]'));
  assert.deepEqual(copied,['WDJB-MJHT']);assert.match(button('[data-add-copy]').textContent,/Copied/);
  reply(200,{state:'waiting'});await advance(3000);
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/status?session=s-grok',method:'GET',body:undefined});
  reply(200,{state:'waiting',message:'Gateway temporarily unavailable. Try again.'});await advance(3000);
  assert.match(button('[data-add-status]').textContent,/Gateway unreachable · retrying/);assert.match(button('[data-add-status]').className,/warn/);
  onLoad=()=>{c.fixture.providers[1].accounts.push(grok(1));};
  reply(200,{state:'ok',account:{key:'xai-1.json',provider:'xai',label:'xai-1.json',replaced:false}});await advance(3000);
  assert.equal(loads,1,'the result waits for the new inventory');
  assert.match(html(),/Grok account added/);assert.match(html(),/Ready to route/);
  assert.doesNotMatch(html(),/xai-1\.json|Grok account 2/,'a generic name is not repeated under the title');
  assert.equal(document.activeElement,button('[data-add-done]'));
  const before=calls.length;await advance(3200);
  assert.equal(dialog.open,false,'the result closes by itself');assert.equal(calls.length,before,'a finished sign-in is not cancelled');
  assert.equal(document.activeElement,opener);

  // Browser path: Sign in again opens on the provider's step; the address is checked before it is sent.
  reset();
  reply(200,{session:'s-claude',provider:'claude',flow:'browser',url:'https://claude.ai/oauth/authorize?state=abc',expires_at:expires(10)});
  c.openAdd('claude','claude-1.json',opener);await flush();
  assert.match(html(),/Sign in to Claude again/);
  const input=()=>button('#add-callback');
  assert.equal(input().getAttribute('placeholder'),'http://localhost:54545/callback?code=…&state=…');
  assert.equal(button('[data-add-continue]').disabled,true);assert.equal(document.activeElement,button('[data-add-open]'));
  assert.match(button('[data-add-status]').textContent,/Sign-in open · 10 min left/);
  await click(button('[data-add-open]'));await advance(0);
  assert.match(button('[data-add-open]').className,/secondary/);assert.match(button('[data-add-open]').textContent,/Open sign-in page again/);
  assert.match(button('[data-add-continue]').className,/confirm/);assert.equal(document.activeElement,input());
  const type=value=>{input().value=value;dialog.dispatch('input',{target:input()});};
  type('https://example.com/callback?code=1');assert.equal(button('[data-add-continue]').disabled,false);
  await click(button('[data-add-continue]'));
  assert.equal(button('#add-callback-error').textContent,"That isn't the sign-in address. It starts with http://localhost.");
  assert.equal(button('#add-callback-error').hidden,false);assert.equal(input().getAttribute('aria-invalid'),'true');
  assert.equal(calls.filter(x=>x.url.endsWith('/callback')).length,0,'an invalid address is never sent');
  type('http://localhost:54545/callback?state=abc');assert.equal(button('#add-callback-error').hidden,true,'editing clears the error');
  await click(button('[data-add-continue]'));assert.equal(button('#add-callback-error').hidden,false,'a code parameter is required');
  type('http://localhost:54545/callback?code=c&state=old');
  reply(400,{error:'invalid_callback',message:'Paste the complete callback URL for this sign-in.'});
  const held={};reply('hold',new Promise(r=>held.resolve=r));
  calls.length=0;replies.reverse();
  // Enter submits; while finishing, the field is read-only but Cancel and Escape remain available.
  dialog.dispatch('keydown',{key:'Enter',target:input()});await flush();
  assert.deepEqual(calls[0],{url:'/api/accounts/add/callback',method:'POST',body:{session:'s-claude',redirect_url:'http://localhost:54545/callback?code=c&state=old'}});
  assert.equal(input().hasAttribute('readonly'),true);assert.match(button('[data-add-continue]').textContent,/Finishing sign-in…/);
  assert.equal(button('[data-dialog-cancel]').disabled,false);
  assert.equal(dialog.dispatch('cancel'),false,'Escape is allowed while finishing');
  held.resolve({status:400,ok:false,json:async()=>({error:'invalid_callback',message:'Paste the complete callback URL for this sign-in.'})});await flush();
  assert.equal(button('#add-callback-error').textContent,'Paste the complete callback URL for this sign-in.');
  assert.equal(input().hasAttribute('readonly'),false);assert.equal(document.activeElement,input());
  replies.length=0;
  type('http://localhost:54545/callback?code=c&state=abc');
  reply(200,{accepted:true});await click(button('[data-add-continue]'));
  assert.match(button('[data-add-continue]').textContent,/Finishing sign-in…/);
  reply(200,{state:'ok',account:{key:'claude-1.json',provider:'claude',label:'b@example.test',replaced:true}});await advance(2000);
  assert.match(html(),/Claude account updated/);assert.match(html(),/b@example.test/);assert.match(html(),/Already connected · sign-in replaced/);
  await click(button('[data-add-done]'));assert.equal(dialog.open,false);

  // One sign-in at a time: a pending one elsewhere shows its message and can be retried.
  reset();c.openAdd(null,null,opener);
  reply(409,{error:'busy',message:'Finish or cancel the current sign-in first.'});
  await click(dialog.querySelectorAll('.provider-option')[1]);
  assert.match(html(),/A sign-in is in progress/);assert.match(html(),/Finish or cancel the current sign-in first\./);
  reply(200,{session:'s-retry',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'AAAA-BBBB',expires_at:expires(15)});
  await click(button('[data-add-restart]'));
  assert.deepEqual(calls.at(-1).body,{provider:'xai'});assert.equal(button('.code-box code').textContent,'AAAA-BBBB');
  // Cancel at any point cancels the pending sign-in on the gateway.
  reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));
  assert.equal(dialog.open,false);assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/cancel',method:'POST',body:{session:'s-retry'}});
  // Closing the dialog (Escape or the browser) cancels too.
  reply(200,{session:'s-esc',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'CCCC-DDDD',expires_at:expires(15)});
  c.openAdd('xai',null,opener);await flush();
  reply(200,{cancelled:true});assert.equal(dialog.dispatch('cancel'),false);dialog.close();dialog.dispatch('close');await flush();
  assert.deepEqual(calls.at(-1).body,{session:'s-esc'});assert.equal(timers.length,0,'polling stops with the dialog');
  // A dialog closed while starting cancels the session that arrives afterwards.
  const late={};reply('hold',new Promise(r=>late.resolve=r));
  c.openAdd('xai',null,opener);await flush();assert.match(html(),/Starting sign-in…/);
  await click(button('[data-dialog-cancel]'));assert.equal(dialog.open,false);
  reply(200,{cancelled:true});
  late.resolve({status:200,ok:true,json:async()=>({session:'s-late',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'EEEE-FFFF',expires_at:expires(15)})});await flush();
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/cancel',method:'POST',body:{session:'s-late'}});assert.equal(dialog.open,false);

  // Expired codes are struck through and offer a new code; errors and an unreachable start offer Try again.
  reset();reply(200,{session:'s-old',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'GGGG-HHHH',expires_at:expires(1)});
  c.openAdd('xai',null,opener);await flush();
  assert.match(button('[data-add-status]').textContent,/Waiting for approval · 60 s left/);
  reply(200,{state:'expired',message:'Sign-in expired. Start again.'});await advance(3000);
  assert.match(button('.code-box').className,/dim/);assert.match(html(),/Code expired/);
  assert.equal(button('[data-add-restart]').textContent,'Get a new code');assert.equal(document.activeElement,button('[data-add-restart]'));
  reply('throw');await click(button('[data-add-restart]'));
  assert.match(html(),/Couldn't start sign-in · gateway unreachable/);assert.equal(button('[data-add-restart]').textContent,'Try again');
  reply(200,{session:'s-new',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'JJJJ-KKKK',expires_at:expires(15)});
  await click(button('[data-add-restart]'));
  reply(200,{state:'error',message:'Sign-in failed. Start again.'});await advance(3000);
  assert.match(html(),/Sign-in failed\. Start again\./);assert.equal(button('[data-add-restart]').textContent,'Try again');
  await click(button('[data-dialog-cancel]'));
  reply(200,{session:'s-js',provider:'xai',flow:'device',url:'javascript:alert(1)',user_code:'X',expires_at:expires(15)});
  c.openAdd('xai',null,opener);await flush();
  assert.match(html(),/Couldn't start sign-in/,'only an https sign-in page is linked');assert.equal(button('[data-add-open]'),null);
  await click(button('[data-dialog-cancel]'));

  // S1: old callback and poll responses cannot mutate a restarted attempt on the same flow object.
  async function staleAttemptResponses() {
    const start=(session)=>reply(200,{session,flow:'browser',url:'https://claude.ai/oauth/authorize',expires_at:expires(10)});
    const response=data=>({status:200,ok:true,json:async()=>data});
    const paste=()=>{button('#add-callback').value='http://localhost:54545/callback?code=c&state=abc';dialog.dispatch('input',{target:button('#add-callback')});};
    reset();start('s1-old');c.openAdd('claude',null,opener);await flush();
    paste();const oldCallback={};reply('hold',new Promise(r=>oldCallback.resolve=r));
    await click(button('[data-add-continue]'));
    reply(200,{state:'expired'});await advance(2000);
    start('s1-new');await click(button('[data-add-restart]'));
    paste();const newCallback={};reply('hold',new Promise(r=>newCallback.resolve=r));
    await click(button('[data-add-continue]'));
    oldCallback.resolve({status:400,ok:false,json:async()=>({message:'Old callback error'})});await flush();
    assert.match(button('[data-add-continue]').textContent,/Finishing sign-in…/);
    assert.doesNotMatch(html(),/Old callback error/);
    newCallback.resolve(response({accepted:true}));await flush();
    reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));

    reset();start('s1-poll-old');c.openAdd('claude',null,opener);await flush();
    const oldPoll={};reply('hold',new Promise(r=>oldPoll.resolve=r));await advance(2000);
    paste();reply(409,{message:'Session ended'});await click(button('[data-add-continue]'));
    start('s1-poll-new');await click(button('[data-add-restart]'));
    oldPoll.resolve(response({state:'ok',account:{key:'old.json'}}));await flush();
    assert.equal(loads,0,'an old poll cannot refresh inventory or finish the new session');
    assert.match(html(),/Add Claude account/);assert(button('[data-add-continue]'));
    reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));
  }
  await staleAttemptResponses();

  // S2: cancellation failures retain the session and offer retry, including after closing and reopening Add.
  async function retryStrandedCancellation() {
    reset();reply(200,{session:'s2-retained',flow:'device',url:'https://auth.x.ai/device',user_code:'AAAA-BBBB'});
    c.openAdd('xai',null,opener);await flush();
    reply(503,{message:'Cancellation unavailable'});await click(button('[data-dialog-cancel]'));
    assert.equal(dialog.open,true);assert.match(html(),/Cancellation unavailable/);
    assert.equal(button('[data-add-cancel-retry]').textContent,'Retry cancel');
    await click(button('[data-dialog-cancel]'));assert.equal(dialog.open,false);
    const before=calls.length;c.openAdd('claude',null,opener);await flush();
    assert.equal(calls.length,before,'reopening Add must recover the retained session before starting another');
    assert.match(html(),/Add Grok account/);assert(button('[data-add-cancel-retry]'));
    reply('throw');await click(button('[data-add-cancel-retry]'));
    assert.match(html(),/Couldn't cancel sign-in/);
    reply(200,{cancelled:true});await click(button('[data-add-cancel-retry]'));
    assert.equal(dialog.open,false);assert.deepEqual(calls.at(-1).body,{session:'s2-retained'});
    reply(200,{session:'s2-next',flow:'device',url:'https://auth.x.ai/device'});
    c.openAdd('xai',null,opener);await flush();assert(button('[data-add-open]'));
    reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));
  }
  await retryStrandedCancellation();

  // A status request can finish on the server just before Cancel acquires the session lock.
  async function terminalCancellationRace() {
    for(const state of ['ok','error','finishing']) {
      reset();reply(200,{session:'cancel-race-'+state,flow:'browser',url:'https://claude.ai/oauth/authorize'});
      c.openAdd('claude','claude-1.json',opener);await flush();
      const pending={};reply('hold',new Promise(resolve=>pending.resolve=resolve));await advance(2000);
      reply(200,{cancelled:false,state});await click(button('[data-dialog-cancel]'));
      pending.resolve({status:200,ok:true,json:async()=>({state:'ok',account:{key:'stale.json'}})});await flush();
      assert.equal(loads,0,'the invalidated in-flight poll cannot finish cancellation');
      assert.equal(button('[data-add-cancel-retry]'),null,'a terminal cancel response is not a cancellation failure');
      if(state==='error') {
        assert.match(html(),/Sign-in failed/);assert.equal(button('[data-add-restart]').textContent,'Try again');
        await click(button('[data-dialog-cancel]'));continue;
      }
      assert.match(button('[data-add-status]').textContent,/Finishing sign-in/);
      if(state==='finishing') {
        reply(200,{state:'finishing'});await advance(2000);
        assert.match(button('[data-add-status]').textContent,/Finishing sign-in/);
      }
      const originalLoad=c.load, inventory={};
      c.load=async()=>{loads++;await new Promise(resolve=>inventory.resolve=resolve);};
      reply(200,{state:'ok',account:{key:'claude-1.json',label:'b@example.test',replaced:true}});await advance(2000);
      assert.equal(loads,1);assert.doesNotMatch(html(),/account updated/,'success waits for fresh inventory');
      inventory.resolve();await flush();c.load=originalLoad;
      assert.match(html(),/Claude account updated/);assert.match(html(),/b@example.test/);
      await click(button('[data-add-done]'));assert.equal(timers.length,0);
    }
  }
  await terminalCancellationRace();

  // S3: Continue remains cancellable; a server that is saving keeps the dialog and status polling alive.
  async function cancelBrowserFinishing() {
    const start=async session=>{
      reset();reply(200,{session,flow:'browser',url:'https://claude.ai/oauth/authorize'});
      c.openAdd('claude',null,opener);await flush();
      button('#add-callback').value='http://localhost:54545/callback?code=c&state=abc';dialog.dispatch('input',{target:button('#add-callback')});
      reply(200,{accepted:true});await click(button('[data-add-continue]'));
    };
    await start('s3-finishing');assert.equal(button('[data-dialog-cancel]').disabled,false);
    reply(200,{cancelled:false,state:'finishing'});windowEvents.pagehide();await flush();
    assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/cancel',method:'POST',body:{session:'s3-finishing'}});
    reply(200,{cancelled:false,state:'finishing'});await click(button('[data-dialog-cancel]'));
    assert.equal(dialog.open,true);assert.match(button('[data-add-status]').textContent,/Finishing sign-in…/);
    assert.doesNotMatch(html(),/cancelled/i);
    reply(200,{state:'finishing',message:'Finishing sign-in. Waiting for the gateway.'});await advance(2000);
    assert.match(button('[data-add-status]').textContent,/Finishing sign-in…/);
    assert.doesNotMatch(button('[data-add-status]').className,/warn/);
    reply(200,{state:'ok',account:{key:'claude-1.json',label:'b@example.test',replaced:true}});await advance(2000);
    assert.match(html(),/Claude account updated/);await click(button('[data-add-done]'));
    await start('s3-escape');reply(200,{cancelled:true});
    assert.equal(dialog.dispatch('cancel'),false,'Escape can cancel after Continue');
    dialog.close();dialog.dispatch('close');await flush();assert.equal(dialog.open,false);
    assert.deepEqual(calls.at(-1).body,{session:'s3-escape'});
  }
  await cancelBrowserFinishing();

  // S4: focus returns to a replacement opener by stable id, with Add account as the removed-card fallback.
  async function restoreReplacedOpener() {
    reset();c.renderAccountEntry(c.fixture);
    const original=mount(new Node('button',{id:'menu-xai-focus.json'}));
    c.openAdd(null,null,original);unmount(original);
    assert.equal(original.isConnected,false);
    const replacement=mount(new Node('button',{id:original.id}));
    await click(button('[data-dialog-cancel]'));
    assert.equal(document.activeElement,replacement,'periodic card replacement preserves the opener identity');
    c.openAdd(null,null,replacement);unmount(replacement);
    await click(button('[data-dialog-cancel]'));
    assert.equal(document.activeElement,document.getElementById('add-account'),'a removed account falls back to Add account');
    // Replacing dialog markup must detach its old controls too.
    c.openAdd(null,null,opener);const oldControl=button('[data-dialog-cancel]');c.renderAccountDialog();
    assert.equal(oldControl.isConnected,false);await click(button('[data-dialog-cancel]'));
  }
  await restoreReplacedOpener();

  // Card menu: Sign in again and Disconnect; over Tailscale Disconnect is shown disabled with its reason.
  reset();vm.runInContext("current.access_transport='tailscale'",c);
  const menuButton=new Node('button',{id:'menu-xai-xai-0.json','data-menu-provider':'xai','data-menu-key':'xai-0.json'});
  mount(menuButton);
  menuButton.getBoundingClientRect=()=>({top:100,bottom:128,left:300,right:326});
  c.openCardMenu(menuButton);
  assert.equal(menuButton.getAttribute('aria-expanded'),'true');assert.equal(menu.hidden,false);
  assert.equal(menu.style.left,'118px');assert.equal(menu.style.top,'134px');
  const items=menu.querySelectorAll('[role=menuitem]');
  assert.deepEqual(items.map(i=>[i.textContent,i.getAttribute('aria-disabled')]),[['Sign in again',null],['Disconnect…Desktop or SSH only','true']]);
  assert.equal(document.activeElement,items[0]);
  menu.dispatch('keydown',{key:'ArrowDown'});assert.equal(document.activeElement,items[1]);
  menu.dispatch('click',{target:items[1]});assert.equal(dialog.open,false,'a disabled item does nothing');
  menu.dispatch('keydown',{key:'Escape'});assert.equal(menu.hidden,true);
  // Sign in again from the menu goes straight to that provider's sign-in, over Tailscale too.
  c.openCardMenu(menuButton);
  reply(200,{session:'s-menu',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'MMMM-NNNN',expires_at:expires(15)});
  menu.dispatch('click',{target:menu.querySelector('[data-menu-signin]')});await flush();
  assert.equal(menu.hidden,true);assert.match(html(),/Sign in to Grok again/);
  reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));
  c.openDisconnect('xai','xai-0.json',menuButton);assert.equal(dialog.open,false,'disconnect never opens over Tailscale');

  // Disconnect: the last ready account warns but is allowed; Cancel takes focus; a failure offers Try again.
  reset();
  c.openDisconnect('xai','xai-0.json',menuButton);
  assert.match(html(),/Disconnect Grok account from the gateway\?/);
  assert.match(html(),/Requests stop using it\. Your Grok subscription and other sign-ins stay as they are\./);
  assert.match(html(),/It's your only ready Grok account\. Grok requests will fail until you add another\./);
  assert.equal(document.activeElement,button('[data-dialog-cancel]'));
  reply('throw');await click(button('[data-disconnect]'));
  assert.deepEqual(calls[0],{url:'/api/accounts/disconnect',method:'POST',body:{key:'xai-0.json',auth_index:'xai-0',confirmed:true}});
  assert.match(html(),/Couldn't disconnect · gateway unreachable/);assert.equal(button('[data-disconnect]').textContent,'Try again');
  const working={};reply('hold',new Promise(r=>working.resolve=r));
  await click(button('[data-disconnect]'));
  assert.equal(button('[data-disconnect]').textContent,'Disconnecting…');assert.equal(button('[data-dialog-cancel]').disabled,true);
  assert.equal(dialog.dispatch('cancel'),true,'Escape is blocked while disconnecting');
  working.resolve({status:200,ok:true,json:async()=>({disconnected:true,key:'xai-0.json',last_ready:true})});await flush();
  assert.equal(dialog.open,false);assert.equal(document.getElementById('account-announce').textContent,'Grok account disconnected');
  await advance(360);assert.equal(loads,1);
  // Another ready account of the provider: no warning.
  c.openDisconnect('claude','claude-0.json',menuButton);
  assert.match(html(),/Disconnect a@example.test from the gateway\?/);assert.doesNotMatch(html(),/only ready/);
  await click(button('[data-dialog-cancel]'));

  // Use for routing on other providers: the switch follows routing_enabled and posts the account's key.
  reset();c.fixture.providers[1].accounts.push(grok(1));
  const section=()=>c.genericCard(c.fixture.providers[1],c.fixture.providers[1].accounts[0],c.fixture.gateway);
  assert.match(section(),/<input id="generic-route-xai-xai-0.json" data-generic-route="xai:xai-0.json" type="checkbox" role="switch" checked/);
  assert.match(section(),/id="menu-xai-xai-0.json"/);
  reply(200,{key:'xai-0.json',enabled:false});
  await c.saveGenericRouting('xai:xai-0.json',false);
  assert.deepEqual(calls[0],{url:'/api/accounts/routing',method:'POST',body:{key:'xai-0.json',auth_index:'xai-0',enabled:false}});
  assert.equal(c.fixture.providers[1].accounts[0].routing_enabled,false);assert.equal(loads,1);assert.equal(renders>=2,true);
  // Turning off the only ready account asks first; Turn off confirms.
  reset();
  await c.saveGenericRouting('xai:xai-0.json',false);
  assert.equal(calls.length,0);assert.match(section(),/No other Grok account is ready\./);
  reply(200,{key:'xai-0.json',enabled:false});
  await c.saveGenericRouting('xai:xai-0.json',false,true);
  assert.equal(calls.length,1);assert.doesNotMatch(section(),/No other Grok account is ready/);
  // An unconfirmed change says so; read-only and offline views lock the switch; sign-in required offers Sign in.
  reset();reply(500,{});await c.saveGenericRouting('xai:xai-0.json',true);
  assert.match(section(),/Change not confirmed\. Try again\./);
  vm.runInContext('current.read_only=true',c);
  assert.match(section(),/role="switch" checked disabled  title="Change routing from your computer dashboard"/);
  reset();c.fixture.providers[1].accounts[0].availability={state:'auth_required',scope:'account'};
  assert.match(section(),/Sign in required<\/span><button type="button" class="resets-button" id="signin-xai-xai-0.json" data-add-provider="xai" data-add-key="xai-0.json"/);
  assert.match(section(),/role="switch"  disabled  title="Sign in to use for routing"/);
  console.log('Account actions UI: S1–S4 regressions; add (code, browser, invalid address, busy, expiry, cancel on close), card menu, disconnect and generic routing passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
