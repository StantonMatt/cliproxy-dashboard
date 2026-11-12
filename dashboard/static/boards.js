/* RouterBoards widget contract (build contract §C).
 * register({type,name,section,span,min,compact,minRows,maxRows,heightPresets,autoHeightOnly,defaults,presets,title,fields,
 *           normalize,render,preview,destroy,table}). Only active widgets render.
 * RouterApp exposes accountColor(row), accountName(row), accentOf(provider),
 * providerName, compact, number, percent, dateTime, duration, promptUsage, totals,
 * usageProviders, usageSince, requestTable, usageTable, cacheAmount,
 * cachedShareLabel, providerChip, gatewayRow, phoneNumber and escape/esc.
 * api() is read-only, deduplicated and cached for 15s. Renderers must scope all
 * DOM access to their host; ctx.preview is read-only. Return cleanup in destroy.
 */
(() => {
'use strict';
const defs=new Map(), cache=new Map(), mounted=new Map(), browseRanges=new Map();
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const clone=x=>JSON.parse(JSON.stringify(x)), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>`<svg aria-hidden="true"><use href="#${n}"/></svg>`;
const ranges=[['today','Today'],['week','7 days'],['month','30 days'],['all','All recorded']];
const sizes=[[3,'1/4'],[4,'1/3'],[6,'1/2'],[8,'2/3'],[9,'3/4'],[12,'Full']];
const S={layout:null,overview:null,active:null,lastHash:location.hash,customizing:false,snapshot:null,saving:false,error:'',overlay:null,started:false};
let packQueued=false;
let uid=0, toastTimer, toastRemaining=0, toastAt=0, toastUndo, overlayCleanup, opener, deferredLeave, drag, resize, renderQueued=false, lastServer='';
const app=()=>window.RouterApp;
const accountFamily=t=>t==='accounts'||t==='account-card';
const accountOf=w=>providers().find(p=>p.id===w.options?.provider)?.accounts?.find(a=>a.key===w.options?.account);
const cardName=(p,a)=>app()?.cardName?.(p,a)||a.label||a.email||a.key;
function orderedAccounts(p){const order=S.layout?.accounts?.[p.id]||[];return [...(p.accounts||[])].sort((a,b)=>(order.indexOf(a.key)+1||Infinity)-(order.indexOf(b.key)+1||Infinity));}
function pieceConflict(b,make,skip){return accountFamily(make.type)&&(b?.widgets||[]).some(w=>w!==skip&&w.type===make.type&&w.options.provider===make.options.provider&&(w.type==='accounts'||w.options.account===make.options.account));}
function shownCards(w,b=find(w.id).b||board()){if(w.options?.hideCards)return 0;const placed=new Set((b?.widgets||[]).filter(v=>v.type==='account-card'&&v.options.provider===w.options.provider).map(v=>v.options.account));return (providers().find(p=>p.id===w.options.provider)?.accounts||[]).filter(a=>!placed.has(a.key)).length;}
const accountValue=o=>JSON.stringify([o.provider,o.account]);
const issuedIds=new Set();
function newId(t){let id;do{id=`${t}-${Date.now().toString(36)}-${(++uid).toString(36)}`;}while(issuedIds.has(id)||(S.layout?.boards||[]).some(b=>b.id===id||b.widgets.some(w=>w.id===id)));issuedIds.add(id);return id;}
const widget=(type,span,options={},title='',id=newId(type))=>({id,type,span,title,options});
const board=()=>S.layout?.boards.find(b=>b.id===S.active) || S.layout?.boards[0];
const find=id=>{for(const b of S.layout?.boards||[]){const w=b.widgets.find(w=>w.id===id);if(w)return {b,w};}return {};};
const name=w=>{const d=defs.get(w.type);try{return w.title || d?.title?.({...d.defaults,...w.options},context(w)) || d?.name || 'Unavailable widget';}catch(_){return w.title || d?.name || w.type;}};
const typeName=w=>w.type==='accounts'&&w.options?.hideCards?'Header':defs.get(w.type)?.name;
const label=w=>`${typeName(w)||'Unavailable widget'} · ${name(w)}`;
const announce=text=>{const el=$('#boards-announce');if(el){el.textContent='';queueMicrotask(()=>el.textContent=text);}};
function placeNode(parent,node,before=null){
  if(node===before||(node.parentElement===parent&&node.nextElementSibling===before))return;
  const focus=node.contains(document.activeElement)?document.activeElement:null;
  if(parent.moveBefore&&node.parentElement===parent)parent.moveBefore(node,before);else parent.insertBefore(node,before);
  if(focus&&document.activeElement!==focus)focus.focus({preventScroll:true});
}
function register(def){if(!def || !/^[a-z][a-z0-9-]{0,31}$/.test(def.type) || typeof def.render!=='function')throw Error('Invalid widget definition');defs.set(def.type,{span:12,min:1,defaults:{},...def});if(S.started)queueRender();}
function providers(){return app()?.providers?.() || S.overview?.providers || [];}
function defaults(order=[]){
  const ps=providers(), ids=[...new Set([...order,...ps.map(p=>p.id)])].filter(id=>ps.some(p=>p.id===id));
  return [
    {id:'overview',name:'Overview',builtin:true,heading:'',range:'today',showRange:false,widgets:[
      ...ids.map(id=>widget('accounts','auto',{provider:id},'','accounts-'+id)),
      widget('heading',12,{text:"Today's activity",link:'usage'},'','activity-heading'),
      widget('tokens',5,{range:'board'},'','today-tokens'),
      widget('chart',7,{chart:'stacked',metric:'requests',group:'account',range:'board',height:'short',compare:false},'Request flow','request-flow'),
      widget('requests',12,{rows:6,link:'requests',failed:false,filters:false,more:false,range:'board'},'Recent requests','recent-requests')]},
    {id:'usage',name:'Usage',builtin:true,heading:'Recorded activity',range:'today',showRange:true,widgets:[
      widget('stat',3,{metric:'requests',range:'board',spark:false,delta:false},'','usage-requests-stat'),
      widget('stat',3,{metric:'tokens',range:'board',spark:false,delta:false},'','usage-tokens-stat'),
      widget('stat',3,{metric:'success_rate',range:'board',spark:false,delta:false,detail:true},'','usage-success-stat'),
      widget('stat',3,{metric:'cache_hit_rate',range:'board',spark:false,delta:false,detail:true},'Cached input','usage-cache-stat'),
      widget('chart',12,{chart:'stacked',metric:'requests',group:'account',range:'board',height:'medium',compare:false},'Requests over time','usage-chart'),
      widget('usage-accounts',12,{range:'board'},'','usage-accounts'),widget('usage-models',12,{range:'board'},'','usage-models'),
      widget('requests',12,{rows:50,link:'none',failed:false,filters:true,more:true,range:'board'},'Requests','usage-requests')]}
  ];
}
function resolveRange(w,b=board(),now=new Date()){
  const opt=w?.options?.range, key=opt && opt!=='board'?opt:browseRanges.get(b?.id)||b?.range||'today', alias={week:'7d',month:'30d'}, k=alias[key]||key;
  const until=new Date(now), since=new Date(now);
  if(k==='today')since.setHours(0,0,0,0);
  else if(k==='all'){const first=app()?.usageSince?.(S.overview?.usage);since.setTime(first&&Number.isFinite(Date.parse(first))?Date.parse(first):Date.now()-89*86400000);since.setHours(0,0,0,0);}
  else if(k==='24h')since.setTime(+until-86400000);
  else {since.setHours(0,0,0,0);since.setDate(since.getDate()-({ '7d':6,'30d':29,'90d':89 }[k]??6));}
  const hours=(until-since)/3600000;
  const bucket=k==='today'?'hour':hours<=6?'minute':hours<=48?'hour':hours<=2400?'day':'week';
  return {key,since,until,label:({today:'Today','24h':'Last 24 hours','7d':'7 days','30d':'30 days','90d':'90 days',all:'All recorded'})[k]||key,overridden:!!opt&&opt!=='board',bucket};
}
async function api(path,params={}){
  const url=new URL(path,location.origin);const tzAware=!['/api/breakdown','/api/retry','/api/overview','/api/requests'].includes(url.pathname);for(const [key,val] of Object.entries(params))if((key!=='tz'||tzAware)&&val!=null && val!=='')url.searchParams.set(key,val instanceof Date?val.toISOString():String(val));
  for(const [key,item] of cache)if(!item.promise&&Date.now()-item.at>=15000)cache.delete(key);
  url.searchParams.sort();const key=url.pathname+url.search, prior=cache.get(key);
  if(prior && (prior.promise || Date.now()-prior.at<15000))return prior.promise || prior.value;
  const item={};item.promise=fetch(key,{headers:{'X-Router-Request':'1'},cache:'no-store',signal:AbortSignal.timeout(15000)}).then(async r=>{if(!r.ok)throw Error(`HTTP ${r.status}`);const value=await r.json();Object.assign(item,{value,at:Date.now(),promise:null});return value;}).catch(e=>{cache.delete(key);throw e;});cache.set(key,item);return item.promise;
}
function state(host,kind,info={}){
  let box=host.__routerState;if(!box?.isConnected){box=document.createElement('div');box.className='widget-state';host.append(box);host.__routerState=box;}
  const hasData=[...host.children].some(el=>el!==box && !el.classList.contains('widget-state'));
  host.classList.toggle('refetch',kind==='loading'&&hasData);box.className='widget-state';box.replaceChildren();
  if(kind==='loading'&&hasData)return;
  if(kind==='stale'||kind==='updated'||kind==='error'&&hasData){box.classList.add('state-chip','chip','warn');box.textContent=kind==='stale'?`STALE · ${info.minutes??'—'} MIN`:`UPDATED ${new Date(info.at||Date.now()).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})}`;return;}
  if(!['loading','error','empty','removed'].includes(kind)){box.remove();return;}
  box.classList.add('wstate',kind==='error'?'err':kind);box.setAttribute('role',kind==='error'?'alert':'status');
  if(kind==='loading'){box.innerHTML='<span class="sk" style="width:55%;height:14px"></span><span class="sk" style="width:35%;height:24px"></span><span>Loading usage…</span>';return;}
  const text=document.createElement('span');text.textContent=info.message||(kind==='error'?"Couldn't load usage":kind==='removed'?'This account was removed':'No requests in this range.');box.append(text);
  if(kind==='error'||kind==='removed'){const btn=document.createElement('button');btn.className='resets-button';btn.textContent=kind==='removed'?'Choose another':'Try again';btn.onclick=()=>info.retry?.();box.append(btn);}
}
function context(w,host,preview=false,b=find(w.id).b||board()){
  const def=defs.get(w.type), options={...def?.defaults,...w.options};
  const resolved=resolveRange({...w,options},b,new Date(Math.floor(Date.now()/15000)*15000));
  return {widget:w,options,board:b,range:resolved,preview,customizing:S.customizing,width:host?.getBoundingClientRect().width||360,overview:S.overview,tz:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',api,state,
    accountColor:r=>app()?.accountColor(r)||'var(--blue)',accountName:r=>app()?.accountName(r)||r.email||r.label||'Account',accentOf:p=>app()?.accentOf(p)||p,
    openRequests:filters=>{if(!preview)openRequests(filters,resolved);},announce,openSettings:()=>{if(!preview){enter();openOverlay('settings',{wid:w.id});}},saveOptions:patch=>saveOptions(w,patch,preview)};
}
function normalized(w){const height=w.height;normalizeHeight(w);const d=defs.get(w.type),result=d?.normalize?.({...d.defaults,...w.options},context(w));if(result){w.options={...(result.options||result)};if(result.note)announce(result.note);}normalizeHeight(w);if(w.height!==height)queueRender();}
const ROW_STEP=57, ROW_GAP=34;
const rowPixels=n=>ROW_STEP*n-ROW_GAP;
const pixelRows=px=>Math.max(1,Math.ceil((px+ROW_GAP)/ROW_STEP));
// A widget measured by fitAccounts can't be shorter than its most compact content (its live minimum). Saved heights keep
// only the registry's bounds, so a narrower window never rewrites the layout; the box grows to the live minimum instead.
function heightBounds(w,stored=false){const d=defs.get(w.type),calendar=w.type==='heatmap'&&w.options?.chart==='calendar',fit=stored?0:fitRows(w);return {min:Math.max(3,d?.minRows||3,calendar?6:3,fit),max:Math.min(20,calendar?16:d?.maxRows||20),auto:!!d?.autoHeightOnly||w.type==='accounts'&&!!w.options?.hideCards};}
function clampHeight(w,value,stored=false){const b=heightBounds(w,stored);return b.auto||value==='auto'||!Number.isInteger(value)?'auto':Math.max(b.min,Math.min(b.max,value));}
function normalizeHeight(w){
  w.height=clampHeight(w,w.height??'auto',true);
}
// A line owns its local row grid. Infinite auto occupancy blocks stacking below
// it, while its measured natural size contributes only to the line's extent.
function packWidgets(list,width,natural=()=>0){
  const {cols,spans}=layoutSpans(list,width),phone=width<=760,lines=[];let line=null;
  const start=()=>{line={items:[],rows:0,fixed:false};lines.push(line);};
  list.forEach((w,i)=>{
    const span=spans[i],height=phone?'auto':clampHeight(w,w.height??'auto'),fixed=height!=='auto',rows=fixed?height:pixelRows(natural(w)||0);
    if(!line)start();
    let found;
    const last=line.items.at(-1),stops=[...new Set([0,...line.items.filter(p=>p.fixed).map(p=>p.row+p.rows)])].sort((a,b)=>a-b);
    for(const row of stops){
      if(last&&row<last.row)continue;
      if(line.items.length&&row>=line.rows)break;
      for(let col=last&&row===last.row?last.col+last.span:0;col+span<=cols;col++){
        if(line.items.every(p=>col+span<=p.col||col>=p.col+p.span||row+(fixed?rows:Infinity)<=p.row||row>=p.row+(p.fixed?p.rows:Infinity))){found={row,col};break;}
      }
      if(found)break;
    }
    if(!found){start();found={row:0,col:0};}
    const item={w,i,span,height,fixed,rows,...found};line.items.push(item);line.fixed ||= fixed;line.rows=Math.max(line.rows,item.row+rows);
  });
  let track=1;const tracks=[],items=[],holes=[];
  lines.forEach((line,lineIndex)=>{
    line.track=track;line.index=lineIndex;
    tracks.push(line.fixed?`repeat(${line.rows},23px)`:'auto');
    for(const p of line.items){p.line=lineIndex;p.gridRow=track+(line.fixed?p.row:0);p.rowSpan=line.fixed?(p.fixed?p.rows:line.rows-p.row):1;items.push(p);}
    if(line.fixed){
      // Tile only the empty area; equal column runs extend vertically together.
      const occupied=Array.from({length:line.rows},()=>Array(cols).fill(false));
      for(const p of line.items)for(let r=p.row;r<(p.fixed?p.row+p.rows:line.rows);r++)for(let c=p.col;c<p.col+p.span;c++)occupied[r][c]=true;
      for(let row=0;row<line.rows;row++)for(let col=0;col<cols;col++){
        if(occupied[row][col])continue;let span=0,rows=1;
        while(col+span<cols&&!occupied[row][col+span])span++;
        while(row+rows<line.rows&&occupied[row+rows].slice(col,col+span).every(v=>!v))rows++;
        for(let r=row;r<row+rows;r++)for(let c=col;c<col+span;c++)occupied[r][c]=true;
        if(rows<3||span<2)continue;
        const next=line.items.find(p=>p.row>row||p.row===row&&p.col>col);
        holes.push({col,span,rows,gridRow:track+row,rowSpan:rows,at:next?next.i:line.items.at(-1).i+1});
      }
    }else{
      const last=line.items.at(-1),col=last.col+last.span;
      if(col<cols)holes.push({col,span:cols-col,gridRow:track,rowSpan:1,at:last.i+1});
    }
    track+=line.fixed?line.rows:1;
  });
  return {cols,tracks:tracks.join(' '),items,holes,lines};
}
function queuePack(){if(packQueued)return;packQueued=true;requestAnimationFrame(()=>{packQueued=false;if(S.started&&!drag){applyPacking();if(resize)resizeReadout();}});}
// Measure an inert, unconstrained copy of the inner content at the same width.
// Its height never comes from the stretched live frame, so row growth cannot
// feed back into the next measurement. No renderer or observer runs on the copy.
function measureNatural(m){
  if(!m?.frame.isConnected||m.w.height!=='auto'||!m.body.childElementCount)return;
  const probe=m.frame.cloneNode(true);probe.classList.remove('edit','fixed-height','mixed-auto','lifted','resizing','resize-settling');probe.classList.add('natural-probe');probe.dataset.height='auto';
  probe.removeAttribute('id');for(const el of $$('[id]',probe))el.removeAttribute('id');
  Object.assign(probe.style,{width:m.frame.getBoundingClientRect().width+'px',height:'auto',gridRow:'',gridColumn:''});
  $('.w-strip',probe)?.remove();for(const el of $$('.resize-grip,.height-grip,.corner-grip,.size-readout,.widget-tools',probe))el.remove();
  probe.inert=true;probe.setAttribute('aria-hidden','true');m.frame.parentElement.append(probe);
  const height=$('.w-body',probe).getBoundingClientRect().height;probe.remove();
  if(height>0&&Math.abs(height-(m.natural||0))>1){m.natural=height;queuePack();}
}
function queueNatural(m){if(accountFamily(m.w.type))queueFit();if(m.measuring||resize?.w===m.w||m.frame.__resizeSettle)return;m.measuring=true;requestAnimationFrame(()=>{m.measuring=false;if(mounted.get(m.w.id)===m&&resize?.w!==m.w&&!m.frame.__resizeSettle)measureNatural(m);});}
// Accounts never scroll or clip. In a capped box (a fixed height, or Auto on a line with one) cards too tall for it condense,
// full → snug → compact; the line's condensed widgets share the first density at which all of them fit, so they read as one.
// Each density is tried in place within one task, every Accounts widget at once, so the measure includes the rows cards
// share (reserveCardRows) and nothing paints in between. The compact need, plus the Customize bar while customizing, is
// the widget's live minimum. An Auto neighbour that cannot fit the fixed line even at Compact stays full and grows it.
// A single card's header offset is needed for Auto packing, but not for its fixed minimum or presets.
// A pass is skipped while no box, mode or content has changed since the last one.
let fitQueued=false,fitDirty=true,fitKey='';
function queueFit(){fitDirty=true;if(fitQueued)return;fitQueued=true;requestAnimationFrame(()=>{fitQueued=false;fitAccounts();});}
function fitRows(w){const f=mounted.get(w.id)?.fit;return f?pixelRows(f.min):0;}
function fitAccounts(){
  if(!S.started||drag)return;
  const all=[...mounted.values()].filter(m=>accountFamily(m.w.type)),set=(m,d)=>{if(d)m.frame.dataset.density=d;else delete m.frame.dataset.density;};
  const ms=window.innerWidth>760?all.filter(m=>m.frame.isConnected&&!m.w.options.hideCards&&$('.account-card',m.body)):[];
  for(const m of all)if(!ms.includes(m)){set(m,'');delete m.fit;}
  const key=S.customizing+ms.map(m=>[m.w.id,m.frame.dataset.line,m.frame.className,m.body.clientWidth,m.body.clientHeight].join()).join('|');
  if(!ms.length||!fitDirty&&key===fitKey)return;fitDirty=false;fitKey=key;
  const reserve=()=>{app()?.levelSplitCards?.();app()?.reserveCardRows?.();},room=ms.map(m=>m.body.clientHeight),inset=ms.map(m=>S.customizing?m.frame.getBoundingClientRect().height-m.body.getBoundingClientRect().height:0);
  const measure=d=>{for(const m of ms)set(m,d);reserve();for(const m of ms)Object.assign(m.body.style,{flex:'none',height:'auto'});const px=ms.map(m=>m.body.getBoundingClientRect().height);for(const m of ms)Object.assign(m.body.style,{flex:'',height:''});return px;};
  const offsets=()=>ms.map(m=>m.w.type==='account-card'?(parseFloat($('.split-card',m.body)?.style.marginTop)||0):0);
  const full=measure(''),fullOffsets=offsets(),compact=measure('compact'),compactOffsets=offsets(),fits=(px,i)=>px[i]<=room[i]+.5,tight=new Map(),pick=ms.map(()=>'');
  const fixedBottom=new Map();for(const m of mounted.values())if(m.frame.classList.contains('fixed-height'))fixedBottom.set(m.frame.dataset.line,Math.max(fixedBottom.get(m.frame.dataset.line)??-Infinity,m.frame.getBoundingClientRect().bottom));
  const grows=ms.map((m,i)=>m.frame.classList.contains('mixed-auto')&&compact[i]+inset[i]>(fixedBottom.get(m.frame.dataset.line)??Infinity)-m.frame.getBoundingClientRect().top+.5);
  ms.forEach((m,i)=>{if(!grows[i]&&m.frame.matches('.fixed-height,.mixed-auto')&&!fits(full,i))tight.set(m.frame.dataset.line,[...(tight.get(m.frame.dataset.line)||[]),i]);});
  if(tight.size){const snug=measure('snug');for(const at of tight.values()){const d=at.every(i=>fits(snug,i))?'snug':'compact';for(const i of at)pick[i]=d;}}
  ms.forEach((m,i)=>set(m,pick[i]));reserve();
  let grew=false;ms.forEach((m,i)=>{const fit={min:Math.ceil(compact[i]+inset[i]-compactOffsets[i]),full:Math.ceil(full[i]+inset[i]-fullOffsets[i]),autoMin:Math.ceil((grows[i]?full[i]:compact[i])+inset[i])};if(fit.min!==m.fit?.min||fit.autoMin!==m.fit?.autoMin)grew=true;m.fit=fit;});
  if(grew)queuePack();
}
function applyPacking(list=activeWidgets(),nodes){
  const grid=$('#board-grid');if(!grid)return;
  const packed=packWidgets(list,window.innerWidth,w=>{const m=mounted.get(w.id);return m?.fit?.autoMin??m?.fit?.min??m?.natural??0;});
  grid.style.setProperty('--cols',packed.cols);grid.style.gridTemplateRows=packed.tracks;
  for(const p of packed.items){const frame=nodes?.get(p.w.id)||mounted.get(p.w.id)?.frame;if(!frame)continue;
    frame.style.setProperty('--span',p.span);frame.style.gridColumn=`${p.col+1} / span ${p.span}`;frame.style.gridRow=`${p.gridRow} / span ${p.rowSpan}`;
    frame.dataset.line=p.line;frame.dataset.height=p.height;frame.style.setProperty('--rows',p.fixed?p.height:0);
    frame.classList.toggle('fixed-height',p.fixed);frame.classList.toggle('mixed-auto',!p.fixed&&packed.lines[p.line].fixed);
    if(resize?.w===p.w){const r=resize,slot=r.slot;slot.style.gridColumn=frame.style.gridColumn;slot.style.gridRow=frame.style.gridRow;slot.style.height=p.fixed?rowPixels(p.height)+'px':'';slot.style.minHeight=p.fixed?'':(mounted.get(p.w.id)?.natural+r.insets||r.rect.height)+'px';}
  }
  const old=new Map($$('.w-gap',grid).map(el=>[el.dataset.gapKey,el])),kept=new Set();
  if(S.customizing)for(const h of packed.holes){const span=h.span*12/packed.cols,at=list[h.at]?board().widgets.indexOf(list[h.at]):board().widgets.length,key=[h.gridRow,h.col,at].join(':');let el=old.get(key);
    if(!el){el=document.createElement('button');el.className='w-gap';el.dataset.gapKey=key;el.dataset.boardAction='picker';grid.append(el);}
    placeNode(grid,el,list[h.at]?(nodes?.get(list[h.at].id)||mounted.get(list[h.at].id)?.frame):null);
    kept.add(el);el.dataset.at=at;el.dataset.span=span;if(h.rows)el.dataset.height=h.rows;else delete el.dataset.height;
    el.style.gridColumn=`${h.col+1} / span ${h.span}`;el.style.gridRow=`${h.gridRow} / span ${h.rowSpan}`;
    const text=`${sizes.find(([n])=>n===span)?.[1]||span+'/12'}${h.rows?' × '+h.rows+' rows':''}`,markup=`${icon('plus')} Add widget <small>${text}</small>`;if(el.innerHTML!==markup)el.innerHTML=markup;
  }
  for(const el of old.values())if(!kept.has(el))el.remove();
  if(!nodes)fitAccounts();
  return packed;
}
function spanOf(w){if(w.span!=='auto')return Math.max(defs.get(w.type)?.min||1,Math.min(12,Number(w.span)||defs.get(w.type)?.span||12));const p=providers().find(p=>p.id===w.options.provider);return Math.min(12,Math.max(3,(shownCards(w)||p?.accounts?.length||0)*3));}
function layoutSpans(list,width){
  const spans=list.map(spanOf);if(width>=1180)return {cols:12,spans};
  const cols=width>760?6:2, small=cols===6?3:1;
  const result=spans.map((n,i)=>cols===6?n<=6?3:6:defs.get(list[i].type)?.compact&&n<=4?1:2);
  for(let i=0;i<result.length;i++)if(result[i]===small){if(result[i+1]===small)i++;else if(list[i].type!=='account-card')result[i]=cols;}
  return {cols,spans:result};
}
const sizeLabel=w=>w.span==='auto'?'Auto':sizes.find(([s])=>s===spanOf(w))?.[1]||`${spanOf(w)}/12`;
function update(data){
  S.overview=data;
  const incoming=data.layout||{}, fingerprint=JSON.stringify(incoming);
  if(!S.layout){S.layout={version:2,revision:incoming.revision||0,accounts:clone(incoming.accounts||{}),inventory:clone(incoming.inventory||Object.keys(incoming.accounts||{})),providers:clone(incoming.providers||[]),boards:Array.isArray(incoming.boards)?clone(incoming.boards):defaults(incoming.providers)};lastServer=fingerprint;}
  else if(!S.customizing&&!S.saving&&(incoming.revision||0)>=(S.layout.revision||0)&&fingerprint!==lastServer){S.layout={...S.layout,...clone(incoming),inventory:clone(incoming.inventory||S.layout.inventory),boards:Array.isArray(incoming.boards)?clone(incoming.boards):S.layout.boards};lastServer=fingerprint;}
  // The saved inventory is separate from normalized account order, which already
  // contains newly connected providers. Keep removals even across a cold reload.
  const overview=S.layout.boards.find(b=>b.id==='overview');
  for(const p of providers())if(!S.saving&&overview&&!S.layout.inventory.includes(p.id)){
    if(!overview.widgets.some(w=>accountFamily(w.type)&&w.options.provider===p.id)){let at=0;overview.widgets.forEach((w,i)=>{if(w.type==='accounts')at=i+1;});overview.widgets.splice(at,0,widget('accounts','auto',{provider:p.id}));}
    S.layout.inventory.push(p.id);S.layout.accounts[p.id]??=(p.accounts||[]).map(a=>a.key).filter(Boolean);
  }
  for(const b of S.layout.boards)for(const w of b.widgets)normalizeHeight(w);
  if(!S.active||!S.layout.boards.some(b=>b.id===S.active))route();render();
}
function activeWidgets(){return (board()?.widgets||[]).filter(w=>S.customizing||!accountFamily(w.type)||(w.type==='account-card'?!!accountOf(w):providers().some(p=>p.id===w.options.provider&&p.accounts?.length)));}
function destroyOne(id){const m=mounted.get(id);if(!m)return;m.frame.__resizeSettle?.clear();try{defs.get(m.w.type)?.destroy?.(m.body);}catch(_){}m.observer?.disconnect();m.contentObserver?.disconnect();m.contentResizeObserver?.disconnect();m.frame.remove();mounted.delete(id);}
function unmount(){for(const id of mounted.keys())destroyOne(id);}
function queueRender(){if(renderQueued)return;renderQueued=true;requestAnimationFrame(()=>{renderQueued=false;render();});}
function accountControls(host){
  host.inert=false;
  for(const el of $$('a,button,input,select,textarea',host)){
    if(S.customizing&&!el.matches('.card-menu,.wstate.removed button')){if(!Object.hasOwn(el.dataset,'boardTabindex'))el.dataset.boardTabindex=el.getAttribute('tabindex')??'';el.tabIndex=-1;}
    else if(Object.hasOwn(el.dataset,'boardTabindex')){if(el.dataset.boardTabindex==='')el.removeAttribute('tabindex');else el.setAttribute('tabindex',el.dataset.boardTabindex);delete el.dataset.boardTabindex;}
  }
}
function renderWidget(m){
  if(!m)return;
  if(drag||resize?.w===m.w||m.frame.__resizeSettle){m.pending=true;return;}
  const {w,body}=m,d=defs.get(w.type);const generation=++m.generation;
  if(!d){m.unavailable=true;body.innerHTML='<div class="wcard"><div class="card-eyebrow">Unavailable widget</div><div class="empty-state">This widget is unavailable.</div></div>';return;}
  if(m.unavailable){body.replaceChildren();m.unavailable=false;}const ownAsync=['tokens','usage-accounts','usage-models'].includes(w.type);if(ownAsync){if(!body.childElementCount)state(body,'loading');else body.classList.add('refetch');}
  body.style.setProperty('--widget-width',(body.clientWidth||360)+'px');
  try{const result=d.render(body,context(w,body));if(accountFamily(w.type))accountControls(body);Promise.resolve(result).then(()=>{if(mounted.get(w.id)===m&&m.generation===generation){body.classList.remove('refetch');queueNatural(m);}},()=>{if(mounted.get(w.id)===m&&m.generation===generation){body.classList.remove('refetch');state(body,'error',{retry:()=>renderWidget(m)});}});}
  catch(_){state(body,'error',{retry:()=>renderWidget(m)});}
}
function strip(w,i,n){return `<button class="grip" data-board-action="grip" aria-label="Move ${esc(label(w))}, ${i+1} of ${n}" aria-describedby="boards-reorder-help">${icon('grip')}</button><button class="mv-btn icon-sm" data-board-action="move" data-dir="-1" aria-label="Move ${esc(name(w))} up" ${i===0?'disabled':''}>↑</button><button class="mv-btn icon-sm" data-board-action="move" data-dir="1" aria-label="Move ${esc(name(w))} down" ${i===n-1?'disabled':''}>↓</button><span class="w-name" title="${esc(label(w))}">${esc(typeName(w)||w.type)} <em>· ${esc(name(w))}</em></span><button class="size-btn" data-board-action="width" aria-label="Size: ${esc(sizeLabel(w))} wide, ${w.height==='auto'?'auto height':w.height+' rows tall'}" aria-haspopup="menu">${sizeLabel(w)}${w.height!=='auto'?' × '+w.height:''} ${icon('chevron')}</button><button class="icon-sm" data-board-action="settings" aria-label="Settings for ${esc(name(w))}" aria-haspopup="dialog">${icon('gear')}</button><button class="icon-sm danger" data-board-action="remove" aria-label="Remove ${esc(label(w))}">${icon('x')}</button>`;}
function renderTools(m){const over=resolveRange(m.w,find(m.w.id).b);$('.widget-tools',m.frame).innerHTML=`${over.overridden?`<span class="chip">${esc(over.label.toUpperCase())}</span>`:''}${!S.customizing&&m.w.type!=='account-card'?`<button class="w-menu" data-board-action="widget-menu" aria-label="More for ${esc(name(m.w))}" aria-haspopup="menu">${icon('more')}</button>`:''}`;}
function render(resizing=false){
  if(!S.started||!S.layout||drag||resize&&!resizing)return;
  const b=board();if(!b)return;
  const system=location.hash==='#system';document.body.classList.toggle('customizing',S.customizing);
  $('#boards-view').hidden=system;$('#overview-view').hidden=true;$('#system-view').hidden=!system;
  const help=$('#boards-reorder-help');if(help)help.textContent='Arrow keys move it. Shift and arrow keys change its size.';
  const custom=$('#customize-board');custom.disabled=S.saving;custom.hidden=system||!!S.overview?.read_only;custom.setAttribute('aria-pressed',String(S.customizing));
  renderTabs();renderBar();
  if(system){unmount();app()?.mountInventory?.(null);return;}
  app()?.mountInventory?.(b.id==='overview'?$('#board-inventory'):null);
  const showRange=b.showRange&&b.widgets.some(w=>(w.options.range||defs.get(w.type)?.defaults.range)==='board');
  $('#board-heading').innerHTML=(b.heading||showRange)?`<div class="board-head">${b.heading?`<h2>${esc(b.heading)}</h2>`:'<span></span>'}${showRange?`<div class="segmented" aria-label="Board time range">${ranges.map(([v,t])=>`<button data-board-action="range" data-value="${v}" class="${resolveRange(null,b).key===v?'selected':''}" aria-pressed="${resolveRange(null,b).key===v}">${t}</button>`).join('')}</div>`:''}</div>`:'';
  const grid=$('#board-grid'), ws=activeWidgets(), live=new Set(ws.map(w=>w.id));
  const scroll=grid.dataset.board===b.id?{x:window.scrollX,y:window.scrollY}:null;
  let prev=null;const place=node=>{let before=prev?prev.nextElementSibling:grid.firstElementChild;while(before&&['w-gap','guides','row-guides'].includes(before.className))before=before.nextElementSibling;placeNode(grid,node,before);prev=node;};
  for(const [id,m] of mounted)if(!live.has(id)||m.board!==b.id||m.w.type!==ws.find(w=>w.id===id)?.type)destroyOne(id);
  if(ws.length)$$('.empty-board',grid).forEach(el=>el.remove());
  grid.dataset.board=b.id;
  ws.forEach((w,i)=>{
    normalizeHeight(w);
    let m=mounted.get(w.id);
    if(!m){const frame=document.createElement('section');frame.className=`w w-${w.type}${w.type==='account-card'?' w-accounts':''}`;frame.dataset.wid=w.id;
      frame.innerHTML='<div class="w-strip"></div><div class="widget-tools"></div><div class="w-body"></div><div class="resize-grip" data-board-action="resize" aria-hidden="true"></div><div class="height-grip" data-board-action="resize-height" aria-hidden="true"></div><div class="corner-grip" data-board-action="resize-corner" aria-hidden="true"></div>';
      m={frame,body:$('.w-body',frame),w,board:b.id,generation:0};mounted.set(w.id,m);
      m.size={width:0,height:0};m.observer=new ResizeObserver(entries=>{const next=entries[0].contentRect;if(Math.abs(next.width-m.size.width)>4||Math.abs(next.height-m.size.height)>4){m.size={width:next.width,height:next.height};if(mounted.get(w.id)===m&&resize?.w!==m.w&&!m.frame.__resizeSettle){renderWidget(m);queueNatural(m);}}});m.observer.observe(m.body);
      m.contentResizeObserver=new ResizeObserver(()=>queueNatural(m));
      const observeContent=()=>{m.contentResizeObserver.disconnect();for(const child of m.body.children)m.contentResizeObserver.observe(child);queueNatural(m);};
      m.contentObserver=new MutationObserver(observeContent);m.contentObserver.observe(m.body,{childList:true,subtree:true,characterData:true});
    }
    m.w=w;m.frame.classList.toggle('edit',S.customizing);m.frame.classList.toggle('editing',S.overlay?.type==='settings'&&S.overlay.wid===w.id);m.frame.setAttribute('aria-label',name(w));
    const st=$('.w-strip',m.frame), markup=S.customizing?strip(w,b.widgets.indexOf(w),b.widgets.length):'';
    if(st.__markup!==markup){st.innerHTML=markup;st.__markup=markup;}st.hidden=!S.customizing;
    renderTools(m);
    $('.resize-grip',m.frame).hidden=!S.customizing;for(const handle of $$('.height-grip,.corner-grip',m.frame))handle.hidden=!S.customizing||heightBounds(w).auto;
    place(m.frame);
    if(accountFamily(w.type))accountControls(m.body);else m.body.inert=S.customizing;
  });
  applyPacking(ws);for(const w of ws){const m=mounted.get(w.id);renderWidget(m);queueNatural(m);}
  if(!ws.length&&!$('.empty-board',grid)){const el=document.createElement('div');el.className='empty-board';el.innerHTML='<div class="ghosts"><i></i><i></i><i></i></div><h2>Add your first widget</h2><button class="solid-btn" data-board-action="picker">+ Add widget</button><div class="from">Or start from: <button data-board-action="template" data-value="limits">Limits</button><button data-board-action="template" data-value="live">Live</button><button data-board-action="template" data-value="overview">Copy of Overview</button></div>';grid.append(el);}
  $('#board-foot').hidden=b.id!=='usage';$('#board-foot').textContent=b.id==='usage'?`History starts ${app()?.dateTime(app()?.usageSince(S.overview?.usage))||'—'}. These are best-effort gateway records, refreshed every 15 seconds. Subscription allowances are reported separately by the provider.`:'';
  app()?.levelHeads?.();
  // Anchor exclusion also covers asynchronous content refreshes below this render.
  if(scroll&&(window.scrollX!==scroll.x||window.scrollY!==scroll.y))window.scrollTo({left:scroll.x,top:scroll.y,behavior:'instant'});
}
function renderTabs(){
  const nav=$('.topbar nav'),addAnchor=$('.nav-add',nav);const system=$('[data-view="system"]',nav), existing=new Map($$('[data-board-tab]',nav).map(a=>[a.dataset.boardTab,a]));
  $$('[data-view]:not([data-view="system"])',nav).forEach(a=>a.remove());
  for(const b of S.layout.boards){let a=existing.get(b.id);if(!a){a=document.createElement('a');a.dataset.boardTab=b.id;}a.href=b.id==='overview'||b.id==='usage'?'#'+b.id:'#board-'+b.id;a.textContent=b.name;a.title=b.name;a.classList.toggle('active',b.id===S.active&&location.hash!=='#system');if(a.classList.contains('active'))a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');placeNode(nav,a,addAnchor||system);existing.delete(b.id);}
  for(const a of existing.values())a.remove();
  let add=$('.nav-add',nav);if(S.customizing&&!add){add=document.createElement('button');add.className='nav-add';add.dataset.boardAction='new-board';add.textContent='+ Board';nav.insertBefore(add,system);}if(!S.customizing)add?.remove();
  nav.classList.toggle('has-more',nav.scrollWidth>nav.clientWidth+2&&nav.scrollLeft+nav.clientWidth<nav.scrollWidth-2);
  nav.onscroll=()=>nav.classList.toggle('has-more',nav.scrollLeft+nav.clientWidth<nav.scrollWidth-2);
  system.classList.toggle('dim',S.customizing);system.classList.toggle('active',location.hash==='#system');if(location.hash==='#system')system.setAttribute('aria-current','page');else system.removeAttribute('aria-current');
}
function renderBar(){
  const locked=S.saving&&S.customizing;
  for(const selector of ['#boards-view','#board-grid','#boards-overlay','#customize-phone','#boards-toast','.topbar nav']){const el=$(selector);if(el)el.inert=locked;}
  const custom=$('#customize-board');if(custom)custom.disabled=S.saving;
  const bar=$('#customize-bar'), b=board(),held=bar.contains(document.activeElement)?document.activeElement.dataset.boardAction:null;bar.hidden=!S.customizing;
  bar.innerHTML=`<div class="cbar-title">${icon('layout')}<span>Customizing</span> <b>${esc(b.name)}</b></div><button class="add-button" data-board-action="picker">${icon('plus')}<span>Add widget</span></button><button class="icon-button board-more" data-board-action="board-menu" aria-label="Board options" aria-haspopup="menu">${icon('more')}</button><span class="spacer"></span>${S.error?`<span class="save-err" role="alert">${esc(S.error)}</span>`:''}<button class="ghost-btn" data-board-action="cancel" ${S.saving?'disabled':''}>Cancel</button><button class="solid-btn" data-board-action="done" ${S.saving?'disabled':''} aria-busy="${S.saving}">${S.saving?'Saving…':'Done'}</button>`;
  if(S.saving)$$('button',bar).forEach(el=>el.disabled=true);
  if(held)$(`[data-board-action="${held}"]`,bar)?.focus({preventScroll:true});
  const phone=$('#customize-phone');phone.hidden=!S.customizing;phone.innerHTML=`<div class="cbar-title">${icon('layout')}<span>Customizing</span> <b>${esc(b.name)}</b></div><button class="icon-button" data-board-action="board-menu" aria-label="Board options">${icon('more')}</button>`;
}
function enter(){if(!S.layout||S.customizing||S.saving||S.overview?.read_only)return;S.snapshot=clone(S.layout);S.customizing=true;S.error='';announce(`Customizing ${board().name}. Drag widgets, or use their move buttons.`);render();}
function dirty(){return S.customizing&&JSON.stringify(S.layout)!==JSON.stringify(S.snapshot);}
function finish(cancel=false){clearTimeout(toastTimer);toastUndo=null;if($('#boards-toast'))$('#boards-toast').hidden=true;if(cancel&&S.snapshot)S.layout=S.snapshot;S.snapshot=null;S.customizing=false;S.error='';closeOverlay(false);route();render();$('#customize-board').focus();app()?.refreshAccounts?.();}
let savingSettled=Promise.resolve(),settleWrite;
function endWrite(){S.saving=false;renderBar();settleWrite?.();settleWrite=null;}
async function persist(force=false,interactive=true){
  if(S.saving)return false;S.saving=true;savingSettled=new Promise(resolve=>settleWrite=resolve);S.error='';renderBar();
  try{const r=await fetch('/api/layout',{method:'POST',headers:{'X-Router-Request':'1','Content-Type':'application/json'},body:JSON.stringify({version:2,base_revision:S.layout.revision||0,...(force?{force:true}:{}),accounts:{...Object.fromEntries(S.layout.inventory.map(p=>[p,[]])),...S.layout.accounts},boards:S.layout.boards.map(b=>({...b,widgets:b.widgets.map(w=>{const saved={...w};if(saved.height==='auto')delete saved.height;return saved;})}))}),signal:AbortSignal.timeout(15000)});
    const data=await r.json();if(r.status===409){endWrite();if(interactive)openOverlay('conflict',{other:data.layout});return false;}if(!r.ok||!data.layout)throw Error('save');
    S.layout={...clone(data.layout),inventory:clone(data.layout.inventory||S.layout.inventory)};lastServer=JSON.stringify(data.layout);if(S.overview)S.overview.layout=clone(data.layout);endWrite();return true;
  }catch(_){endWrite();S.error="Couldn't save your layout. Try again";renderBar();announce(S.error);return false;}
}
async function done(force=false){if(await persist(force)){const next=deferredLeave;deferredLeave=null;finish();next?.();}}
async function saveOptions(w,patch,preview){
  if(preview||S.customizing&&S.saving)return;const current=find(w.id).w;if(!current)return;w=current;if(!S.customizing&&S.saving){await savingSettled;return saveOptions(find(w.id).w||w,patch,false);}const old=clone(w.options);w.options={...w.options,...patch};normalized(w);
  if(S.customizing){renderWidget(mounted.get(w.id));return;}
  if(!(await persist(false,false))){w.options=old;throw Error('Could not save widget');}render();
}
let pendingCards=null,cardWriting=false,acceptedCards=null,cardWaiters=[];
function cardOrder(layout){
  const accounts=clone(layout.accounts);
  if(S.customizing){if(S.saving)return Promise.resolve(false);S.layout.accounts=accounts;render();return Promise.resolve(true);}
  if(!cardWriting)acceptedCards=clone(S.layout.accounts);
  S.layout.accounts=accounts;pendingCards=accounts;
  const result=new Promise(resolve=>cardWaiters.push(resolve));
  if(!cardWriting)drainCardOrders();
  return result;
}
async function drainCardOrders(){
  cardWriting=true;let ok=true;
  while(pendingCards){while(S.saving)await savingSettled;const mine=pendingCards;pendingCards=null;S.layout.accounts=mine;ok=await persist(false,false);
    if(ok)acceptedCards=clone(S.layout.accounts);
    if(pendingCards){S.layout.accounts=pendingCards;continue;}
    if(!ok){S.layout.accounts=acceptedCards;toast("Couldn't save the new order");}
  }
  cardWriting=false;render();for(const resolve of cardWaiters.splice(0))resolve(ok);
}
// The header picker only changes this page view. Saved defaults belong to Board settings.
function saveBoardRange(b,value){browseRanges.set(b.id,value);render();return Promise.resolve();}
function leave(next){if(S.customizing&&dirty()){deferredLeave=next;openOverlay('leave');return;}if(S.customizing)finish();next();}
function route(){if(!S.layout)return;const prior=S.active;const hash=location.hash.slice(1);const requested=hash.startsWith('board-')?hash.slice(6):hash.startsWith('usage')?'usage':hash==='overview'?'overview':S.layout.boards[0]?.id;S.active=S.layout.boards.some(b=>b.id===requested)?requested:S.layout.boards[0]?.id;if(prior!==S.active||hash==='system')app()?.closeAccountMenus?.();}
function navigate(id){closeOverlay(false);app()?.closeAccountMenus?.();S.active=id;location.hash=id==='usage'||id==='overview'?id:'board-'+id;render();}
function openRequests(filters={},sourceRange=null){
  leave(()=>{const full=b=>b.widgets.some(w=>w.type==='requests'&&(w.options.filters||w.options.more)),any=b=>b.widgets.some(w=>w.type==='requests');const b=S.layout.boards.find(b=>b.id===S.active&&full(b))||S.layout.boards.find(b=>b.id==='usage'&&any(b))||S.layout.boards.find(full)||S.layout.boards.find(b=>b.id===S.active&&any(b))||S.layout.boards.find(any);if(!b)return;navigate(b.id);const w=b.widgets.find(w=>w.type==='requests'&&w.options.filters)||b.widgets.find(w=>w.type==='requests');const m=mounted.get(w?.id);if(m){m.body.__requests?.jump(filters,sourceRange);m.frame.scrollIntoView({block:'start',behavior:motion()?'smooth':'instant'});$('h2',m.body)?.focus({preventScroll:true});}});
}
const motion=()=>!matchMedia('(prefers-reduced-motion: reduce)').matches;
function rects(){return new Map([...mounted].map(([id,m])=>{const r=m.frame.getBoundingClientRect();return [id,{left:r.left,top:r.top,width:r.width,height:r.height,line:m.frame.dataset?.line}];}));}
function cancelGlides(){for(const m of mounted.values())for(const a of m.frame.getAnimations())if(a.id==='board-reorder')a.cancel();}
// Hold neighbours before repacking, including through renderWidget and natural ResizeObservers.
// Retargets capture rects() first, then remove the previous tween to measure the new grid boxes.
function holdNeighbours(){for(const m of mounted.values()){
  const frame=m.frame;if(frame===resize?.frame||frame.__resizeSettle&&!frame.__resizeSettle.neighbour)continue;
  frame.__resizeSettle?.clear();
  const settle={neighbour:true,clear(){cancelAnimationFrame(settle.raf);if(frame.__resizeSettle!==settle)return;delete frame.__resizeSettle;frame.classList.remove('resize-settling');for(const key of ['width','height','translate','marginBottom'])frame.style[key]='';}};
  frame.__resizeSettle=settle;
}}
function glide(before,sizing=false){
  cancelGlides();
  // Read all landing boxes before drawing any neighbour's old size.
  // Each tween starts on its first rAF timestamp, shared with the resizing widget's settle.
  const boxes=[...mounted].filter(([,m])=>!m.frame.classList.contains('lifted')&&m.frame!==resize?.frame&&(!m.frame.__resizeSettle||m.frame.__resizeSettle.neighbour)).map(([id,m])=>({m,a:before.get(id),b:m.frame.getBoundingClientRect()}));
  for(const {m,a,b} of boxes){
    const frame=m.frame;
    if(!sizing){if(motion()&&a&&(a.left!==b.left||a.top!==b.top))frame.animate([{translate:`${a.left-b.left}px ${a.top-b.top}px`},{translate:'0 0'}],{id:'board-reorder',duration:260,easing:'cubic-bezier(.2,.7,.2,1)'});continue;}
    const settle=frame.__resizeSettle;
    if(!settle)continue;
    const land=()=>{settle.clear();if(mounted.get(m.w.id)===m){m.size={width:m.body.clientWidth,height:m.body.clientHeight};renderWidget(m);queueNatural(m);}};
    if(a&&['left','top','width','height'].every(key=>Math.abs(a[key]-b[key])<.5)){settle.clear();continue;}
    if(!motion()||!a){land();continue;}
    frame.classList.add('resize-settling');
    const draw=t=>{const eased=1-(1-t)*(1-t),height=a.height+(b.height-a.height)*eased;
      frame.style.width=a.width+(b.width-a.width)*eased+'px';frame.style.height=height+'px';
      // Preserve the final grid footprint while animating the real box, so Auto tracks stay put.
      frame.style.marginBottom=b.height-height+'px';frame.style.translate=`${(a.left-b.left)*(1-eased)}px ${(a.top-b.top)*(1-eased)}px`;
    };
    draw(0);let start;const tick=now=>{if(frame.__resizeSettle!==settle)return;start??=now;const t=Math.max(0,Math.min(1,(now-start)/270));draw(t);if(t<1)settle.raf=requestAnimationFrame(tick);else land();};settle.raf=requestAnimationFrame(tick);
  }
}
function focusWidget(id){const el=mounted.get(id)?.frame;($(innerWidth<=760?'.mv-btn:not(:disabled)':'.grip',el||document)||el)?.focus({preventScroll:true});}
function changed(fn,focus){const before=rects();for(const m of mounted.values())m.frame.__resizeSettle?.clear();fn();render();glide(before);if(focus)focusWidget(focus);}
function move(id,dir){if(S.saving)return;const {b,w}=find(id);if(!w)return;const at=b.widgets.indexOf(w), next=at+dir;if(next<0||next>=b.widgets.length)return;changed(()=>{b.widgets.splice(at,1);b.widgets.splice(next,0,w);},id);announce(`${name(w)} moved to position ${next+1} of ${b.widgets.length}.`);}
function setWidth(id,value){if(S.saving)return;const {w}=find(id);if(!w)return;w.span=value==='auto'?'auto':Math.max(defs.get(w.type)?.min||1,Math.min(12,+value));closeOverlay(false);changed(()=>{},id);announce(`Width ${spanOf(w)} of 12 columns.`);}
function currentRows(w){return w.height!=='auto'&&Number.isInteger(w.height)?w.height:Math.round(((mounted.get(w.id)?.frame.getBoundingClientRect().height||0)+ROW_GAP)/ROW_STEP);}
function heightAnnouncement(w){const b=heightBounds(w),h=w.height;return h==='auto'?'Height auto.':`Height ${h} rows${h===b.min?', the minimum':h===b.max?', the maximum':''}.`;}
function setHeight(id,value){if(S.saving)return;const {w}=find(id);if(!w||heightBounds(w).auto)return;closeOverlay(false);changed(()=>w.height=clampHeight(w,value),id);announce(heightAnnouncement(w));}
function remove(id){const {b,w}=find(id);if(!w)return;const at=b.widgets.indexOf(w);closeOverlay(false);b.widgets.splice(at,1);render();focusWidget(b.widgets[Math.min(at,b.widgets.length-1)]?.id);toast(`Removed ${name(w)}`,()=>{b.widgets.splice(at,0,w);render();focusWidget(w.id);announce(`Restored ${name(w)}.`);});announce(`Removed ${name(w)}.`);}
function toast(text,undo){clearTimeout(toastTimer);toastUndo=undo;const host=$('#boards-toast');host.innerHTML=`<span>${esc(text)}</span>${undo?'<button class="undo" data-board-action="undo">Undo</button>':''}<button class="icon-sm" data-board-action="toast-close" aria-label="Dismiss">${icon('x')}</button>`;host.hidden=false;toastRemaining=undo?9000:3500;resumeToast();}
function resumeToast(){clearTimeout(toastTimer);toastAt=Date.now();toastTimer=setTimeout(()=>{$('#boards-toast').hidden=true;toastUndo=null;},Math.max(0,toastRemaining));}
function pauseToast(){clearTimeout(toastTimer);toastRemaining-=Date.now()-toastAt;}
function add(make,at,span,height){const b=board();if(b.widgets.length>=40){announce('A board can hold up to 40 widgets.');return;}if(pieceConflict(b,make))return;
  const d=defs.get(make.type),piece=make.type==='account-card'||make.type==='accounts'&&make.options?.hideCards,w=widget(make.type,!piece&&span?Math.max(span,d?.min||1):make.span||d?.span||12,{...d?.defaults,...clone(make.options||{})},make.title||'');if(height!=null&&!piece)w.height=height;normalized(w);b.widgets.splice(at??b.widgets.length,0,w);closeOverlay(false);render();const m=mounted.get(w.id);m?.frame.classList.add('flash');m?.frame.scrollIntoView({block:'center',behavior:motion()?'smooth':'instant'});focusWidget(w.id);setTimeout(()=>m?.frame.classList.remove('flash'),1400);announce(`Added ${typeName(w)||make.type}.`);}
function template(type){
  if(type==='overview')return clone(S.layout.boards.find(b=>b.id==='overview').widgets).map(w=>({...w,id:newId(w.type)}));
  if(type==='copy')return clone(board().widgets).map(w=>({...w,id:newId(w.type)}));
  if(type==='limits'){const all=window.RouterWidgets?.limits?.({overview:S.overview})||app()?.limits?.()||[],groups=new Map();for(const l of all){const key=(l.p?.id||l.provider)+':'+(l.a?.key||l.account?.key),w=l.w||l.window,old=groups.get(key),score=x=>x?.kind==='seven_day'?3:x?.seconds===604800&&!x?.model?2:1;if(!old||score(w)>score(old.w||old.window))groups.set(key,l);}const limits=[...groups.values()];return [...Array.from({length:4},(_,i)=>widget('gauge',3,{limit:limits[i]?.value||limits[i]?.id||'',style:'ring'})),widget('timeline',12,{window:'7d',forecast:true})];}
  if(type==='live')return [widget('pulse',4,{split:'provider'}),widget('health',4),widget('retry',4,{range:'board'}),widget('requests',12,{rows:10,link:'requests',filters:false,more:false,range:'board'},'Recent requests')];return [];
}
function copyBoardName(base){let n=1,name;do{const suffix=n===1?' copy':` copy ${n}`;name=base.slice(0,24-suffix.length).trimEnd()+suffix;n++;}while(S.layout.boards.some(b=>b.name===name));return name;}
function duplicateBoard(){if(S.layout.boards.length>=12)return announce('You can have up to 12 boards.');const b=clone(board());b.id=newId('board');b.builtin=false;b.name=copyBoardName(b.name);b.widgets=b.widgets.map(w=>({...w,id:newId(w.type)}));S.layout.boards.splice(S.layout.boards.indexOf(board())+1,0,b);navigate(b.id);}
function rename(){closeOverlay(false);const b=board(),tab=$(`[data-board-tab="${CSS.escape(b.id)}"]`);if(!tab)return;const input=document.createElement('input');input.className='tab-edit';input.maxLength=24;input.value=b.name;input.setAttribute('aria-label','Board name');tab.replaceWith(input);let ended=false;const end=cancel=>{if(ended)return;ended=true;if(!S.saving&&!cancel&&input.value.trim())b.name=input.value.trim();renderTabs();renderBar();input.remove();$(`[data-board-tab="${CSS.escape(b.id)}"]`)?.focus();};input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();end(false);}if(e.key==='Escape'){e.preventDefault();e.stopPropagation();end(true);}};input.onblur=()=>end(false);input.focus();input.select();}
function closeOverlay(focus=true){overlayCleanup?.();overlayCleanup=null;const old=S.overlay;S.overlay=null;$('#boards-overlay')?.replaceChildren();for(const m of mounted.values())m.frame.classList.remove('editing');if(focus){if(opener?.isConnected)opener.focus({preventScroll:true});else if(old?.wid){const frame=mounted.get(old.wid)?.frame;$(old.type==='width'?'.size-btn':S.customizing?'[data-board-action="settings"]':'[data-board-action="widget-menu"]',frame||document)?.focus({preventScroll:true});}else{const target=old?.type==='picker'?$('#customize-bar [data-board-action="picker"]'):old?.type==='new-board'?$('.nav-add'):['board-menu','board-settings'].includes(old?.type)?$(innerWidth<=760?'#customize-phone [data-board-action="board-menu"]':'#customize-bar [data-board-action="board-menu"]'):$('#customize-board');target?.focus({preventScroll:true});}}if(old?.type==='leave')deferredLeave=null;}
function openOverlay(type,extra={}){const returnTo=extra.anchor||document.activeElement;closeOverlay(false);opener=returnTo;S.overlay={type,...extra};renderOverlay();}
// A fitted widget's Short is its live minimum (its compact cards); Medium fits its full cards; Tall stays above both.
function presetsOf(w,b){const p=defs.get(w.type)?.heightPresets||[4,6,8],f=mounted.get(w.id)?.fit;if(!f)return p;const mid=Math.max(p[1],b.min+1,pixelRows(f.full));return [b.min,mid,Math.max(p[2],mid+2)];}
const overlayHead=title=>`<div class="grab"></div><div class="wpop-head"><h2>${esc(title)}</h2><button class="icon-sm" data-board-action="close" aria-label="Close">${icon('x')}</button></div>`;
function renderOverlay(){
  const o=S.overlay;if(!o)return;const root=$('#boards-overlay'), phone=innerWidth<=760, b=board(),{w}=find(o.wid),d=defs.get(w?.type);let html='',cl='menu-list',role='menu',modal=false;
  if(o.type==='width'){
    const widths=[...(w.type==='accounts'?[['auto','Auto']]:[]),...sizes].map(([v,t])=>`<button role="menuitemradio" aria-checked="${w.span===v}" data-board-action="set-width" data-value="${v}" ${v!=='auto'&&v<(d?.min||1)?'disabled title="Too narrow for this widget"':''}><span class="size-ico">${Array.from({length:12},(_,i)=>`<i class="${i<(v==='auto'?spanOf(w):v)?'on':''}"></i>`).join('')}</span>${t}</button>`).join('');
    const heightIcon=n=>n==='auto'?'<span class="h-ico auto"><span></span></span>':`<span class="h-ico"><span>${Array.from({length:8},(_,i)=>`<i class="${i>=8-n?'on':''}"></i>`).join('')}</span></span>`;
    const bounds=heightBounds(w),heights=bounds.auto?'':`<hr><div role="group" aria-label="Height"><div class="size-group-label">HEIGHT</div>${[['auto','Auto',0],...presetsOf(w,bounds).map((v,i)=>[v,['Short','Medium','Tall'][i],[4,6,8][i]])].map(([v,t,bars])=>`<button role="menuitemradio" aria-checked="${w.height===v}" data-board-action="set-height" data-value="${v}" ${v!=='auto'&&(v<bounds.min||v>bounds.max)?`disabled title="${v<bounds.min?'Too short':'Too tall'} for this widget"`:''}>${heightIcon(bars||'auto')}${t}${v==='auto'?'':`<small>${v} rows</small>`}</button>`).join('')}</div>`;
    html=`<div role="group" aria-label="Width"><div class="size-group-label">WIDTH</div>${widths}</div>${heights}`;cl+=' size-menu';
  }
  if(o.type==='widget-menu'){html=`${d?.table?'<button role="menuitem" data-board-action="table">View as table</button>':''}<button role="menuitem" data-board-action="settings">Customize this widget</button>`;}
  if(o.type==='board-menu'){const at=S.layout.boards.indexOf(b);html=`<button role="menuitem" data-board-action="rename">Rename board</button><button role="menuitem" data-board-action="board-settings">Heading &amp; time range</button><button role="menuitem" data-board-action="board-move" data-dir="-1" ${at===0?'disabled':''}>Move left</button><button role="menuitem" data-board-action="board-move" data-dir="1" ${at===S.layout.boards.length-1?'disabled':''}>Move right</button><button role="menuitem" data-board-action="board-duplicate">Duplicate board</button><hr>${b.builtin?'<button role="menuitem" data-board-action="reset">Reset to default</button>':'<button role="menuitem" class="danger" data-board-action="board-delete">Delete board</button>'}`;}
  if(o.type==='settings'){
    if(!w)return closeOverlay();cl='wpop settings';role='dialog';const ctx=context(w,null,true), options=ctx.options;
    html=overlayHead(typeName(w)||'Widget')+'<div class="preview" aria-label="Live preview"><div class="preview-host"></div></div><form class="form" onsubmit="return false">'+fieldControl({key:'$title',label:'Title',kind:'text',placeholder:d?.title?.(options,ctx)||d?.name||''},w.title||'')+(d?.fields?.(options,ctx)||[]).filter(f=>!f.hidden).map(f=>fieldControl(f,f.key==='$account'?accountValue(options):options[f.key])).join('')+'</form>'+`<div class="wpop-foot"><button class="ghost-btn" data-board-action="duplicate" ${accountFamily(w.type)?`disabled title="Each ${w.type==='accounts'?'provider':'account'} can appear once per board"`:''}>Duplicate</button><label class="visually-hidden" for="move-board-select">Move to board</label><select id="move-board-select" class="f-select" data-move-board><option value="">Move to board</option>${S.layout.boards.filter(x=>x!==b&&!pieceConflict(x,w)).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select><button class="ghost-btn danger" data-board-action="remove" aria-label="Remove ${esc(name(w))}">Remove</button></div>`;
    mounted.get(w.id)?.frame.classList.add('editing');
  }
  if(o.type==='picker'){cl='picker';role='dialog';modal=true;html=`<div class="picker-head"><span class="grab"></span><h2>Add widget</h2><div class="picker-search">${icon('search')}<input class="f-input" data-picker-search placeholder="Search widgets" aria-label="Search widgets" autocomplete="off"></div><button class="icon-sm" data-board-action="close" aria-label="Close">${icon('x')}</button></div><div class="picker-body"></div>`;}
  if(o.type==='new-board'){cl='wpop';role='dialog';html=overlayHead('New board')+`<div class="form"><label for="board-new-name">Name</label><input class="f-input" id="board-new-name" maxlength="24" placeholder="Board name"><label>Start from</label><div role="radiogroup" aria-label="Start from" class="nb-templates">${[['empty','Empty'],['copy','Copy of '+b.name],['limits','Limits'],['live','Live']].map(([v,t])=>`<button type="button" class="nb-t" role="radio" tabindex="${v===(o.template||'empty')?0:-1}" aria-checked="${v===(o.template||'empty')}" data-board-action="new-template" data-value="${v}"><span class="mini">${v==='empty'?'':'<i></i><i></i><i></i><i></i>'}</span>${esc(t)}</button>`).join('')}</div></div><div class="wpop-foot"><button class="ghost-btn" data-board-action="close">Cancel</button><button class="solid-btn" data-board-action="create">Create</button></div>`;}
  if(o.type==='board-settings'){cl='wpop';role='dialog';html=overlayHead(b.name)+`<div class="form"><label for="board-heading-input">Heading</label><input id="board-heading-input" class="f-input" maxlength="60" placeholder="None" value="${esc(b.heading)}"><label for="board-range-select">Time range</label><select id="board-range-select" class="f-select">${ranges.map(([v,t])=>`<option value="${v}" ${v===b.range?'selected':''}>${t}</option>`).join('')}</select><label class="f-switch"><span>Show the range picker</span><input type="checkbox" class="sw" role="switch" data-board-show-range ${b.showRange?'checked':''}></label></div><div class="wpop-foot"><button class="solid-btn" data-board-action="close">Done</button></div>`;}
  if(o.type==='leave'||o.type==='conflict'){cl='board-confirm';role='alertdialog';modal=true;html=`<h2>${o.type==='leave'?'Save your layout changes?':'Your layout changed on another device.'}</h2><div class="acts">${o.type==='leave'?'<button class="solid-btn" data-board-action="leave-save">Save</button><button class="ghost-btn" data-board-action="leave-discard">Discard changes</button><button class="ghost-btn" data-board-action="close">Keep customizing</button>':'<button class="solid-btn" data-board-action="keep-mine">Keep mine</button><button class="ghost-btn" data-board-action="use-other">Use the other one</button>'}</div>`;}
  if(o.type==='table'){cl='wpop data-table-dialog';role='dialog';modal=true;html=overlayHead(name(w))+'<div class="table-wrap"><div class="empty-state loading">Loading usage…</div></div>';}
  root.innerHTML=`<div class="scrim ${modal||phone?'':'clear'}" data-board-action="close"></div><div class="${cl} ${phone&&!['picker','board-confirm','wpop data-table-dialog'].includes(cl)?'sheet':''}" role="${role}" ${modal||phone?'aria-modal="true"':''} aria-label="${esc(o.type==='settings'?(typeName(w)||'Widget')+' settings':o.type==='width'?'Size':o.type==='picker'?'Add widget':o.type==='new-board'?'New board':o.type==='leave'?'Save your layout changes?':o.type==='conflict'?'Your layout changed on another device.':o.type==='board-menu'?'Board options':o.type==='board-settings'?'Heading & time range':o.type==='table'?name(w)+' data':o.type)}" tabindex="-1">${html}</div>`;
  if(o.type==='picker')renderPicker('');if(o.type==='settings')renderPreview();
  if(o.type==='table')Promise.resolve(d.table(context(w))).then(data=>{if(S.overlay!==o)return;$('.table-wrap',root).innerHTML=`<table><thead><tr>${data.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${data.rows.map(row=>`<tr>${row.map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;}).catch(()=>{if(S.overlay===o)state($('.table-wrap',root),'error',{retry:renderOverlay});});
  positionOverlay();const touch=phone||matchMedia('(pointer:coarse)').matches;const first=(touch?$('[role=dialog],[role=alertdialog]',root):$('[data-picker-search],.form input,.form select,[role^=menuitem]:not(:disabled),.acts button',root))||$('[role=dialog]',root);first?.focus({preventScroll:true});
}
function fieldControl(f,value){
  const id='board-field-'+f.key.replace(/\W/g,''), key=esc(f.key), choices=f.kind==='range'?[{value:'board',label:'Follow board · '+resolveRange(null).label},...[[ 'today','Today'],['24h','Last 24 hours'],['7d','7 days'],['30d','30 days'],['90d','90 days'],['all','All recorded']].map(([value,label])=>({value,label}))]:f.choices||[];
  if(f.kind==='switch')return `<label class="f-switch" for="${id}"><span>${esc(f.label)}</span><input id="${id}" class="sw" type="checkbox" role="switch" data-option="${key}" ${value?'checked':''}></label>`;
  let control;if(f.kind==='text'||f.kind==='textarea'){const attrs=`id="${id}" class="f-input" data-option="${key}" placeholder="${esc(f.placeholder||'')}" maxlength="${f.key==='text'?4000:f.key==='$title'?60:80}"`;control=f.kind==='textarea'?`<textarea ${attrs}>${esc(value||'')}</textarea>`:`<input ${attrs} value="${esc(value||'')}">`;}
  else if(f.kind==='tiles'||f.kind==='segmented')control=`<div class="${f.kind==='tiles'?'ctype-row':'f-seg'}" role="radiogroup" aria-label="${esc(f.label)}">${choices.map(c=>`<button type="button" class="${f.kind==='tiles'?'ctype':''}" role="radio" tabindex="${String(value)===String(c.value)?0:-1}" aria-checked="${String(value)===String(c.value)}" data-board-action="option" data-key="${key}" data-value="${esc(c.value)}" ${c.disabled?`disabled title="${esc(c.reason||'Unavailable')}"`:''}>${c.icon||''}<span>${esc(c.label)}</span></button>`).join('')}</div>`;
  else control=`<select id="${id}" class="f-select" data-option="${key}">${(()=>{let group;return choices.map(c=>{const head=c.group!==group?(group?'</optgroup>':'')+(c.group?`<optgroup label="${esc(c.group)}">`:''):'';group=c.group;return head+`<option value="${esc(c.value)}" ${String(value)===String(c.value)?'selected':''} ${c.disabled?`disabled title="${esc(c.reason||'Unavailable')}"`:''}>${esc(c.label)}</option>`;}).join('')+(group?'</optgroup>':'');})()}</select>`;
  return `<div class="f-row ${f.kind==='tiles'||f.kind==='segmented'&&choices.length>=5?'stack':''}"><label for="${id}">${esc(f.label)}</label>${control}</div>`;
}
function renderPreview(){const {w}=find(S.overlay?.wid),d=defs.get(w?.type),host=$('.preview-host');if(!w||!d||!host)return;host.inert=true;overlayCleanup?.();overlayCleanup=()=>d.destroy?.(host);try{Promise.resolve(d.render(host,context(w,host,true))).catch(()=>state(host,'empty',{message:'Preview unavailable'}));}catch(_){state(host,'empty',{message:'Preview unavailable'});}}
function editOption(key,value,rebuild=false){if(S.saving)return;const {w}=find(S.overlay?.wid);if(!w)return;if(key==='$title')w.title=String(value).slice(0,60);else if(key==='$account'){let pid,acc;try{[pid,acc]=JSON.parse(value);}catch(_){return;}const a=providers().find(p=>p.id===pid)?.accounts.find(a=>a.key===acc),next={type:'account-card',options:{provider:pid,account:acc}};if(!a||pieceConflict(find(w.id).b,next,w))return;app()?.unmountCard?.(mounted.get(w.id)?.body);Object.assign(w.options,{provider:pid,account:acc,label:cardName(pid,a).slice(0,80)});}else{if(['metric','group'].includes(key)&&((w.id==='usage-chart'&&w.title==='Requests over time')||(w.id==='request-flow'&&w.title==='Request flow')||(w.id==='usage-cache-stat'&&w.title==='Cached input')))w.title='';w.options[key]=value;}normalized(w);if(key==='$account'||key==='hideCards')render();const titleInput=$('[data-option="$title"]');if(titleInput){const d=defs.get(w.type);titleInput.placeholder=d?.title?.(context(w).options,context(w))||d?.name||'';}renderWidget(mounted.get(w.id));const m=mounted.get(w.id);if(m){renderTools(m);const b=find(w.id).b,st=$('.w-strip',m.frame),markup=strip(w,b.widgets.indexOf(w),b.widgets.length);st.innerHTML=markup;st.__markup=markup;m.frame.setAttribute('aria-label',name(w));}$('#boards-overlay [data-board-action="remove"]')?.setAttribute('aria-label','Remove '+name(w));if(rebuild){const active=document.activeElement,field=active?.dataset.option,tile=active?.dataset.key,value=active?.dataset.value;renderOverlay();const root=$('#boards-overlay'),target=field?$(`[data-option="${CSS.escape(field)}"]`,root):tile?$(`[data-key="${CSS.escape(tile)}"][data-value="${CSS.escape(value||'')}"]`,root):null;target?.focus({preventScroll:true});}else renderPreview();}
function pickerItems(){const items=[];for(const d of defs.values()){
  if(d.type==='accounts'){for(const p of providers()){
    items.push({name:'Accounts',group:p,section:d.section,type:d.type,options:{provider:p.id},span:'auto',def:d});
    items.push({name:'Header',group:p,section:d.section,type:d.type,options:{provider:p.id,hideCards:true},span:'auto',def:d});
    for(const a of orderedAccounts(p)){const label=cardName(p.id,a);items.push({name:label,group:p,section:d.section,type:'account-card',options:{provider:p.id,account:a.key,label:label.slice(0,80)},span:3,def:defs.get('account-card')});}}continue;}
  if(d.type==='account-card')continue;
  items.push({name:d.name,section:d.section,type:d.type,options:{...d.defaults},span:d.span,def:d});for(const p of d.presets||[])items.push({name:p.name,section:d.section,type:d.type,options:{...d.defaults,...p.options},span:p.span||d.span,def:d});
}return items;}
function renderPicker(q){const root=$('.picker-body');if(!root)return;const items=pickerItems();root.replaceChildren();
  const tile=it=>{const on=pieceConflict(board(),it);return `<button class="pk" data-board-action="add" data-item="${it.i}" ${on?'disabled aria-disabled="true"':''}><span class="pk-thumb"></span><span class="pk-name"><span class="pk-label">${esc(it.name)}</span>${on?'<small>ADDED</small>':''}</span></button>`;};
  for(const section of ['Accounts','Charts','Limits','Numbers','Tables','Other']){
    const rows=items.map((v,i)=>({...v,i})).filter(it=>it.section===section&&((it.group?it.group.name+' ':'')+it.name+(it.type==='account-card'?' account card':'')).toLowerCase().includes(q.toLowerCase()));if(!rows.length)continue;
    const sec=document.createElement('section');sec.className='pk-sec';sec.innerHTML=`<h3>${section.toUpperCase()}</h3>`+(section==='Accounts'?providers().map(p=>{const group=rows.filter(it=>it.group===p);return group.length?`<h4 class="pk-prov">${app()?.providerIcon?.(p,true)||''}${esc(p.name)}</h4><div class="pk-grid">${group.map(tile).join('')}</div>`:'';}).join(''):`<div class="pk-grid">${rows.map(tile).join('')}</div>`);root.append(sec);
    for(const it of rows){const host=$(`[data-item="${it.i}"] .pk-thumb`,sec);try{it.def.preview?.(host,it.options);}catch(_){host.textContent=it.name;}}
  }if(!root.children.length)root.innerHTML='<div class="empty-state">No widgets match.</div>';
}
function positionOverlay(){const pop=$('#boards-overlay > :last-child');if(!pop||innerWidth<=760||pop.matches('.picker,.board-confirm,.data-table-dialog'))return;const anchor=S.overlay?.anchor?.isConnected?S.overlay.anchor:mounted.get(S.overlay?.wid)?.frame.querySelector('[data-board-action="settings"]')||opener,r=anchor?.getBoundingClientRect?.()||{left:innerWidth/2,right:innerWidth/2,top:100,bottom:120};let left=r.right-pop.offsetWidth,top=r.bottom+8;const bar=$('#customize-bar');let min=bar&&!bar.hidden?Math.max(12,bar.getBoundingClientRect().bottom+10):12;
  if(pop.classList.contains('settings')){const wr=mounted.get(S.overlay.wid)?.frame.getBoundingClientRect();if(wr?.right+16+384<innerWidth-12){left=wr.right+16;top=Math.max(min,wr.top);}else if(wr?.left-400>12){left=wr.left-400;top=Math.max(min,wr.top);}pop.style.maxHeight=`${innerHeight-min-12}px`;}
  top=Math.max(min,Math.min(top,innerHeight-pop.offsetHeight-12));left=Math.max(12,Math.min(left,document.documentElement.clientWidth-pop.offsetWidth-12));pop.style.left=left+'px';pop.style.top=top+'px';
}
function handleAction(el,e){
  const act=el.dataset.boardAction,o=S.overlay,wid=el.closest('[data-wid]')?.dataset.wid||o?.wid,{b,w}=find(wid);
  if(S.saving||el.disabled||el.getAttribute('aria-disabled')==='true')return;
  switch(act){
    case 'customize':return S.customizing?done():enter();case 'done':return done();case 'cancel':return finish(true);
    case 'picker':enter();return openOverlay('picker',{anchor:el,at:el.dataset.at==null?null:+el.dataset.at,span:el.dataset.span==null?null:+el.dataset.span,height:el.dataset.height==null?null:+el.dataset.height});
    case 'add':return add(pickerItems()[+el.dataset.item],o.at,o.span,o.height);case 'close':return closeOverlay();
    case 'settings':enter();return openOverlay('settings',{wid,anchor:mounted.get(wid)?.frame.querySelector('[data-board-action="settings"]')||el});
    case 'width':return openOverlay('width',{wid,anchor:el});case 'set-width':return setWidth(wid,el.dataset.value);case 'set-height':return setHeight(wid,el.dataset.value==='auto'?'auto':+el.dataset.value);
    case 'remove':return remove(wid);case 'move':return move(wid,+el.dataset.dir);
    case 'widget-menu':return openOverlay('widget-menu',{wid,anchor:el});case 'table':return openOverlay('table',{wid,anchor:opener});
    case 'undo':{const undo=toastUndo;toastUndo=null;$('#boards-toast').hidden=true;clearTimeout(toastTimer);undo?.();return;}
    case 'toast-close':clearTimeout(toastTimer);toastUndo=null;$('#boards-toast').hidden=true;return;
    case 'duplicate':{if(b.widgets.length>=40)return announce('A board can hold up to 40 widgets.');if(accountFamily(w.type))return announce(`Each ${w.type==='accounts'?'provider':'account'} can appear once per board.`);const c={...clone(w),id:newId(w.type),title:w.title?(w.title+' copy').slice(0,60):''};b.widgets.splice(b.widgets.indexOf(w)+1,0,c);closeOverlay(false);render();focusWidget(c.id);return;}
    case 'option':{const old=context(w).options[el.dataset.key],v=el.dataset.value;return editOption(el.dataset.key,typeof old==='number'?+v:v,true);}
    case 'range':return saveBoardRange(board(),el.dataset.value);
    case 'board-menu':return openOverlay('board-menu',{anchor:el});case 'rename':return rename();case 'board-settings':return openOverlay('board-settings',{anchor:opener});
    case 'board-move':{const i=S.layout.boards.indexOf(board()),j=i+ +el.dataset.dir;if(j<0||j>=S.layout.boards.length)return;const [b]=S.layout.boards.splice(i,1);S.layout.boards.splice(j,0,b);closeOverlay(false);render();return;}
    case 'board-duplicate':return duplicateBoard();
    case 'board-delete':{const b=board();if(b.builtin||b.id==='overview'||b.id==='usage')return;const at=S.layout.boards.indexOf(b);S.layout.boards.splice(at,1);navigate(S.layout.boards[Math.max(0,at-1)].id);toast('Deleted '+b.name,()=>{S.layout.boards.splice(at,0,b);navigate(b.id);announce('Board restored.');});return;}
    case 'reset':{const replacement=defaults(S.layout.providers).find(x=>x.id===board().id);if(replacement){const ids=new Set(S.layout.boards.flatMap(b=>[b.id,...(b===board()?[]:b.widgets.map(w=>w.id))]));for(const w of replacement.widgets){if(ids.has(w.id))w.id=newId(w.type);ids.add(w.id);}Object.assign(board(),replacement);closeOverlay(false);unmount();render();toast('Reset '+board().name+' to default');}return;}
    case 'new-board':return openOverlay('new-board',{anchor:el});case 'new-template':o.template=el.dataset.value;$$('.nb-t').forEach(t=>{t.setAttribute('aria-checked',String(t===el));t.tabIndex=t===el?0:-1;});return;
    case 'create':{if(S.layout.boards.length>=12)return announce('You can have up to 12 boards.');const b={id:newId('board'),name:($('#board-new-name').value.trim()||'Untitled').slice(0,24),builtin:false,heading:'',range:'today',showRange:!['empty','limits'].includes(o.template||'empty'),widgets:template(o.template||'empty')};S.layout.boards.push(b);navigate(b.id);return;}
    case 'template':enter();board().widgets=template(el.dataset.value);board().showRange=el.dataset.value==='live';render();return;
    case 'leave-save':return done();case 'leave-discard':{const next=deferredLeave;deferredLeave=null;finish(true);next?.();return;}
    case 'keep-mine':return done(true);case 'use-other':{const next=deferredLeave;deferredLeave=null;S.layout={...clone(o.other),version:2,accounts:clone(o.other.accounts||{}),inventory:clone(o.other.inventory||Object.keys(o.other.accounts||{})),boards:Array.isArray(o.other.boards)?clone(o.other.boards):defaults(o.other.providers||[])};lastServer=JSON.stringify(o.other);finish();next?.();return;}
  }
}
function start(){
  if(S.started)return;S.started=true;registerLegacy();
  $('#customize-board').dataset.boardAction='customize';
  document.addEventListener('router:widget-settings',e=>{if(find(e.detail?.id).w){enter();openOverlay('settings',{wid:e.detail.id});}});
  document.addEventListener('click',e=>{
    if(S.customizing&&e.target.closest?.('.w.edit .w-body')&&!e.target.closest('.card-menu,.wstate.removed button')){e.preventDefault();e.stopImmediatePropagation();return;}
    const el=e.target.closest?.('[data-board-action]');if(el){e.preventDefault();handleAction(el,e);return;}
    const tab=e.target.closest?.('[data-board-tab]');if(tab){if(S.customizing&&tab.dataset.boardTab===S.active){e.preventDefault();rename();}return;}
    const link=e.target.closest?.('a[href]');if(link&&S.customizing){const url=new URL(link.href,location.href),internal=url.origin===location.origin&&url.pathname===location.pathname&&(url.hash==='#overview'||url.hash==='#usage'||url.hash.startsWith('#board-'));if(!internal){e.preventDefault();leave(()=>{if(url.hash==='#usage/requests'&&url.origin===location.origin)openRequests();else if(link.target==='_blank')window.open(url.href,'_blank','noopener');else location.href=url.href;});}}
  },true);
  document.addEventListener('change',e=>{if(S.customizing&&e.target.closest?.('.w.edit.w-accounts .w-body')){e.preventDefault();e.stopImmediatePropagation();app()?.refreshAccounts?.();}},true);
  document.addEventListener('input',e=>{if(S.saving)return;const el=e.target;if(el.matches('[data-picker-search]'))renderPicker(el.value);else if(el.matches('[data-option]')&&!['checkbox','select-one'].includes(el.type))editOption(el.dataset.option,el.value);else if(el.id==='board-heading-input'){board().heading=el.value;render();}});
  document.addEventListener('change',e=>{if(S.saving)return;const el=e.target;if(el.matches('[data-option]')){if(!['checkbox','select-one'].includes(el.type))return;const {w}=find(S.overlay?.wid),old=w?context(w).options[el.dataset.option]:undefined;editOption(el.dataset.option,el.type==='checkbox'?el.checked:typeof old==='number'?+el.value:el.value,true);}else if(el.id==='board-range-select'){board().range=el.value;browseRanges.delete(board().id);render();}else if(el.matches('[data-board-show-range]')){board().showRange=el.checked;render();}else if(el.matches('[data-move-board]')&&el.value){const {b,w}=find(S.overlay.wid),target=S.layout.boards.find(b=>b.id===el.value);if(pieceConflict(target,w))return;if(target.widgets.length>=40)return announce('A board can hold up to 40 widgets.');b.widgets.splice(b.widgets.indexOf(w),1);target.widgets.push(w);closeOverlay(false);render();announce('Moved to '+target.name);}});
  document.addEventListener('keydown',e=>{
    if(S.saving&&S.customizing)return;
    if(e.key==='Escape'){if(drag||resize){e.preventDefault();stopPointer(true);return;}if(S.overlay){e.preventDefault();e.stopImmediatePropagation();closeOverlay();return;}}
    const radio=e.target.closest?.('[role=radio]');if(radio&&radio.closest('#boards-overlay')&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key)){const group=radio.closest('[role=radiogroup]'),items=$$('[role=radio]:not(:disabled)',group),at=items.indexOf(radio);if(items.length){e.preventDefault();e.stopPropagation();const next=items[e.key==='Home'?0:e.key==='End'?items.length-1:(at+(['ArrowRight','ArrowDown'].includes(e.key)?1:-1)+items.length)%items.length];next.focus();next.click();}return;}
    const grip=e.target.closest?.('.grip');if(grip&&S.customizing&&/^Arrow/.test(e.key)){e.preventDefault();const id=grip.closest('[data-wid]').dataset.wid;if(e.shiftKey&&['ArrowLeft','ArrowRight'].includes(e.key))setWidth(id,spanOf(find(id).w)+(e.key==='ArrowRight'?1:-1));else if(e.shiftKey)setHeight(id,currentRows(find(id).w)+(e.key==='ArrowDown'?1:-1));else move(id,['ArrowLeft','ArrowUp'].includes(e.key)?-1:1);return;}
    const pop=$('#boards-overlay > :last-child');if(e.key==='Tab'&&pop){const focus=$$('button,input,select,textarea,a[href],[tabindex="0"]',pop).filter(el=>!el.disabled&&el.tabIndex>=0&&!el.closest('[inert]')&&el.getClientRects().length);if(focus.length){const first=focus[0],last=focus.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}}
    if(S.overlay&&['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&e.target.closest('[role=menu]')){const items=$$('[role^=menuitem]:not(:disabled)',pop),at=items.indexOf(e.target);e.preventDefault();items[e.key==='Home'?0:e.key==='End'?items.length-1:(at+(e.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();}
  },true);
  window.addEventListener('beforeunload',e=>{if(dirty()){e.preventDefault();e.returnValue='';}});
  window.addEventListener('hashchange',()=>{const target=location.hash;if(dirty()&&(target==='#system'||target==='#usage/requests')){history.replaceState(null,'',location.pathname+location.search+(S.lastHash||'#'+S.active));leave(()=>{location.hash=target;});return;}S.lastHash=target;closeOverlay(false);route();render();if(target==='#usage/requests')openRequests();});
  window.addEventListener('resize',()=>{stopPointer(true);for(const m of mounted.values())m.frame.__resizeSettle?.clear();queueRender();positionOverlay();});window.addEventListener('scroll',()=>{positionOverlay();if(drag?.phase==='drag'){drag.homes=layoutRects($('#board-grid'),dragItems());follow();}},{passive:true});
  new MutationObserver(()=>{for(const m of mounted.values())renderWidget(m);if(S.overlay?.type==='settings')renderPreview();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  const t=$('#boards-toast');t.addEventListener('pointerenter',pauseToast);t.addEventListener('pointerleave',resumeToast);t.addEventListener('focusin',pauseToast);t.addEventListener('focusout',resumeToast);
  document.addEventListener('dblclick',e=>{const handle=e.target.closest?.('.height-grip,.corner-grip');if(handle&&S.customizing&&innerWidth>=1180){e.preventDefault();stopPointer(true);setHeight(handle.closest('[data-wid]').dataset.wid,'auto');}});
  document.addEventListener('pointerdown',pointerStart);document.addEventListener('pointermove',pointerMove,{passive:false});document.addEventListener('pointerup',()=>stopPointer());document.addEventListener('pointercancel',()=>stopPointer(true));window.addEventListener('blur',()=>stopPointer(true));
  document.addEventListener('touchmove',e=>{if(drag?.phase==='drag')e.preventDefault();},{passive:false});
}
function guides(){const el=document.createElement('div');el.className='guides';el.innerHTML='<i></i>'.repeat(innerWidth>=1180?12:6);$('#board-grid').prepend(el);return el;}
// Resizing shows blocks: the 12 column tracks crossed by the row pitch. Each 37 px block ends on a
// line a fixed-height bottom edge can land on; the 20 px between rows matches the column gap.
const CELL=ROW_STEP-20;
function cellGuides(){const el=document.createElement('div');el.className='guides';el.dataset.cells='';el.innerHTML='<div></div><b><div></div></b>';$('#board-grid').prepend(el);return el;}
// The landing cell's blocks are lit through a window clipped to the cell, so they never show above
// the widget's top edge.
function markCells(r,gr,fr){const [box,lit]=r.guides?.children||[];if(!box||!lit)return;
  const top=fr.top-gr.top,first=((top-ROW_GAP-CELL)%ROW_STEP+ROW_STEP)%ROW_STEP-ROW_STEP,height=gr.height||0;
  const reach=Math.max(6,(r.kind==='width'?0:Math.min(top+rowPixels(heightBounds(r.w).max),innerHeight-gr.top)-height)+6);r.guides.style.bottom=-reach+'px';
  box.style.marginTop=first+6+'px';const fill=(el,cols,px)=>{const need=cols*(Math.ceil(px/ROW_STEP)+1);if(el.children.length<need)el.insertAdjacentHTML('beforeend','<i></i>'.repeat(need-el.children.length));};fill(box,12,height+reach-first);
  const span=Math.max(1,Math.round((fr.width+20)/((gr.width+20)/12)));
  Object.assign(lit.style,{left:fr.left-gr.left+'px',top:top+6+'px',width:fr.width+'px',height:fr.height+'px'});
  Object.assign(lit.firstElementChild.style,{gridTemplateColumns:`repeat(${span},minmax(0,1fr))`,width:fr.width+'px',marginTop:first-top+'px'});fill(lit.firstElementChild,span,fr.height+top-first);
}
const stripControls='a,button,input,select,textarea,label,summary,[role="button"],[role="switch"],[role="checkbox"],[role="radio"],[role="slider"],[role="combobox"],[role="tab"],[contenteditable]';
function pointerStart(e){if(S.saving||!S.customizing||innerWidth<=760||e.button!==0||e.isPrimary===false||drag||resize)return;const grip=e.target.closest?.('.grip'),edge=e.target.closest?.('.resize-grip'),bottom=e.target.closest?.('.height-grip'),corner=e.target.closest?.('.corner-grip');const frame=(grip||edge||bottom||corner||e.target).closest?.('[data-wid]');if(!frame)return;const {w}=find(frame.dataset.wid);if(!w)return;
  if(edge||bottom||corner){if(innerWidth<1180||((bottom||corner)&&heightBounds(w).auto))return;e.preventDefault();closeOverlay(false);cancelGlides();
    const rect=frame.getBoundingClientRect();frame.__resizeSettle?.clear();
    const grid=$('#board-grid'),gr=grid.getBoundingClientRect(),body=$('.w-body',frame),br=body.getBoundingClientRect(),step=(gr.width+20)/12;
    const slot=document.createElement('div');slot.className='w-slot resize-slot';slot.setAttribute('aria-hidden','true');grid.insertBefore(slot,frame);
    resize={rect,slot,step,maxSpan:Math.min(12,Math.round((gr.right-rect.left+20)/step)),insets:rect.height-br.height,w,frame,pointer:e.pointerId,start:e.clientX,startY:e.clientY,span:spanOf(w),rows:clampHeight(w,currentRows(w)),originalSpan:w.span,originalHeight:w.height,kind:corner?'corner':bottom?'height':'width',guides:cellGuides()};
    Object.assign(frame.style,{left:rect.left-gr.left+'px',top:rect.top-gr.top+'px',width:rect.width+'px',height:rect.height+'px'});
    frame.classList.add('resizing');frame.dataset.resize=resize.kind;applyPacking();frame.insertAdjacentHTML('beforeend','<span class="size-readout"></span>');resizeReadout();return;
  }
  // Content keeps its Customize restrictions. Only strip controls, the existing
  // content actions and cards in an Accounts group opt out of widget movement.
  if(!grip&&(e.target.closest?.('.w-strip')&&e.target.closest?.(stripControls)||e.target.closest?.('.card-menu,.wstate.removed button')||w.type==='accounts'&&$$('.account-card',frame).length>1&&e.target.closest?.('.account-card')))return;
  frame.__resizeSettle?.clear();
  drag={phase:'press',frame,w,x0:e.clientX,y0:e.clientY,x:e.clientX,y:e.clientY,grip:grip||frame,pointer:e.pointerId,order:[...board().widgets]};
  if(e.pointerType!=='mouse')drag.timer=setTimeout(lift,350);else e.preventDefault();
}
function packDrag(){const d=drag;if(!d?.slot)return;const nodes=new Map(),list=dragItems().map(el=>{const w=el===d.slot?d.w:find(el.dataset.wid).w;nodes.set(w.id,el);return w;});applyPacking(list,nodes);}
function lift(){const d=drag;if(!d||d.phase!=='press')return;clearTimeout(d.timer);closeOverlay(false);d.phase='drag';const r=d.frame.getBoundingClientRect();d.rect=r;d.slot=document.createElement('div');d.slot.className='w-slot';d.slot.style.minHeight=d.w.height==='auto'?r.height+'px':'';d.frame.before(d.slot);Object.assign(d.frame.style,{left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});d.frame.classList.add('lifted');d.guides=guides();document.body.classList.add('reordering');packDrag();d.homes=layoutRects($('#board-grid'),dragItems());d.travel={};reorderIndex(d.homes,r,d.travel);try{d.grip.setPointerCapture(d.pointer);}catch(_){}follow();}
function resizeReadout(){const r=resize;if(!r)return;const n=spanOf(r.w),b=heightBounds(r.w),rows=r.w.height==='auto'?r.rows:currentRows(r.w),limit=rows===b.min?'Min':rows===b.max?'Max':'';
  $('.size-readout',r.frame).innerHTML=r.kind==='width'?(sizes.some(([s])=>s===n)?`${sizeLabel(r.w)} · ${n}/12`:`${n}/12`):`${r.kind==='corner'?sizeLabel(r.w)+' × ':''}${rows} rows${limit?` <small>${limit}</small>`:''}`;
  const grid=$('#board-grid'),gr=grid.getBoundingClientRect(),fr=r.slot.getBoundingClientRect();markCells(r,gr,fr);
  r.rowGuides?.remove();if(r.kind==='width')return;
  const box=document.createElement('div');box.className='row-guides';
  const neighbours=[...mounted.values()].map(m=>m.frame).filter(f=>f!==r.frame&&f.dataset.height!=='auto'&&f.dataset.line===r.frame.dataset.line&&Math.abs(f.getBoundingClientRect().bottom-fr.bottom)<1),boxes=[fr,...neighbours.map(f=>f.getBoundingClientRect())],left=Math.min(...boxes.map(b=>b.left)),right=Math.max(...boxes.map(b=>b.right));
  if(neighbours.length){const line=document.createElement('i');line.className='match';Object.assign(line.style,{top:fr.bottom-gr.top+'px',left:left-gr.left-6+'px',width:right-left+12+'px'});box.append(line);}grid.append(box);r.rowGuides=box;
}
function updateResize(r){const e=r.latest;if(!e)return;r.latest=null;const dx=e.clientX-r.start,dy=e.clientY-r.startY,min=defs.get(r.w.type)?.min||1,bounds=heightBounds(r.w);
    if(r.kind!=='height')r.frame.style.width=Math.max(min*r.step-20,Math.min(r.maxSpan*r.step-20,r.rect.width+dx))+'px';
    if(r.kind!=='width')r.frame.style.height=Math.max(rowPixels(bounds.min),Math.min(rowPixels(bounds.max),r.rect.height+dy))+'px';
    const span=r.kind==='height'?r.w.span:Math.max(min,Math.min(r.maxSpan,Math.round(r.span+dx/r.step)));
    const moved=Math.abs(e.clientY-r.startY)>0||r.kind==='corner'&&Math.abs(e.clientX-r.start)>0,rows=r.kind==='width'||!moved&&r.w.height==='auto'?r.w.height:clampHeight(r.w,Math.round(r.rows+(e.clientY-r.startY)/ROW_STEP));
    if(span!==r.w.span||rows!==r.w.height){const before=rects(),sizing=rows!==r.w.height?'rows':'width';holdNeighbours();r.w.span=span;r.w.height=rows;render(true);glide(before,sizing);resizeReadout();}
}
function pointerMove(e){if(resize){e.preventDefault();const r=resize;if(e.pointerId!=null&&r.pointer!=null&&e.pointerId!==r.pointer)return;r.latest={clientX:e.clientX,clientY:e.clientY};if(!r.raf)r.raf=requestAnimationFrame(()=>{r.raf=null;if(resize===r)updateResize(r);});return;}
  const d=drag;if(!d||d.pointer!==e.pointerId)return;d.x=e.clientX;d.y=e.clientY;if(d.phase==='press'){const n=Math.hypot(d.x-d.x0,d.y-d.y0);if(d.timer&&n>8){clearTimeout(d.timer);drag=null;return;}if(!d.timer&&n>4)lift();}if(d.phase==='drag'){e.preventDefault();follow();}}

function dragItems(){return $$('#board-grid > .w, #board-grid > .w-slot').filter(el=>el!==drag?.frame);}
// A filler has no neighbouring card to overlap. Use its place in the current
// DOM order, since its saved-layout index can be stale during a drag.
function gapIndex(items){
  const d=drag,grid=$('#board-grid');
  for(const gap of $$('.w-gap',grid)){
    if(spanOf(d.w)>Number(gap.dataset.span))continue;
    const r=gap.getBoundingClientRect();
    if(d.x<r.left||d.x>=r.right||d.y<r.top||d.y>=r.bottom)continue;
    const children=[...grid.children],at=children.indexOf(gap);
    return items.filter(el=>el!==d.slot&&children.indexOf(el)<at).length;
  }
  return null;
}
function follow(){const d=drag;if(!d||d.phase!=='drag')return;d.frame.style.translate=`${d.x-d.x0}px ${d.y-d.y0}px`;
  const items=dragItems(),others=items.filter(el=>el!==d.slot),drawn=d.frame.getBoundingClientRect(),overlapIndex=reorderIndex(d.homes,drawn,d.travel),gap=gapIndex(items),index=gap??overlapIndex;
  if(gap!==null)d.travel.swap={to:index,gap:true,x:(drawn.left+drawn.right)/2,y:(drawn.top+drawn.bottom)/2};
  if(index!==items.indexOf(d.slot)){
    const positions=rects();cancelGlides();const before=others[index];
    if(before)before.before(d.slot);else others.at(-1).after(d.slot);
    packDrag();d.homes=layoutRects($('#board-grid'),dragItems());glide(positions);
  }
  const zone=64,push=d.y<zone?d.y-zone:d.y>innerHeight-zone?d.y-innerHeight+zone:0;if(push&&!d.scrollFrame)d.scrollFrame=requestAnimationFrame(()=>{d.scrollFrame=null;if(drag===d){const was=scrollY;scrollBy(0,Math.round(push/zone*14));if(scrollY!==was){d.homes=layoutRects($('#board-grid'),dragItems());follow();}}});
}
function stopPointer(cancel=false){
  if(resize){const r=resize;cancelAnimationFrame(r.raf);updateResize(r);const free=r.frame.getBoundingClientRect(),before=rects();holdNeighbours();resize=null;if(cancel){r.w.span=r.originalSpan;r.w.height=r.originalHeight;}r.slot.remove();r.guides?.remove();r.rowGuides?.remove();r.frame.classList.remove('resizing');delete r.frame.dataset.resize;$('.size-readout',r.frame)?.remove();
    const frame=r.frame,m=mounted.get(r.w.id),settle={clear(){cancelAnimationFrame(settle.raf);if(frame.__resizeSettle!==settle)return;delete frame.__resizeSettle;frame.classList.remove('resize-settling');for(const key of ['width','height','translate','marginBottom'])frame.style[key]='';}};
    frame.__resizeSettle=settle;frame.classList.add('resize-settling');
    for(const key of ['left','top','width','height'])frame.style[key]='';
    render();measureNatural(m);applyPacking();before.delete(r.w.id);const target=frame.getBoundingClientRect();
    const land=()=>{settle.clear();if(mounted.get(r.w.id)===m){m.size={width:m.body.clientWidth,height:m.body.clientHeight};renderWidget(m);queueNatural(m);}};
    // Hold the final Auto track footprint; neighbours animate independently to their final boxes.
    if(motion()){
      const draw=t=>{const eased=1-(1-t)*(1-t),height=free.height+(target.height-free.height)*eased;frame.style.width=free.width+(target.width-free.width)*eased+'px';frame.style.height=height+'px';frame.style.marginBottom=target.height-height+'px';frame.style.translate=`${(free.left-target.left)*(1-eased)}px ${(free.top-target.top)*(1-eased)}px`;};
      draw(0);glide(before,r.kind==='width'?'width':'rows');let start;const tick=now=>{if(frame.__resizeSettle!==settle)return;start??=now;const t=Math.max(0,Math.min(1,(now-start)/270));draw(t);if(t<1)settle.raf=requestAnimationFrame(tick);else land();};settle.raf=requestAnimationFrame(tick);
    }else{glide(before,r.kind==='width'?'width':'rows');land();}
    announce(r.kind==='width'?`Width ${spanOf(r.w)} of 12 columns.`:heightAnnouncement(r.w));return;}
  const d=drag;if(!d)return;drag=null;clearTimeout(d.timer);cancelAnimationFrame(d.scrollFrame);if(d.phase!=='drag')return;
  const before=rects();d.frame.classList.remove('lifted');for(const key of ['left','top','width','height','translate'])d.frame.style[key]='';if(!cancel){d.slot.before(d.frame);const ids=$$('#board-grid > .w').map(el=>el.dataset.wid);board().widgets.sort((a,b)=>ids.indexOf(a.id)-ids.indexOf(b.id));}else board().widgets=d.order;d.slot.remove();d.guides.remove();document.body.classList.remove('reordering');render();glide(before);focusWidget(d.w.id);announce(`${name(d.w)} moved to position ${board().widgets.indexOf(d.w)+1} of ${board().widgets.length}.`);
}
async function usageFor(ctx){if(ctx.range.key==='today')return ctx.overview;return ctx.api('/api/overview',{since:ctx.range.since});}
const rangeField={key:'range',label:'Time range',kind:'range'};
const choices=values=>values.map(v=>Array.isArray(v)?{value:v[0],label:v[1]}:{value:v,label:String(v)});
function mini(host,type,opts={}){
  const text=(x,y,t,size=8)=>`<text x="${x}" y="${y}" fill="var(--muted)" font-size="${size}">${t}</text>`;
  const line=(x,y,w,color='var(--line)')=>`<rect x="${x}" y="${y}" width="${w}" height="3" rx="1" fill="${color}"/>`;
  let content='';
  if(type==='heading')content=text(18,37,'Section heading',12);
  else if(type==='tokens')content=text(18,34,'1.2B',20)+line(18,45,120,'var(--blue)');
  else if(type==='accounts'){const accent=`var(--${opts.accent||'blue'})`,n=Math.min(3,opts.n||1),width=(124-(n-1)*6)/n;
    content=`<rect x="18" y="16" width="9" height="9" rx="3" fill="${accent}"/>`+text(31,24,esc(opts.name||''))+Array.from({length:n},(_,i)=>`<rect x="${18+i*(width+6)}" y="30" width="${width}" height="26" rx="4" fill="var(--raised)" stroke="var(--line)"/>`+line(23+i*(width+6),44,Math.max(8,width-14),accent)).join('');}
  else if(type==='header'){content=`<rect x="18" y="22" width="18" height="18" rx="5" fill="var(--${opts.accent})"/>`+text(44,31,esc(opts.name),9)+`<circle cx="46" cy="38" r="2" fill="var(--mint)"/>`+line(51,37,40)+[0,1,2].map(i=>line(18+i*34,49,24)).join('');}
  else if(type==='account-card'){const accent=`var(--${opts.accent})`,pct=opts.pct??62;content=`<rect x="48" y="14" width="64" height="42" rx="5" fill="var(--raised)" stroke="var(--line)"/><rect x="54" y="19" width="8" height="8" rx="2" fill="${accent}"/>`+line(66,22,28)+`<text x="54" y="43" fill="${accent}" font-size="13">${pct}%</text>`+line(54,48,52)+line(54,48,Math.round(52*pct/100),accent);}
  else if(type==='usage-accounts')content=text(18,23,'Account   Requests   Tokens')+[0,1,2].map(i=>line(18,30+i*9,25,i?'var(--claude)':'var(--codex)')+text(64,34+i*9,'1,240   2.4M')).join('');
  else if(type==='usage-models')content=text(18,23,'Model      Requests')+[0,1,2].map(i=>text(18,35+i*9,['gpt-5','claude','other'][i])+line(66,30+i*9,70-i*18,'var(--blue)')).join('');
  else content=text(18,23,'Requests       Latest')+[0,1,2].map(i=>`<circle cx="22" cy="${33+i*9}" r="2" fill="var(--mint)"/>`+line(30,31+i*9,65)+text(109,35+i*9,`${i+1}s ago`)).join('');
  host.innerHTML=`<svg viewBox="0 0 160 70" aria-hidden="true"><rect x="8" y="10" width="144" height="50" rx="5" fill="var(--surface)" stroke="var(--line)"/>${content}</svg>`;
}
// A fixed-height Requests widget loads enough rows to fill its box (about 41.5 px per row), never fewer than its setting.
function requestsWanted(c){const rows=Number(c.options.rows)||6,h=c.widget?.height;return window.innerWidth>760&&Number.isInteger(h)?Math.max(rows,Math.ceil((57*h-34)/41.5)+1):rows;}
function registerLegacy(){
  const thumbOptions=o=>({accent:app()?.accentOf?.(o.provider)||o.provider,name:app().providerName(o.provider),n:providers().find(p=>p.id===o.provider)?.accounts.length});
  register({type:'accounts',minRows:6,maxRows:20,heightPresets:[8,10,14],name:'Accounts',section:'Accounts',span:'auto',min:3,defaults:{provider:'codex',hideRouting:false,hideCards:false},title:o=>app().providerName(o.provider),fields:()=>[{key:'hideCards',label:'Hide account cards',kind:'switch'},{key:'hideRouting',label:'Hide routing control',kind:'switch'}],preview:(h,o)=>mini(h,o.hideCards?'header':'accounts',thumbOptions(o)),render(host,ctx){
    host.classList.toggle('hide-routing',!!ctx.options.hideRouting);host.classList.toggle('split-head',!!ctx.options.hideCards||ctx.preview&&!shownCards(ctx.widget,ctx.board));
    if(ctx.preview){app().previewAccounts(host,ctx.options.provider);return;}app().mountAccounts(host,ctx.options.provider,ctx.widget.title);
  },destroy:host=>app().unmountAccounts(host)});
  register({type:'account-card',minRows:4,maxRows:18,heightPresets:[6,8,12],name:'Account',section:'Accounts',span:3,min:3,defaults:{provider:'',account:'',label:''},
    title:o=>{const a=accountOf({options:o});return `${app().providerName(o.provider)} · ${a?cardName(o.provider,a):o.label||o.account}`;},
    fields:(o,ctx)=>{const list=[];for(const p of providers())for(const a of orderedAccounts(p)){const options={provider:p.id,account:a.key},mine=p.id===o.provider&&a.key===o.account;list.push({value:accountValue(options),group:p.name,label:`${p.name} · ${cardName(p.id,a)}`,disabled:!mine&&pieceConflict(ctx.board,{type:'account-card',options},ctx.widget),reason:'Already on this board'});}
      if(o.account&&!accountOf({options:o}))list.unshift({value:accountValue(o),label:`${app().providerName(o.provider)} · ${o.label||o.account} (removed)`,disabled:true});return [{key:'$account',label:'Account',kind:'select',choices:list}];},
    preview:(h,o)=>{const value=app()?.cardRemaining?.(o.provider,o.account),pct=Number.isFinite(value)?value:62;mini(h,'account-card',{...thumbOptions(o),pct:Math.round(Math.max(0,Math.min(100,pct)))});},
    render(host,ctx){const o=ctx.options;if(ctx.preview?app().previewCard(host,o.provider,o.account):app().mountCard(host,o.provider,o.account))return;
      if(!ctx.preview)app().unmountCard(host);host.replaceChildren();state(host,'removed',{message:`${o.label||'This account'} was removed`,retry:ctx.openSettings});if(ctx.preview)host.querySelector('button')?.remove();
    },destroy:host=>app().unmountCard(host)});
  register({type:'heading',autoHeightOnly:true,name:'Heading',section:'Other',span:12,min:3,defaults:{text:'Section',link:'none'},title:o=>o.text||'Heading',fields:()=>[{key:'text',label:'Text',kind:'text'},{key:'link',label:'Link',kind:'select',choices:choices([['none','None'],['usage','Explore usage'],['requests','See all requests'],...S.layout.boards.map(b=>['board-'+b.id,b.name])])}],preview:h=>mini(h,'heading'),render(host,ctx){const o=ctx.options,url=o.link==='usage'?'#usage':o.link==='requests'?'#usage/requests':'#'+o.link;host.innerHTML=`<div class="section-label"><h2>${esc(ctx.widget.title||o.text)}</h2>${o.link&&o.link!=='none'?`<a class="text-link" href="${esc(url)}">${o.link==='usage'?'Explore usage':o.link==='requests'?'See all':esc(S.layout.boards.find(b=>'board-'+b.id===o.link)?.name||'Open board')} ${icon('arrow')}</a>`:''}</div>`;}});
  register({type:'tokens',minRows:4,maxRows:8,name:'Tokens & cache',section:'Numbers',span:5,min:3,defaults:{range:'board'},title:()=> 'Tokens processed',fields:()=>[rangeField],preview:h=>mini(h,'tokens'),async render(host,ctx){const generation=host.__usageGeneration=(host.__usageGeneration||0)+1;const data=await usageFor(ctx);if(host.__usageGeneration!==generation)return;const a=app(),u=data.usage;if(!u?.available)return ctx.state(host,'empty',{message:'Waiting for recorded usage'});const summaries=Object.entries(a.usageProviders(u)),all=a.totals(summaries.flatMap(([p,s])=>(s.accounts||[]).map(r=>({...r,...a.promptUsage(p,r)}))));const split=summaries.map(([p,s])=>`<span><i style="background:var(--${a.accentOf(p)})"></i>${esc(a.providerName(p))} ${esc(a.compact(a.totals(s.accounts||[]).total_tokens))}</span>`).join('');host.innerHTML=`<article class="activity-card"><div class="card-eyebrow">${esc(ctx.widget.title||'TOKENS PROCESSED')}</div><div class="activity-value">${a.compact(all.total_tokens)}</div><p class="token-detail" title="${esc(a.number(all.prompt_tokens)+' input · '+a.number(all.output_tokens)+' output tokens')}">${split}</p><div class="token-track"><span style="width:${a.cachedShare(all)||0}%"></span></div><div class="track-label"><span>Cached input</span><b>${a.cachedShareLabel(all)}</b></div><div class="track-label"><span>Cache read · write</span><b>${a.cacheAmount(all,'cache_read',a.compact)} · ${a.cacheAmount(all,'cache_creation',a.compact)}</b></div></article>`;}});
  for(const type of ['usage-accounts','usage-models'])register({type,minRows:4,maxRows:20,name:type==='usage-accounts'?'Usage by account':'Models',section:'Tables',span:12,min:6,defaults:{range:'board'},title:()=>type==='usage-accounts'?'Usage by account':'Models',fields:()=>[rangeField],preview:h=>mini(h,type),async render(host,ctx){const generation=host.__usageGeneration=(host.__usageGeneration||0)+1;const data=await usageFor(ctx);if(host.__usageGeneration!==generation)return;const a=app(),u=data.usage;if(!u?.available)return ctx.state(host,'empty',{message:'Usage data is currently unavailable.'});const summaries=Object.entries(a.usageProviders(u));let html;
    if(type==='usage-accounts'){const rows=summaries.flatMap(([p,s])=>(s.accounts||[]).map(r=>({...r,...a.promptUsage(p,r),provider:a.providerAccount(p,r.auth_index,r.email,r.key)?.provider||p}))),both=(r,key)=>a.phoneNumber(a.cacheAmount(r,key,a.number),a.cacheAmount(r,key,a.compact));html=a.usageTable(['Gateway','Account','Requests','Input tokens','Cached input','Cache read','Cache write','Output tokens','Failures'],[['u-side'],['u-name'],['u-fig','Requests'],['u-fig','Input'],['u-fig','Cached'],['u-fig','Cache read'],['u-fig','Cache write'],['u-fig','Output'],['u-fig','Failures']],rows.map(r=>[`<span class="mono gateway-name">${esc(a.providerName(r.provider))}</span>`,a.providerChip(r.provider,r.auth_index,r.email,r.key),a.number(r.requests),a.phoneNumber(a.number(r.prompt_tokens),a.compact(r.prompt_tokens)),both(r,'cached_prompt'),both(r,'cache_read'),both(r,'cache_creation'),a.phoneNumber(a.number(r.output_tokens),a.compact(r.output_tokens)),a.number(r.failures)]),rows.map(r=>a.gatewayRow(r.provider)));}
    else {const rows=summaries.flatMap(([provider,s])=>(s.models||[]).map(r=>({...r,provider}))).sort((a,b)=>(b.requests||0)-(a.requests||0));html=a.usageTable(['Model','Requests','Tokens','Average duration'],[['u-name'],['u-fig','Requests'],['u-fig','Tokens'],['u-fig','Average']],rows.map(r=>[esc(r.model),a.number(r.requests),a.phoneNumber(a.number(r.total_tokens),a.compact(r.total_tokens)),r.latency_ms==null?'—':`${(r.latency_ms/1000).toFixed(1)}s`]),rows.map(r=>a.gatewayRow(r.provider)));}
    host.innerHTML=`<div class="section-label"><h2>${esc(ctx.widget.title|| (type==='usage-accounts'?'Usage by account':'Models'))}</h2>${type==='usage-accounts'?'<span>Token counts, not subscription allowance</span>':''}</div><div class="table-wrap stack-table">${html}</div>`;
  }});
  register({type:'requests',minRows:4,maxRows:20,name:'Requests',section:'Tables',span:12,min:4,defaults:{rows:6,link:'requests',failed:false,filters:false,more:false,range:'board'},title:()=> 'Requests',fields:()=>[{key:'rows',label:'Requests loaded',kind:'segmented',choices:choices([6,10,20,50])},{key:'link',label:'Link',kind:'select',choices:choices([['none','None'],['requests','See all requests'],['usage','Explore usage']])},{key:'failed',label:'Failed requests only',kind:'switch'},{key:'filters',label:'Filters above the list',kind:'switch'},{key:'more',label:'Show more button',kind:'switch'},rangeField],preview:h=>mini(h,'requests'),render(host,ctx){if(ctx.preview){host.innerHTML=`<div class="section-label"><h2>${esc(ctx.widget.title||'Requests')}</h2></div><div class="table-wrap stack-table">${app().requestTable((ctx.overview?.usage?.recent||[]).slice(0,3),ctx.range.key!=='today')}</div>`;return;}if(!host.__requests)host.__requests=new Requests(host);return host.__requests.render(ctx);},destroy(host){host.__requests?.destroy();delete host.__requests;}});
}
class Requests {
  constructor(host){this.host=host;this.rows=[];this.next=null;this.until=null;this.provider='';this.account='';this.failed=false;this.kind='';this.epoch=0;this.phase='idle';this.key='';this.optionsKey='';this.newer=false;this.jumpOpen=false;this.fresh=null;this.freshTimer=0;this.handler=e=>this.event(e);for(const name of ['click','change','submit','keydown'])host.addEventListener(name,this.handler);}
  destroy(){this.epoch++;clearTimeout(this.freshTimer);for(const name of ['click','change','submit','keydown'])this.host.removeEventListener(name,this.handler);}
  async render(ctx){this.baseCtx=ctx;if(this.rangeOverride){if(ctx.range.key!==this.rangeOverrideBoard)this.rangeOverride=null;else ctx={...ctx,range:this.rangeOverride};}this.ctx=ctx;const optKey=JSON.stringify([ctx.options,ctx.widget.title]),key=JSON.stringify([ctx.range.key,ctx.range.since.toISOString(),requestsWanted(ctx),ctx.options.failed,ctx.options.failedOnly]);if(key!==this.key){const first=!this.key,nextFailed=!!(ctx.options.failed||ctx.options.failedOnly);this.key=key;if(first){this.provider='';this.account='';this.kind='';}if(first||nextFailed!==this.defaultFailed)this.failed=nextFailed;this.defaultFailed=nextFailed;if(this.until&&Date.parse(this.until)<+ctx.range.since)this.until=null;return this.load();}if(this.provider&&!providers().some(p=>p.id===this.provider)){this.provider='';this.account='';this.kind='';return this.load();}
    if(this.optionsKey!==optKey){this.optionsKey=optKey;this.draw();}if(this.phase!=='idle')return;
    if(this.error&&!this.rows.length)return this.load();
    if(ctx.options.filters||ctx.options.more||this.rows.length>requestsWanted(ctx)||this.until||this.nextUsed){try{const epoch=this.epoch;const page=await this.page();if(this.epoch!==epoch)return;if(!this.until&&page.rows?.some((r,i)=>r.id!==this.rows[i]?.id||r.failed!==this.rows[i]?.failed)){this.newer=true;this.draw();}}catch(_){}return;}
    return this.load(true);
  }
  async page(before){const c=this.ctx;return c.api('/api/requests',{since:c.range.since,limit:requestsWanted(c),...(this.until?{until:this.until}:{}),...(this.provider?{provider:this.provider}:{}),...(this.account?{account:this.account}:{}),...(this.failed?{failed:1}:{}),...(this.failed&&this.kind?{kind:this.kind}:{}),...(before?{before}: {})});}
  async load(quiet=false,more=false){const counts=this.counts();if(this.kind&&counts&&counts[this.kind]===0)this.kind='';const epoch=++this.epoch;this.phase=more?'more':this.rows.length?'reloading':'loading';this.error='';this.moreError=false;if(!quiet)this.draw();try{const previousIds=this.showNew?new Set(this.rows.map(r=>r.id)):null;const page=await this.page(more?this.next:null);if(!Array.isArray(page?.rows)||!(page.next==null||typeof page.next==='string'))throw Error('Invalid page');if(this.epoch!==epoch)return;this.fresh=previousIds?new Set(page.rows.filter(r=>!previousIds.has(r.id)).map(r=>r.id)):null;this.showNew=false;clearTimeout(this.freshTimer);if(this.fresh?.size)this.freshTimer=setTimeout(()=>{this.fresh=null;this.draw();},2500);this.rows=more?[...this.rows,...page.rows]:page.rows||[];this.next=page.next||null;this.archived=page.archived_before;this.phase='idle';this.newer=false;this.nextUsed=more;this.draw();if(more){const row=$('tbody',this.host)?.rows[this.rows.length-(page.rows?.length||0)];if(row){row.tabIndex=-1;row.focus({preventScroll:true});}}if(this.seek){this.host.closest('.w')?.scrollIntoView({block:'start'});$('h2',this.host)?.focus({preventScroll:true});this.seek=false;}}
    catch(_){if(this.epoch!==epoch)return;this.phase='idle';if(more)this.moreError=true;else this.error="Couldn't load requests.";this.draw();}
  }
  jump(filters,sourceRange){if(sourceRange&&sourceRange.key!==this.ctx.range.key){this.rangeOverrideBoard=this.ctx.range.key;this.rangeOverride={...sourceRange,overridden:true};this.ctx={...this.ctx,range:this.rangeOverride};this.key=JSON.stringify([this.ctx.range.key,this.ctx.range.since.toISOString(),requestsWanted(this.ctx),this.ctx.options.failed,this.ctx.options.failedOnly]);}this.until=filters.until?(filters.until instanceof Date?filters.until.toISOString():typeof filters.until==='number'?new Date(filters.until).toISOString():filters.until):null;this.provider=filters.provider||'';this.account=filters.account||'';this.failed=!!filters.failed;this.kind=filters.kind||'';this.nextUsed=false;this.seek=true;this.load();}
  draw(){
    const h=this.host,c=this.ctx,o=c.options,a=app(),busy=['loading','reloading','more'].includes(this.phase),title=c.widget.title||'Requests';
    // Preserve a focused jump input while the live overview refreshes.
    const focus=document.activeElement,held=h.contains(focus)?focus.dataset?.request:null,heldKind=held==='kind'?focus.dataset.kind:null,editing=h.contains(focus)&&focus.matches('[data-request-time-input]'),editValue=editing?focus.value:null;
    const link=o.link&&o.link!=='none'?`<a class="text-link" href="${o.link==='usage'?'#usage':'#usage/requests'}">${o.link==='usage'?'Explore usage':'See all'} ${icon('arrow')}</a>`:'';
    const ps=providers(),counts=this.counts(),ftext={rate_limit:'Rate limited',capacity:'At capacity',auth:'Sign-in required',server_error:'Provider error',timeout:'Timed out',cancelled:'Cancelled',other:'Other',unknown:'Unknown'};
    h.innerHTML=`<section class="requests"><div class="section-label requests-head"><h2 tabindex="-1">${esc(title)}</h2><div class="requests-tools">${this.rangeOverride?`<span class="chip">${esc(c.range.label.toUpperCase())}</span>`:''}${this.account||this.provider?`<button class="chip request-filter" data-request="clear-filter" aria-label="Clear account and provider filter">${esc(this.filterLabel())} ×</button>`:''}${link}${this.newer&&!this.until?'<button class="text-link" data-request="new">Show new requests '+icon('refresh')+'</button>':''}${this.until||this.rangeOverride||this.account||this.provider?'<button class="text-link" data-request="latest">Back to latest '+icon('to-top')+'</button>':''}${o.filters?`<label class="requests-failed"><span>Failed only</span><input type="checkbox" role="switch" data-request="failed" ${this.failed?'checked':''}></label><div class="requests-picks">${ps.length>1?`<select class="routing-trigger filter-trigger" data-request="provider" aria-label="Provider"><option value="">All providers</option>${ps.map(p=>`<option value="${esc(p.id)}" ${this.provider===p.id?'selected':''}>${esc(p.name)}</option>`).join('')}</select>`:''}<button class="routing-trigger time-trigger" data-request="time" aria-label="Time" aria-haspopup="dialog" aria-expanded="${this.jumpOpen}">${icon('clock')}<span class="routing-value">${this.until?esc(a.dateTime(this.until)):'Latest'}</span>${icon('chevron')}</button></div>`:''}</div></div>${o.filters&&this.failed?`<div class="request-reasons" role="group" aria-label="Failure reason"><button class="reason-chip ${!this.kind?'selected':''}" data-request="kind" data-kind="" aria-pressed="${!this.kind}">All reasons</button>${Object.entries(ftext).filter(([key])=>!counts||counts[key]>0||key===this.kind).map(([key,text])=>`<button class="reason-chip ${this.kind===key?'selected':''}" data-request="kind" data-kind="${key}" aria-pressed="${this.kind===key}">${text}${counts?` <span>${app().number(counts[key])}</span>`:''}</button>`).join('')}</div>`:''}
      ${this.jumpOpen?`<div class="board-request-jump" role="dialog" aria-label="Show requests up to"><form data-request-form><label>Show requests up to <input class="jump-input" type="${c.range.key==='today'?'time':'datetime-local'}" step="60" data-request-time-input required value="${esc(editValue||this.timeValue())}"></label><p class="jump-error" role="alert" ${this.jumpError?'':'hidden'}>${esc(this.jumpError||'')}</p><div class="jump-actions"><button class="resets-button" type="button" data-request="latest">Latest</button><button class="reset-primary" type="submit">Go</button></div></form></div>`:''}
      <div class="table-wrap stack-table request-list ${this.phase==='reloading'?'refetch':''}" ${busy?'aria-busy="true"':''}>${this.error&&!this.rows.length?`<div class="empty-state" role="alert">${esc(this.error)} <button class="resets-button" data-request="retry">Try again</button></div>`:this.rows.length?a.requestTable(this.rows,c.range.key!=='today',!!o.filters,this.fresh):`<div class="empty-state ${busy?'loading':''}">${busy?'Loading requests…':this.empty()}</div>`}</div>${this.error&&this.rows.length?`<p class="requests-error" role="alert">${esc(this.error)} <button class="resets-button" data-request="retry">Try again</button></p>`:''}${this.archived&&!this.next&&+c.range.since<Date.parse(this.archived)?'<div class="empty-state">Older requests archived</div>':''}${o.more&&this.next?`<div class="requests-more">${this.moreError?'<p class="requests-error" role="alert">Couldn’t load more requests.</p>':''}<button class="more-button" data-request="more" aria-busy="${this.phase==='more'}" ${busy?'aria-disabled="true"':''}>${this.phase==='more'?'Loading…':this.moreError?'Try again':'Show more'}</button></div>`:''}<p class="visually-hidden" role="status">${this.rows.length} ${this.failed?'failed ':''}requests shown</p></section>`;
    if(editing){const input=$('[data-request-time-input]',h);input?.focus({preventScroll:true});}else if(held){const target=held==='kind'?$(`[data-request="kind"][data-kind="${CSS.escape(heldKind||'')}"]`,h):$(`[data-request="${CSS.escape(held)}"]`,h);target?.focus({preventScroll:true});}
  }
  filterLabel(){const p=providers().find(p=>p.id===this.provider),a=p?.accounts?.find(a=>a.auth_index===this.account);return this.account?(a?.email||a?.label||this.account):app().providerName(this.provider);}
  resetLatest(){this.until=null;this.jumpOpen=false;this.nextUsed=false;this.provider='';this.account='';this.kind='';this.failed=!!(this.baseCtx.options.failed||this.baseCtx.options.failedOnly);this.rangeOverride=null;this.ctx=this.baseCtx;this.key=JSON.stringify([this.ctx.range.key,this.ctx.range.since.toISOString(),requestsWanted(this.ctx),this.ctx.options.failed,this.ctx.options.failedOnly]);}
  counts(){
    const c=this.ctx,u=c.overview?.usage;if(c.range.key!=='today'||this.until||this.account||!u?.available)return null;
    const valid=x=>x&&['rate_limit','capacity','auth','server_error','timeout','cancelled','other','unknown'].every(k=>Number.isInteger(x[k])&&x[k]>=0);
    if(!this.provider)return valid(u.failure_counts)?u.failure_counts:null;
    const rows=Object.entries(app().usageProviders(u)),mine=rows.find(([p])=>p===this.provider)?.[1];
    if(!valid(mine?.failure_counts))return null;
    if((mine.accounts||[]).some(r=>app().providerAccount(this.provider,r.auth_index,r.email,r.key)?.provider&&app().providerAccount(this.provider,r.auth_index,r.email,r.key).provider!==this.provider&&r.failures))return null;
    return mine.failure_counts;
  }
  empty(){if(this.archived&&!this.next&&+this.ctx.range.since<Date.parse(this.archived))return 'Older requests archived.';const who=(this.failed?'failed ':'')+(this.provider?app().providerName(this.provider)+' ':'');return `No ${esc(who)}requests ${this.ctx.range.key==='today'?'today':this.ctx.range.label==='All recorded'?'recorded':'in the last '+this.ctx.range.label.toLowerCase()}.`;}
  timeValue(){const d=new Date(this.until||Date.now()),z=n=>String(n).padStart(2,'0'),time=z(d.getHours())+':'+z(d.getMinutes());return this.ctx.range.key==='today'?time:d.getFullYear()+'-'+z(d.getMonth()+1)+'-'+z(d.getDate())+'T'+time;}
  event(e){const el=e.target.closest?.('[data-request]');if(e.type==='keydown'){if(e.key==='Escape'&&this.jumpOpen){e.preventDefault();this.jumpOpen=false;this.draw();$('[data-request="time"]',this.host)?.focus();}return;}
    if(e.type==='submit'&&e.target.matches('[data-request-form]')){e.preventDefault();const input=$('[data-request-time-input]',this.host);if(!input.value)return;const date=this.ctx.range.key==='today'?new Date():new Date(input.value);if(this.ctx.range.key==='today'){const [h,m]=input.value.split(':').map(Number);date.setHours(h,m,0,0);}if(!Number.isFinite(+date)||date>new Date()||date<this.ctx.range.since){this.jumpError='Choose a time in this range, up to now.';this.draw();$('[data-request-time-input]',this.host)?.focus();return;}date.setSeconds(59,999);this.until=date.toISOString();this.jumpOpen=false;this.jumpError='';this.load();return;}
    if(!el)return;const action=el.dataset.request;if(busyRequest(this)&&action==='more')return;
    if(e.type==='change'){if(action==='failed'){this.failed=el.checked;if(!this.failed)this.kind='';this.load();}if(action==='provider'){this.provider=el.value;this.account='';this.load();}return;}
    if(e.type!=='click'||['failed','provider'].includes(action))return;e.preventDefault();
    if(action==='more'){this.load(false,true);return;}if(action==='time'){this.jumpOpen=!this.jumpOpen;this.jumpError='';this.draw();$('[data-request-time-input]',this.host)?.focus();return;}
    if(action==='clear-filter'){this.account='';this.provider='';}if(action==='latest')this.resetLatest();if(action==='new'){this.nextUsed=false;this.showNew=true;}if(action==='kind')this.kind=el.dataset.kind;this.load();
  }
}
const busyRequest=r=>['loading','reloading','more'].includes(r.phase);
window.RouterBoards={accountControls,register,start,update,api,openRequests,announce,resolveRange,layoutSpans,defaults,cardOrder,
  get layout(){return S.layout;},get customizing(){return S.customizing;},get active(){return S.active;},
  // Settings and removed-reference actions can request the same accessible editor.
  settings(id){enter();openOverlay('settings',{wid:id});}};
})();
