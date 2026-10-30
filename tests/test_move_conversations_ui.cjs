'use strict';
// Executes the routing-change modal through the real app.js and render() with a small DOM built from index.html.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const A='account-a@example.com', B='account-b@example.com';
const OPTIONS=['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B];
const LABELS={'round-robin':'Round-robin','earliest-weekly-reset':'Earliest reset','highest-remaining-quota':'Higher remaining quota'};
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
const elements=new Map();
let textWrites=new Map(), dialog=null;

function element(id, className='', tag='div'){
  const events={}, attributes={}, classes=new Set(className.split(/\s+/).filter(Boolean));
  let text='';
  const el={id,tag,hidden:false,disabled:false,value:'',innerHTML:'',checked:false,parentElement:null,children:[],dataset:{},events,
    title:'',
    get textContent(){return text;},
    set textContent(value){text=String(value);textWrites.set(id,(textWrites.get(id) || 0)+1);},
    addEventListener(type,fn){(events[type] ||= []).push(fn);},
    // Events bubble from the target through its ancestors, as click does.
    dispatch(type,event={}){for(let node=el;node;node=node.parentElement)for(const fn of node.events[type] || [])fn({target:el,preventDefault(){},...event});},
    setAttribute(name,value){attributes[name]=String(value);},getAttribute(name){return name in attributes ? attributes[name] : null;},
    removeAttribute(name){delete attributes[name];},
    // An open modal leaves everything outside it inert; a closed one's own controls can't take focus.
    focus(){if(!el.disabled && !el.hidden && !!dialog?.contains(el)===!!dialog?.open)document.activeElement=el;},
    options:OPTIONS.map(value=>({value,textContent:LABELS[value] || 'Prefer '+value})),
    classList:{add(name){classes.add(name);},remove(name){classes.delete(name);},toggle(name,force){if(force ?? !classes.has(name))classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)},
    contains(other){for(let node=other;node;node=node.parentElement)if(node===el)return true;return false;},
    closest(selector){
      for(let node=el;node;node=node.parentElement){
        if(selector==='[data-routing-choice]' && node.dataset?.routingChoice)return node;
      }
      return null;
    },
    querySelector(selector){
      const choice=selector.match(/^\[data-routing-choice="(\w+)"\]$/);
      if(choice)return el.children.find(child=>child.dataset.routingChoice===choice[1]) || null;
      const names=selector.split(',').map(part=>part.trim().replace(/^\./,''));
      return el.children.find(child=>names.some(name=>child.classList.contains(name))) || null;
    },
    querySelectorAll(selector){
      assert.equal(selector,'[data-routing-choice]');
      return el.children.filter(child=>child.dataset.routingChoice);
    },
    before(node){
      if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(child=>child!==node);
      const siblings=el.parentElement.children;siblings.splice(siblings.indexOf(el),0,node);node.parentElement=el.parentElement;
    }
  };
  // Browsers drop focus to the body when the focused control is disabled or hidden.
  for(const name of ['disabled','hidden']){
    let value=el[name];
    Object.defineProperty(el,name,{get:()=>value,set(next){value=!!next;if(value && document.activeElement===el)document.activeElement=document.body;}});
  }
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
    const parent=id==='routing-link' ? $('routing-split') : $(row);
    el.parentElement=parent;parent.children.push(el);
  }
}
$('watch-section').hidden=false;
// The routing modal from index.html, opened and closed as a browser does: closing a modal returns focus to the
// element focused when it opened, if that can take it, and the close event follows as a task.
const dialogStart=html.indexOf('<dialog id="routing-dialog"'), dialogHtml=html.slice(dialogStart,html.indexOf('</dialog>',dialogStart));
dialog=element('routing-dialog','routing-dialog','dialog');elements.set('routing-dialog',dialog);
for(const [,tag,attrs,label] of dialogHtml.slice(dialogHtml.indexOf('>')+1).matchAll(/<(h2|p|button)([^>]*)>([^<]*)/g)){
  const id=(attrs.match(/\bid="([^"]+)"/) || [,''])[1], child=element(id,(attrs.match(/class="([^"]*)"/) || [,''])[1],tag);
  child.hidden=/\shidden(\s|$)/.test(attrs);
  const choice=attrs.match(/data-routing-choice="(\w+)"/);if(choice){child.dataset.routingChoice=choice[1];child.textContent=label;}
  const described=attrs.match(/aria-describedby="([^"]+)"/);if(described)child.setAttribute('aria-describedby',described[1]);
  child.parentElement=dialog;dialog.children.push(child);
  if(id)elements.set(id,child);
}
Object.assign(dialog,{open:false,opens:0,opener:null,
  showModal(){assert.equal(dialog.open,false,'showModal on an open dialog throws');dialog.open=true;dialog.opens++;dialog.opener=document.activeElement;},
  close(){
    if(!dialog.open)return;
    dialog.open=false;document.activeElement=document.body;dialog.opener?.focus?.();
    setImmediate(()=>dialog.dispatch('close'));
  },
  getBoundingClientRect:()=>({left:100,right:480,top:200,bottom:420})});
// Escape asks through cancel first; a browser can also close a modal without asking (a repeated Escape).
function escape(){let prevented=false;dialog.dispatch('cancel',{preventDefault(){prevented=true;}});if(!prevented)dialog.close();}

let hash='';
const context=vm.createContext({document,Date,JSON,Promise,Map,Set,Intl,URLSearchParams,localStorage:{getItem:()=>null},
  location:{get hash(){return hash;}},setTimeout:()=>1,clearTimeout(){},AbortSignal:{timeout:()=>1}});
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
// Only the routing renderers run for real; the rest of render() is outside this feature.
vm.runInContext('var realRenderSystem=renderSystem, realRenderLegend=renderLegend;',context);
vm.runInContext(`renderOperations=()=>{};renderWatch=()=>{};renderLegend=()=>{};renderResetModal=()=>{};renderToday=()=>{};renderUsage=()=>{};renderSystem=()=>{};
  renderAccounts=gateway=>{renderRouting(gateway);renderRouting(gateway,'claude');};`,context);

const active=(policy,email)=>({policy,status:'active',selected_email:email,preferred_email:email,saved_mode:policy,saved_preferred_email:email});
// Both Codex credentials stay listed, as the retired one does until deploy; only the configured count decides.
function snapshot({linked=true,available=true,codexAccounts=2}={}){
  return {gateway:{online:true,routing_linked:linked,move_available:available,checked_at:new Date().toISOString(),
    accounts:[{id:'codex-a',email:A},{id:'codex-b',email:B,disabled:codexAccounts<2}],
    ...(codexAccounts==null ? {} : {codex_account_count:codexAccounts}),
    preference:active('preferred-account',B),claude_preference:active('preferred-account',B)},
    usage:{},health:{},watch_accounts:[{id:'claude',email:A},{id:'claude-b',email:B}]};
}
const rows={
  codex:{row:'codex-routing',select:'routing-mode',feedback:'routing-feedback',retry:'routing-retry',route:'/api/routing',flag:'routingSaving',linked:false,
    name:'Codex routing',note:'Also moves Claude conversations.'},
  claude:{row:'claude-routing',select:'claude-routing-mode',feedback:'claude-routing-feedback',retry:'claude-routing-retry',route:'/api/claude-routing',flag:'claudeRoutingSaving',linked:false,
    name:'Claude routing',note:'Also moves Codex conversations.'},
  all:{row:'routing-all',select:'routing-all-mode',feedback:'routing-all-feedback',retry:'routing-all-retry',route:'/api/routing-all',flag:'routingAllSaving',linked:true,
    name:'routing',note:''}
};
let posts=[], respond=async()=>({}), refreshes=0, nextSnapshot=null;
context.request=async(route,options)=>{posts.push({route,body:JSON.parse(options.body)});return respond(route,options);};
context.load=async()=>{refreshes++;if(nextSnapshot){context.fixture=nextSnapshot;vm.runInContext('current=fixture',context);}context.render();};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const state=name=>vm.runInContext(name,context);
const buttons=()=>dialog.children.filter(child=>child.dataset.routingChoice);
const button=choice=>buttons().find(b=>b.dataset.routingChoice===choice);
const text=id=>$(id).textContent;
const outsideClick=()=>dialog.dispatch('click',{clientX:40,clientY:300});
const ERROR='Change not confirmed. Try again.';

async function reset(key,options={}){
  vm.runInContext('closeRoutingChoice(true);routingRetry.clear();routingSaving=false;claudeRoutingSaving=false;routingAllSaving=false;routingLinkSaving=false;failed=false',context);
  await flush();
  posts=[];respond=async()=>({});nextSnapshot=null;hash='';
  context.fixture=snapshot({linked:rows[key].linked,...options});vm.runInContext('current=fixture',context);context.render();
  for(const row of Object.values(rows)){$(row.feedback).textContent='';}
  document.activeElement=document.body;textWrites=new Map();
}
function change(key,value){$(rows[key].select).value=value;$(rows[key].select).dispatch('change');}
function open(key,value=A){
  const select=$(rows[key].select);select.focus();
  assert.equal(document.activeElement,select,`${key}: the select takes focus before the modal opens`);
  change(key,value);
  assert.equal(dialog.open,true,`${key}: the modal opens`);
  return select;
}
function nextTabStop(key,from){
  const children=$(rows[key].row).children.flatMap(child=>[child,...child.children]);
  return children.slice(children.indexOf(from)+1).find(el=>['button','select','input'].includes(el.tag) && !el.hidden && !el.disabled && !el.parentElement?.hidden);
}
function okResult(key,move,status='active'){
  const part={policy:'preferred-account',selected_email:A,preferred_email:A,status};
  if(key==='all')return {codex:part,claude:{...part},...(move?{move}:{})};
  return {...part,...(move?{move}:{})};
}
function applied(){
  const next=snapshot({linked:context.fixture.gateway.routing_linked,codexAccounts:context.fixture.gateway.codex_account_count});
  next.gateway.preference=active('preferred-account',A);next.gateway.claude_preference=active('preferred-account',A);
  return next;
}

(async()=>{
  assert.match(html,/<dialog id="routing-dialog" class="routing-dialog" aria-labelledby="routing-dialog-title">/,'the modal is named by its title');
  for(const key of Object.keys(rows)){
    const r=rows[key];
    // Opening: a change opens the modal on the primary choice, names the change and sends nothing.
    await reset(key);
    assert.equal($(r.row).hidden,false,`${key} row is visible`);
    $(r.feedback).textContent='Saved';
    const opens=dialog.opens, select=open(key,'earliest-weekly-reset');
    assert.equal(dialog.opens,opens+1);
    assert.equal(posts.length,0,`${key}: change does not POST`);
    assert.equal(select.value,'earliest-weekly-reset');assert(select.classList.contains('pending'));
    assert.equal($(r.feedback).textContent,'');
    assert.equal($('routing-link').disabled,true,`${key}: switch disabled while choosing`);
    assert.equal($(r.select.replace('-mode','-resume')).disabled,true);
    assert.equal(text('routing-dialog-title'),`Switch ${r.name} to Earliest reset?`);
    assert.deepEqual(buttons().map(b=>b.textContent),['New conversations only','Move all conversations now','Cancel']);
    assert.equal(document.activeElement,button('new'),`${key}: focus starts on New conversations only`);
    assert.equal(button('move').getAttribute('aria-describedby'),'routing-dialog-note');
    assert.equal(text('routing-dialog-note'),r.note,`${key}: move note`);assert.equal($('routing-dialog-note').hidden,!r.note);
    assert.equal(text('routing-dialog-error'),'');
    // Focus can't leave it: everything behind the modal is inert.
    select.focus();assert.equal(document.activeElement,button('new'));
    // Tab wraps from Cancel to the first choice, and Shift+Tab back.
    button('cancel').focus();button('cancel').dispatch('keydown',{key:'Tab',shiftKey:false});assert.equal(document.activeElement,button('new'));
    button('new').dispatch('keydown',{key:'Tab',shiftKey:true});assert.equal(document.activeElement,button('cancel'));
    button('move').focus();button('move').dispatch('keydown',{key:'Tab',shiftKey:false});assert.equal(document.activeElement,button('move'),'Tab between choices stays native');
    button('new').focus();
    // A preferred account reads as a preference; the applied value closes it.
    change(key,A);assert.equal(text('routing-dialog-title'),`Switch ${r.name} to prefer ${A}?`);assert.equal(dialog.opens,opens+1);
    change(key,B);await flush();assert.equal(dialog.open,false);assert.equal(select.value,B);assert(!select.classList.contains('pending'));
    assert.equal(posts.length,0);

    // New conversations only: one POST without a move, then the modal closes and focus returns to the select.
    await reset(key);open(key);respond=async()=>okResult(key);nextSnapshot=applied();
    button('new').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');assert.equal(dialog.open,false);
    assert.equal(document.activeElement,$(r.select),`${key}: focus returns after New`);

    // Escape, a click on the backdrop and Cancel revert the select and return focus to it; a click inside the box does not.
    await reset(key);open(key);escape();await flush();
    assert.equal(dialog.open,false);assert.equal($(r.select).value,B);assert.equal(document.activeElement,$(r.select));
    assert(!$(r.select).classList.contains('pending'));
    open(key);dialog.dispatch('click',{clientX:300,clientY:300});await flush();assert.equal(dialog.open,true,'a click inside the box keeps it');
    outsideClick();await flush();
    assert.equal(dialog.open,false);assert.equal($(r.select).value,B);assert.equal(document.activeElement,$(r.select));
    // Cancel reverts to the gateway's current value, not the value when the choice opened.
    open(key);
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');
    button('cancel').dispatch('click');await flush();
    assert.equal(dialog.open,false);assert.equal($(r.select).value,'round-robin');assert.equal(posts.length,0);
    assert.equal(document.activeElement,$(r.select));

    // Switching view cancels; the bare URL is the overview and keeps it.
    await reset(key);open(key);hash='';context.showView();assert.equal(dialog.open,true);
    hash='#overview';context.showView();assert.equal(dialog.open,true);
    hash='#usage';context.showView();await flush();assert.equal(dialog.open,false);assert.equal($(r.select).value,B);hash='';

    // A refresh keeps the modal, its focus and its text; it never rewrites the error, so nothing is re-announced.
    await reset(key);open(key);const move=button('move');move.focus();textWrites=new Map();
    for(let i=0;i<3;i++)context.render();
    assert.equal(dialog.open,true);assert.equal(document.activeElement,move);assert.equal($(r.select).value,A);
    for(const id of ['routing-dialog-title','routing-dialog-error','routing-dialog-note'])assert.equal(textWrites.get(id),undefined,`${key}: a refresh rewrites ${id}`);
    assert.equal(move.disabled,false);
    // Refresh-cancel rules through render(): disabled select, hidden row, or the pending value applied elsewhere.
    for(const [name,apply] of [
      ['failed',()=>vm.runInContext('failed=true',context)],
      ['read only',()=>{context.fixture.read_only=true;}],
      ['offline',()=>{context.fixture.gateway.online=false;}],
      ['link change',()=>{context.fixture.gateway.routing_linked=!r.linked;}],
      ['applied elsewhere',()=>{context.fixture.gateway.preference=active('preferred-account',A);context.fixture.gateway.claude_preference=active('preferred-account',A);}],
      ['move unavailable',()=>{context.fixture.gateway.move_available=false;}],
      ...(key==='claude' ? [] : [['one Codex account',()=>{context.fixture.gateway.codex_account_count=1;}]])]){
      await reset(key);open(key);apply();context.render();await flush();
      assert.equal(dialog.open,false,`${key}: ${name} cancels`);
      assert(!$(r.select).classList.contains('pending'));
      if(name!=='applied elsewhere')assert.equal($(r.select).value,B,`${key}: ${name} shows the gateway value`);
      assert.equal(posts.length,0);assert.equal($(r.feedback).textContent,'');
    }
    // Buttons are never clickable while the select is disabled.
    await reset(key);open(key);vm.runInContext('routingLinkSaving=true',context);context.render();
    assert.equal($(r.select).disabled,true);assert(buttons().every(b=>b.disabled));
    button('move').dispatch('click');assert.equal(posts.length,0);
    vm.runInContext('routingLinkSaving=false',context);

    // Applying: locked, chosen button busy, modal open across refreshes and Escape, then outcome and focus.
    for(const choice of ['new','move']){
      await reset(key);open(key);button(choice).focus();let resolve;
      respond=()=>new Promise(done=>{resolve=done;});
      button(choice).dispatch('click');await flush();
      assert.equal(posts.length,1);assert.equal(posts[0].route,r.route);
      assert.equal(posts[0].body.move,choice==='move' ? true : undefined);
      assert.equal(state(r.flag),true);
      assert.equal($(r.select).disabled,true);assert.equal($('routing-link').disabled,true);
      assert.equal($(r.feedback).textContent,'Applying…');
      assert(buttons().every(b=>b.disabled));
      assert.deepEqual(buttons().map(b=>b.getAttribute('aria-busy')),buttons().map(b=>b.dataset.routingChoice===choice ? 'true' : null));
      escape();outsideClick();await flush();assert.equal(dialog.open,true,`${key}: Escape and the backdrop wait for the request`);
      vm.runInContext('failed=true',context);context.render();vm.runInContext('failed=false',context);
      context.fixture.gateway.routing_linked=!r.linked;context.render();context.fixture.gateway.routing_linked=r.linked;
      hash='#usage';context.showView();hash='';await flush();
      assert.equal(dialog.open,true,`${key}: periodic loads keep an applying choice`);
      assert.equal($(r.feedback).textContent,'Applying…');
      nextSnapshot=applied();resolve(okResult(key,choice==='move' ? {outcome:'moved',code:'moved'} : null));await flush();await flush();
      assert.equal(dialog.open,false);assert(!$(r.select).classList.contains('pending'));
      assert.equal($(r.feedback).textContent,choice==='move' ? 'Saved · conversations moved' : 'Saved');
      assert.equal(state(r.flag),false);assert.equal(document.activeElement,$(r.select),`${key}: focus returns after ${choice}`);
      assert.equal($(r.retry).hidden,true);
    }

    // Outcomes come from the server's code.
    const outcomes=[
      [okResult(key),'new','Saved',false],
      [okResult(key,null,'held'),'new','Saved · not applied yet',false],
      [key==='all' ? {codex:{status:'active',applied:false},claude:{status:'active'}} : {status:'active',applied:false},'new','Saved · not applied yet',false],
      [key==='all' ? {codex:{status:'active'},claude:{status:'active',pending:true}} : {status:'active',pending:true},'new','Saved · not applied yet',false],
      // Neutral fallback is saved but not in effect, on both paths, as the move gate already treats it.
      [okResult(key,null,'fallback'),'new','Saved · not applied yet',false],
      ...(key==='all' ? [[{codex:{status:'fallback'},claude:{status:'active'}},'new','Saved · not applied yet',false]] : []),
      [okResult(key,{outcome:'not_moved',code:'not_applied'},'fallback'),'move','Saved · not applied yet · conversations not moved',true],
      [okResult(key,{outcome:'moved',code:'moved'}),'move','Saved · conversations moved',false],
      // Try again for every outcome except moved and unavailable: it re-runs every server-side check.
      [okResult(key,{outcome:'not_moved',code:'not_applied'},'held'),'move','Saved · not applied yet · conversations not moved',true],
      ...['too_soon','in_progress','no_time','error','helper_refused','helper_failed','helper_not_started'].map(code=>[okResult(key,{outcome:'not_moved',code}),'move','Saved · conversations not moved',true]),
      [okResult(key,{outcome:'not_moved',code:'unavailable'}),'move','Saved · conversations not moved',false],
      // A retryable flag from an older server no longer decides anything.
      [okResult(key,{outcome:'not_moved',code:'helper_refused',retryable:false}),'move','Saved · conversations not moved',true],
      ...['helper_timeout','reload_not_observed','error'].map(code=>[okResult(key,{outcome:'uncertain',code}),'move','Saved · move not confirmed',true])];
    for(const [result,choice,feedback,retry] of outcomes){
      await reset(key);open(key);respond=async()=>result;
      await context.routingChoiceApply(key,choice);
      assert.equal(dialog.open,false);
      assert.equal($(r.feedback).textContent,feedback,`${key}: ${JSON.stringify(result)}`);
      assert.equal($(r.retry).hidden,!retry,`${key}: Try again for ${JSON.stringify(result.move || result)}`);
    }
    // The state line explains why a fallback result isn't applied.
    await reset(key);open(key);respond=async()=>okResult(key,null,'fallback');
    await context.routingChoiceApply(key,'new');
    const stateLine=key==='all' ? $('routing-all-state').innerHTML : $(r.select.replace('-mode','-state')).textContent;
    assert.match(stateLine,/Neutral fallback · quota data held/,`${key}: fallback state line`);

    // A 400 or 503 carries no move and its body is never read: the modal stays open with the error inside,
    // its choices available again and focus back on the one that failed.
    for(const [status,choice] of [[400,'move'],[503,'new']]){
      await reset(key);open(key);respond=async()=>{throw new Error(`HTTP ${status}`);};
      button(choice).focus();button(choice).dispatch('click');await flush();await flush();
      assert.equal(dialog.open,true,`${key}: HTTP ${status} keeps the modal`);
      assert.equal(text('routing-dialog-error'),ERROR);
      assert.equal($(r.feedback).textContent,'',`${key}: the error is not also in the row`);
      assert.equal($(r.retry).hidden,true,`${key}: no Try again after HTTP ${status}`);
      assert(buttons().every(b=>!b.disabled && b.getAttribute('aria-busy')===null));
      assert.equal(state(r.flag),false);
      assert.equal(document.activeElement,button(choice),`${key}: focus returns to ${choice} after a failure`);
      assert.equal($(r.select).value,A);assert($(r.select).classList.contains('pending'));
      // A refresh keeps it and doesn't rewrite the error.
      textWrites=new Map();context.render();assert.equal(dialog.open,true);assert.equal(textWrites.get('routing-dialog-error'),undefined);
    }
    // A new attempt clears the error and closes on success.
    let resolveRetry;respond=()=>new Promise(done=>{resolveRetry=done;});nextSnapshot=applied();
    button('new').dispatch('click');await flush();assert.equal(text('routing-dialog-error'),'');
    resolveRetry(okResult(key));await flush();await flush();
    assert.equal(dialog.open,false);assert.equal($(r.feedback).textContent,'Saved');assert.equal(document.activeElement,$(r.select));
    // Cancel after an error reverts and leaves nothing behind.
    await reset(key);open(key);respond=async()=>{throw new Error('HTTP 503');};await context.routingChoiceApply(key,'new');
    assert.equal(text('routing-dialog-error'),ERROR);
    button('cancel').dispatch('click');await flush();
    assert.equal(dialog.open,false);assert.equal($(r.select).value,B);assert.equal($(r.feedback).textContent,'');
    assert.equal(document.activeElement,$(r.select));
    open(key);assert.equal(text('routing-dialog-error'),'','a new choice starts without the old error');
    // A refresh that closes it moves the error to the row.
    await reset(key);open(key);respond=async()=>{throw new Error('HTTP 503');};await context.routingChoiceApply(key,'new');
    vm.runInContext('failed=true',context);context.render();vm.runInContext('failed=false',context);await flush();
    assert.equal(dialog.open,false);assert.equal($(r.feedback).textContent,'Not confirmed · refresh and try again');
    // A browser closing it mid-request (a repeated Escape) leaves the result to the row.
    for(const fails of [true,false]){
      await reset(key);open(key);let settle;
      respond=()=>new Promise((done,fail)=>{settle=fails ? fail : done;});
      button('move').dispatch('click');await flush();dialog.close();await flush();
      assert.equal(state('routingChoice?.applying'),'move');assert.equal($(r.feedback).textContent,'Applying…');
      if(!fails)nextSnapshot=applied();
      settle(fails ? new Error('HTTP 503') : okResult(key,{outcome:'moved',code:'moved'}));await flush();await flush();
      assert.equal(dialog.open,false);assert.equal(state('routingChoice'),null);
      assert.equal($(r.feedback).textContent,fails ? 'Not confirmed · refresh and try again' : 'Saved · conversations moved');
      assert.equal($(r.select).value,fails ? B : A);assert.equal(document.activeElement,$(r.select));
    }

    // Try again: next Tab stop, locks through the normal save, never duplicated, removed on success.
    await reset(key);open(key);respond=async()=>okResult(key,{outcome:'uncertain',code:'helper_timeout'});nextSnapshot=applied();
    button('move').focus();await context.routingChoiceApply(key,'move');
    assert.equal(document.activeElement,$(r.select));
    assert.equal(nextTabStop(key,$(r.select)),$(r.retry),`${key}: Try again is the next Tab stop`);
    assert.equal($(r.row).children.filter(child=>child.id===r.retry).length,1);
    let resolve;respond=()=>new Promise(done=>{resolve=done;});posts=[];
    $(r.retry).focus();$(r.retry).dispatch('click');
    assert.equal(state(r.flag),true,`${key}: retry uses its own saving flag`);
    for(const other of Object.values(rows).filter(other=>other!==r))assert.equal(state(other.flag),false);
    assert.equal($(r.select).disabled,true);assert.equal($(r.retry).hidden,true);assert.equal(dialog.open,false);
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A,move:true}}]);
    $(r.retry).dispatch('click');context.render();assert.equal(posts.length,1,'a locked retry cannot be repeated');
    assert.equal($(r.feedback).textContent,'Applying…');
    resolve(okResult(key,{outcome:'not_moved',code:'too_soon'}));await flush();await flush();
    assert.equal($(r.feedback).textContent,'Saved · conversations not moved');assert.equal($(r.retry).hidden,false);
    assert.equal(document.activeElement,$(r.select));
    respond=async()=>okResult(key,{outcome:'moved',code:'moved'});
    await context.retryRoutingMove(key);
    assert.equal($(r.feedback).textContent,'Saved · conversations moved');assert.equal($(r.retry).hidden,true);
    // Opening the next choice in the row removes Try again; without a failed move there is nothing to retry.
    await reset(key);open(key);respond=async()=>okResult(key,{outcome:'not_moved',code:'too_soon'});nextSnapshot=applied();
    await context.routingChoiceApply(key,'move');assert.equal($(r.retry).hidden,false);
    open(key,'round-robin');assert.equal($(r.retry).hidden,true);
    vm.runInContext('closeRoutingChoice(true)',context);
    await context.retryRoutingMove(key);assert.equal(posts.length,1,'no retry without a failed move');
    // A refresh showing a different routing removes it too.
    vm.runInContext(`routingRetry.set(${JSON.stringify(key)},${JSON.stringify(A)})`,context);context.render();assert.equal($(r.retry).hidden,false);
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');context.render();
    assert.equal($(r.retry).hidden,true);
    // A rollback suspending the routing removes Try again and its move feedback: the select still shows the saved
    // value, but re-posting it would resume routing. A failed status does the same. The linked row checks both providers.
    const suspended={policy:null,status:'disabled',suspended:true,saved_mode:'preferred-account',saved_preferred_email:A};
    const stale=[['suspended',suspended],['failed',{...active('preferred-account',A),status:'failed'}]];
    for(const [name,part] of stale)for(const provider of key==='all' ? ['preference','claude_preference'] : [key==='claude' ? 'claude_preference' : 'preference']){
      for(const clicked of [false,true]){
        await reset(key);open(key);respond=async()=>okResult(key,{outcome:'not_moved',code:'too_soon'});nextSnapshot=applied();
        await context.routingChoiceApply(key,'move');assert.equal($(r.retry).hidden,false);posts=[];
        context.fixture.gateway[provider]=part;
        assert.equal(context.routingApplied(key),A,`${key}: ${name} ${provider} still shows the retried value`);
        // Clicking before any refresh re-checks the same condition and never posts.
        if(clicked)await context.retryRoutingMove(key);else context.render();
        assert.equal(posts.length,0,`${key}: ${name} ${provider} never re-posts`);
        assert.equal($(r.retry).hidden,true,`${key}: ${name} ${provider} removes Try again`);
        assert.equal($(r.feedback).textContent,'',`${key}: ${name} ${provider} removes the move feedback`);
        assert.equal(state(`routingRetry.has(${JSON.stringify(key)})`),false);
      }
    }
    // Held and fallback keep it: the retry re-runs the server's checks and reports not_applied again.
    await reset(key);open(key);respond=async()=>okResult(key,{outcome:'not_moved',code:'not_applied'},'held');nextSnapshot=applied();
    await context.routingChoiceApply(key,'move');
    context.fixture.gateway.preference.status='held';context.fixture.gateway.claude_preference.status='fallback';context.render();
    assert.equal($(r.retry).hidden,false,`${key}: held keeps Try again`);

    // Without the helper the dashboard behaves as before: a change applies immediately, with no modal.
    await reset(key,{available:false});respond=async()=>okResult(key);nextSnapshot=applied();
    $(r.select).focus();change(key,A);
    assert.equal(dialog.open,false);await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');
  }

  // A link change, either way, drops every row's old Try again and move feedback, so unlinking never revives them.
  const linkRespond=linked=>async(route,options)=>route==='/api/routing-link' ? {linked} :
    okResult(route==='/api/routing-all' ? 'all' : 'codex',JSON.parse(options.body).move ? {outcome:'not_moved',code:'too_soon'} : null);
  for(const [key,first,second] of [['codex',true,false],['claude',true,false],['all',false,true]]){
    await reset(key);open(key);nextSnapshot=applied();respond=linkRespond(first);
    await context.routingChoiceApply(key,'move');
    assert.equal($(rows[key].retry).hidden,false);assert.equal($(rows[key].feedback).textContent,'Saved · conversations not moved');
    nextSnapshot=null;
    await context.saveRoutingLink(first);respond=linkRespond(second);await context.saveRoutingLink(second);
    assert.equal(state('routingLinked(current.gateway)'),second);assert.equal($(rows[key].row).hidden,false,`${key} row shown again`);
    assert.equal($(rows[key].retry).hidden,true,`${key}: no stale Try again after link and unlink`);
    // Relinking reapplies the shared routing, so the linked row shows that save's own result.
    assert.equal($(rows[key].feedback).textContent,second ? 'Saved' : '',`${key}: no stale move feedback after link and unlink`);
    assert.equal(state(`routingRetry.has(${JSON.stringify(key)})`),false);
  }
  // A link change made elsewhere, seen on refresh, clears it the same way.
  await reset('codex');open('codex');nextSnapshot=applied();respond=async()=>okResult('codex',{outcome:'not_moved',code:'too_soon'});
  await context.routingChoiceApply('codex','move');assert.equal($('routing-retry').hidden,false);
  context.fixture.gateway.routing_linked=true;context.render();context.fixture.gateway.routing_linked=false;context.render();
  assert.equal($('routing-retry').hidden,true);assert.equal($('routing-feedback').textContent,'');

  // Layout: the switch sits on the select's line, ahead of the full-width state lines.
  await reset('claude');
  const order=$('claude-routing').children.map(child=>child.id);
  assert(order.indexOf('claude-routing-feedback')<order.indexOf('routing-split'));
  assert(order.indexOf('routing-split')<order.indexOf('claude-routing-state'),order.join(' '));
  const linkedOrder=(await reset('all'),$('routing-all').children.map(child=>child.id));
  assert(linkedOrder.indexOf('routing-split')<linkedOrder.indexOf('routing-all-state'),linkedOrder.join(' '));

  // Only one choice is open at a time; the previous close event never closes the next choice.
  await reset('codex');open('codex');change('claude',A);await flush();
  assert.equal(dialog.open,true);assert.equal(text('routing-dialog-title'),`Switch Claude routing to prefer ${A}?`);
  assert.equal($('routing-mode').value,B);assert(!$('routing-mode').classList.contains('pending'));
  assert.equal(state('routingChoice.key'),'claude');

  // One configured Codex account, linked or not: only Claude routing shows, without the switch, so no Codex select
  // offers the retired account. Its modal posts for Claude alone and claims no Codex move.
  for(const linked of [true,false]){
    await reset('claude',{linked,codexAccounts:1});
    assert.equal($('codex-routing').hidden,true);assert.equal($('routing-all').hidden,true);
    assert.equal($('claude-routing').hidden,false);assert.equal($('routing-split').hidden,true);
    assert.deepEqual($('claude-routing-mode').options.map(o=>o.value).slice(3),[A,B],'Claude keeps both preferred accounts');
    open('claude','earliest-weekly-reset');
    assert.equal(text('routing-dialog-title'),'Switch Claude routing to Earliest reset?');
    assert.equal(text('routing-dialog-note'),'');assert.equal($('routing-dialog-note').hidden,true);
    respond=async()=>({policy:'earliest-weekly-reset',status:'active',move:{outcome:'moved',code:'moved'}});
    button('move').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:'/api/claude-routing',body:{mode:'earliest-weekly-reset',move:true}}]);
    assert.equal($('claude-routing-feedback').textContent,'Saved · conversations moved');
    assert.equal(dialog.open,false);assert.equal(document.activeElement,$('claude-routing-mode'));
  }
  // A second configured account brings the Codex choice back; an older backend without the count keeps it.
  for(const codexAccounts of [2,null]){
    await reset('all',{codexAccounts});
    assert.equal($('routing-all').hidden,false);assert.equal($('routing-split').hidden,false);assert.equal($('claude-routing').hidden,true);
    await reset('codex',{codexAccounts});
    assert.equal($('codex-routing').hidden,false);assert.equal($('claude-routing').hidden,false);
  }

  // Request flow: a retired second Codex account has no series or legend entry unless shown history has its requests.
  {
    const legends=[element('legend')];const querySelectorAll=document.querySelectorAll;
    document.querySelectorAll=selector=>selector==='[data-account-legend]' ? legends : [];
    vm.runInContext('renderLegend=realRenderLegend',context);
    const codexEntries=()=>[...legends[0].innerHTML.matchAll(/<i class="(codex-\w+)"><\/i>([^<]*)/g)].map(([,key,name])=>key+' '+name);
    for(const [count,hours,expected] of [
      [1,[],['codex-one '+A]],
      [1,[{auth_index:'codex-b',email:B,requests:0}],['codex-one '+A]],
      [1,[{auth_index:'codex-b',email:B,requests:3}],['codex-one '+A,'codex-two '+B]],
      [2,[],['codex-one '+A,'codex-two '+B]],
      [null,[],['codex-one '+A,'codex-two '+B]]]){
      await reset('all',{codexAccounts:count});context.fixture.usage={hours};vm.runInContext('today=current',context);
      context.renderLegend();
      assert.deepEqual(codexEntries(),expected,`legend with ${count} configured and ${JSON.stringify(hours)}`);
      assert.equal(context.seriesNames()['codex-two'] ? 'named' : 'unnamed',expected.length>1 ? 'named' : 'unnamed');
    }
    // After deploy deletes the retired credential, its history keeps a separate series instead of joining the remaining account.
    for(const [requests,expected] of [[3,['codex-one '+A,'codex-two Removed account']],[0,['codex-one '+A]]]){
      await reset('all',{codexAccounts:1});
      context.fixture.gateway.accounts=context.fixture.gateway.accounts.filter(a=>a.email===A);
      context.fixture.usage={hours:[{auth_index:'codex-b',requests}]};vm.runInContext('today=current',context);
      assert.equal(context.series('codex','codex-b'),'codex-two');
      context.renderLegend();
      assert.deepEqual(codexEntries(),expected,`removed credential with ${requests} requests`);
    }
    document.querySelectorAll=querySelectorAll;
  }

  // System page: both values a move writes read as the configured 1h.
  {
    const querySelector=document.querySelector;document.querySelector=()=>({textContent:''});
    $('advanced-link').closest=()=>({hidden:false});
    for(const [ttl,shown] of [['1h0m0s','1h'],['1h0m1s','1h'],['1h','1h'],['2h','2h']]){
      await reset('all');
      context.realRenderSystem({...context.fixture,usage:{available:true},
        gateway:{...context.fixture.gateway,routing:{'session-affinity':true,'session-affinity-ttl':ttl}}});
      assert.match($('system-details').innerHTML,new RegExp(`<dt>Session affinity</dt><dd>Enabled · ${shown}</dd>`),ttl);
    }
    document.querySelector=querySelector;
  }

  assert.match(html,/<option selected value="account-a@example.com">Prefer account-a@example.com<\/option>/);
  console.log('Routing modal UI: open/cancel/Escape/backdrop/focus, errors inside the modal, refresh rules via render(), applying lock, outcomes, Try again, one configured Codex account, the retired chart series and the System TTL passed for all three rows');
})().catch(error=>{console.error(error);process.exitCode=1;});
