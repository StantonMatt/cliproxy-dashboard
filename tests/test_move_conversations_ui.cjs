'use strict';
// Executes the routing-change choice through the real app.js and render() with a small DOM built from index.html.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const A='account-a@example.com', B='account-b@example.com';
const OPTIONS=['round-robin','earliest-weekly-reset','highest-remaining-quota',A,B];
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
const elements=new Map(), documentEvents={};
let hostWrites=new Map();

function element(id, className='', tag='div'){
  const events={}, attributes={}, classes=new Set(className.split(/\s+/).filter(Boolean));
  const el={id,tag,hidden:false,disabled:false,value:'',textContent:'',checked:false,parentElement:null,children:[],dataset:{},events,
    title:'',
    get innerHTML(){return el._html || '';},
    set innerHTML(value){
      el._html=value;
      if(!classes.has('routing-choice-host'))return;
      hostWrites.set(id,(hostWrites.get(id) || 0)+1);
      if(el.children.includes(document.activeElement))document.activeElement=document.body;
      for(const child of el.children)child.parentElement=null;
      el.children=[...value.matchAll(/<button[^>]*data-routing-choice="(\w+)"[^>]*>([^<]*)<\/button>/g)].map(([tagText,choice,label])=>{
        const button=element('','','button');button.dataset.routingChoice=choice;button.textContent=label;button.parentElement=el;
        button.describedBy=(tagText.match(/aria-describedby="([^"]+)"/) || [])[1];
        return button;
      });
    },
    addEventListener(type,fn){(events[type] ||= []).push(fn);},
    // Events bubble from the target through its ancestors, as click and focusout do.
    dispatch(type,event={}){for(let node=el;node;node=node.parentElement)for(const fn of node.events[type] || [])fn({target:el,preventDefault(){},...event});},
    setAttribute(name,value){attributes[name]=String(value);},getAttribute(name){return name in attributes ? attributes[name] : null;},
    removeAttribute(name){delete attributes[name];},
    focus(){if(!el.disabled && !el.hidden)document.activeElement=el;},
    options:OPTIONS.map(value=>({value,textContent:value.includes('@')?'Prefer '+value:value})),
    classList:{add(name){classes.add(name);},remove(name){classes.delete(name);},toggle(name,force){if(force ?? !classes.has(name))classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)},
    contains(other){for(let node=other;node;node=node.parentElement)if(node===el)return true;return false;},
    closest(selector){
      for(let node=el;node;node=node.parentElement){
        if(selector==='[data-routing-choice]' && node.dataset?.routingChoice)return node;
      }
      return null;
    },
    querySelector(selector){
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
  addEventListener(type,fn){(documentEvents[type] ||= []).push(fn);},
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
const outside=element('outside');

let hash='';
const context=vm.createContext({document,Date,JSON,Promise,Map,Set,Intl,URLSearchParams,localStorage:{getItem:()=>null},
  location:{get hash(){return hash;}},setTimeout:()=>1,clearTimeout(){},AbortSignal:{timeout:()=>1}});
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
// Only the routing renderers run for real; the rest of render() is outside this feature.
vm.runInContext('var realRenderSystem=renderSystem;',context);
vm.runInContext(`renderOperations=()=>{};renderWatch=()=>{};renderLegend=()=>{};renderResetModal=()=>{};renderToday=()=>{};renderUsage=()=>{};renderSystem=()=>{};`,context);

const active=(policy,email)=>({policy,status:'active',selected_email:email,preferred_email:email,saved_mode:policy,saved_preferred_email:email});
function snapshot({linked=true,available=true}={}){
  return {gateway:{online:true,routing_linked:linked,move_available:available,accounts:[],checked_at:new Date().toISOString(),
    preference:active('preferred-account',B),claude_preference:active('preferred-account',B)},
    usage:{},health:{},watch_accounts:[{id:'claude',email:A},{id:'claude-b',email:B}]};
}
const rows={
  codex:{row:'codex-routing',select:'routing-mode',feedback:'routing-feedback',retry:'routing-retry',host:'routing-choice-host',route:'/api/routing',flag:'routingSaving',linked:false},
  claude:{row:'claude-routing',select:'claude-routing-mode',feedback:'claude-routing-feedback',retry:'claude-routing-retry',host:'claude-routing-choice-host',route:'/api/claude-routing',flag:'claudeRoutingSaving',linked:false},
  all:{row:'routing-all',select:'routing-all-mode',feedback:'routing-all-feedback',retry:'routing-all-retry',host:'routing-all-choice-host',route:'/api/routing-all',flag:'routingAllSaving',linked:true}
};
let posts=[], respond=async()=>({}), refreshes=0, nextSnapshot=null;
context.request=async(route,options)=>{posts.push({route,body:JSON.parse(options.body)});return respond(route,options);};
context.load=async()=>{refreshes++;if(nextSnapshot){context.fixture=nextSnapshot;vm.runInContext('current=fixture',context);}context.render();};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const state=name=>vm.runInContext(name,context);
const buttons=key=>$(rows[key].host).children;
const button=(key,choice)=>buttons(key).find(b=>b.dataset.routingChoice===choice);
const keydown=(key,target)=>{for(const fn of documentEvents.keydown)fn({key,target,preventDefault(){}});};

async function reset(key,options={}){
  vm.runInContext('closeRoutingChoice(true);routingRetry.clear();routingSaving=false;claudeRoutingSaving=false;routingAllSaving=false;routingLinkSaving=false;failed=false',context);
  posts=[];respond=async()=>({});nextSnapshot=null;hash='';hostWrites=new Map();
  context.fixture=snapshot({linked:rows[key].linked,...options});vm.runInContext('current=fixture',context);context.render();
  for(const row of Object.values(rows)){$(row.feedback).textContent='';}
  document.activeElement=document.body;
}
function change(key,value){$(rows[key].select).value=value;$(rows[key].select).dispatch('change');}
function open(key,value=A){
  const select=$(rows[key].select);select.focus();change(key,value);
  assert.equal(buttons(key).length,3,`${key}: choice opens`);
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
  const next=snapshot({linked:context.fixture.gateway.routing_linked});
  next.gateway.preference=active('preferred-account',A);next.gateway.claude_preference=active('preferred-account',A);
  return next;
}

(async()=>{
  for(const key of Object.keys(rows)){
    const r=rows[key];
    // Opening: change shows the choice and sends nothing.
    await reset(key);
    assert.equal($(r.row).hidden,false,`${key} row is visible`);
    $(r.feedback).textContent='Saved';
    const select=open(key);
    assert.equal(posts.length,0,`${key}: change does not POST`);
    assert.equal(select.value,A);assert(select.classList.contains('pending'));
    assert.equal($(r.feedback).textContent,'');
    assert.equal($('routing-link').disabled,true,`${key}: switch disabled while choosing`);
    assert.equal($(r.select.replace('-mode','-resume')).disabled,true);
    assert.deepEqual(buttons(key).map(b=>b.textContent),['New conversations only','Move all conversations now','Cancel']);
    assert.equal(button(key,'move').describedBy,$(r.host).innerHTML.match(/<span id="([^"]+)"/)[1]);
    assert.match($(r.host).innerHTML,/^<div class="route-confirm" role="alert"><div>/,'the alert has no aria-label');
    assert.equal(document.activeElement,select,'focus stays on the select');
    // Picking another value keeps it open; the applied value closes it.
    const first=button(key,'new');change(key,'round-robin');
    assert.equal(button(key,'new'),first,'the choice is not rebuilt for a new pending value');
    change(key,B);assert.equal(buttons(key).length,0);assert.equal(select.value,B);assert(!select.classList.contains('pending'));
    assert.equal(posts.length,0);

    // Enter on the select never applies: some browsers deliver it before the select's value updates,
    // and on others Enter opens the picker. The default is the next Tab stop, activated natively.
    for(const value of [A,'round-robin']){
      await reset(key);open(key);respond=async()=>okResult(key);
      $(r.select).value=value;keydown('Enter',$(r.select));await flush();await flush();
      assert.equal(posts.length,0,`${key}: Enter on the select sends nothing`);
      assert.equal(buttons(key).length,3);assert.equal(state('routingChoice.value'),A);
    }
    await reset(key);open(key);respond=async()=>okResult(key);nextSnapshot=applied();
    assert.equal(nextTabStop(key,$(r.select)),button(key,'new'),`${key}: Tab reaches New conversations only first`);
    button(key,'new').focus();button(key,'new').dispatch('click');await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');assert.equal(buttons(key).length,0);
    assert.equal(document.activeElement,$(r.select),`${key}: focus returns after New`);

    // Escape anywhere in the row, or on the body, cancels; Escape elsewhere does not.
    await reset(key);open(key);button(key,'move').focus();keydown('Escape',button(key,'move'));
    assert.equal(buttons(key).length,0);assert.equal($(r.select).value,B);assert.equal(document.activeElement,$(r.select));
    open(key);keydown('Escape',document.body);assert.equal(buttons(key).length,0);
    open(key);keydown('Escape',outside);assert.equal(buttons(key).length,3);
    // Cancel reverts to the gateway's current value, not the value when the choice opened.
    context.fixture.gateway.preference=active('round-robin');context.fixture.gateway.claude_preference=active('round-robin');
    button(key,'cancel').dispatch('click');await flush();
    assert.equal($(r.select).value,'round-robin');assert.equal(posts.length,0);

    // Focus leaving the row cancels; a null relatedTarget or a target inside the row keeps it.
    await reset(key);open(key);
    $(r.row).dispatch('focusout',{relatedTarget:null});assert.equal(buttons(key).length,3);
    $(r.row).dispatch('focusout',{relatedTarget:button(key,'new')});assert.equal(buttons(key).length,3);
    $(r.row).dispatch('focusout',{relatedTarget:outside});assert.equal(buttons(key).length,0);assert.equal($(r.select).value,B);

    // Switching view cancels; the bare URL is the overview and keeps it.
    await reset(key);open(key);hash='';context.showView();assert.equal(buttons(key).length,3);
    hash='#overview';context.showView();assert.equal(buttons(key).length,3);
    hash='#usage';context.showView();assert.equal(buttons(key).length,0);assert.equal($(r.select).value,B);hash='';

    // A refresh keeps the choice, its nodes and focus; it never re-announces the alert.
    await reset(key);open(key);const move=button(key,'move');move.focus();
    const writes=hostWrites.get(r.host);
    for(let i=0;i<3;i++)context.render();
    assert.equal(button(key,'move'),move);assert.equal(hostWrites.get(r.host),writes);
    assert.equal(document.activeElement,move);assert.equal($(r.select).value,A);
    assert.equal(move.disabled,false);
    // Refresh-cancel rules through render(): disabled select, hidden row, or the pending value applied elsewhere.
    for(const [name,apply] of [
      ['failed',()=>vm.runInContext('failed=true',context)],
      ['read only',()=>{context.fixture.read_only=true;}],
      ['offline',()=>{context.fixture.gateway.online=false;}],
      ['link change',()=>{context.fixture.gateway.routing_linked=!r.linked;}],
      ['applied elsewhere',()=>{context.fixture.gateway.preference=active('preferred-account',A);context.fixture.gateway.claude_preference=active('preferred-account',A);}],
      ['move unavailable',()=>{context.fixture.gateway.move_available=false;}]]){
      await reset(key);open(key);apply();context.render();
      assert.equal(buttons(key).length,0,`${key}: ${name} cancels`);
      assert(!$(r.select).classList.contains('pending'));
      if(name!=='applied elsewhere')assert.equal($(r.select).value,B,`${key}: ${name} shows the gateway value`);
      assert.equal(posts.length,0);
    }
    // Buttons are never clickable while the select is disabled.
    await reset(key);open(key);vm.runInContext('routingLinkSaving=true',context);context.render();
    assert.equal($(r.select).disabled,true);assert(buttons(key).every(b=>b.disabled));
    button(key,'move').dispatch('click');assert.equal(posts.length,0);
    vm.runInContext('routingLinkSaving=false',context);

    // Applying: locked, chosen button busy, choice visible across refreshes, then outcome and focus.
    for(const choice of ['new','move']){
      await reset(key);open(key);button(key,choice).focus();let resolve;
      respond=()=>new Promise(done=>{resolve=done;});
      button(key,choice).dispatch('click');await flush();
      assert.equal(posts.length,1);assert.equal(posts[0].route,r.route);
      assert.equal(posts[0].body.move,choice==='move' ? true : undefined);
      assert.equal(state(r.flag),true);
      assert.equal($(r.select).disabled,true);assert.equal($('routing-link').disabled,true);
      assert.equal($(r.feedback).textContent,'Applying…');
      assert(buttons(key).every(b=>b.disabled));
      assert.deepEqual(buttons(key).map(b=>b.getAttribute('aria-busy')),buttons(key).map(b=>b.dataset.routingChoice===choice ? 'true' : null));
      vm.runInContext('failed=true',context);context.render();vm.runInContext('failed=false',context);
      context.fixture.gateway.routing_linked=!r.linked;context.render();context.fixture.gateway.routing_linked=r.linked;
      hash='#usage';context.showView();hash='';
      assert.equal(buttons(key).length,3,`${key}: periodic loads keep an applying choice`);
      assert.equal($(r.feedback).textContent,'Applying…');
      nextSnapshot=applied();resolve(okResult(key,choice==='move' ? {outcome:'moved',code:'moved'} : null));await flush();await flush();
      assert.equal(buttons(key).length,0);assert(!$(r.select).classList.contains('pending'));
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
    for(const [result,choice,text,retry] of outcomes){
      await reset(key);open(key);respond=async()=>result;
      await context.routingChoiceApply(key,choice);
      assert.equal($(r.feedback).textContent,text,`${key}: ${JSON.stringify(result)}`);
      assert.equal($(r.retry).hidden,!retry,`${key}: Try again for ${JSON.stringify(result.move || result)}`);
    }
    // The state line explains why a fallback result isn't applied.
    await reset(key);open(key);respond=async()=>okResult(key,null,'fallback');
    await context.routingChoiceApply(key,'new');
    const stateLine=key==='all' ? $('routing-all-state').innerHTML : $(r.select.replace('-mode','-state')).textContent;
    assert.match(stateLine,/Neutral fallback · quota data held/,`${key}: fallback state line`);
    // A 400 or 503 carries no move, and its body is never read.
    for(const status of [400,503]){
      await reset(key);open(key);respond=async()=>{throw new Error(`HTTP ${status}`);};
      button(key,'move').focus();await context.routingChoiceApply(key,'move');
      assert.equal($(r.feedback).textContent,'Not confirmed · refresh and try again',`${key}: HTTP ${status}`);
      assert.equal($(r.retry).hidden,true,`${key}: no Try again after HTTP ${status}`);
      assert.equal(document.activeElement,$(r.select),`${key}: focus returns after a failure`);
    }

    // Try again: next Tab stop, locks through the normal save, never duplicated, removed on success.
    await reset(key);open(key);respond=async()=>okResult(key,{outcome:'uncertain',code:'helper_timeout'});nextSnapshot=applied();
    button(key,'move').focus();await context.routingChoiceApply(key,'move');
    assert.equal(document.activeElement,$(r.select));
    assert.equal(nextTabStop(key,$(r.select)),$(r.retry),`${key}: Try again is the next Tab stop`);
    assert.equal($(r.row).children.filter(child=>child.id===r.retry).length,1);
    let resolve;respond=()=>new Promise(done=>{resolve=done;});posts=[];
    $(r.retry).focus();$(r.retry).dispatch('click');
    assert.equal(state(r.flag),true,`${key}: retry uses its own saving flag`);
    for(const other of Object.values(rows).filter(other=>other!==r))assert.equal(state(other.flag),false);
    assert.equal($(r.select).disabled,true);assert.equal($(r.retry).hidden,true);
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

    // Without the helper the dashboard behaves as before: a change applies immediately.
    await reset(key,{available:false});respond=async()=>okResult(key);nextSnapshot=applied();
    $(r.select).focus();change(key,A);
    assert.equal(buttons(key).length,0);await flush();await flush();
    assert.deepEqual(posts,[{route:r.route,body:{mode:'preferred-account',preferred_email:A}}]);
    assert.equal($(r.feedback).textContent,'Saved');
    keydown('Enter',$(r.select));await flush();assert.equal(posts.length,1);
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

  // Desktop layout: in the Claude row the switch sits on the select's line, ahead of the choice.
  await reset('claude');
  const order=$('claude-routing').children.map(child=>child.id);
  assert(order.indexOf('routing-split')<order.indexOf('claude-routing-choice-host'),order.join(' '));
  assert(order.indexOf('claude-routing-feedback')<order.indexOf('routing-split'));
  const linkedOrder=(await reset('all'),$('routing-all').children.map(child=>child.id));
  assert(linkedOrder.indexOf('routing-split')<linkedOrder.indexOf('routing-all-choice-host'),linkedOrder.join(' '));

  // Only one choice is open at a time.
  await reset('codex');vm.runInContext('current.gateway.routing_linked=false',context);context.render();
  open('codex');open('claude');assert.equal(buttons('codex').length,0);assert.equal($('routing-mode').value,B);

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
  console.log('Move conversations UI: open/cancel/keyboard/focus, refresh rules via render(), applying lock, outcomes, Try again, availability and the System TTL passed for all three rows');
})().catch(error=>{console.error(error);process.exitCode=1;});
