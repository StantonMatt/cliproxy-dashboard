'use strict';
// Executes the routing menu through the real app.js and render() with a small DOM built from index.html.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const A='owner@example.test', B='alternate@example.test';
const OPTIONS=['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B];
const LABELS={'round-robin':'Round-robin','earliest-weekly-reset':'Earliest reset','highest-remaining-quota':'Higher remaining quota'};
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
const elements=new Map();
let textWrites=new Map(), menuWrites=0;

function hiddenWithin(el){for(let node=el;node;node=node.parentElement)if(node.hidden)return true;return false;}
function element(id, className='', tag='div'){
  const events={}, attributes={}, classes=new Set(className.split(/\s+/).filter(Boolean));
  let text='';
  const el={id,tag,disabled:false,value:'',innerHTML:'',checked:false,parentElement:null,children:[],dataset:{},events,style:{},
    title:'',offsetWidth:240,offsetHeight:150,
    get textContent(){return text;},
    set textContent(value){text=String(value);const key=id || className;textWrites.set(key,(textWrites.get(key) || 0)+1);},
    addEventListener(type,fn){(events[type] ||= []).push(fn);},
    // Events bubble from the target through its ancestors, as click does.
    dispatch(type,event={}){for(let node=el;node;node=node.parentElement)for(const fn of node.events[type] || [])fn({target:el,preventDefault(){},...event});},
    setAttribute(name,value){attributes[name]=String(value);},getAttribute(name){return name in attributes ? attributes[name] : null;},
    hasAttribute:name=>name in attributes,removeAttribute(name){delete attributes[name];},
    // Disabled controls and anything inside a hidden element can't take focus.
    focus(){if(!el.disabled && !hiddenWithin(el))document.activeElement=el;},
    getBoundingClientRect:()=>({left:900,right:1010,top:100,bottom:124,width:110,height:24}),
    options:OPTIONS.map(value=>({value,textContent:LABELS[value] || 'Prefer '+value})),
    classList:{add(name){classes.add(name);},remove(name){classes.delete(name);},toggle(name,force){if(force ?? !classes.has(name))classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)},
    contains(other){for(let node=other;node;node=node.parentElement)if(node===el)return true;return false;},
    closest(selector){
      for(let node=el;node;node=node.parentElement){
        if(selector==='[role^=menuitem]' && /^menuitem/.test(node.getAttribute?.('role') || ''))return node;
        if(selector==='#routing-menu' && node.id==='routing-menu')return node;
      }
      return null;
    },
    querySelector(selector){
      if(selector==='[aria-checked="true"]')return el.children.find(child=>child.getAttribute('aria-checked')==='true') || null;
      if(selector==='[role^=menuitem]')return el.querySelectorAll(selector)[0] || null;
      const choice=selector.match(/^\[data-routing-choice="(\w+)"\]$/);
      if(choice)return el.children.find(child=>child.dataset.routingChoice===choice[1]) || null;
      const names=selector.split(',').map(part=>part.trim().replace(/^\./,''));
      return el.children.find(child=>names.some(name=>child.classList.contains(name))) || null;
    },
    querySelectorAll(selector){
      if(selector==='[role^=menuitem]')return el.children.filter(child=>/^menuitem/.test(child.getAttribute('role') || ''));
      if(selector==='[data-routing-choice]')return el.children.filter(child=>child.dataset.routingChoice);
      if(selector==='[data-routing-option]')return el.children.filter(child=>child.dataset.routingOption!=null);
      throw new Error('Unexpected selector '+selector);
    },
    after(node){
      if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(child=>child!==node);
      const siblings=el.parentElement.children;siblings.splice(siblings.indexOf(el)+1,0,node);node.parentElement=el.parentElement;
    }
  };
  // Browsers drop focus to the body when the focused control is disabled, or it or an ancestor is hidden.
  let disabled=false, hidden=false;
  Object.defineProperty(el,'disabled',{get:()=>disabled,set(next){disabled=!!next;if(disabled && document.activeElement===el)document.activeElement=document.body;}});
  Object.defineProperty(el,'hidden',{get:()=>hidden,set(next){hidden=!!next;if(hidden && el.contains(document.activeElement))document.activeElement=document.body;}});
  return el;
}
const document={documentElement:{dataset:{}},body:{id:'body',dataset:{}},activeElement:null,querySelectorAll:()=>[],querySelector:()=>null,
  addEventListener(){},
  getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);}};
document.activeElement=document.body;
const $=id=>document.getElementById(id);
// Each routing row's direct children, in index.html order, so tab order and switch placement are real.
for(const row of ['codex-routing','claude-routing','routing-all']){
  const start=html.indexOf(`<div id="${row}"`);
  const end=html.indexOf('</small></div>',start);
  const segment=html.slice(start,end);
  const rowClass=segment.match(/class="([^"]*)"/)[1];
  elements.set(row,element(row,rowClass));
  for(const [,tag,attrs] of segment.slice(segment.indexOf('>')+1).matchAll(/<(\w+)([^>]*\bid="[^"]+"[^>]*)>/g)){
    const id=attrs.match(/\bid="([^"]+)"/)[1], className=(attrs.match(/class="([^"]*)"/) || [,''])[1];
    const el=element(id,className,tag);el.hidden=/\shidden(\s|$|>)/.test(attrs+' ');elements.set(id,el);
    for(const name of ['aria-expanded','aria-haspopup','aria-controls','aria-labelledby','aria-describedby']){const m=attrs.match(new RegExp(`${name}="([^"]*)"`));if(m)el.setAttribute(name,m[1]);}
    const parent=id==='routing-link' ? $('routing-split') : $(row);
    el.parentElement=parent;parent.children.push(el);
    // The button's policy label is its only unnamed child.
    if(className==='routing-trigger'){const value=element('',  'routing-value','span');value.parentElement=el;el.children.push(value);}
  }
}
$('watch-section').hidden=false;
// The routing menu from index.html: a popover whose markup becomes buttons, the title and the error line. A replaced
// child that had focus drops it to the body, as a browser does.
const menuHtml=html.match(/<div id="routing-menu"[^>]*><\/div>/)[0];
const menu=element('routing-menu','menu-pop routing-menu');elements.set('routing-menu',menu);menu.hidden=true;
menu.showPopover=()=>{};menu.hidePopover=()=>{};
let menuMarkup='';
Object.defineProperty(menu,'innerHTML',{get:()=>menuMarkup,set(markup){
  menuWrites++;menuMarkup=markup;
  const focused=menu.contains(document.activeElement);
  menu.children=[];
  for(const [,tag,attrs,inner] of markup.matchAll(/<(p|button)([^>]*)>([\s\S]*?)<\/\1>/g)){
    const child=element('',(attrs.match(/class="([^"]*)"/) || [,''])[1],tag);
    for(const [,name,value] of attrs.matchAll(/\b((?:aria|role|data)[\w-]*)="([^"]*)"/g)){
      if(name.startsWith('data-'))child.dataset[name.slice(5).replace(/-(\w)/g,(_,c)=>c.toUpperCase())]=value.replace(/&amp;/g,'&');
      else child.setAttribute(name,value);
    }
    const strip=value=>value.replace(/<[^>]+>/g,'').replace(/&amp;/g,'&');
    child.label=strip(inner.replace(/<small>[\s\S]*<\/small>/,''));child.note=(inner.match(/<small>([\s\S]*?)<\/small>/) || [,''])[1];
    child.checkMark=/href="#check"/.test(inner);
    if(tag==='p')child.textContent=strip(inner);
    child.parentElement=menu;menu.children.push(child);
  }
  if(focused)document.activeElement=document.body;
}});

let hash='';
const context=vm.createContext({document,Date,JSON,Promise,Map,Set,Intl,URLSearchParams,localStorage:{getItem:()=>null},
  location:{get hash(){return hash;}},setTimeout:()=>1,clearTimeout(){},AbortSignal:{timeout:()=>1},innerWidth:1440,innerHeight:900,scrollX:0,scrollY:0});
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
// Only the routing renderers run for real; the rest of render() is outside this feature.
vm.runInContext('var realRenderSystem=renderSystem;',context);
vm.runInContext(`renderOperations=()=>{};renderWatch=()=>{};renderResetModal=()=>{};renderSystem=()=>{};
  renderAccounts=gateway=>{renderRouting(gateway);renderRouting(gateway,'claude');};`,context);

const active=(policy,email)=>({policy,status:'active',selected_email:email,preferred_email:email,saved_mode:policy,saved_preferred_email:email});
// Both Codex credentials stay listed, as the retired one does until deploy; only the configured count decides.
function snapshot({linked=true,available=true,codexAccounts=2}={}){
  return {gateway:{online:true,routing_linked:linked,move_available:available,checked_at:new Date().toISOString(),
    accounts:[{id:'codex-a',email:A},{id:'codex-b',email:B,disabled:codexAccounts<2}],
    ...(codexAccounts==null ? {} : {codex_account_count:codexAccounts}),
    preference:active('preferred-account',B),claude_preference:active('preferred-account',B)},
    usage:{},health:{},watch_accounts:[{id:'claude',email:A},{id:'claude-b',independent_windows:true,email:B}]};
}
const rows={
  codex:{row:'codex-routing',select:'routing-mode',trigger:'routing-trigger',feedback:'routing-feedback',retry:'routing-retry',route:'/api/routing',flag:'routingSaving',linked:false,
    name:'Codex routing',note:'Also moves Claude conversations.'},
  claude:{row:'claude-routing',select:'claude-routing-mode',trigger:'claude-routing-trigger',feedback:'claude-routing-feedback',retry:'claude-routing-retry',route:'/api/claude-routing',flag:'claudeRoutingSaving',linked:false,
    name:'Claude routing',note:'Also moves Codex conversations.'},
  all:{row:'routing-all',select:'routing-all-mode',trigger:'routing-all-trigger',feedback:'routing-all-feedback',retry:'routing-all-retry',route:'/api/routing-all',flag:'routingAllSaving',linked:true,
    name:'Routing',note:''}
};
let posts=[], respond=async()=>({}), refreshes=0, nextSnapshot=null;
context.request=async(route,options)=>{posts.push({route,body:JSON.parse(options.body)});return respond(route,options);};
context.load=async()=>{refreshes++;if(nextSnapshot){context.fixture=nextSnapshot;vm.runInContext('current=fixture',context);}context.render();};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const state=name=>vm.runInContext(name,context);
const items=()=>menu.children.filter(child=>/^menuitem/.test(child.getAttribute('role') || ''));
const item=kind=>items().find(i=>i.dataset.routingChoice===kind);
const option=value=>items().find(i=>i.dataset.routingOption===value);
const labels=()=>items().map(i=>i.label);
const part=className=>menu.children.find(child=>child.classList.contains(className));
const title=()=>part('routing-menu-title')?.textContent;
const error=()=>part('routing-menu-error')?.textContent;
const menuKey=()=>menu.hidden ? null : state('routingMenu?.key ?? null');
const shown=key=>$(rows[key].trigger).children[0].textContent;
const outsideClick=()=>context.routingMenuOutside({target:document.body});
const key=name=>({key:name,preventDefault(){}});
// Escape and Tab reach the menu from whatever has focus inside it.
const press=name=>(menu.contains(document.activeElement) ? document.activeElement : menu).dispatch('keydown',key(name));
const escape=()=>press('Escape');
const ERROR='Change not confirmed. Try again.';

async function reset(row,options={}){
  vm.runInContext('closeRoutingChoice(true);closeRoutingMenu(false);routingRetry.clear();routingSaving=false;claudeRoutingSaving=false;routingAllSaving=false;routingLinkSaving=false;failed=false',context);
  await flush();
  posts=[];respond=async()=>({});nextSnapshot=null;hash='';
  context.fixture=snapshot({linked:rows[row].linked,...options});vm.runInContext('current=fixture',context);context.render();
  for(const r of Object.values(rows)){$(r.feedback).textContent='';}
  document.activeElement=document.body;textWrites=new Map();menuWrites=0;
}
function openMenu(row){
  const trigger=$(rows[row].trigger);trigger.focus();
  assert.equal(document.activeElement,trigger,`${row}: the button takes focus`);
  trigger.dispatch('click');
  assert.equal(menuKey(),row,`${row}: the menu opens`);
  return trigger;
}
function open(row,value=A){
  openMenu(row);option(value).dispatch('click');
  assert.equal(menuKey(),row,`${row}: the menu stays open`);assert.equal(state('routingChoice?.value'),value,`${row}: the choice opens`);
  return $(rows[row].trigger);
}
function nextTabStop(row,from){
  const children=$(rows[row].row).children.flatMap(child=>[child,...child.children]);
  return children.slice(children.indexOf(from)+1).find(el=>['button','select','input'].includes(el.tag) && !el.hidden && !el.disabled && !el.parentElement?.hidden);
}
function okResult(row,move,status='active'){
  const part={policy:'preferred-account',selected_email:A,preferred_email:A,status};
  if(row==='all')return {codex:part,claude:{...part},...(move?{move}:{})};
  return {...part,...(move?{move}:{})};
}
function applied(){
  const next=snapshot({linked:context.fixture.gateway.routing_linked,codexAccounts:context.fixture.gateway.codex_account_count});
  next.gateway.preference=active('preferred-account',A);next.gateway.claude_preference=active('preferred-account',A);
  return next;
}

(async()=>{
  assert.equal(menuHtml,'<div id="routing-menu" class="menu-pop routing-menu" popover="manual" role="menu" hidden></div>','one menu popover, like the card menu');
  assert.doesNotMatch(html,/routing-dialog|<dialog id="routing/,'no routing modal');
  for(const row of Object.keys(rows)){
    const r=rows[row], trigger=$(r.trigger);
    // The button names its row and shows the policy; the select it replaced stays hidden as the row's value.
    await reset(row);
    assert.equal($(r.row).hidden,false,`${row} row is visible`);
    assert.equal($(r.select).hidden,true);assert.equal(trigger.disabled,false);
    assert.equal(trigger.getAttribute('aria-haspopup'),'menu');assert.equal(trigger.getAttribute('aria-controls'),'routing-menu');
    assert.equal(trigger.getAttribute('aria-labelledby'),`${r.trigger.replace('-trigger','-label')} ${r.trigger}`);
    assert.equal(trigger.getAttribute('aria-expanded'),'false');assert.equal(shown(row),'Prefer '+B);

    // The list: every policy, the current one checked and focused, nothing sent. Arrows move between them.
    $(r.feedback).textContent='Saved';
    openMenu(row);
    assert.equal(trigger.getAttribute('aria-expanded'),'true');
    assert.equal(menu.getAttribute('aria-label'),r.name);
    assert.deepEqual(labels(),['Round-robin','Earliest reset','Higher remaining quota','Prefer '+A,'Prefer '+B]);
    assert.deepEqual(items().map(i=>i.getAttribute('aria-checked')),['false','false','false','false','true']);
    assert.deepEqual(items().map(i=>i.checkMark),[false,false,false,false,true]);
    assert(items().every(i=>i.getAttribute('role')==='menuitemradio'));
    assert.equal(document.activeElement,option(B),`${row}: focus starts on the current policy`);
    assert.equal(posts.length,0);assert.equal($(r.feedback).textContent,'Saved','opening the list changes nothing');
    assert.deepEqual([menu.style.left,menu.style.top,menu.style.minWidth],['900px','130px','208px'],'under the button, left edges aligned');
    press('ArrowDown');assert.equal(document.activeElement,option('round-robin'),'ArrowDown wraps');
    press('End');assert.equal(document.activeElement,option(B));press('Home');assert.equal(document.activeElement,option('round-robin'));
    press('ArrowUp');assert.equal(document.activeElement,option(B));
    // Escape closes it and returns focus to the button; the button toggles it; ArrowDown on the button opens it.
    escape();assert.equal(menuKey(),null);assert.equal(document.activeElement,trigger);assert.equal(trigger.getAttribute('aria-expanded'),'false');
    trigger.dispatch('click');assert.equal(menuKey(),row);trigger.dispatch('click');assert.equal(menuKey(),null);assert.equal(document.activeElement,trigger);
    trigger.dispatch('keydown',key('ArrowDown'));assert.equal(menuKey(),row);assert.equal(document.activeElement,option(B));
    // Tab leaves from the button, as if the menu had not been open.
    press('Tab');assert.equal(menuKey(),null);assert.equal(document.activeElement,trigger);

    // A different policy: the menu turns into its two ways to apply it, in place.
    openMenu(row);option('earliest-weekly-reset').dispatch('click');
    assert.equal(menuKey(),row);assert.equal(posts.length,0,`${row}: picking does not POST`);
    assert.equal($(r.select).value,'earliest-weekly-reset');assert(trigger.classList.contains('pending'));assert.equal(shown(row),'Earliest reset');
    assert.equal($(r.feedback).textContent,'');
    assert.equal($('routing-link').disabled,true,`${row}: switch disabled while choosing`);
    assert.equal($(r.select.replace('-mode','-resume')).disabled,true);
    assert.equal(title(),'Switch to Earliest reset');assert.equal(menu.getAttribute('aria-label'),`${r.name} · Switch to Earliest reset`);
    assert.deepEqual(labels(),['New conversations only','Move all conversations now','Back']);
    assert(items().every(i=>i.getAttribute('role')==='menuitem'));
    assert.equal(document.activeElement,item('new'),`${row}: focus starts on New conversations only`);
    assert.equal(item('move').note,r.note,`${row}: move note`);
    assert.equal(error(),'');
    // Back returns to the list with the picked policy focused and the row's value restored.
    item('back').dispatch('click');
    assert.equal(menuKey(),row);assert.equal(state('routingChoice'),null);assert.equal(option('round-robin').label,'Round-robin');
    assert.equal(document.activeElement,option('earliest-weekly-reset'));assert.equal($(r.select).value,B);assert(!trigger.classList.contains('pending'));
    // A preferred account reads as a preference.
    option(A).dispatch('click');assert.equal(title(),`Switch to prefer ${A}`);
    item('back').dispatch('click');

    // The current policy: posting it again changes nothing, so only the move is offered.
    option(B).dispatch('click');
    assert.equal(menuKey(),row);assert.equal(posts.length,0);assert.equal(state('routingChoice?.keep'),true);
    assert.equal(title(),`Keep preferring ${B}`);
    assert.deepEqual(labels(),['Move all conversations now','Back']);
    assert.equal(document.activeElement,item('move'));assert.equal($(r.select).value,B);assert(trigger.classList.contains('pending'));
    // A refresh keeps it while the policy stays current.
    context.render();assert.equal(menuKey(),row);assert.equal(document.activeElement,item('move'));
    respond=async()=>({...okResult(row,{outcome:'moved',code:'moved'}),...(row==='all' ? {} : {policy:'preferred-account',selected_email:B,preferred_email:B})});
    item('move').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:B,move:true}}],`${row}: Move re-posts the current policy`);
    assert.equal($(r.feedback).textContent,'Saved · conversations moved');assert.equal(menuKey(),null);
    assert.equal(document.activeElement,trigger,`${row}: focus returns to the button after a move`);
    // A refresh showing another policy closes a choice to keep the old one.
    await reset(row);open(row,B);
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');context.render();await flush();
    assert.equal(menuKey(),null);assert.equal(state('routingChoice'),null);assert.equal($(r.select).value,'round-robin');assert.equal(posts.length,0);
    // A suspended routing: posting its saved policy resumes it, so both ways are offered.
    await reset(row);
    for(const p of row==='all' ? ['preference'] : [row==='claude' ? 'claude_preference' : 'preference'])
      context.fixture.gateway[p]={policy:null,status:'disabled',suspended:true,saved_mode:'preferred-account',saved_preferred_email:B};
    context.render();open(row,B);
    assert.equal(title(),`Resume preferring ${B}`);assert.deepEqual(labels(),['New conversations only','Move all conversations now','Back']);
    respond=async()=>okResult(row);item('new').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:B}}]);
    // Without the move helper the current policy has nothing to offer: the menu just closes.
    await reset(row,{available:false});openMenu(row);option(B).dispatch('click');
    assert.equal(menuKey(),null);assert.equal(posts.length,0);assert.equal(document.activeElement,trigger);

    // New conversations only: one POST without a move, then the menu closes and focus returns to the button.
    await reset(row);open(row);respond=async()=>okResult(row);nextSnapshot=applied();
    item('new').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');assert.equal(menuKey(),null);
    assert.equal(document.activeElement,trigger,`${row}: focus returns after New`);assert.equal(shown(row),'Prefer '+A);

    // Escape and a click outside revert the row and close the menu; a click inside it does not.
    await reset(row);open(row);escape();await flush();
    assert.equal(menuKey(),null);assert.equal($(r.select).value,B);assert.equal(document.activeElement,trigger);
    assert(!trigger.classList.contains('pending'));assert.equal(shown(row),'Prefer '+B);
    open(row);context.routingMenuOutside({target:part('routing-menu-title')});assert.equal(menuKey(),row,'a click inside keeps it');
    context.routingMenuOutside({target:trigger.children[0]});assert.equal(menuKey(),row,'the button handles its own clicks');
    outsideClick();await flush();
    assert.equal(menuKey(),null);assert.equal($(r.select).value,B);assert.equal(state('routingChoice'),null);
    // Back reverts to the gateway's current value, not the value when the choice opened.
    open(row);
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');
    item('back').dispatch('click');await flush();
    assert.equal(menuKey(),row);assert.equal($(r.select).value,'round-robin');assert.equal(posts.length,0);
    assert.equal(option('round-robin').getAttribute('aria-checked'),'true');
    escape();assert.equal(document.activeElement,trigger);

    // Switching view cancels; the bare URL is the overview and keeps it.
    await reset(row);open(row);hash='';context.showView();assert.equal(menuKey(),row);
    hash='#overview';context.showView();assert.equal(menuKey(),row);
    hash='#usage';context.showView();await flush();assert.equal(menuKey(),null);assert.equal($(r.select).value,B);hash='';

    // A refresh keeps the menu, its focus and its markup; it never rewrites the error, so nothing is re-announced.
    await reset(row);open(row);const move=item('move');move.focus();menuWrites=0;textWrites=new Map();
    for(let i=0;i<3;i++)context.render();
    assert.equal(menuKey(),row);assert.equal(document.activeElement,move);assert.equal($(r.select).value,A);
    assert.equal(menuWrites,0,`${row}: a refresh rewrites the menu`);assert.equal(textWrites.get('routing-menu-error'),undefined);
    assert.equal(move.getAttribute('aria-disabled'),null);
    // Refresh-cancel rules through render(): disabled row, hidden row, or the pending value applied elsewhere.
    for(const [name,apply] of [
      ['failed',()=>vm.runInContext('failed=true',context)],
      ['read only',()=>{context.fixture.read_only=true;}],
      ['offline',()=>{context.fixture.gateway.online=false;}],
      ['link change',()=>{context.fixture.gateway.routing_linked=!r.linked;}],
      ['applied elsewhere',()=>{context.fixture.gateway.preference=active('preferred-account',A);context.fixture.gateway.claude_preference=active('preferred-account',A);}],
      ['move unavailable',()=>{context.fixture.gateway.move_available=false;}],
      ...(row==='claude' ? [] : [['one Codex account',()=>{context.fixture.gateway.codex_account_count=1;}]])]){
      await reset(row);open(row);apply();context.render();await flush();
      assert.equal(menuKey(),null,`${row}: ${name} cancels`);
      assert(!trigger.classList.contains('pending'));
      if(name!=='applied elsewhere')assert.equal($(r.select).value,B,`${row}: ${name} shows the gateway value`);
      assert.equal(posts.length,0);assert.equal($(r.feedback).textContent,'');
    }
    // The list closes too when its row can no longer change.
    for(const apply of [()=>{context.fixture.read_only=true;},()=>vm.runInContext('routingLinkSaving=true',context)]){
      await reset(row);openMenu(row);apply();context.render();
      assert.equal(menuKey(),null);assert.equal(trigger.disabled,true);
      vm.runInContext('routingLinkSaving=false',context);
    }
    // Choices are never clickable while the row is disabled.
    await reset(row);open(row);vm.runInContext('routingLinkSaving=true',context);context.render();
    assert.equal($(r.select).disabled,true);assert.equal(trigger.disabled,true);assert(items().every(b=>b.getAttribute('aria-disabled')==='true'));
    item('move').dispatch('click');assert.equal(posts.length,0);
    vm.runInContext('routingLinkSaving=false',context);

    // Applying: locked, the chosen item busy and still focused, the menu open across refreshes and Escape.
    for(const choice of ['new','move']){
      await reset(row);open(row);item(choice).focus();let resolve;
      respond=()=>new Promise(done=>{resolve=done;});
      item(choice).dispatch('click');await flush();
      assert.equal(posts.length,1);assert.equal(posts[0].route,r.route);
      assert.equal(posts[0].body.move,choice==='move' ? true : undefined);
      assert.equal(state(r.flag),true);
      assert.equal($(r.select).disabled,true);assert.equal(trigger.disabled,true);assert.equal($('routing-link').disabled,true);
      assert.equal($(r.feedback).textContent,'Applying…');
      assert(items().every(b=>b.getAttribute('aria-disabled')==='true'));
      assert.deepEqual(items().map(b=>b.getAttribute('aria-busy')),items().map(b=>b.dataset.routingChoice===choice ? 'true' : null));
      assert.equal(document.activeElement,item(choice),`${row}: focus stays on the applying choice`);
      escape();press('Tab');await flush();assert.equal(menuKey(),row,`${row}: Escape and Tab wait for the request`);
      assert.equal(document.activeElement,item(choice));
      vm.runInContext('failed=true',context);context.render();vm.runInContext('failed=false',context);
      context.fixture.gateway.routing_linked=!r.linked;context.render();context.fixture.gateway.routing_linked=r.linked;
      assert.equal(menuKey(),row,`${row}: periodic loads keep an applying choice`);
      assert.equal($(r.feedback).textContent,'Applying…');
      nextSnapshot=applied();resolve(okResult(row,choice==='move' ? {outcome:'moved',code:'moved'} : null));await flush();await flush();
      assert.equal(menuKey(),null);assert(!trigger.classList.contains('pending'));
      assert.equal($(r.feedback).textContent,choice==='move' ? 'Saved · conversations moved' : 'Saved');
      assert.equal(state(r.flag),false);assert.equal(document.activeElement,trigger,`${row}: focus returns after ${choice}`);
      assert.equal($(r.retry).hidden,true);
    }

    // Outcomes come from the server's code.
    const outcomes=[
      [okResult(row),'new','Saved',false],
      [okResult(row,null,'held'),'new','Saved · not applied yet',false],
      [row==='all' ? {codex:{status:'active',applied:false},claude:{status:'active'}} : {status:'active',applied:false},'new','Saved · not applied yet',false],
      [row==='all' ? {codex:{status:'active'},claude:{status:'active',pending:true}} : {status:'active',pending:true},'new','Saved · not applied yet',false],
      // Neutral fallback is saved but not in effect, on both paths, as the move gate already treats it.
      [okResult(row,null,'fallback'),'new','Saved · not applied yet',false],
      ...(row==='all' ? [[{codex:{status:'fallback'},claude:{status:'active'}},'new','Saved · not applied yet',false]] : []),
      [okResult(row,{outcome:'not_moved',code:'not_applied'},'fallback'),'move','Saved · not applied yet · conversations not moved',true],
      [okResult(row,{outcome:'moved',code:'moved'}),'move','Saved · conversations moved',false],
      // Try again for every outcome except moved and unavailable: it re-runs every server-side check.
      [okResult(row,{outcome:'not_moved',code:'not_applied'},'held'),'move','Saved · not applied yet · conversations not moved',true],
      ...['too_soon','in_progress','no_time','error','helper_refused','helper_failed','helper_not_started'].map(code=>[okResult(row,{outcome:'not_moved',code}),'move','Saved · conversations not moved',true]),
      [okResult(row,{outcome:'not_moved',code:'unavailable'}),'move','Saved · conversations not moved',false],
      // A retryable flag from an older server no longer decides anything.
      [okResult(row,{outcome:'not_moved',code:'helper_refused',retryable:false}),'move','Saved · conversations not moved',true],
      ...['helper_timeout','reload_not_observed','error'].map(code=>[okResult(row,{outcome:'uncertain',code}),'move','Saved · move not confirmed',true])];
    for(const [result,choice,feedback,retry] of outcomes){
      await reset(row);open(row);respond=async()=>result;
      await context.routingChoiceApply(row,choice);
      assert.equal(menuKey(),null);
      assert.equal($(r.feedback).textContent,feedback,`${row}: ${JSON.stringify(result)}`);
      assert.equal($(r.retry).hidden,!retry,`${row}: Try again for ${JSON.stringify(result.move || result)}`);
    }
    // The state line explains why a fallback result isn't applied.
    await reset(row);open(row);respond=async()=>okResult(row,null,'fallback');
    await context.routingChoiceApply(row,'new');
    const stateLine=$(r.select.replace('-mode','-state')).innerHTML;
    assert.match(stateLine,/<span>Fallback<\/span>.*data-title="(Codex · |Claude · )?Neutral fallback" hidden>Quota data held/,`${row}: fallback state line`);

    // A 400 or 503 carries no move and its body is never read: the menu stays open with the error inside,
    // its choices available again and focus back on the one that failed.
    for(const [status,choice] of [[400,'move'],[503,'new']]){
      await reset(row);open(row);respond=async()=>{throw new Error(`HTTP ${status}`);};
      item(choice).focus();item(choice).dispatch('click');await flush();await flush();
      assert.equal(menuKey(),row,`${row}: HTTP ${status} keeps the menu`);
      assert.equal(error(),ERROR);assert.equal(part('routing-menu-error').getAttribute('role'),'alert');
      assert.equal($(r.feedback).textContent,'',`${row}: the error is not also in the row`);
      assert.equal($(r.retry).hidden,true,`${row}: no Try again after HTTP ${status}`);
      assert(items().every(b=>b.getAttribute('aria-disabled')===null && b.getAttribute('aria-busy')===null));
      assert.equal(state(r.flag),false);
      assert.equal(document.activeElement,item(choice),`${row}: focus returns to ${choice} after a failure`);
      assert.equal($(r.select).value,A);assert(trigger.classList.contains('pending'));
      // A refresh keeps it and doesn't rewrite the error.
      textWrites=new Map();context.render();assert.equal(menuKey(),row);assert.equal(textWrites.get('routing-menu-error'),undefined);
    }
    // A new attempt clears the error and closes on success.
    let resolveRetry;respond=()=>new Promise(done=>{resolveRetry=done;});nextSnapshot=applied();
    item('new').dispatch('click');await flush();assert.equal(error(),'');
    resolveRetry(okResult(row));await flush();await flush();
    assert.equal(menuKey(),null);assert.equal($(r.feedback).textContent,'Saved');assert.equal(document.activeElement,trigger);
    // Back after an error reverts and leaves nothing behind; a new choice starts without the old error.
    await reset(row);open(row);respond=async()=>{throw new Error('HTTP 503');};await context.routingChoiceApply(row,'new');
    assert.equal(error(),ERROR);
    item('back').dispatch('click');await flush();
    assert.equal(menuKey(),row);assert.equal($(r.select).value,B);assert.equal($(r.feedback).textContent,'');
    option(A).dispatch('click');assert.equal(error(),'','a new choice starts without the old error');
    escape();assert.equal(document.activeElement,trigger);
    // A refresh that closes it moves the error to the row.
    await reset(row);open(row);respond=async()=>{throw new Error('HTTP 503');};await context.routingChoiceApply(row,'new');
    vm.runInContext('failed=true',context);context.render();vm.runInContext('failed=false',context);await flush();
    assert.equal(menuKey(),null);assert.equal($(r.feedback).textContent,'Not confirmed · refresh and try again');
    // A click outside mid-request only hides the menu; the row reports the result.
    for(const fails of [true,false]){
      await reset(row);open(row);let settle;
      respond=()=>new Promise((done,fail)=>{settle=fails ? fail : done;});
      item('move').dispatch('click');await flush();outsideClick();await flush();
      assert.equal(menuKey(),null);assert.equal(state('routingChoice?.applying'),'move');assert.equal($(r.feedback).textContent,'Applying…');
      if(!fails)nextSnapshot=applied();
      settle(fails ? new Error('HTTP 503') : okResult(row,{outcome:'moved',code:'moved'}));await flush();await flush();
      assert.equal(menuKey(),null);assert.equal(state('routingChoice'),null);
      assert.equal($(r.feedback).textContent,fails ? 'Not confirmed · refresh and try again' : 'Saved · conversations moved');
      assert.equal($(r.select).value,fails ? B : A);assert.equal(document.activeElement,trigger);
    }
    // Leaving the overview mid-request does the same.
    await reset(row);open(row);let settleView;respond=()=>new Promise(done=>{settleView=done;});
    item('new').dispatch('click');await flush();hash='#usage';context.showView();hash='';
    assert.equal(menuKey(),null);assert.equal(state('routingChoice?.applying'),'new');
    nextSnapshot=applied();settleView(okResult(row));await flush();await flush();
    assert.equal($(r.feedback).textContent,'Saved');assert.equal(state('routingChoice'),null);

    // Try again: next Tab stop, locks through the normal save, never duplicated, removed on success.
    await reset(row);open(row);respond=async()=>okResult(row,{outcome:'uncertain',code:'helper_timeout'});nextSnapshot=applied();
    item('move').focus();await context.routingChoiceApply(row,'move');
    assert.equal(document.activeElement,trigger);
    assert.equal(nextTabStop(row,trigger),$(r.retry),`${row}: Try again is the next Tab stop`);
    assert.equal($(r.row).children.filter(child=>child.id===r.retry).length,1);
    let resolve;respond=()=>new Promise(done=>{resolve=done;});posts=[];
    $(r.retry).focus();$(r.retry).dispatch('click');
    assert.equal(state(r.flag),true,`${row}: retry uses its own saving flag`);
    for(const other of Object.values(rows).filter(other=>other!==r))assert.equal(state(other.flag),false);
    assert.equal($(r.select).disabled,true);assert.equal($(r.retry).hidden,true);assert.equal(menuKey(),null);
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A,move:true}}]);
    $(r.retry).dispatch('click');context.render();assert.equal(posts.length,1,'a locked retry cannot be repeated');
    assert.equal($(r.feedback).textContent,'Applying…');
    resolve(okResult(row,{outcome:'not_moved',code:'too_soon'}));await flush();await flush();
    assert.equal($(r.feedback).textContent,'Saved · conversations not moved');assert.equal($(r.retry).hidden,false);
    assert.equal(document.activeElement,trigger);
    respond=async()=>okResult(row,{outcome:'moved',code:'moved'});
    await context.retryRoutingMove(row);
    assert.equal($(r.feedback).textContent,'Saved · conversations moved');assert.equal($(r.retry).hidden,true);
    // Opening the next choice in the row removes Try again; without a failed move there is nothing to retry.
    await reset(row);open(row);respond=async()=>okResult(row,{outcome:'not_moved',code:'too_soon'});nextSnapshot=applied();
    await context.routingChoiceApply(row,'move');assert.equal($(r.retry).hidden,false);
    open(row,'round-robin');assert.equal($(r.retry).hidden,true);
    vm.runInContext('closeRoutingChoice(true)',context);
    await context.retryRoutingMove(row);assert.equal(posts.length,1,'no retry without a failed move');
    // A refresh showing a different routing removes it too.
    vm.runInContext(`routingRetry.set(${JSON.stringify(row)},${JSON.stringify(A)})`,context);context.render();assert.equal($(r.retry).hidden,false);
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');context.render();
    assert.equal($(r.retry).hidden,true);
    // A rollback suspending the routing removes Try again and its move feedback: the row still shows the saved
    // value, but re-posting it would resume routing. A failed status does the same. The linked row checks both providers.
    const suspended={policy:null,status:'disabled',suspended:true,saved_mode:'preferred-account',saved_preferred_email:A};
    const stale=[['suspended',suspended],['failed',{...active('preferred-account',A),status:'failed'}]];
    for(const [name,stalePart] of stale)for(const provider of row==='all' ? ['preference','claude_preference'] : [row==='claude' ? 'claude_preference' : 'preference']){
      for(const clicked of [false,true]){
        await reset(row);open(row);respond=async()=>okResult(row,{outcome:'not_moved',code:'too_soon'});nextSnapshot=applied();
        await context.routingChoiceApply(row,'move');assert.equal($(r.retry).hidden,false);posts=[];
        context.fixture.gateway[provider]=stalePart;
        assert.equal(context.routingApplied(row),A,`${row}: ${name} ${provider} still shows the retried value`);
        // Clicking before any refresh re-checks the same condition and never posts.
        if(clicked)await context.retryRoutingMove(row);else context.render();
        assert.equal(posts.length,0,`${row}: ${name} ${provider} never re-posts`);
        assert.equal($(r.retry).hidden,true,`${row}: ${name} ${provider} removes Try again`);
        assert.equal($(r.feedback).textContent,'',`${row}: ${name} ${provider} removes the move feedback`);
        assert.equal(state(`routingRetry.has(${JSON.stringify(row)})`),false);
      }
    }
    // Held and fallback keep it: the retry re-runs the server's checks and reports not_applied again.
    await reset(row);open(row);respond=async()=>okResult(row,{outcome:'not_moved',code:'not_applied'},'held');nextSnapshot=applied();
    await context.routingChoiceApply(row,'move');
    context.fixture.gateway.preference.status='held';context.fixture.gateway.claude_preference.status='fallback';context.render();
    assert.equal($(r.retry).hidden,false,`${row}: held keeps Try again`);

    // Without the helper the menu applies a pick at once, as the select did.
    await reset(row,{available:false});respond=async()=>okResult(row);nextSnapshot=applied();
    openMenu(row);option(A).dispatch('click');
    assert.equal(menuKey(),null);await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');assert.equal(document.activeElement,trigger);
  }

  // Linked, Codex and Claude on different policies: posting the shared one brings Claude onto it, so both ways stay.
  await reset('all');context.fixture.gateway.claude_preference=active('round-robin');context.render();
  open('all',B);assert.equal(title(),`Switch to prefer ${B}`);assert.deepEqual(labels(),['New conversations only','Move all conversations now','Back']);
  respond=async()=>okResult('all');item('new').dispatch('click');await flush();await flush();
  assert.deepEqual(posts,[{route:'/api/routing-all',body:{mode:'preferred-account',preferred_email:B}}]);

  // An unavailable policy stays listed but inert.
  await reset('codex');$('routing-mode').options[1].disabled=true;openMenu('codex');
  assert.equal(option('earliest-weekly-reset').getAttribute('aria-disabled'),'true');
  option('earliest-weekly-reset').dispatch('click');assert.equal(state('routingChoice'),null);assert.equal(menuKey(),'codex');
  delete $('routing-mode').options[1].disabled;escape();

  // Near the foot of a phone screen the menu opens above its button, at least as wide, and stays on screen.
  await reset('claude');
  const rect=$('claude-routing-trigger').getBoundingClientRect;
  Object.assign(context,{innerWidth:390,innerHeight:700});$('claude-routing-trigger').getBoundingClientRect=()=>({left:20,right:370,top:640,bottom:676,width:350,height:36});
  openMenu('claude');
  assert.deepEqual([menu.style.left,menu.style.top,menu.style.minWidth],['20px','484px','350px']);
  escape();Object.assign(context,{innerWidth:1440,innerHeight:900});$('claude-routing-trigger').getBoundingClientRect=rect;

  // A link change, either way, drops every row's old Try again and move feedback, so unlinking never revives them.
  const linkRespond=linked=>async(route,options)=>route==='/api/routing-link' ? {linked} :
    okResult(route==='/api/routing-all' ? 'all' : 'codex',JSON.parse(options.body).move ? {outcome:'not_moved',code:'too_soon'} : null);
  for(const [row,first,second] of [['codex',true,false],['claude',true,false],['all',false,true]]){
    await reset(row);open(row);nextSnapshot=applied();respond=linkRespond(first);
    await context.routingChoiceApply(row,'move');
    assert.equal($(rows[row].retry).hidden,false);assert.equal($(rows[row].feedback).textContent,'Saved · conversations not moved');
    nextSnapshot=null;
    await context.saveRoutingLink(first);respond=linkRespond(second);await context.saveRoutingLink(second);
    assert.equal(state('routingLinked(current.gateway)'),second);assert.equal($(rows[row].row).hidden,false,`${row} row shown again`);
    assert.equal($(rows[row].retry).hidden,true,`${row}: no stale Try again after link and unlink`);
    // Relinking reapplies the shared routing, so the linked row shows that save's own result.
    assert.equal($(rows[row].feedback).textContent,second ? 'Saved' : '',`${row}: no stale move feedback after link and unlink`);
    assert.equal(state(`routingRetry.has(${JSON.stringify(row)})`),false);
  }
  // A link change made elsewhere, seen on refresh, clears it the same way.
  await reset('codex');open('codex');nextSnapshot=applied();respond=async()=>okResult('codex',{outcome:'not_moved',code:'too_soon'});
  await context.routingChoiceApply('codex','move');assert.equal($('routing-retry').hidden,false);
  context.fixture.gateway.routing_linked=true;context.render();context.fixture.gateway.routing_linked=false;context.render();
  assert.equal($('routing-retry').hidden,true);assert.equal($('routing-feedback').textContent,'');

  // Layout: the switch ends the label line, after the save feedback and ahead of the button and its state.
  await reset('claude');
  const order=$('claude-routing').children.map(child=>child.id);
  assert(order.indexOf('claude-routing-feedback')<order.indexOf('routing-split'));
  assert(order.indexOf('routing-split')<order.indexOf('claude-routing-trigger'),order.join(' '));
  assert(order.indexOf('claude-routing-trigger')<order.indexOf('claude-routing-state'),order.join(' '));
  const linkedOrder=(await reset('all'),$('routing-all').children.map(child=>child.id));
  assert(linkedOrder.indexOf('routing-split')<linkedOrder.indexOf('routing-all-state'),linkedOrder.join(' '));

  // Only one menu is open at a time: opening another row's closes the first and reverts its choice.
  await reset('codex');open('codex');
  $('claude-routing-trigger').dispatch('click');assert.equal(menuKey(),'claude');
  assert.equal($('routing-mode').value,B);assert(!$('routing-trigger').classList.contains('pending'));
  assert.equal($('routing-trigger').getAttribute('aria-expanded'),'false');
  option(A).dispatch('click');assert.equal(title(),`Switch to prefer ${A}`);assert.equal(state('routingChoice.key'),'claude');

  // One configured Codex account, linked or not: only Claude routing shows, without the switch, so no Codex button
  // offers the retired account. Its menu posts for Claude alone and claims no Codex move.
  for(const linked of [true,false]){
    await reset('claude',{linked,codexAccounts:1});
    assert.equal($('codex-routing').hidden,true);assert.equal($('routing-all').hidden,true);
    assert.equal($('claude-routing').hidden,false);assert.equal($('routing-split').hidden,true);
    assert.deepEqual($('claude-routing-mode').options.map(o=>o.value).slice(3),[A,B],'Claude keeps both preferred accounts');
    open('claude','earliest-weekly-reset');
    assert.equal(title(),'Switch to Earliest reset');
    assert.equal(item('move').label,'Move all conversations now');assert.equal(item('move').note,'');
    respond=async()=>({policy:'earliest-weekly-reset',status:'active',move:{outcome:'moved',code:'moved'}});
    item('move').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:'/api/claude-routing',body:{mode:'earliest-weekly-reset',move:true}}]);
    assert.equal($('claude-routing-feedback').textContent,'Saved · conversations moved');
    assert.equal(menuKey(),null);assert.equal(document.activeElement,$('claude-routing-trigger'));
  }
  // A second configured account brings the Codex choice back; an older backend without the count keeps it.
  for(const codexAccounts of [2,null]){
    await reset('all',{codexAccounts});
    assert.equal($('routing-all').hidden,false);assert.equal($('routing-split').hidden,false);assert.equal($('claude-routing').hidden,true);
    await reset('codex',{codexAccounts});
    assert.equal($('codex-routing').hidden,false);assert.equal($('claude-routing').hidden,false);
  }

  // Removed account history remains separate from current filename-keyed series.
  {
    const {historyKeys}=require('./helpers/board-widgets.cjs');
    await reset('all',{codexAccounts:1});
    context.fixture.gateway.accounts=context.fixture.gateway.accounts.filter(a=>a.email===A);
    assert.equal(context.series('codex','codex-b'),'codex:removed:codex-b');
    for(const requests of [3,0]){
      context.fixture.usage={hours:[{auth_index:'codex-b',requests}]};vm.runInContext('today=current',context);
      const keys=historyKeys(context);assert.equal(keys.some(k=>k.name==='Removed account'),requests>0);assert(keys.some(k=>k.name===A));
    }
  }

  // System page: both values a move writes read as the configured 1h.
  {
    const querySelector=document.querySelector;document.querySelector=()=>({textContent:''});
    $('advanced-link').closest=()=>({hidden:false});
    for(const [ttl,ttlShown] of [['1h0m0s','1h'],['1h0m1s','1h'],['1h','1h'],['2h','2h']]){
      await reset('all');
      context.realRenderSystem({...context.fixture,usage:{available:true},
        gateway:{...context.fixture.gateway,routing:{'session-affinity':true,'session-affinity-ttl':ttl}}});
      assert.match($('system-details').innerHTML,new RegExp(`<dt>Session affinity</dt><dd>Enabled · ${ttlShown}</dd>`),ttl);
    }
    document.querySelector=querySelector;
  }

  assert.doesNotMatch(html,/<option[^>]+value="[^"]*@/);
  console.log('Routing menu UI: list, keyboard, current-policy move, Back/Escape/outside/focus, errors inside the menu, refresh rules via render(), applying lock, outcomes, Try again, one configured Codex account, the retired chart series and the System TTL passed for all three rows');
})().catch(error=>{console.error(error);process.exitCode=1;});
