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
  get value(){return this.attrs.value ?? (this.tag==='textarea' ? this.textContent : '');} set value(v){this.attrs.value=v;}
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
  insertBefore(node,before){if(node===before)return node;if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(c=>c!==node);const at=this.children.indexOf(before);this.children.splice(at<0?this.children.length:at,0,node);node.parentElement=this;return node;}
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
vm.runInContext(source.slice(source.indexOf("window.addEventListener('pagehide'"),source.indexOf("$('theme').addEventListener")),c);
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
const grok=(i,extra={})=>({key:`xai-${i}.json`,auth_index:`xai-${i}`,provider:'xai',label:`xai-${i}.json`,confirm_text:'Grok',status:'active',disabled:false,active:true,
  availability:ready,routing_enabled:true,today:{requests:0,hours:[]},...extra});
const claude=(i,email)=>({key:`claude-${i}.json`,auth_index:`claude-${i}`,provider:'claude',email,label:email,confirm_text:email,status:'active',disabled:false,active:true,availability:ready});
const catalog=[{id:'claude',name:'Claude',login:'browser',callback_example:'http://localhost:54545/callback?code=…&state=…'},
  {id:'xai',name:'Grok',login:'device'},{id:'kimi',name:'Kimi Code (China)',login:'device'},{id:'kimi-ai',name:'Kimi Code (Global)',login:'device'}];
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
  // The top-bar entry follows the catalog and hides account mutations in read-only views.
  reset();
  c.renderAccountEntry(c.fixture);
  assert.equal(document.getElementById('add-account').hidden,false);assert.equal(document.getElementById('add-account').disabled,false);
  c.renderAccountEntry({...c.fixture,read_only:true});assert.equal(document.getElementById('add-account').disabled,true);
  assert.equal(document.getElementById('add-account').hidden,true);
  c.renderAccountEntry({...c.fixture,add_providers:undefined});assert.equal(document.getElementById('add-account').hidden,true);
  assert.equal(vm.runInContext("cardMenuButton('xai',{key:'xai-0.json'},'Grok account')",c).includes('aria-label="Account actions · Grok account"'),true);
  vm.runInContext('current={...fixture,add_providers:undefined}',c);
  assert.equal(c.cardMenuButton('xai',{key:'xai-0.json'},'Grok account'),'');

  // Picker: catalog order, Code or Browser, and recognisable Kimi regions. The first option takes focus.
  reset();
  c.openAdd(null,null,opener);
  assert.equal(dialog.open,true);
  const options=dialog.querySelectorAll('.provider-option');
  assert.deepEqual(options.map(o=>[o.children[1].textContent,o.querySelector('small').textContent]),[['Claude','Browser'],['Grok','Code'],['Kimi Code (China)','Code'],['Kimi Code (Global)','Code']]);
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
  reply(200,{session:'s-claude',provider:'claude',flow:'browser',auto_callback:false,url:'https://claude.ai/oauth/authorize?state=abc',expires_at:expires(10)});
  c.openAdd('claude','claude-1.json',opener);await flush();
  assert.equal(button('[data-add-manual]'),null,'without an automatic callback the paste step shows at once');
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

  // Tailscale is writable; explicit read-only views hide all account mutations.
  reset();vm.runInContext("current.access_transport='tailscale';current.read_only=true",c);
  c.renderAccountEntry(c.fixture);assert.equal(document.getElementById('add-account').hidden,true);
  c.openAdd(null,null,opener);assert.equal(dialog.open,false);
  c.openDisconnect('xai','xai-0.json',opener);assert.equal(dialog.open,false);
  assert.equal(c.cardMenuButton('xai',c.fixture.providers[1].accounts[0],'Grok'),'');
  assert.equal(calls.length,0);
  vm.runInContext('current.read_only=false',c);c.renderAccountEntry(c.fixture);
  assert.equal(document.getElementById('add-account').hidden,false);
  const menuButton=new Node('button',{id:'menu-xai-xai-0.json','data-menu-provider':'xai','data-menu-key':'xai-0.json'});
  mount(menuButton);
  menuButton.getBoundingClientRect=()=>({top:100,bottom:128,left:300,right:326});
  c.openCardMenu(menuButton);
  assert.equal(menuButton.getAttribute('aria-expanded'),'true');assert.equal(menu.hidden,false);
  assert.equal(menu.style.left,'118px');assert.equal(menu.style.top,'134px');
  const items=menu.querySelectorAll('[role=menuitem]');
  assert.deepEqual(items.map(i=>[i.textContent,i.getAttribute('aria-disabled')]),[['Sign in again',null],['Disconnect…',null]]);
  assert.equal(document.activeElement,items[0]);
  menu.dispatch('keydown',{key:'ArrowDown'});assert.equal(document.activeElement,items[1]);
  menu.dispatch('keydown',{key:'Escape'});assert.equal(menu.hidden,true);
  // A single-card menu in Customize contains only its widget action; view mode keeps account actions.
  const splitFrame=mount(new Node('section',{class:'w-account-card','data-wid':'split-test'}));splitFrame.insertBefore(menuButton,null);
  c.window.RouterBoards={customizing:true};c.openCardMenu(menuButton);
  assert.deepEqual(menu.querySelectorAll('[role=menuitem]').map(n=>n.textContent),['Customize this widget']);
  assert.equal(menu.querySelectorAll('hr').length,0);assert.doesNotMatch(menu.textContent,/No other cards/);c.closeCardMenu();
  c.window.RouterBoards.customizing=false;c.openCardMenu(menuButton);
  assert(menu.querySelector('[data-menu-disconnect]'));assert(menu.querySelector('hr'));assert(menu.querySelector('[data-menu-widget]'));c.closeCardMenu();
  delete c.window.RouterBoards;document.body.insertBefore(menuButton,null);unmount(splitFrame);
  c.openCardMenu(menuButton);menu.dispatch('click',{target:menu.querySelector('[data-menu-disconnect]')});
  assert.equal(menu.hidden,true);assert.match(html(),/Disconnect Grok account from the gateway\?/,'disconnect opens from any connection');
  await click(button('[data-dialog-cancel]'));assert.equal(dialog.open,false);
  // A provider the catalog can't sign in to keeps the item, disabled, with its reason; a disabled item does nothing.
  c.fixture.providers.push({id:'meta',name:'Meta',routable:false,accounts:[{...grok(0),key:'meta-0.json',provider:'meta'}]});
  const metaButton=mount(new Node('button',{id:'menu-meta-meta-0.json','data-menu-provider':'meta','data-menu-key':'meta-0.json'}));
  metaButton.getBoundingClientRect=menuButton.getBoundingClientRect;c.openCardMenu(metaButton);
  assert.deepEqual(menu.querySelectorAll('[role=menuitem]').map(i=>[i.textContent,i.getAttribute('aria-disabled')]),[['Sign in againNot available for this provider','true'],['Disconnect…',null]]);
  menu.dispatch('click',{target:menu.querySelector('[data-menu-signin]')});assert.equal(dialog.open,false,'a disabled item does nothing');
  menu.dispatch('keydown',{key:'Escape'});unmount(metaButton);
  // Sign in again from the menu goes straight to that provider's sign-in, over Tailscale too.
  c.openCardMenu(menuButton);
  reply(200,{session:'s-menu',provider:'xai',flow:'device',url:'https://auth.x.ai/device',user_code:'MMMM-NNNN',expires_at:expires(15)});
  menu.dispatch('click',{target:menu.querySelector('[data-menu-signin]')});await flush();
  assert.equal(menu.hidden,true);assert.match(html(),/Sign in to Grok again/);
  reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));

  // Disconnect: the last ready account warns but is allowed. The server's confirmation must be typed
  // first (any case, outer spaces ignored) and is sent along; a failure offers Try again and a mismatch marks the field.
  reset();
  c.openDisconnect('xai','xai-0.json',menuButton);
  assert.match(html(),/Disconnect Grok account from the gateway\?/);
  assert.match(html(),/Requests stop using it\. Your Grok subscription and other sign-ins stay as they are\./);
  assert.match(html(),/It's your only ready Grok account\. Grok requests will fail until you add another\./);
  assert.equal(button('.confirm-step b').textContent,'Grok');
  const confirmInput=()=>button('#disconnect-confirm'), typeConfirm=value=>{confirmInput().value=value;dialog.dispatch('input',{target:confirmInput()});};
  assert.equal(document.activeElement,confirmInput());assert.equal(confirmInput().getAttribute('autocomplete'),'off');
  assert.equal(button('[data-disconnect]').disabled,true);
  typeConfirm('xai-0');assert.equal(button('[data-disconnect]').disabled,true);
  await click(button('[data-disconnect]'));dialog.dispatch('keydown',{key:'Enter',target:confirmInput()});await flush();
  assert.equal(calls.length,0,'nothing is sent until the text matches');
  typeConfirm('  GROK ');assert.equal(button('[data-disconnect]').disabled,false);
  reply('throw');await click(button('[data-disconnect]'));
  assert.deepEqual(calls[0],{url:'/api/accounts/disconnect',method:'POST',body:{key:'xai-0.json',auth_index:'xai-0',confirmed:true,confirm_text:'GROK'}});
  assert.match(html(),/Couldn't disconnect · gateway unreachable/);assert.equal(button('[data-disconnect]').textContent,'Try again');
  assert.equal(confirmInput().value,'  GROK ','the typed text survives the re-render');assert.equal(document.activeElement,button('[data-disconnect]'));
  reply(400,{error:'confirm_mismatch'});dialog.dispatch('keydown',{key:'Enter',target:confirmInput()});await flush();
  assert.equal(calls.at(-1).body.confirm_text,'GROK','Enter sends once it matches');
  assert.equal(button('#disconnect-confirm-error').textContent,"That doesn't match.");assert.equal(button('#disconnect-confirm-error').hidden,false);
  assert.equal(confirmInput().getAttribute('aria-invalid'),'true');assert.equal(document.activeElement,confirmInput());
  assert.equal(button('[data-disconnect]').textContent,'Disconnect');
  typeConfirm('grok');assert.equal(button('#disconnect-confirm-error').hidden,true,'editing clears the mismatch');
  assert.equal(confirmInput().hasAttribute('aria-invalid'),false);
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
  assert.equal(button('.confirm-step b').textContent,'a@example.test');
  await click(button('[data-dialog-cancel]'));
  // An explicit read-only view blocks direct calls too; writable Tailscale can disconnect.
  vm.runInContext("current.access_transport='tailscale';current.read_only=true",c);
  const beforeReadonly=calls.length;
  c.openDisconnect('claude','claude-1.json',menuButton);assert.equal(dialog.open,false);
  c.openCardMenu(menuButton);assert.equal(menu.hidden,true);
  assert.equal(calls.length,beforeReadonly);
  vm.runInContext('current.read_only=false',c);
  c.openDisconnect('claude','claude-1.json',menuButton);assert.equal(dialog.open,true);
  confirmInput().value='B@Example.test';dialog.dispatch('input',{target:confirmInput()});
  reply(200,{disconnected:true,key:'claude-1.json'});await click(button('[data-disconnect]'));
  assert.deepEqual(calls.at(-1).body,{key:'claude-1.json',auth_index:'claude-1',confirmed:true,confirm_text:'B@Example.test'});
  assert.equal(dialog.open,false);await advance(360);

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

  // Automatic callback: the dialog waits by itself; pasting the address is a fallback one link away.
  reset();
  reply(200,{session:'s-auto',provider:'claude',flow:'browser',auto_callback:true,url:'https://claude.ai/oauth/authorize?state=abc',expires_at:expires(10)});
  c.openAdd('claude',null,opener);await flush();
  assert.match(html(),/Add Claude account/);assert.equal(button('#add-callback'),null,'no paste field until asked for');
  assert.equal(document.activeElement,button('[data-add-open]'));assert.equal(button('[data-add-open]').textContent,'Open sign-in page ');
  assert.match(button('[data-add-status]').textContent,/Waiting for approval · 10 min left/);
  assert.equal(button('[data-add-manual]').textContent,"Page didn't load after approving?");
  await click(button('[data-add-open]'));await advance(0);
  assert.doesNotMatch(button('[data-add-open]').className,/secondary/,'opening keeps the dialog as it is');assert.equal(button('#add-callback'),null);
  reply(200,{state:'waiting'});await advance(2000);
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/status?session=s-auto',method:'GET',body:undefined});
  reply(200,{state:'finishing'});await advance(2000);
  assert.match(button('[data-add-status]').textContent,/Finishing sign-in…/);assert.equal(button('[data-add-manual]'),null,'nothing to paste while finishing');
  onLoad=()=>{c.fixture.providers[0].accounts.push(claude(2,'c@example.test'));};
  reply(200,{state:'ok',account:{key:'claude-2.json',provider:'claude',label:'c@example.test',replaced:false}});await advance(2000);
  assert.match(html(),/Claude account added/);assert.match(html(),/c@example.test/);assert.equal(calls.filter(x=>x.url.endsWith('/callback')).length,0);
  await click(button('[data-add-done]'));assert.equal(dialog.open,false);
  // The fallback still posts the pasted address, and stays open when the sign-in starts again.
  reset();
  reply(200,{session:'s-auto2',flow:'browser',auto_callback:true,url:'https://claude.ai/oauth/authorize',expires_at:expires(10)});
  c.openAdd('claude',null,opener);await flush();
  await click(button('[data-add-manual]'));
  assert.equal(button('[data-add-manual]'),null);assert.equal(document.activeElement,button('#add-callback'));
  assert.equal(button('.paste-step label').textContent,"After approving, copy the address of the page that can't load and paste it here.");
  assert.equal(button('[data-add-continue]').disabled,true);
  button('#add-callback').value='http://localhost:54545/callback?code=c&state=abc';dialog.dispatch('input',{target:button('#add-callback')});
  reply(200,{accepted:true});await click(button('[data-add-continue]'));
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/add/callback',method:'POST',body:{session:'s-auto2',redirect_url:'http://localhost:54545/callback?code=c&state=abc'}});
  reply(200,{state:'expired'});await advance(2000);
  assert.equal(button('[data-add-restart]').textContent,'Start again');
  reply(200,{session:'s-auto3',flow:'browser',auto_callback:true,url:'https://claude.ai/oauth/authorize',expires_at:expires(10)});
  await click(button('[data-add-restart]'));
  assert(button('#add-callback'),'the chosen fallback stays open');assert.equal(button('#add-callback').value,'');
  reply(200,{cancelled:true});await click(button('[data-dialog-cancel]'));assert.deepEqual(calls.at(-1).body,{session:'s-auto3'});

  // API keys: their own group in the picker; picking one opens a form built from its fields, without a sign-in session.
  const keyCatalog=[...catalog.map(x=>({...x,kind:'oauth'})),
    {id:'claude-api-key',name:'Anthropic API key',kind:'api_key',fields:[{id:'api_key',label:'API key',required:true,secret:true,placeholder:'sk-ant-…'}]},
    {id:'openai-compatibility',name:'Other OpenAI-compatible API',kind:'api_key',fields:[{id:'name',label:'Name',required:true,secret:false},
      {id:'base_url',label:'Base URL',required:true,secret:false,placeholder:'https://…/v1'},{id:'api_key',label:'API key',required:true,secret:true},
      {id:'models',label:'Models',required:true,secret:false}]}];
  reset();c.fixture.add_providers=keyCatalog;
  c.openAdd(null,null,opener);
  assert.deepEqual(dialog.querySelectorAll('.provider-group').map(g=>g.textContent),['Sign in','API keys']);
  const lists=dialog.querySelectorAll('.provider-list'), keyOptions=lists[1].querySelectorAll('.provider-option');
  assert.equal(lists[0].querySelectorAll('.provider-option').length,4);assert.equal(lists[1].getAttribute('aria-labelledby'),'add-group-key');
  assert.deepEqual(keyOptions.map(o=>[o.children[1].textContent,o.getAttribute('aria-label'),o.children[0].className,o.querySelector('small')]),
    [['Anthropic','Anthropic API key','provider-icon claude-mark',null],['Other OpenAI-compatible API',null,'provider-icon compat-mark',null]],
    'under the API keys heading a key option reads as its provider');
  assert.equal(document.activeElement,lists[0].querySelector('.provider-option'));
  await click(keyOptions[1]);
  assert.equal(calls.length,0,'an API key needs no sign-in session');
  assert.match(html(),/Add OpenAI-compatible API/);
  const field=id=>button(`#key-field-${id}`), submit=()=>button('[data-key-submit]');
  const fill=(id,value)=>{field(id).value=value;dialog.dispatch('input',{target:field(id)});};
  assert.deepEqual(dialog.querySelectorAll('.key-field label').map(l=>l.textContent),['Name','Base URL','API key','Models']);
  assert.deepEqual(['type','autocomplete','spellcheck'].map(k=>field('api_key').getAttribute(k)),['password','off','false']);
  assert.equal(field('base_url').getAttribute('type'),'url');assert.equal(field('base_url').getAttribute('placeholder'),'https://…/v1');
  assert.equal(field('models').tag,'textarea','models take one per line or comma-separated');
  assert.equal(document.activeElement,field('name'));assert.equal(submit().disabled,true);
  fill('name','openrouter');fill('base_url','https://openrouter.ai/api/v1');
  assert.equal(submit().disabled,true,'every required field first');
  fill('api_key','sk-or-secret-1');assert.equal(submit().disabled,true,'models are required for compatible accounts');
  fill('models','model-a');assert.equal(submit().disabled,false);
  // A rejected field is marked; the key is cleared as soon as it is sent and never kept.
  reply(400,{error:'invalid_field',field:'base_url'});
  dialog.dispatch('keydown',{key:'Enter',target:field('api_key')});await flush();
  assert.deepEqual(calls[0],{url:'/api/accounts/api-keys/add',method:'POST',body:{provider:'openai-compatibility',fields:{name:'openrouter',base_url:'https://openrouter.ai/api/v1',api_key:'sk-or-secret-1',models:'model-a'}}});
  assert.equal(field('api_key').value,'');assert.equal(field('name').value,'openrouter');assert.equal(field('base_url').value,'https://openrouter.ai/api/v1');
  assert.equal(button('#key-field-base_url-error').textContent,'Check this and try again.');assert.equal(button('#key-field-base_url-error').hidden,false);
  assert.equal(field('base_url').getAttribute('aria-invalid'),'true');assert.equal(document.activeElement,field('base_url'));
  assert.doesNotMatch(JSON.stringify(vm.runInContext('accountFlow',c)),/sk-or-secret/,'no key in the dialog state');
  fill('base_url','https://openrouter.ai/api/v1/');assert.equal(button('#key-field-base_url-error').hidden,true,'editing clears the error');
  assert.equal(submit().disabled,true,'the key is entered again');
  // A duplicate says so above the button.
  fill('api_key','sk-or-secret-1');fill('models','gpt-4o, claude-sonnet\nllama');
  reply(409,{error:'duplicate'});await click(submit());
  assert.equal(calls.at(-1).body.fields.models,'gpt-4o, claude-sonnet\nllama');
  assert.match(html(),/This key is already connected\./);assert.equal(field('api_key').value,'');
  assert.equal(field('models').textContent,'gpt-4o, claude-sonnet\nllama');assert.equal(document.activeElement,field('api_key'));
  // Success waits for the new inventory, names the account and closes by itself; Escape waits for the answer.
  fill('api_key','sk-or-secret-2');
  onLoad=()=>{c.fixture.providers.push({id:'openai-compatibility',name:'OpenRouter',routable:false,accounts:[{key:'compat-openrouter',kind:'api_key',provider:'openai-compatibility',label:'openrouter',availability:ready,status:'active',active:true}]});};
  const adding={};reply('hold',new Promise(r=>adding.resolve=r));
  await click(submit());
  assert.equal(submit().textContent,'Adding…');assert.equal(button('[data-dialog-cancel]').disabled,true);assert.equal(field('api_key').value,'');
  assert.equal(dialog.dispatch('cancel'),true,'Escape is blocked while adding');
  adding.resolve({status:200,ok:true,json:async()=>({key:'compat-openrouter',provider:'openai-compatibility',label:'openrouter'})});await flush();
  assert.equal(loads,1);assert.match(html(),/OpenAI-compatible API added/);assert.match(html(),/openrouter/);assert.match(html(),/Ready to route/);
  assert.equal(document.activeElement,button('[data-add-done]'));
  await advance(3200);assert.equal(dialog.open,false,'the result closes by itself');assert.equal(document.activeElement,opener);
  // A single-field key: focus starts there; a rejected key and an unreachable gateway each explain themselves.
  reset();c.fixture.add_providers=keyCatalog;c.openAdd(null,null,opener);
  await click(dialog.querySelectorAll('.provider-list')[1].querySelector('.provider-option'));
  assert.match(html(),/Add Anthropic API key/);assert.equal(document.activeElement,field('api_key'));
  assert.equal(field('api_key').getAttribute('placeholder'),'sk-ant-…');
  fill('api_key','sk-ant-bad');reply(400,{error:'invalid_field',field:'api_key'});await click(submit());
  assert.equal(button('#key-field-api_key-error').textContent,"This key wasn't accepted.");assert.equal(field('api_key').value,'');
  fill('api_key','sk-ant-good');reply('throw');await click(submit());
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/api-keys/add',method:'POST',body:{provider:'claude-api-key',fields:{api_key:'sk-ant-good'}}});
  assert.match(html(),/Couldn't add the key · gateway unreachable/);
  fill('api_key','sk-ant-good');reply(503,{error:'config_write_failed',message:"The gateway can't save its config."});await click(submit());
  assert.match(html(),/The gateway can't save its config\./);assert.equal(field('api_key').value,'');
  await click(button('[data-dialog-cancel]'));assert.equal(dialog.open,false);

  // Gateway versions: an untested one warns in the picker; an unsupported one explains the empty picker.
  reset();c.fixture.gateway_compat={version:'8.0.52',tested:false,supported:true};
  c.openAdd(null,null,opener);
  assert.match(html(),/Gateway 8\.0\.52 hasn't been tested with this dashboard\./);assert.equal(dialog.querySelectorAll('.provider-option').length,4);
  await click(button('[data-dialog-cancel]'));
  reset();c.fixture.gateway_compat={version:'8.0.47',tested:true,supported:true};
  c.openAdd(null,null,opener);assert.doesNotMatch(html(),/hasn't been tested|support gateway/);await click(button('[data-dialog-cancel]'));
  reset();c.fixture.gateway_compat={version:'9.1.0',tested:false,supported:false};c.fixture.add_providers=[];
  c.openAdd(null,null,opener);
  assert.match(html(),/This dashboard doesn't support gateway 9\.1\.0 yet, so adding accounts is off\./);
  assert.doesNotMatch(html(),/hasn't been tested|isn't available on this gateway version/);
  assert.equal(document.activeElement,button('[data-dialog-cancel]'));await click(button('[data-dialog-cancel]'));

  // API-key cards: requests today and the endpoint; never an OAuth sign-in; the menu offers Disconnect only.
  reset();
  const orKey={key:'openrouter',kind:'api_key',provider:'openai-compatibility',auth_index:'compat-index',label:'OpenRouter',confirm_text:'openrouter',base_url:'openrouter.ai',status:'active',active:true,disabled:false,
    availability:ready,today:{requests:3,success_rate:1,hours:[]},usage_refresh:{supported:false},models:{count:2,items:[{id:'openai/gpt-4o',display_name:'GPT-4o'},{id:'meta/llama'}]}};
  const orp={id:'openai-compatibility',name:'Other OpenAI-compatible API',routable:false,accounts:[orKey]};c.fixture.providers.push(orp);
  const keyCard=c.genericCard(orp,orKey,c.fixture.gateway);
  assert.match(keyCard,/<span class="small-badge">API key<\/span>/);
  assert.match(keyCard,/<span>Endpoint<\/span><strong title="openrouter.ai">openrouter.ai<\/strong>/);
  assert.match(keyCard,/<div class="quota-label">Requests today<\/div>/);assert.doesNotMatch(keyCard,/usage-refresh|Sign in/);
  const masked={...orKey,label:'API key ••••abcd',base_url:null,availability:{state:'auth_required',scope:'account'}};
  const maskedCard=c.genericCard(orp,masked,c.fixture.gateway);
  assert.doesNotMatch(maskedCard,/small-badge|Endpoint|data-add-provider|Sign in/,'no repeated badge, no endpoint, no OAuth sign-in');
  assert.match(maskedCard,/is-off/);assert.match(maskedCard,/<span>Key not accepted<\/span>/);
  const anthropicKey={key:'claude-key-abcd',kind:'api_key',provider:'claude-api-key',auth_index:'claude-api-index',label:'API key ••••abcd',confirm_text:'abcd',status:'active',active:true,disabled:false,
    availability:ready,today:{requests:0,hours:[]},models:{count:1,items:[{id:'claude-sonnet-4-5'}]}};
  c.fixture.providers.push({id:'claude-api-key',name:'Anthropic API key',routable:false,accounts:[anthropicKey]});
  const keyMenu=mount(new Node('button',{id:'menu-claude-api-key-claude-key-abcd','data-menu-provider':'claude-api-key','data-menu-key':'claude-key-abcd'}));
  keyMenu.getBoundingClientRect=()=>({top:100,bottom:128,left:300,right:326});c.openCardMenu(keyMenu);
  assert.deepEqual(menu.querySelectorAll('[role=menuitem]').map(i=>i.textContent),['Disconnect…']);assert.equal(menu.querySelector('hr'),null);
  menu.dispatch('click',{target:menu.querySelector('[data-menu-disconnect]')});
  assert.match(html(),/Disconnect API key ••••abcd from the gateway\?/);assert.match(html(),/Requests stop using it\. The key stays valid until you revoke it\./);
  assert.equal(button('.confirm-step b').textContent,'abcd','an API key confirms with its last four characters');
  assert.match(dialog.innerHTML,/Type the key's last 4 characters, <b>abcd<\/b>, to confirm/);
  confirmInput().value='  ABCD ';dialog.dispatch('input',{target:confirmInput()});assert.equal(button('[data-disconnect]').disabled,false);
  reply(200,{disconnected:true,key:anthropicKey.key});await click(button('[data-disconnect]'));
  assert.deepEqual(calls.at(-1),{url:'/api/accounts/disconnect',method:'POST',body:{key:anthropicKey.key,auth_index:'claude-api-index',confirmed:true,confirm_text:'ABCD'}},'the runtime auth_index is sent for API keys');
  assert.equal(dialog.open,false);unmount(keyMenu);
  // Existing API entries can share a stable key while their runtime indexes and settings differ.
  const keyProvider=c.fixture.providers.find(p=>p.id==='claude-api-key');
  const firstKey={...anthropicKey,auth_index:'first-index',models:{count:1,items:[{id:'first-model'}]}};
  const secondKey={...anthropicKey,auth_index:'second-index',disabled:true,active:false,availability:{state:'disabled',scope:'account'},models:{count:1,items:[{id:'second-model'}]}};
  keyProvider.accounts=[firstKey,secondKey];
  const sharedCards=[firstKey,secondKey].map(a=>mount(parse(c.genericCard(keyProvider,a,c.fixture.gateway)).children[0]));
  const sharedMenus=sharedCards.map(card=>card.querySelector('.card-menu'));
  assert.notEqual(sharedMenus[0].id,sharedMenus[1].id,'same-key cards have distinct menu IDs');
  for(const b of sharedMenus)b.getBoundingClientRect=()=>({top:100,bottom:128,left:300,right:326});
  c.openCardMenu(sharedMenus[0]);c.openCardMenu(sharedMenus[1]);
  assert.equal(menu.hidden,false,'opening the second same-key menu does not close the first as a toggle');
  assert.equal(sharedMenus[0].getAttribute('aria-expanded'),'false');
  assert.equal(sharedMenus[1].getAttribute('aria-expanded'),'true');
  menu.dispatch('click',{target:menu.querySelector('[data-menu-disconnect]')});
  assert.equal(dialog.open,true);assert.doesNotMatch(html(),/only ready/,'the dialog resolves the disabled second entry');
  confirmInput().value='abcd';dialog.dispatch('input',{target:confirmInput()});
  reply(200,{disconnected:true,key:anthropicKey.key});await click(button('[data-disconnect]'));
  assert.deepEqual(calls.at(-1).body,{key:anthropicKey.key,auth_index:'second-index',confirmed:true,confirm_text:'abcd'});
  for(const card of sharedCards)unmount(card);
  keyProvider.accounts=[anthropicKey];
  c.openDisconnect('openai-compatibility',orKey.key,opener);
  assert.equal(button('.confirm-step b').textContent,'openrouter');assert.doesNotMatch(html(),/last 4 characters/);
  confirmInput().value='OPENROUTER';dialog.dispatch('input',{target:confirmInput()});
  reply(200,{disconnected:true,key:orKey.key});await click(button('[data-disconnect]'));
  assert.deepEqual(calls.at(-1).body,{key:orKey.key,auth_index:'compat-index',confirmed:true,confirm_text:'OPENROUTER'});
  // Real backend groups stay separate from subscriptions. Requests use upstream provider IDs.
  const codexKey={key:'codex-key-wxyz',kind:'api_key',provider:'codex-api-key',auth_index:'codex-api-index',label:'API key ••••wxyz',status:'active',active:true,disabled:false,availability:ready,today:{requests:1,hours:[]}};
  c.fixture.providers.push({id:'codex-api-key',name:'OpenAI API key',routable:false,accounts:[codexKey]});
  c.fixture.usage={available:true,providers:{
    claude:{accounts:[{auth_index:'claude-0',requests:2,failures:0},{auth_index:'claude-api-index',requests:5,failures:1}],hours:[{auth_index:'claude-api-index',requests:5}]},
    codex:{accounts:[{auth_index:'codex-api-index',requests:1,failures:0}],hours:[{auth_index:'codex-api-index',requests:1}]},
    'openai-compatible-openrouter':{accounts:[{auth_index:'compat-index',requests:3,failures:0}],hours:[{auth_index:'compat-index',requests:3}]}
  }};
  c.renderProviders(c.fixture);
  assert.equal(document.getElementById('claude-api-key-today-requests').textContent,'5');
  assert.equal(document.getElementById('codex-api-key-today-requests').textContent,'1');
  assert.equal(document.getElementById('openai-compatibility-today-requests').textContent,'3');
  assert.equal(c.usageAccounts(c.fixture.usage,'claude').reduce((n,r)=>n+r.requests,0),2,'OAuth section excludes API-key usage');
  for(const [source,id,p,key] of [['claude','claude-api-index','claude-api-key',anthropicKey],['codex','codex-api-index','codex-api-key',codexKey],['openai-compatible-openrouter','compat-index','openai-compatibility',orKey]]) {
    assert.equal(c.providerAccount(source,id).key,key.key);
    assert.doesNotMatch(c.providerChip(source,id),/Removed account/);
    assert.match(c.providerChip(source,id),new RegExp(key.label));
    assert.equal(c.series(source,id),c.series(p,id,null,key.key),'request and card share one chart series');
  }
  assert.equal([...c.seriesEntries().values()].some(x=>x.name==='Removed account'),false,'no stray series');
  assert.equal(c.seriesEntries().size,c.fixture.providers.reduce((n,p)=>n+p.accounts.length,0));

  // Cards list no models, even when the account reports them.
  assert.doesNotMatch(c.genericCard(orp,orKey,c.fixture.gateway),/account-models|models-toggle|Models/);
  // Renewal date and plan live in the existing account menu and dialog. They are offered where the provider doesn't
  // report them (billing_editable), excluding API keys, and carry the current auth binding.
  reset();
  const menuItems=()=>menu.querySelectorAll('[role=menuitem]').map(i=>i.textContent);
  const openMenu=(provider,key,authIndex)=>{
    const b=mount(new Node('button',{id:vm.runInContext('menuId',c)(provider,key,authIndex),'data-menu-provider':provider,'data-menu-key':key,...(authIndex ? {'data-menu-auth-index':authIndex} : {})}));
    b.getBoundingClientRect=()=>({top:100,bottom:128,left:300,right:326});c.openCardMenu(b);return b;
  };
  const claude0=c.fixture.providers[0].accounts[0], grok0=c.fixture.providers[1].accounts[0];
  let m=openMenu('claude','claude-0.json');
  assert.deepEqual(menuItems(),['Sign in again','Disconnect…'],'no billing items without the flag');
  c.closeCardMenu();unmount(m);
  claude0.billing_editable={renewal:true,plan:false};
  m=openMenu('claude','claude-0.json');
  assert.deepEqual(menuItems(),['Set renewal date…','Sign in again','Disconnect…']);
  menu.dispatch('click',{target:menu.querySelector('[data-menu-billing]')});unmount(m);
  assert.equal(button('#account-dialog-title').textContent,'Claude renewal date','the title names the provider');
  assert.equal(button('.dialog-note').textContent,'a@example.test');assert.equal(button('label').textContent,'Next renewal');
  assert.equal(button('[data-billing-clear]'),null,'nothing to clear yet');
  let sent=calls.length;
  document.getElementById('billing-date').value='';await click(button('[data-billing-save]'));
  assert.equal(calls.length,sent);assert.match(html(),/Choose a date\./);assert.equal(button('#billing-date').getAttribute('aria-invalid'),'true');
  document.getElementById('billing-date').value='2026-11-01';
  reply(200,{ok:true});await click(button('[data-billing-save]'));
  assert.deepEqual(calls.at(-1).body,{key:'claude-0.json',auth_index:'claude-0',renewal_date:'2026-11-01'});
  assert.equal(calls.at(-1).url,'/api/accounts/billing');assert.equal(dialog.open,false);
  // Grok reports neither: both items, plan first as the badge comes before the dates on the card.
  Object.assign(grok0,{billing_editable:{renewal:true,plan:true},billing:{}});
  m=openMenu('xai','xai-0.json');
  assert.deepEqual(menuItems(),['Set plan…','Set renewal date…','Sign in again','Disconnect…']);
  menu.dispatch('click',{target:menu.querySelector('[data-menu-plan]')});unmount(m);
  assert.equal(button('#account-dialog-title').textContent,'Grok plan');assert.equal(button('label').textContent,'Plan');
  // Suggestions are in-page buttons under the field (no native datalist): one Tab stop, nothing chosen and no error yet.
  const planInput=()=>button('#billing-plan'), picks=()=>dialog.querySelectorAll('[data-plan-pick]'), pressed=()=>picks().filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>b.dataset.planPick);
  const planError=()=>button('#billing-plan-error');
  assert.equal(planInput().getAttribute('maxlength'),'40');assert.equal(planInput().hasAttribute('list'),false);assert.equal(button('datalist'),null);
  assert.equal(button('.plan-picks').getAttribute('role'),'group');
  assert.deepEqual(picks().map(b=>b.textContent),['Plus','Premium','Super','Ultra']);
  assert.deepEqual(pressed(),[]);assert.deepEqual(picks().map(b=>b.getAttribute('tabindex')),['0','-1','-1','-1']);
  assert.equal(planError().hidden,true,'no error before a Save');assert.equal(planInput().hasAttribute('aria-invalid'),false);
  assert.equal(button('[data-billing-clear]'),null);
  // Arrow keys move through the suggestions (ArrowDown from the field enters them); picking one fills the field.
  sent=calls.length;
  dialog.dispatch('keydown',{key:'ArrowDown',target:planInput()});assert.equal(document.activeElement,picks()[0]);
  dialog.dispatch('keydown',{key:'ArrowLeft',target:picks()[0]});assert.equal(document.activeElement,picks()[3],'arrows wrap');
  dialog.dispatch('keydown',{key:'Home',target:picks()[3]});assert.equal(document.activeElement,picks()[0]);
  dialog.dispatch('keydown',{key:'ArrowRight',target:picks()[0]});assert.equal(document.activeElement,picks()[1]);
  assert.deepEqual(picks().map(b=>b.tabIndex),[-1,0,-1,-1],'the arrow-focused suggestion becomes the Tab stop');
  await click(picks()[1]);
  assert.equal(planInput().value,'Premium');assert.deepEqual(pressed(),['Premium']);
  assert.deepEqual(picks().map(b=>b.getAttribute('tabindex')),['-1','0','-1','-1'],'the chosen suggestion is the Tab stop');
  assert.equal(calls.length,sent,'picking does not save');assert.equal(dialog.open,true);
  // Typing any other name is allowed and unchecks the suggestion; typing one checks it.
  planInput().value='Team plan';dialog.dispatch('input',{target:planInput()});assert.deepEqual(pressed(),[]);
  planInput().value='Ultra ';dialog.dispatch('input',{target:planInput()});assert.deepEqual(pressed(),['Ultra']);
  // The error shows only after Save with an invalid value, stays while still invalid, and goes once the value is valid.
  planInput().value='   ';await click(button('[data-billing-save]'));
  assert.equal(calls.length,sent);assert.match(html(),/Enter a plan name\./);
  assert.equal(planInput().getAttribute('aria-invalid'),'true');assert.equal(planError().hidden,false);assert.equal(document.activeElement,planInput());
  assert.deepEqual(pressed(),[]);
  planInput().value='  ';dialog.dispatch('input',{target:planInput()});assert.equal(planError().hidden,false);
  planInput().value='  S';dialog.dispatch('input',{target:planInput()});
  assert.equal(planError().hidden,true);assert.equal(planError().textContent,'');assert.equal(planInput().hasAttribute('aria-invalid'),false);
  planInput().value='';await click(button('[data-billing-save]'));assert.equal(planError().hidden,false);
  await click(picks()[0]);assert.equal(planError().hidden,true,'picking a suggestion clears it too');assert.deepEqual(pressed(),['Plus']);
  planInput().value='  SuperGrok ';
  dialog.dispatch('keydown',{key:'Enter',target:planInput(),isComposing:true});assert.equal(calls.length,sent,'Enter that ends an IME composition does not save');
  reply(200,{ok:true});dialog.dispatch('keydown',{key:'Enter',target:planInput()});await flush();
  assert.deepEqual(calls.at(-1).body,{key:'xai-0.json',auth_index:'xai-0',plan:'SuperGrok'},'Enter saves the trimmed name');assert.equal(dialog.open,false);
  // Once set, the dialog opens on the saved value, checked among the suggestions, and offers Clear; a failed clear keeps the dialog with its error.
  Object.assign(grok0,{plan:'Super',plan_source:'user',billing:{plan:'Super',renewal_date:'2026-11-21',confirmed:true,source:'user_confirmed'}});
  m=openMenu('xai','xai-0.json');menu.dispatch('click',{target:menu.querySelector('[data-menu-plan]')});unmount(m);
  assert.equal(planInput().value,'Super');assert.equal(button('[data-billing-clear]').textContent,'Clear plan');
  assert.deepEqual(pressed(),['Super']);assert.equal(planError().hidden,true);
  assert.match(button('.dialog-actions').className,/split/);
  reply(503,{error:'unavailable'});await click(button('[data-billing-clear]'));
  assert.deepEqual(calls.at(-1).body,{key:'xai-0.json',auth_index:'xai-0',plan:null});
  assert.equal(dialog.open,true);assert.match(html(),/Plan not cleared\. Try again\./);
  let release;reply('hold',new Promise(resolve=>{release=resolve;}));click(button('[data-billing-clear]'));await flush();
  assert.equal(button('[data-billing-clear]').textContent,'Clearing…');assert.equal(button('[data-billing-clear]').getAttribute('aria-busy'),'true');
  assert.equal(button('[data-billing-save]').disabled,true);assert.equal(button('[data-dialog-cancel]').disabled,true);
  assert.equal(picks().every(b=>b.disabled),true,'suggestions wait with the actions');
  release({status:200,ok:true,json:async()=>({ok:true})});await flush();assert.equal(dialog.open,false);
  m=openMenu('xai','xai-0.json');menu.dispatch('click',{target:menu.querySelector('[data-menu-billing]')});unmount(m);
  assert.equal(button('#account-dialog-title').textContent,'Grok renewal date');assert.equal(button('#billing-date').value,'2026-11-21');
  reply(409,{error:'account_changed'});await click(button('[data-billing-save]'));
  assert.deepEqual(calls.at(-1).body,{key:'xai-0.json',auth_index:'xai-0',renewal_date:'2026-11-21'});assert.match(html(),/Account changed\. Close and refresh\./);
  reply(400,{error:'invalid_request'});await click(button('[data-billing-save]'));assert.match(html(),/Date not saved\. Try again\./);
  assert.equal(button('.plan-picks'),null,'no suggestions for dates');
  // A date's error also goes once the date is valid.
  button('#billing-date').value='';await click(button('[data-billing-save]'));assert.equal(button('#billing-date-error').hidden,false);
  button('#billing-date').value='2026-11-21';dialog.dispatch('input',{target:button('#billing-date')});
  assert.equal(button('#billing-date-error').hidden,true);assert.equal(button('#billing-date').hasAttribute('aria-invalid'),false);
  reply(200,{ok:true});await click(button('[data-billing-clear]'));
  assert.deepEqual(calls.at(-1).body,{key:'xai-0.json',auth_index:'xai-0',renewal_date:null});assert.equal(dialog.open,false);
  // Suggestions only where the provider's plans are known (Kimi's second sign-in service shares them).
  assert.deepEqual([...c.billingDialogHtml({kind:'billing',field:'plan',provider:'kimi-ai',name:'k',value:'Allegretto'}).matchAll(/data-plan-pick="([^"]+)" aria-pressed="(\w+)"/g)].map(m=>m.slice(1)),
    [['Moderato','false'],['Allegretto','true'],['Vivace','false']]);
  assert.doesNotMatch(c.billingDialogHtml({kind:'billing',field:'plan',provider:'meta',name:'m',value:''}),/plan-pick|datalist|list=/);
  // Codex offers a manual date only without a live billing date. API keys have neither.
  const codex0={key:'codex-0.json',auth_index:'codex-0',provider:'codex',email:'c@example.test',label:'c@example.test',status:'active',billing_editable:{renewal:false,plan:false}};
  c.fixture.providers.push({id:'codex',name:'Codex',routable:false,accounts:[codex0]},
    {id:'xai-api-key',name:'xAI API key',routable:false,accounts:[{key:'xai-key',kind:'api_key',provider:'xai-api-key',auth_index:'xai-key-index',label:'API key ••••wxyz',billing_editable:{renewal:true,plan:true}}]});
  m=openMenu('codex','codex-0.json');
  assert.equal(menu.querySelector('[data-menu-billing]'),null);assert.equal(menu.querySelector('[data-menu-plan]'),null);c.closeCardMenu();unmount(m);
  codex0.billing_editable.renewal=true;
  m=openMenu('codex','codex-0.json');
  assert.equal(menuItems()[0],'Set renewal date…');
  assert.equal(menu.querySelector('[data-menu-plan]'),null);
  menu.dispatch('click',{target:menu.querySelector('[data-menu-billing]')});unmount(m);
  assert.equal(button('#account-dialog-title').textContent,'Codex renewal date');
  button('#billing-date').value='2026-12-01';
  reply(200,{ok:true});await click(button('[data-billing-save]'));
  assert.deepEqual(calls.at(-1).body,{key:'codex-0.json',auth_index:'codex-0',renewal_date:'2026-12-01'});
  assert.equal(dialog.open,false);
  codex0.billing={renewal_date:'2026-12-01',confirmed:true,source:'user_confirmed'};
  m=openMenu('codex','codex-0.json');menu.dispatch('click',{target:menu.querySelector('[data-menu-billing]')});unmount(m);
  assert.equal(button('#billing-date').value,'2026-12-01');
  reply(200,{ok:true});await click(button('[data-billing-clear]'));
  assert.deepEqual(calls.at(-1).body,{key:'codex-0.json',auth_index:'codex-0',renewal_date:null});
  assert.equal(dialog.open,false);
  m=openMenu('xai-api-key','xai-key','xai-key-index');assert.deepEqual(menuItems(),['Disconnect…']);c.closeCardMenu();unmount(m);
  console.log('Account actions UI: S1–S4 regressions; add (code, browser, automatic callback, paste fallback, invalid address, busy, expiry, cancel on close), API keys, gateway versions, card menu, typed disconnect, API-key cards without models, generic routing, and user-set renewal dates and plans passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
