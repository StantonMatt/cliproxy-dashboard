'use strict';
// Reordering providers and cards: the saved layout's order, where a dragged item lands, a drop's save, a cancel, a
// refresh during a drag, and the keyboard moves, run against a small DOM that lays its containers out in wrapped rows.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');

let documentRef;
const camel=name=>name.replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
class El {
  constructor(tag='div',attrs={}){this.tag=tag;this.attrs={};this.children=[];this.parentElement=null;this.text='';this.listeners={};this.animations=[];
    this.style={setProperty:(k,v)=>{this.style[k]=v;},getPropertyValue:k=>this.style[k] || ''};
    for(const [k,v] of Object.entries(attrs))this.setAttribute(k,v);
    this.classList={add:(...c)=>c.forEach(x=>this.toggleClass(x,true)),remove:(...c)=>c.forEach(x=>this.toggleClass(x,false)),toggle:(c,on)=>this.toggleClass(c,on),contains:c=>this.className.split(/\s+/).includes(c)};
    this.dataset=new Proxy({},{get:(_,k)=>this.attrs['data-'+String(k).replace(/[A-Z]/g,c=>'-'+c.toLowerCase())],
      set:(_,k,v)=>{this.attrs['data-'+String(k).replace(/[A-Z]/g,c=>'-'+c.toLowerCase())]=String(v);return true;}});}
  toggleClass(name,on){const has=this.classList.contains(name);on=on===undefined ? !has : on;const list=this.className.split(/\s+/).filter(c=>c && c!==name);if(on)list.push(name);this.attrs.class=list.join(' ');return on;}
  get className(){return this.attrs.class || '';} set className(v){this.attrs.class=v;}
  get id(){return this.attrs.id || '';}
  get hidden(){return Object.hasOwn(this.attrs,'hidden');} set hidden(v){if(v)this.attrs.hidden='';else delete this.attrs.hidden;}
  get tabIndex(){return Number(this.attrs.tabindex ?? -1);} set tabIndex(v){this.attrs.tabindex=String(v);}
  setAttribute(k,v){this.attrs[k]=String(v);} getAttribute(k){return this.attrs[k] ?? null;} removeAttribute(k){delete this.attrs[k];} hasAttribute(k){return Object.hasOwn(this.attrs,k);}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');} set textContent(v){this.text=String(v);this.children=[];}
  get innerHTML(){return this.markup || '';}
  set innerHTML(v){if(this.contains(documentRef?.activeElement))documentRef.activeElement=documentRef.body;for(const c of this.children)c.parentElement=null;this.markup=v;const root=parse(v);this.text=root.text;this.children=root.children;for(const c of this.children)c.parentElement=this;}
  get isConnected(){return this===documentRef.body || !!this.parentElement?.isConnected;}
  get nextElementSibling(){const s=this.parentElement?.children;return s ? s[s.indexOf(this)+1] || null : null;}
  insertBefore(node,before){node.remove();const at=before ? this.children.indexOf(before) : -1;this.children.splice(at<0 ? this.children.length : at,0,node);node.parentElement=this;return node;}
  after(node){const p=this.parentElement;p.insertBefore(node,this.nextElementSibling);}
  before(node){this.parentElement.insertBefore(node,this);}
  append(node){this.insertBefore(node,null);}
  prepend(node){this.insertBefore(node,this.children[0] || null);}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;}
  contains(node){return node===this || this.children.some(c=>c.contains(node));}
  addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);} removeEventListener(){}
  focus(){if(this.isConnected)documentRef.activeElement=this;}
  getAnimations(){return this.animations;} animate(frames,options){const a={frames,options,id:options?.id,cancel(){}};this.animations.push(a);return a;}
  setPointerCapture(){} releasePointerCapture(){}
  showPopover(){} hidePopover(){}
  // Layout: a container places its flowing children in rows of fixed-size boxes (box: left, top, width, item, gap).
  get flow(){return !this.hidden && !this.classList.contains('reorder-lifted');}
  get place(){
    if(this.parentElement?.layoutChild)return this.parentElement.layoutChild(this);
    const p=this.parentElement, box=p?.box;if(!box || !this.flow)return this.fixed || {x:0,y:0,w:0,h:0};
    const items=p.children.filter(c=>c.flow), i=items.indexOf(this), per=Math.max(1,Math.floor((box.width+box.gap)/(box.item+box.gap)));
    return {x:box.left+(i%per)*(box.item+box.gap),y:box.top+Math.floor(i/per)*(box.height+box.gap),w:box.item,h:box.height};
  }
  get offsetParent(){return documentRef.body;}
  get offsetLeft(){return this.box ? this.box.left : this.place.x;} get offsetTop(){return this.box ? this.box.top : this.place.y;}
  get offsetWidth(){return this.box ? this.box.width : this.place.w;} get offsetHeight(){return this.box ? 1000 : this.place.h;}
  get clientLeft(){return 0;} get clientTop(){return 0;}
  getBoundingClientRect(){let l=this.offsetLeft,t=this.offsetTop,w=this.offsetWidth,h=this.offsetHeight;
    if(this.classList.contains('reorder-lifted')||this.classList.contains('lifted')){const [x,y]=(this.style.translate || '0 0').split(' ').map(parseFloat);l=parseFloat(this.style.left)+x;t=parseFloat(this.style.top)+y;w=parseFloat(this.style.width);h=parseFloat(this.style.height);}
    return {left:l,top:t,right:l+w,bottom:t+h,width:w,height:h};}
  matches(selector){return selector.split(',').some(s=>matchChain(this,s.trim()));}
  closest(selector){for(let n=this;n;n=n.parentElement)if(n.matches(selector))return n;return null;}
  querySelectorAll(selector){const out=[];const walk=n=>{for(const c of n.children){if(c.matches(selector))out.push(c);walk(c);}};walk(this);return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0] || null;}
}
// Selectors: compounds of tag, #id, .class, [attr] and [attr="value"], :not([hidden]); joined by spaces or >.
function matchOne(el,compound){
  let rest=compound.replace(/:not\(\[hidden\]\)/,()=>{if(el.hidden)rest='\0';return '';});
  if(rest==='\0')return false;
  if(el.hidden && compound.includes(':not([hidden])'))return false;
  const tag=rest.match(/^[a-z][\w-]*/)?.[0];if(tag && el.tag!==tag)return false;
  for(const m of rest.matchAll(/([.#])([\w-]+)|\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g)){
    if(m[1]==='.' && !el.classList.contains(m[2]))return false;
    if(m[1]==='#' && el.id!==m[2])return false;
    if(m[3] && (!el.hasAttribute(m[3]) || m[4]!==undefined && el.getAttribute(m[3])!==m[4]))return false;
  }
  return true;
}
function matchChain(el,selector){
  const parts=selector.replace(/\s*>\s*/g,' > ').split(/\s+/);
  const test=(node,i)=>{
    if(!node || !matchOne(node,parts[i]))return false;
    if(i===0)return true;
    if(parts[i-1]==='>')return test(node.parentElement,i-2);
    for(let a=node.parentElement;a;a=a.parentElement)if(test(a,i-1))return true;
    return false;
  };
  return test(el,parts.length-1);
}
function parse(markup){const root=new El(),stack=[root];
  for(const token of markup.match(/<[^>]+>|[^<]+/g) || []){
    if(token.startsWith('</')){stack.pop();continue;}
    if(!token.startsWith('<')){stack.at(-1).text+=token;continue;}
    const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const attrs={};
    for(const [,k,v] of token.matchAll(/\s([\w-]+)(?:="([^"]*)")?/g))attrs[k]=(v ?? '').replace(/&quot;/g,'"').replace(/&amp;/g,'&');
    const node=new El(tag,attrs);node.parentElement=stack.at(-1);node.parentElement.children.push(node);
    if(!['input','hr','use','br','wbr'].includes(tag) && !token.endsWith('/>'))stack.push(node);
  }return root;}

const ids=new Map();
const document=documentRef={documentElement:{dataset:{}},body:new El('body'),activeElement:null,listeners:{},addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},removeEventListener(){},
  getElementById(id){const found=this.body.querySelector(`#${id}`);if(found)return found;if(!ids.has(id)){const n=new El('div',{id});n.box=null;ids.set(id,n);}return ids.get(id);},
  createElement:tag=>new El(tag),querySelector(s){return this.body.querySelector(s);},querySelectorAll(s){return this.body.querySelectorAll(s);}};
document.activeElement=document.body;
// The page: Codex, Claude and Grok on one row; Claude's three cards in their own row.
const gateways=new El('div',{id:'gateways',class:'gateways'});gateways.box={left:0,top:0,width:960,item:300,height:400,gap:20};
document.body.insertBefore(gateways,null);
function section(provider,name,keys,gridId){
  const s=new El('section',{class:'gateway','data-provider':provider});
  s.innerHTML=`<header class="gateway-head"><div class="pool-name"><span class="provider-icon"></span><div><div class="gateway-title"><h2>${name}</h2><a class="text-link" href="#">Billing</a></div></div></div></header>`;
  const grid=new El('div',{class:provider==='claude' ? 'watch-grid' : 'account-grid',...(gridId ? {id:gridId} : {})});grid.box={left:0,top:500,width:960,item:300,height:300,gap:20};
  s.insertBefore(grid,null);gateways.insertBefore(s,null);
  grid.innerHTML=keys.map(k=>card(provider,k)).join('');
  return s;
}
const card=(provider,key)=>`<article class="account-card" data-provider="${provider}" data-account-key="${key}"><div class="account-top"><div class="account-identity"><div class="account-name"><span>${key}</span></div></div><a class="small-badge plan-badge" href="#">Plan</a><button type="button" class="card-menu" id="menu-${provider}-${key}" data-menu-provider="${provider}" data-menu-key="${key}">…</button></div></article>`;
gateways.insertBefore(new El('div',{id:'inventory-empty',class:'accounts-empty',hidden:''}),null);
const codex=section('codex','Codex',['c1'],'account-grid'), claude=section('claude','Claude',['a','b','k'],'watch-grid'), grok=section('xai','Grok',['g1']);
const claudeGrid=claude.children[1];

let clock=Date.parse('2026-10-31T12:00:00Z'),timers=[],timerId=0;
const setTimeout=(fn,ms=0)=>{const id=++timerId;timers.push({id,at:clock+ms,fn});return id;};
const clearTimeout=id=>{timers=timers.filter(t=>t.id!==id);};
const advance=ms=>{clock+=ms;for(const t of timers.filter(t=>t.at<=clock)){timers=timers.filter(x=>x!==t);t.fn();}};
const calls=[],replies=[];
async function fetch(url,options={}){calls.push({url,method:options.method,headers:options.headers,body:options.body ? JSON.parse(options.body) : undefined});
  const next=await (replies.shift() || {status:200,data:{ok:true}});return {status:next.status,ok:next.status<300,json:async()=>next.data};}
const window={matchMedia:()=>({matches:true}),getSelection:()=>({removeAllRanges(){}}),addEventListener(){},removeEventListener(){}};
const context=vm.createContext({document,window,console,setTimeout,clearTimeout,fetch,navigator:{vibrate(){}},CSS:{escape:s=>s},
  AbortSignal:{timeout:()=>undefined},requestAnimationFrame:()=>0,cancelAnimationFrame(){},innerWidth:1200,innerHeight:900,scrollX:0,scrollY:0,scrollBy(){},
  localStorage:{getItem:()=>null,setItem(){}},Date:class extends Date{constructor(...a){super(...(a.length ? a : [clock]));}static now(){return clock;}}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
vm.runInContext(source.split('\n').find(line=>line.startsWith("document.addEventListener('lostpointercapture'")),context);
let renders=0,levels=0;
// render stands in for the renderers: Claude's cards and the sections take the order shown.
vm.runInContext('render=()=>{globalThis.renderCalls();applyProviderOrder();const g=$("watch-grid");for(const x of layoutSort("claude",g.children.map(c=>({key:c.dataset.accountKey,c}))))g.insertBefore(x.c,null);};levelHeads=()=>{globalThis.levelCalls();};',Object.assign(context,{renderCalls:()=>renders++,levelCalls:()=>levels++}));
const layout={providers:['codex','claude','xai'],accounts:{codex:['c1'],claude:['a','b','k'],xai:['g1']}};
context.fixture={read_only:false,add_providers:[],layout:structuredClone(layout),gateway:{online:true,accounts:[]},
  providers:[{id:'codex',name:'Codex',accounts:[{key:'c1'}]},{id:'claude',name:'Claude',accounts:[{key:'a'},{key:'b'},{key:'k',kind:'api_key'}]},{id:'xai',name:'Grok',accounts:[{key:'g1'}]}]};
vm.runInContext('current=fixture',context);
const run=code=>vm.runInContext(code,context);
const providerOrder=()=>gateways.children.filter(c=>c.classList.contains('gateway') && !c.classList.contains('reorder-slot')).map(c=>c.dataset.provider);
const cardOrder=()=>claudeGrid.children.filter(c=>c.dataset.accountKey).map(c=>c.dataset.accountKey);
const plain=value=>JSON.parse(JSON.stringify(value));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const pointer=(target,x,y,extra={})=>({button:0,isPrimary:true,pointerId:1,pointerType:'mouse',target,clientX:x,clientY:y,...extra});
const centre=el=>{const r=(el.closest('.account-card,.gateway') || el).getBoundingClientRect();return {x:(r.left+r.right)/2,y:(r.top+r.bottom)/2};};
const name=key=>claudeGrid.children.find(c=>c.dataset.accountKey===key).querySelector('.account-name');

// The saved order sorts the cards and the sections; anything it leaves out keeps its own order after it.
assert.deepEqual(plain(context.layoutSorted(['a','b','c','d'],['c','a'],k=>k)),['c','a','b','d']);
assert.deepEqual(plain(context.layoutSorted(['a','b'],undefined,k=>k)),['a','b']);
context.fixture.layout={providers:['xai','codex','claude'],accounts:{claude:['k','b','a']}};
assert.deepEqual(plain(run("layoutSort")('claude',[{key:'a'},{key:'b'},{key:'k'}]).map(a=>a.key)),['k','b','a']);
run('applyProviderOrder()');
assert.deepEqual(providerOrder(),['xai','codex','claude']);
// Without a saved order Codex and Claude lead, then the gateway's order; the empty-inventory panel stays first.
context.fixture.layout=undefined;run('applyProviderOrder()');
assert.deepEqual(providerOrder(),['codex','claude','xai']);
assert.equal(gateways.children[0].id,'inventory-empty');
context.fixture.layout=structuredClone(layout);
// The renderers take the order: Claude's subscription and API-key cards sort together.
assert.match(source,/layoutSort\('claude',\[\.\.\.accounts,\.\.\.keys\]\)/);
assert.match(source,/layoutSort\('codex',\[\.\.\.accounts,\.\.\.keys\]\)/);
assert.match(source,/layoutSort\(p\.id,p\.accounts\)\.map\(a=>genericCard/);

// Pulling a card out must not move it to the end when another provider is reordered.
{
  const original=claudeGrid.children.find(c=>c.dataset.accountKey==='a'),shell=new El('section',{class:'gateway split-card','data-provider':'claude'}),grid=new El('div',{class:'account-grid'});
  shell.append(grid);document.body.append(shell);grid.append(original);
  const next=context.layoutMove(context.pageLayout(),'xai','g1',null);
  assert.deepEqual(plain(next.accounts.claude),['a','b','k'],'reordering another provider preserves the saved order of pulled-out accounts');
  // A drag in the remaining Claude grid swaps only visible slots; a stays in its saved slot.
  const k=claudeGrid.children.find(c=>c.dataset.accountKey==='k');claudeGrid.insertBefore(k,claudeGrid.children[0]);
  assert.deepEqual(plain(context.pageLayout('claude').accounts.claude),['a','k','b']);
  claudeGrid.insertBefore(original,claudeGrid.children[0]);
  claudeGrid.insertBefore(claudeGrid.children.find(c=>c.dataset.accountKey==='b'),k);shell.remove();
}

// Sortable once there are two; each provider's tile then takes focus for the arrow keys. Read-only views stay put.
run('markSortable()');
assert.ok(gateways.classList.contains('sortable') && claudeGrid.classList.contains('sortable'));
assert.ok(!codex.children[1].classList.contains('sortable'),'a lone card has nothing to swap with');
const tile=claude.querySelector('.provider-icon');
assert.equal(tile.getAttribute('tabindex'),'0');assert.equal(tile.getAttribute('aria-label'),'Reorder Claude');assert.equal(tile.getAttribute('aria-describedby'),'reorder-help');
assert.ok(html.includes('id="reorder-help"') && html.includes('id="layout-note"'));
context.fixture.read_only=true;run('markSortable()');
assert.ok(!gateways.classList.contains('sortable') && !claudeGrid.classList.contains('sortable') && !tile.hasAttribute('tabindex'));
context.fixture.read_only=false;run('markSortable()');

// Both board widgets and account cards call this rule with cached homes and the drawn rect.
const box=(left,top,width=100,height=100,slot=false)=>({left,top,right:left+width,bottom:top+height,slot});
function dragFixture(width=100,neighbour=100,stacked=false){
  let order=['slot','other'],swaps=0,points=[];const travel={};
  const homes=()=>{let pos=0;return order.map(id=>{const size=id==='slot'?width:neighbour;const r=stacked?box(0,pos,100,size,id==='slot'):box(pos,0,size,100,id==='slot');pos+=size+10;return r;});};
  const step=(position,grab=0)=>{
    // Convert a pointer position back to the drawn card; its grab offset must cancel out.
    const left=(position+grab)-grab,drawn=stacked?box(0,left,100,width):box(left,0,width,100);
    const at=context.reorderIndex(homes(),drawn,travel);
    if(at!==order.indexOf('slot')){order.reverse();swaps++;points.push(position);}
    return swaps;
  };
  step(0);return {step,get swaps(){return swaps;},points};
}
for(const stacked of [false,true]){
  const d=dragFixture(100,100,stacked);
  for(let x=2;x<=80;x+=2)d.step(x);
  assert.equal(d.swaps,1,'2px slow movement swaps once');assert.equal(d.points[0],40,'30% overlap');
  for(let n=0;n<30;n++)for(const x of [80,77,83,80])d.step(x);
  assert.equal(d.swaps,1,'hold and ±3px jitter cannot swap back');
  d.step(71);assert.equal(d.swaps,1,'12px reverse dead zone is inclusive');
  d.step(70);assert.equal(d.swaps,2,'reverse beyond dead zone swaps back once');
  for(let x=68;x>=0;x-=2)d.step(x);
  assert.equal(d.swaps,2,'continued reverse does not flip again');
}
for(const [width,other] of [[300,100],[100,300],[100,100]]){
  const points=[];
  for(const grab of [0,width-1]){
    const d=dragFixture(width,other),threshold=10+.3*Math.min(width,other);
    d.step(threshold-1,grab);assert.equal(d.swaps,0);
    d.step(threshold,grab);assert.equal(d.swaps,1,'30% of the smaller card in either width order');
    for(let n=0;n<20;n++)for(const x of [threshold,threshold-3,threshold+3])d.step(x,grab);
    assert.equal(d.swaps,1);points.push(d.points[0]);
  }
  assert.equal(points[0],points[1],'grab offset does not change swap point');
}
const travel={};const wrapped=[box(0,0,100,100,true),box(110,0),box(0,120)];
context.reorderIndex(wrapped,box(0,0),travel);
assert.equal(context.reorderIndex(wrapped,box(0,49),travel),0,'less than 30% stacked overlap');
assert.equal(context.reorderIndex(wrapped,box(0,50),travel),2,'wrapped row uses vertical overlap');
const perpendicular={};const row=[box(0,0,100,100,true),box(110,0)];
context.reorderIndex(row,box(0,0),perpendicular);
assert.equal(context.reorderIndex(row,box(40,51),perpendicular),0,'requires half of the smaller perpendicular dimension');
assert.equal(context.reorderIndex(row,box(42,50),perpendicular),1);
assert.equal(context.reorderIndex([],box(0,0),{}),0);

// A horizontal swap can repack its slot onto a fixed-height line above the
// neighbour. Incidental vertical drift/edge scrolling must not undo that swap.
for(const height of [280,308]){
  const travel={},homes=[box(0,0,680,536),box(0,570,580,height),box(600,570,380,height,true)];
  context.reorderIndex(homes,box(600,570,380,height),travel);
  assert.equal(context.reorderIndex(homes,box(400,570,380,height),travel),1);
  const repacked=[homes[0],box(700,0,380,height===280?536:height,true),homes[1]];
  const scrolledHomes=()=>repacked.map(r=>({...r,top:r.top-context.scrollY,bottom:r.bottom-context.scrollY}));
  assert.equal(context.reorderIndex(repacked,box(380,572,380,height),travel),1,'repacking cannot turn slight vertical drift into a reverse swap');
  for(let n=0;n<10;n++)for(const dy of [2,5,-1,2]){
    context.scrollY+=3;
    assert.equal(context.reorderIndex(scrolledHomes(),box(380,570+dy,380,height),travel),1,'hold, jitter and edge scrolling retain the reordered slot');
  }
  assert.equal(context.reorderIndex(scrolledHomes(),box(380,570+height*.08,380,height),travel),1,'the new-axis dead zone is inclusive');
  assert.equal(context.reorderIndex(scrolledHomes(),box(380,570+height*.08+1,380,height),travel),2,'deliberate travel on the new axis can reverse the reorder');
  context.scrollY=0;
}

// Exercise the board's real pointer handlers and packing against fixed-height gaps.
{
  const boardSource=fs.readFileSync(path.join(__dirname,'../dashboard/static/boards.js'),'utf8');
  Object.assign(context,{location:{hash:'#overview'},matchMedia:()=>({matches:true}),queueMicrotask,Intl});
  vm.runInContext(boardSource.replace('window.RouterBoards={','window.boardTest={S,mounted,applyPacking,pointerStart,pointerMove,stopPointer};window.RouterBoards={'),context);
  const B=window.RouterBoards,I=window.boardTest,grid=new El('div',{id:'board-grid'});
  grid.box={left:0,top:0,width:1180};document.body.append(grid);window.innerWidth=1440;
  grid.layoutChild=el=>{
    if(el.classList.contains('lifted'))return {x:0,y:0,w:0,h:0};
    const [col,span]=(el.style.gridColumn||'').split(/\D+/).filter(Boolean).map(Number),[row,rows]=(el.style.gridRow||'').split(/\D+/).filter(Boolean).map(Number);
    return {x:(col-1)*100,y:(row-1)*57,w:span*100-20,h:rows===1?280:rows*57-34};
  };
  B.register({type:'probe',render(){}});
  const order=()=>plain(I.S.layout.boards[0].widgets.map(w=>w.id));
  const movePointer=(x,y)=>I.pointerMove({pointerId:1,clientX:x,clientY:y,preventDefault(){}});
  function setup(height='auto',span=7,widgets){
    for(const el of [...grid.children])el.remove();I.mounted.clear();
    const ws=(widgets||[{id:'models',span,height:10},{id:'timeline',span:6,height:'auto'},{id:'heatmap',span:4,height}]).map(w=>({...w,type:'probe',options:{}}));
    I.S.layout={boards:[{id:'overview',widgets:ws}]};I.S.active='overview';I.S.customizing=true;
    for(const w of ws){const frame=new El('article',{class:'w edit','data-wid':w.id});frame.append(new El('button',{class:'grip'}));grid.append(frame);I.mounted.set(w.id,{w,frame});}
    I.applyPacking();return ws;
  }
  function press(){const frame=I.mounted.get('heatmap').frame,r=frame.getBoundingClientRect();I.pointerStart(pointer(frame.querySelector('.grip'),r.left+20,r.top+14,{preventDefault(){}}));movePointer(r.left+26,r.top+14);}
  // Widget bodies, frame padding and strip text share the grip's drag path.
  for(const start of ['body','frame','strip','single-card','header']){
    setup();const m=I.mounted.get('heatmap'),body=new El('div',{class:'w-body'});
    m.frame.append(body);
    let target=body;
    if(start==='frame')target=m.frame;
    if(start==='strip'){target=new El('span',{class:'w-name'});m.frame.append(target);}
    if(start==='single-card'){m.w.type='account-card';target=new El('div',{class:'account-top'});const card=new El('article',{class:'account-card'});card.append(target);body.append(card);}
    if(start==='header'){m.w.type='accounts';m.w.options.hideCards=true;}
    const r=m.frame.getBoundingClientRect(),at=pointer(target,r.left+20,r.top+70,{preventDefault(){}});
    I.pointerStart(at);movePointer(at.clientX+2,at.clientY);I.stopPointer();
    assert.deepEqual(order(),['models','timeline','heatmap'],'a click/sub-threshold move leaves the layout alone');
    I.pointerStart(at);movePointer(at.clientX+6,at.clientY);assert.ok(m.frame.classList.contains('lifted'),start+' lifts after four pixels');
    movePointer(724,80);I.stopPointer();assert.deepEqual(order(),['models','heatmap','timeline'],start+' drops at the new position');
  }
  // Only strip controls and the two existing content exceptions keep their own actions.
  for(const attrs of [{tag:'button'},{tag:'a'},{tag:'input'},{tag:'select'},{tag:'textarea'},{tag:'label'},{role:'switch'},{role:'button'},{tag:'button',class:'card-menu'},{tag:'button',class:'choose-another'}]){
    setup();const m=I.mounted.get('heatmap'),control=new El(attrs.tag||'div',Object.fromEntries(Object.entries(attrs).filter(([k])=>k!=='tag'))),child=new El('span');
    const content=new El('div',{class:attrs.class==='choose-another'?'wstate removed':attrs.class==='card-menu'?'w-body':'w-strip'});m.frame.append(content);control.append(child);content.append(control);
    I.pointerStart(pointer(child,620,584,{preventDefault(){}}));movePointer(724,80);
    assert.ok(!m.frame.classList.contains('lifted'),'control keeps its own behaviour: '+JSON.stringify(attrs));I.stopPointer();
    assert.deepEqual(order(),['models','timeline','heatmap']);
  }
  // Blocked content controls may initiate movement; their click/change actions stay suppressed.
  for(const type of ['accounts','account-card']){
    setup();const m=I.mounted.get('heatmap');m.w.type=type;
    const body=new El('div',{class:'w-body'}),card=new El('article',{class:'account-card'}),toggle=new El('input',{type:'checkbox',role:'switch'});m.frame.append(body);body.append(card);card.append(toggle);
    I.pointerStart(pointer(toggle,620,584,{preventDefault(){}}));movePointer(626,584);
    assert.ok(m.frame.classList.contains('lifted'),'a blocked routing control can move its single-card widget');
    movePointer(724,80);I.stopPointer();assert.deepEqual(order(),['models','heatmap','timeline']);
  }
  // A multi-card Accounts widget delegates card presses to the existing account reorder handlers.
  {
    setup();const m=I.mounted.get('heatmap');m.w.type='accounts';m.w.options.provider='claude';m.frame.append(claudeGrid);
    const before=cardOrder(),target=name('a'),at=centre(target);calls.length=0;
    context.reorderPress(pointer(target,at.x,at.y));I.pointerStart(pointer(target,at.x,at.y,{preventDefault(){}}));
    context.reorderMove(pointer(target,at.x+8,at.y));movePointer(at.x+8,at.y);
    assert.equal(run('reorder').phase,'drag');assert.ok(!m.frame.classList.contains('lifted'),'card drag must not lift its Accounts widget');
    context.reorderMove(pointer(target,900,650));movePointer(900,650);context.reorderDrop();I.stopPointer();
    assert.notDeepEqual(cardOrder(),before,'the card reordered');assert.deepEqual(order(),['models','timeline','heatmap']);
    run('reorderClick=false');claude.append(claudeGrid);
    for(const key of before)claudeGrid.append(claudeGrid.children.find(c=>c.dataset.accountKey===key));
    run('layoutPending=null;layoutSaved=null');calls.length=0;
  }
  // Touch bodies preserve the long-press: moving first scrolls; holding lifts.
  for(const pointerType of ['touch','pen']){
    setup();const m=I.mounted.get('heatmap'),at=pointer(m.frame,620,584,{pointerType,preventDefault(){}});
    I.pointerStart(at);movePointer(620,596);advance(400);assert.ok(!m.frame.classList.contains('lifted'));
    I.pointerStart(at);advance(350);assert.ok(m.frame.classList.contains('lifted'));I.stopPointer(true);
  }
  for(const state of ['phone','view']){
    setup();if(state==='phone')context.innerWidth=760;else I.S.customizing=false;
    I.pointerStart(pointer(I.mounted.get('heatmap').frame,620,584,{preventDefault(){}}));movePointer(724,80);I.stopPointer();
    assert.deepEqual(order(),['models','timeline','heatmap'],state+' ignores body drags');context.innerWidth=1440;
  }
  for(const height of ['auto',6]){
    const ws=setup(height),dragged=ws[2];press();movePointer(724,80);
    assert.deepEqual(grid.children.filter(el=>el.classList.contains('w-slot')||el.dataset.wid&& !el.classList.contains('lifted')).map(el=>el.dataset.wid||'slot'),['models','slot','timeline'],'the gap opens a landing slot without crossing another widget');
    for(const x of [724,727,721,724])movePointer(x,80);
    movePointer(724,650);
    assert.deepEqual(grid.children.filter(el=>el.classList.contains('w-slot')||el.dataset.wid&& !el.classList.contains('lifted')).map(el=>el.dataset.wid||'slot'),['models','timeline','slot'],'a second gap uses the current drag order');
    movePointer(724,80);
    I.stopPointer();assert.deepEqual(order(),['models','heatmap','timeline']);
    I.applyPacking();const frame=I.mounted.get('heatmap').frame;
    assert.equal(frame.dataset.line,'0');assert.equal(frame.classList.contains('mixed-auto'),height==='auto');assert.equal(dragged.height,height);
  }
  setup('auto',9);press();movePointer(924,80);I.stopPointer();
  assert.deepEqual(order(),['models','timeline','heatmap'],'a 4-column widget cannot enter a 3-column gap');
  setup();press();movePointer(724,80);I.stopPointer(true);
  assert.deepEqual(order(),['models','timeline','heatmap'],'Escape restores the original order after entering a gap');
  for(const height of ['auto',6]){
    setup(height);press();
    for(let n=1;n<=30;n++)movePointer(620-580*n/30,584+16*n/30);
    for(const y of [600,603,597,600])movePointer(40,y);
    I.stopPointer();assert.deepEqual(order(),['models','heatmap','timeline'],'dragging onto a neighbour survives a mixed/fixed-line repack');
  }
  for(const further of [false,true]){
    setup('auto',8,[{id:'tall',span:8,height:10},{id:'heatmap',span:4,height:'auto'},{id:'short',span:8,height:4},{id:'full',span:12,height:10}]);
    press();movePointer(824,650);
    const slots=()=>grid.children.filter(el=>el.classList.contains('w-slot')||el.dataset.wid&&!el.classList.contains('lifted')).map(el=>el.dataset.wid||'slot');
    assert.deepEqual(slots(),['tall','short','slot','full']);
    assert.ok(!grid.children.some(el=>el.classList.contains('w-gap')&&el.style.gridRow==='11 / span 4'),'the acquired gap disappears');
    movePointer(824,651);
    assert.deepEqual(slots(),['tall','short','slot','full'],'one pixel after a gap landing cannot select an overlapping full-width widget');
    for(let n=0;n<10;n++)for(const y of [650,653,647,650])movePointer(824,y);
    movePointer(824,692);
    assert.deepEqual(slots(),['tall','short','slot','full'],'gap landing survives jitter and travel inside the dead zone');
    if(further)movePointer(824,694);
    I.stopPointer();assert.deepEqual(order(),further?['tall','short','full','heatmap']:['tall','short','heatmap','full'],'deliberate travel can leave the gap landing');
  }
  grid.remove();delete window.RouterBoards;delete window.boardTest;
}

(async()=>{
  // Exercise the save path, too: moving Grok must preserve Claude's pulled-out first card.
  {
    const original=claudeGrid.children.find(c=>c.dataset.accountKey==='a'),shell=new El('section',{class:'gateway split-card','data-provider':'claude'}),grid=new El('div',{class:'account-grid'});
    shell.append(grid);document.body.append(shell);grid.append(original);calls.length=0;
    context.moveProvider(grok,-1);
    assert.deepEqual(calls[0].body.accounts.claude,['a','b','k'],'another provider move saves the untouched account order');
    await settle();claudeGrid.insertBefore(original,claudeGrid.children[0]);shell.remove();
    context.fixture.layout=structuredClone(layout);run('layoutPending=null;layoutSaved=null;applyProviderOrder()');calls.length=0;renders=0;levels=0;
  }
  // A mouse drag of Claude's first card: a press alone changes nothing; past four pixels it lifts, a slot takes its
  // place, the slot follows the pointer back and forth, and the drop saves the full layout.
  const start=centre(name('a'));
  context.reorderPress(pointer(name('a'),start.x,start.y));
  assert.equal(run("reorder").phase,'press');
  context.reorderMove(pointer(name('a'),start.x+2,start.y+1));
  assert.equal(run("reorder").phase,'press');
  context.reorderMove(pointer(name('a'),start.x+6,start.y));
  const r=run("reorder");
  assert.equal(r.phase,'drag');
  assert.ok(r.item.classList.contains('reorder-lifted') && document.body.classList.contains('reordering'));
  assert.deepEqual(claudeGrid.children.map(c=>c.dataset.accountKey || 'slot'),['slot','a','b','k']);
  assert.equal(r.item.style.width,'300px');assert.equal(r.slot.style.minHeight,'300px');
  // Card overlap moves the slot past b; reverse travel returns it; then on to the end.
  const firstHomes=r.homes;
  context.reorderMove(pointer(name('a'),start.x+8,start.y));
  assert.equal(r.homes,firstHomes,'pointer movement reuses settled homes');
  context.reorderMove(pointer(name('a'),480,650));
  assert.notEqual(r.homes,firstHomes,'reorder captures new settled homes immediately');
  assert.deepEqual(claudeGrid.children.filter(c=>c!==r.item).map(c=>c.dataset.accountKey || 'slot'),['b','slot','k']);
  context.reorderMove(pointer(name('a'),100,650));
  assert.deepEqual(claudeGrid.children.filter(c=>c!==r.item).map(c=>c.dataset.accountKey || 'slot'),['slot','b','k']);
  context.reorderMove(pointer(name('a'),900,650));
  assert.deepEqual(claudeGrid.children.filter(c=>c!==r.item).map(c=>c.dataset.accountKey || 'slot'),['b','k','slot']);
  assert.equal(r.item.style.translate,`${900-start.x}px ${650-start.y}px`);
  // A refresh mid-drag renders nothing until the drop; the grid keeps the dragged card.
  const before=claudeGrid.children.slice();
  context.renderWatch({providers:[],gateway:{}});context.renderAccounts({accounts:[]});context.renderProviders({providers:[]});
  assert.deepEqual(claudeGrid.children,before);assert.equal(run("reorderDeferred"),true);
  // The click that ends a drag is swallowed once.
  calls.length=0;renders=0;levels=0;
  context.reorderDrop();
  assert.equal(run("reorder"),null);assert.equal(run("reorderClick"),true);
  assert.deepEqual(cardOrder(),['b','k','a']);
  assert.ok(!r.item.classList.contains('reorder-lifted') && !document.body.classList.contains('reordering') && !claudeGrid.querySelector('.reorder-slot'));
  assert.equal(r.item.style.translate,'');assert.equal(r.item.style.width,'');
  assert.equal(renders,1,'the deferred refresh renders once the card has landed');assert.equal(run("reorderDeferred"),false);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/layout');assert.equal(calls[0].method,'POST');
  assert.equal(calls[0].headers['X-Router-Request'],'1');assert.equal(calls[0].headers['Content-Type'],'application/json');
  assert.deepEqual(calls[0].body,{providers:['codex','claude','xai'],accounts:{codex:['c1'],claude:['b','k','a'],xai:['g1']}});
  assert.deepEqual(plain(run('layoutNow()')),calls[0].body,'the new order shows before the save answers');
  assert.equal(document.getElementById('account-announce').textContent,'a moved to 3 of 3');
  await settle();
  assert.equal(run('layoutPending'),null);
  assert.equal(run('layoutSaved.after'),run('fastLoadEpoch'),'kept until an overview asked for after the save');
  assert.deepEqual(plain(run('layoutNow()')),calls[0].body,'the accepted order stays shown over an older overview');
  // An overview requested before the save answered keeps the new order; a later one carries it. (load()'s own
  // generation check, run here by hand.)
  run('fastLoadEpoch=layoutSaved.after;if(layoutSaved?.after<fastLoadEpoch)layoutSaved=null');
  assert.ok(run('layoutSaved'),'an older overview leaves the drop in place');
  run('fastLoadEpoch=layoutSaved.after+1;if(layoutSaved?.after<fastLoadEpoch)layoutSaved=null');
  assert.equal(run('layoutSaved'),null);
  assert.match(source,/current=results\[0\];today=selected==='today'\?current:results\[1\];\n[^\n]*\n    if\(layoutSaved\?\.after<generation\)layoutSaved=null;/);

  // A drop where it started saves nothing and levels the rows again.
  calls.length=0;levels=0;
  const b=centre(name('b'));
  context.reorderPress(pointer(name('b'),b.x,b.y));context.reorderMove(pointer(name('b'),b.x+10,b.y));context.reorderDrop();
  assert.equal(calls.length,0);assert.equal(levels,1);

  // Escape puts everything back: the slot returns and the card rejoins its first place, with nothing saved.
  context.fixture.layout={...structuredClone(layout),accounts:{...layout.accounts,claude:cardOrder()}};
  const k=centre(name('k'));
  context.reorderPress(pointer(name('k'),k.x,k.y));context.reorderMove(pointer(name('k'),k.x+10,k.y));
  context.reorderMove(pointer(name('k'),20,650));
  assert.deepEqual(claudeGrid.children.filter(c=>!c.classList.contains('reorder-lifted')).map(c=>c.dataset.accountKey || 'slot'),['slot','b','a']);
  context.reorderDrop(true);
  assert.deepEqual(cardOrder(),['b','k','a']);assert.equal(calls.length,0);assert.equal(run("reorder"),null);

  // Provider sections now move only as board widgets in customize mode.
  const g=gateways.children.find(c=>c.dataset.provider==='xai'), title=g.querySelector('h2'), at=centre(title);
  context.reorderPress(pointer(title,at.x,at.y,{pointerType:'touch'}));
  advance(400);assert.equal(run('reorder'),null,'provider headings no longer drag');
  // Touch on an account still waits for a long press; motion first scrolls.
  const top=name('b'), touch=centre(top);
  context.reorderPress(pointer(top,touch.x,touch.y,{pointerType:'touch'}));
  context.reorderMove(pointer(top,touch.x,touch.y+12,{pointerType:'touch'}));
  advance(400);assert.equal(run('reorder'),null);
  // Touch/pen transfer implicit capture from the touched child to the drag handle. Only that handle losing this
  // pointer's capture cancels; the child's bubbled loss and another pointer's loss leave the drag alone.
  for(const pointerType of ['touch','pen']) {
    context.reorderPress(pointer(top,touch.x,touch.y,{pointerType}));advance(350);
    const drag=run('reorder'), lost=event=>document.listeners.lostpointercapture.forEach(fn=>fn(event));
    assert.equal(drag.phase,'drag');assert.notEqual(drag.handle,top);
    lost({target:top,pointerId:drag.pointer});
    assert.equal(run('reorder'),drag,'capture transferred from the child keeps dragging');
    lost({target:drag.handle,pointerId:drag.pointer+1});
    assert.equal(run('reorder'),drag,'a different pointer cannot cancel the drag');
    lost({target:drag.handle,pointerId:drag.pointer});
    assert.equal(run('reorder'),null,'losing the handle capture cancels the drag');
    assert.deepEqual(providerOrder(),['codex','claude','xai']);assert.equal(calls.length,0);
  }
  // Links, buttons and the plan badge inside a handle never start a drag.
  context.reorderPress(pointer(claude.querySelector('.text-link'),10,10));assert.equal(run("reorder"),null);
  context.reorderPress(pointer(claudeGrid.querySelector('.card-menu'),10,10));assert.equal(run("reorder"),null);
  context.reorderPress(pointer(claudeGrid.querySelector('.plan-badge'),10,10));assert.equal(run("reorder"),null);
  context.reorderPress(pointer(name('a'),10,10,{button:2}));assert.equal(run("reorder"),null);

  // Keyboard: the card menu offers Move left and right (up and down across lines) and moves one place.
  context.openCardMenu(claudeGrid.querySelector('[data-menu-key="k"]'));
  const menu=document.getElementById('card-menu');
  assert.deepEqual(menu.querySelectorAll('[role=menuitem]').map(b=>b.textContent),['Move left','Move right','Disconnect…']);
  context.closeCardMenu();
  context.openCardMenu(claudeGrid.querySelector('[data-menu-key="b"]'));
  assert.deepEqual(menu.querySelectorAll('[data-menu-move]').map(b=>b.textContent),['Move right'],'the first card only moves later');
  context.closeCardMenu();
  calls.length=0;renders=0;
  context.moveCard('claude','b',undefined,1);
  assert.equal(renders,1);
  assert.deepEqual(calls[0].body.accounts.claude,['k','b','a']);
  assert.deepEqual(plain(run('layoutNow()').accounts.claude),['k','b','a']);
  assert.equal(document.activeElement,claudeGrid.querySelector('[data-menu-key="b"]'),'focus stays on the card menu button');
  context.moveCard('claude','a',undefined,1);assert.equal(calls.length,1,'the last card cannot move later');
  await settle();
  // The menu click handler moves the card; it is checked before Disconnect.
  assert.match(source,/else if\(item\.dataset\.menuMove\)moveCard\(provider,key,authIndex,Number\(item\.dataset\.menuMove\)\);\n  else if\(item\.hasAttribute\('data-menu-signin'\)\)/);
  // Arrow keys on a provider's tile move the section, earlier or later, and keep focus on its tile.
  calls.length=0;
  context.moveProvider(g,-1);
  assert.deepEqual(calls[0].body.providers,['codex','xai','claude']);
  assert.equal(document.activeElement,g.querySelector('.provider-icon'));
  assert.equal(document.getElementById('account-announce').textContent,'Grok moved to 2 of 3');
  context.moveProvider(gateways.children.find(c=>c.dataset.provider==='codex'),-1);assert.equal(calls.length,1,'the first section cannot move earlier');
  await settle();
  assert.match(source,/\{ArrowLeft:-1,ArrowUp:-1,ArrowRight:1,ArrowDown:1\}\[event\.key\]/);

  // A refused save puts the saved order back with a glide and a note by the provider's name.
  context.fixture.layout={providers:['codex','claude','xai'],accounts:{codex:['c1'],claude:['k','b','a'],xai:['g1']}};
  run('layoutPending=null;layoutSaved=null');renders=0;
  replies.push({status:503,data:{error:'unavailable'}});
  context.moveCard('claude','a',undefined,-1);
  assert.deepEqual(plain(run('layoutNow()').accounts.claude),['k','a','b']);
  await settle();await settle();
  assert.equal(run("layoutPending"),null);
  assert.deepEqual(plain(run('layoutNow()').accounts.claude),['k','b','a']);
  assert.equal(renders,2,'rendered for the move and again for the revert');
  assert.equal(document.getElementById('account-announce').textContent,"Couldn't save the new order");
  // A save the next drop supersedes changes nothing when it fails.
  replies.push({status:503,data:{}},{status:200,data:{ok:true}});
  context.moveCard('claude','a',undefined,-1);context.moveCard('claude','b',undefined,-1);
  await settle();await settle();
  assert.deepEqual(plain(run('layoutNow().accounts.claude')),calls.at(-1).body.accounts.claude);
  // After an accepted save, a refused one goes back to the accepted order, not the overview from before it.
  const accepted=calls.at(-1).body.accounts.claude;
  context.fixture.layout={...context.fixture.layout,accounts:{...context.fixture.layout.accounts,claude:['a','k','b']}};
  replies.push({status:503,data:{}});
  context.moveCard('claude',accepted[1],undefined,-1);
  await settle();await settle();
  assert.deepEqual(plain(run('layoutNow().accounts.claude')),accepted);
  assert.notDeepEqual(accepted,['a','k','b']);

  // Rapid moves serialize writes and coalesce to the latest order. Resolve the latest response first: concurrent
  // requests would accept it and then wrongly overwrite it when the older request finally succeeds.
  const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
  for(const status of [200,503]) {
    const first={...structuredClone(layout),providers:['xai','codex','claude']};
    const middle={...structuredClone(layout),providers:['claude','xai','codex']};
    const latest={...structuredClone(layout),accounts:{...layout.accounts,claude:['a','k','b']}};
    const older=deferred(), newer=deferred();replies.push(older.promise,newer.promise);calls.length=0;renders=0;
    context.saveLayout(first,'xai');context.saveLayout(middle,'claude');context.saveLayout(latest,'claude');
    newer.resolve({status:200,data:{ok:true}});await settle();
    assert.equal(calls.length,1,'only the oldest request is in flight');
    assert.deepEqual(calls[0].body,first);
    assert.deepEqual(plain(run('layoutNow()')),latest,'the latest move stays visible while queued');
    older.resolve({status,data:{}});await settle();
    assert.equal(calls.length,2,'intermediate moves coalesce into one latest save');
    assert.deepEqual(calls[1].body,latest);
    assert.deepEqual(plain(run('layoutSaved.layout')),latest,'an obsolete success cannot replace the latest accepted order');
    assert.equal(run('layoutPending'),null);assert.equal(run('layoutSaving'),false);
    assert.equal(renders,0,'a superseded failure never reverts a newer move');
  }
  // If the queued latest order fails, revert to the preceding successful write, even when it was superseded in UI.
  const first={...structuredClone(layout),providers:['xai','claude','codex']};
  const latest={...structuredClone(layout),providers:['claude','codex','xai']};
  const older=deferred(), newer=deferred();replies.push(older.promise,newer.promise);calls.length=0;renders=0;
  context.saveLayout(first,'xai');context.saveLayout(latest,'claude');
  older.resolve({status:200,data:{ok:true}});await settle();
  assert.equal(calls.length,2);assert.deepEqual(plain(run('layoutNow()')),latest);assert.equal(renders,0);
  newer.resolve({status:503,data:{}});await settle();
  assert.deepEqual(plain(run('layoutNow()')),first);assert.equal(renders,1);

  // The real provider renderer replaces generic header controls. Restore focus to the same provider after making
  // its new icon keyboard-focusable, both on periodic renders and renders after a move. Static sections keep focus.
  grok.dataset.genericProvider='xai';context.fixtureCard=(p,a)=>card(p.id,a.key);
  run('genericCard=(p,a)=>fixtureCard(p,a)');
  for(const provider of ['xai','codex','claude']) {
    run('markSortable()');
    const section=gateways.children.find(s=>s.dataset.provider===provider), original=section.querySelector('.provider-icon');
    original.focus();context.renderProviders(context.fixture);run('markSortable()');
    const replacement=section.querySelector('.provider-icon');
    if(provider==='xai')assert.notEqual(replacement,original,'generic icons are replaced');
    else assert.equal(replacement,original,'static provider icons stay in place');
    assert.equal(replacement.tabIndex,0);assert.equal(document.activeElement,replacement,'focus stays on '+provider);
    context.moveProvider(section,providerOrder().indexOf(provider) ? -1 : 1);
    await settle();
    context.renderProviders(context.fixture);run('markSortable()');
    assert.equal(document.activeElement,section.querySelector('.provider-icon'),'focus survives the next render after a move');
  }

  // Wiring and styles: pointer events, the touch scroll lock, Escape, and the reduced-motion and coarse-pointer rules.
  for(const line of ["accountEvents.addEventListener('pointerdown',reorderPress);","document.addEventListener('touchmove',event=>{if(reorder?.phase==='drag')event.preventDefault();},{passive:false});",
    "document.addEventListener('pointercancel',event=>{if(reorder?.pointer===event.pointerId)reorderDrop(true);});"])assert.ok(source.includes(line),line);
  assert.match(source,/event\.key==='Escape' && reorder\?\.phase==='drag'/);
  assert.match(source,/r\.timer=setTimeout\(\(\)=>reorderLift\(r\),350\)/);
  assert.match(source,/prefers-reduced-motion: reduce/);
  assert.match(css,/\.reorder-lifted\{position:fixed!important/);
  assert.match(css,/@media\(pointer:coarse\)\{[^}]*user-select:none/);
  console.log('Reorder UI: saved order, sortable marks, landing index, fixed-height board gaps, drag and drop, cancel, touch long-press, refresh deferral, keyboard moves and refused saves passed.');
})().catch(error=>{console.error(error);process.exit(1);});
