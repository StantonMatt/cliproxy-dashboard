"""Board registry/state checks in the dashboard's existing Node VM UI style.

Browser acceptance uses read-only live GETs and intercepted static/layout routes;
these deterministic tests never connect to the dashboard or management service.
"""
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map();
function node(){return {innerHTML:'',textContent:'',hidden:false,dataset:{},children:[],style:{setProperty(){}},classList:{add(){},remove(){},toggle(){},contains(){return false}},setAttribute(){},removeAttribute(){},getAttribute(){return null},contains(){return false},addEventListener(){},removeEventListener(){},querySelector(){return null},querySelectorAll(){return []},replaceChildren(){},focus(){},getBoundingClientRect(){return {width:360,left:0,top:0,right:360,bottom:30}}};}
const document={documentElement:{clientWidth:1440},activeElement:null,querySelector(s){if(!s.startsWith('#')||s.includes(' '))return null;if(!nodes.has(s))nodes.set(s,node());return nodes.get(s);},querySelectorAll(){return [];}};
const ps=[{id:'codex',name:'Codex',accounts:[{key:'c'}]},{id:'claude',name:'Claude',accounts:[{key:'a'},{key:'b'}]}];
let calls=[],reply={status:200,data:{}};
const sandbox={window:{RouterApp:{providers:()=>ps,providerName:p=>p,requestTable(rows,dated,seconds,fresh){assert(fresh==null||fresh instanceof Set);return '<table>'+rows.length+'</table>';},usageSince:u=>u?.since,refreshAccounts(){}}},document,location:{origin:'http://example.test',hash:'#overview'},URL,Date,JSON,Intl,Math,Map,Set,AbortSignal,Promise,queueMicrotask,setTimeout,clearTimeout,requestAnimationFrame(){},cancelAnimationFrame(){},performance:{now:()=>0},innerWidth:1440,innerHeight:900,matchMedia:()=>({matches:true}),
fetch:async (url,opt={})=>{calls.push({url,opt});return {ok:reply.status<400,status:reply.status,json:async()=>reply.data};}};
const c=vm.createContext(sandbox);
let source=fs.readFileSync('dashboard/static/boards.js','utf8');
source=source.replace('window.RouterBoards={','window.internals={fitAccounts,accountOf,accountValue,activeWidgets,spanOf,shownCards,pieceConflict,orderedAccounts,renderPicker,fieldControl,packWidgets,normalizeHeight,clampHeight,heightBounds,rowPixels,currentRows,setHeight,strip,renderOverlay,start,add,applyPacking,render,pointerStart,pointerMove,stopPointer,rects,glide,holdNeighbours,renderWidget,queueNatural,mounted,S,registerLegacy,enter,finish,dirty,persist,done,saveOptions,cardOrder,template,normalized,Requests,defs,context,pickerItems,saveBoardRange,copyBoardName,placeNode,editOption,handleAction,move,setWidth,newId,duplicateBoard};window.RouterBoards={');
vm.runInContext(source,c);
const B=sandbox.window.RouterBoards,I=sandbox.window.internals;
const plain=x=>JSON.parse(JSON.stringify(x));
function initial(layout={version:1,revision:0,providers:['claude','codex'],accounts:{codex:['c'],claude:['b','a']}}){B.update({layout,providers:ps,usage:{since:'2026-09-22T00:00:00Z'}});return I.S.layout;}
"""


class BoardsUI(unittest.TestCase):
    def test_customize_content_controls_remain_blocked_at_every_width(self):
        self.run_js(r"""
initial();
const handlers={};document.addEventListener=(event,fn)=>{(handlers[event]??=[]).push(fn);};sandbox.window.addEventListener=()=>{};sandbox.MutationObserver=class{observe(){}};
I.start();I.S.started=false;I.S.customizing=true;
// Customize must suppress content actions on desktop, tablet and phone alike.
const target={closest:s=>s.includes('.w.edit')?{}:null};
for(const width of [1440,900,760,390]){
  sandbox.innerWidth=width;
  for(const kind of ['click','change']){
    let prevented=false,stopped=false;
    handlers[kind][0]({target,preventDefault(){prevented=true;},stopImmediatePropagation(){stopped=true;}});
    assert.equal(prevented,true,kind+' default at '+width);assert.equal(stopped,true,kind+' propagation at '+width);
  }
}
""")

    def test_resize_updates_gaps_before_release_and_preserves_pointer_state(self):
        self.run_js(r"""
initial();I.S.customizing=true;I.S.started=true;
let now=0,rafId=0;const rafs=new Map();sandbox.performance={now:()=>now};sandbox.requestAnimationFrame=fn=>{rafs.set(++rafId,fn);return rafId;};sandbox.cancelAnimationFrame=id=>rafs.delete(id);
const step=ms=>{now+=ms;const pending=[...rafs.values()];rafs.clear();for(const fn of pending)fn(now);};
const pointerMove=event=>{I.pointerMove(event);step(16);};
// Supply tree placement for the real grid renderer; the harness has no CSS layout.
function element(){
  const el=node(),parts=new Map(),styles=new Map();
  el.getAnimations=()=>[];el.writes=0;const box=el.getBoundingClientRect;el.getBoundingClientRect=()=>el.className==='w-slot resize-slot'?frame.getBoundingClientRect():box();el.style={setProperty:(k,v)=>{el.writes++;styles.set(k,v);},getPropertyValue:k=>styles.get(k)};
  Object.defineProperties(el,{
    firstElementChild:{get:()=>el.children[0]||null},
    nextElementSibling:{get:()=>el.parentElement?.children[el.parentElement.children.indexOf(el)+1]||null}
  });
  el.remove=()=>{if(el.parentElement){const list=el.parentElement.children;list.splice(list.indexOf(el),1);el.parentElement=null;}};
  el.insertBefore=(child,before)=>{child.moves=(child.moves||0)+1;child.remove();const at=before?el.children.indexOf(before):el.children.length;assert.ok(at>=0);el.children.splice(at,0,child);child.parentElement=el;};
  el.append=child=>el.insertBefore(child,null);
  el.prepend=child=>el.insertBefore(child,el.firstElementChild);
  el.querySelector=s=>{if(!parts.has(s))parts.set(s,{...node(),remove(){parts.delete(s);}});return parts.get(s);};
  el.querySelectorAll=s=>s==='.w-gap'?el.children.filter(c=>c.className==='w-gap'):[];
  el.insertAdjacentHTML=()=>{};
  return el;
}
document.body=node();document.createElement=element;
const grid=element();nodes.set('#board-grid',grid);grid.getBoundingClientRect=()=>({left:0,top:0,right:1180,width:1180});
Object.assign(sandbox.window,{innerWidth:1440,scrollX:0,scrollY:0});
const query=document.querySelector.bind(document),nav=element();
const system=element();nav.insertBefore(system,null);
nav.querySelector=s=>s==='[data-view="system"]'?system:s==='.nav-add'?nav.children.find(e=>e.className==='nav-add')||null:null;
document.querySelector=s=>s==='.topbar nav'?nav:query(s);
let renders=0;B.register({type:'probe',render(){renders++;}});
const ws=I.S.layout.boards[0].widgets=[3,3,4,6,12].map((span,i)=>({id:'probe-'+i,type:'probe',span,title:'',options:{}}));
for(const w of ws){const frame=element(),body=element();frame.dataset.wid=w.id;I.mounted.set(w.id,{frame,body,w,board:'overview',generation:0});}
I.render();
const gaps=()=>grid.children.filter(e=>e.className==='w-gap').map(e=>[+e.dataset.at,+e.dataset.span,e.style.gridColumn]);
const order=()=>grid.children.filter(e=>e.dataset.wid).map(e=>e.dataset.wid);
assert.deepEqual(gaps(),[[3,2,'11 / span 2'],[4,6,'7 / span 6']]);
const placements=ws.map(w=>I.mounted.get(w.id).frame.moves);I.render();assert.deepEqual(ws.map(w=>I.mounted.get(w.id).frame.moves),placements,'unchanged frames stay attached so native double-clicks survive');
const frame=I.mounted.get(ws[2].id).frame,edge={closest:s=>s==='[data-wid]'?frame:null};
I.pointerStart({button:0,clientX:100,clientY:100,target:{closest:s=>s==='.resize-grip'?edge:null},preventDefault(){}});
assert.equal(frame.querySelector('.w-body').style.width,undefined,'the live body is never frozen to the snapped slot');
const resizeGeneration=I.mounted.get(ws[2].id).generation;
const guide=grid.children.find(e=>e.className==='guides'),readout=frame.querySelector('.size-readout'),before=renders;
function move(x){pointerMove({clientX:x,clientY:100,preventDefault(){}});assert.equal(I.mounted.get(ws[2].id).generation,resizeGeneration,'the resizing widget keeps its live content');assert.ok(grid.children.includes(guide));assert.deepEqual(order(),ws.map(w=>w.id));}
I.pointerMove({clientX:100.5,clientY:100,preventDefault(){}});I.pointerMove({clientX:101,clientY:100,preventDefault(){}});assert.equal(frame.style.width,'360px','pointer events wait for the next animation frame');step(16);
move(101);assert.equal(frame.style.width,'361px','the edge follows the first pixel before any grid step');assert.equal(ws[2].span,4);assert.equal(ws[2].height,'auto','width-only gestures preserve Auto');assert.equal(frame.querySelector('.w-body').style.width,undefined,'the body has no frozen width');assert.equal(frame.querySelector('.w-body').style.height,undefined,'the body has no frozen height');
move(200);assert.equal(ws[2].span,5);assert.deepEqual(gaps(),[[3,1,'12 / span 1'],[4,6,'7 / span 6']]);assert.match(readout.innerHTML,/5\/12/);
const sameGaps=grid.children.filter(e=>e.className==='w-gap'),writes=grid.writes;move(204);assert.equal(grid.writes,writes,'no render between snapped widths');assert.deepEqual(grid.children.filter(e=>e.className==='w-gap'),sameGaps);
move(300);assert.equal(ws[2].span,6);assert.deepEqual(gaps(),[[4,6,'7 / span 6']]);assert.match(readout.innerHTML,/6\/12/);
move(400);assert.equal(ws[2].span,7);assert.deepEqual(gaps(),[[2,6,'7 / span 6'],[3,5,'8 / span 5'],[4,6,'7 / span 6']]);
I.stopPointer(true);assert.equal(frame.style.width,'');assert.equal(frame.style.height,'');assert.equal(frame.style.left,'');assert.equal(frame.style.top,'');assert.ok(!grid.children.some(e=>e.className==='w-slot resize-slot'));assert.equal(ws[2].span,4);assert.deepEqual(gaps(),[[3,2,'11 / span 2'],[4,6,'7 / span 6']]);assert.ok(!grid.children.includes(guide));
// Bottom and corner use the same live renderer, clamp rows, and cancel both axes.
B.register({type:'probe',minRows:4,maxRows:8,render(){renders++;}});
frame.getBoundingClientRect=()=>({left:0,top:0,right:360,bottom:251,width:360,height:251});
const press=kind=>I.pointerStart({button:0,clientX:100,clientY:251,target:{closest:s=>s===kind?edge:null},preventDefault(){}});
const cells=()=>grid.children.filter(e=>e.className==='guides'&&'cells' in e.dataset);
ws[2].height='auto';press('.height-grip');assert.equal(cells().length,1,'the bottom handle shows the same block grid');pointerMove({clientX:100,clientY:252,preventDefault(){}});assert.equal(ws[2].height,5,'first motion from Auto starts at current rendered rows');assert.equal(frame.style.height,'252px','height starts from the measured Auto pixels');
pointerMove({clientX:100,clientY:251+57*20,preventDefault(){}});assert.equal(ws[2].height,8);assert.match(frame.querySelector('.size-readout').innerHTML,/8 rows.*Max/);I.stopPointer(true);assert.equal(ws[2].height,'auto');
assert.equal(cells().length,0);press('.corner-grip');assert.equal(cells().length,1);pointerMove({clientX:200,clientY:308,preventDefault(){}});assert.equal(ws[2].span,5);assert.equal(ws[2].height,6);assert.match(frame.querySelector('.size-readout').innerHTML,/5\/12 × 6 rows/);I.stopPointer(true);assert.equal(ws[2].span,4);assert.equal(ws[2].height,'auto');
press('.height-grip');pointerMove({clientX:100,clientY:200,preventDefault(){}});assert.equal(ws[2].height,4);I.stopPointer();assert.equal(ws[2].height,4);assert.equal(frame.style.height,'');assert.equal(frame.style.width,'');
// Auto stretched beyond its maximum starts the gesture at the legal row limit.
ws[2].height='auto';frame.getBoundingClientRect=()=>({left:0,top:0,right:360,bottom:536,width:360,height:536});
press('.height-grip');assert.match(frame.querySelector('.size-readout').innerHTML,/8 rows.*Max/);
pointerMove({clientX:100,clientY:251-57,preventDefault(){}});assert.equal(ws[2].height,7,'one row of travel immediately leaves the max');I.stopPointer(true);assert.equal(ws[2].height,'auto');
// An Auto neighbour stretches with the line, so only a fixed neighbour can align.
frame.dataset.line='0';const neighbour=I.mounted.get(ws[0].id).frame;neighbour.dataset.line='0';neighbour.getBoundingClientRect=frame.getBoundingClientRect;
const guidesNow=()=>grid.children.find(e=>e.className==='row-guides').children,matches=()=>guidesNow().filter(e=>e.className==='match').length;
neighbour.dataset.height='auto';press('.height-grip');assert.equal(matches(),0);assert.equal(guidesNow().length,0,'the grid replaces row ticks and the edge line');I.stopPointer(true);
ws[0].height=8;neighbour.dataset.height='8';press('.height-grip');assert.equal(matches(),1);assert.equal(guidesNow().length,1);I.stopPointer(true);

// Motion interpolates actual layout dimensions on every frame, then returns to the grid.
sandbox.matchMedia=()=>({matches:false});ws[2].height=6;
frame.getBoundingClientRect=()=>{const height=parseFloat(frame.style.height)||57*ws[2].height-34;return {left:0,top:0,right:360,bottom:height,width:360,height};};
press('.height-grip');pointerMove({clientX:100,clientY:276,preventDefault(){}});I.stopPointer();
assert.equal(frame.style.height,'333px');assert.ok(frame.__resizeSettle);step(0);const heights=[];
for(let i=0;i<16;i++){step(16);heights.push(parseFloat(frame.style.height));}
assert.ok(heights.every((h,i)=>h< (i?heights[i-1]:333)&&h>308),'sizes interpolate on every frame without overshoot');
assert.ok(heights.every((h,i)=>((i?heights[i-1]:333)-h)/25<.15),'no step exceeds 15% of the landing distance');assert.equal(frame.querySelector('.w-body').style.height,undefined);step(16);assert.equal(frame.style.width,'');assert.equal(frame.style.height,'');assert.equal(frame.style.marginBottom,'','the held line margin clears on landing');assert.equal(frame.__resizeSettle,undefined);
// A regrab starts at the current visual size and cancels only its own old tween.
press('.height-grip');pointerMove({clientX:100,clientY:252,preventDefault(){}});I.stopPointer();const old=frame.__resizeSettle;
step(32);press('.height-grip');assert.equal(frame.__resizeSettle,undefined);old.clear();assert.equal(frame.style.width,'360px');step(16);assert.equal(frame.style.width,'360px');
I.stopPointer(true);for(let i=0;i<18;i++)step(16);assert.equal(frame.style.width,'');
press('.height-grip');pointerMove({clientX:100,clientY:276,preventDefault(){}});I.stopPointer();assert.ok(frame.__resizeSettle);I.setHeight(ws[2].id,'auto');assert.equal(frame.__resizeSettle,undefined,'Auto cancels a stale landing target');assert.equal(frame.style.height,'');

""")

    def test_save_omits_auto_heights_and_retains_fixed_heights(self):
        self.run_js(r"""
initial();I.registerLegacy();
const widgets=I.S.layout.boards[0].widgets;
widgets.forEach(w=>I.normalizeHeight(w));
const fixed=widgets.find(w=>w.type==='tokens');fixed.height=6;
sandbox.fetch=async(url,opt)=>{calls.push({url,opt});const body=JSON.parse(opt.body);return {ok:true,status:200,json:async()=>({layout:{...body,revision:1}})};};
assert(await I.persist());const saved=JSON.parse(calls.at(-1).opt.body).boards[0].widgets;
assert.equal(saved.find(w=>w.id===fixed.id).height,6);
for(const w of saved.filter(w=>w.id!==fixed.id))assert(!('height' in w),'Auto must remain optional in saved layouts');
""")

    def test_short_card_does_not_condense_auto_neighbours_that_grow_the_line(self):
        self.run_js(r"""
I.registerLegacy();initial();I.S.started=true;sandbox.window.innerWidth=1440;
function measured(id,fixed,room,full,snug,compact){
 const w={id,type:'accounts',span:3,height:fixed?6:'auto',options:{}},frame={isConnected:true,dataset:{line:'0'},className:fixed?'fixed-height':'mixed-auto',matches:()=>true,classList:{contains:c=>c===(fixed?'fixed-height':'mixed-auto')},getBoundingClientRect:()=>({top:0,bottom:room,height:room})};
 const body={style:{},clientWidth:320,clientHeight:room,querySelector:s=>s==='.account-card'?{}:null,getBoundingClientRect:()=>({height:body.style.height==='auto'?({compact,snug}[frame.dataset.density]??full):body.clientHeight})};
 const m={w,frame,body};I.mounted.set(id,m);return m;
}
const short=measured('short',true,308,420,340,260),auto=measured('auto',false,422,513,470,422);
I.fitAccounts();assert.equal(auto.frame.dataset.density,undefined,'an Auto neighbour whose compact minimum exceeds the fixed line stays full');assert.equal(auto.fit.autoMin,513,'packing grows the line to fit that full card');assert.equal(short.frame.dataset.density,'compact');
auto.body.clientHeight=536;I.fitAccounts();assert.equal(auto.frame.dataset.density,undefined);assert.equal(auto.fit.autoMin,513,'the grown line remains stable');
// When condensing really can keep the line at the fixed height, retain the best fitting density.
I.mounted.clear();measured('tall-fixed',true,536,450,350,260);const fits=measured('fits',false,536,600,500,400);
I.fitAccounts();assert.equal(fits.frame.dataset.density,'snug');assert.equal(fits.fit.autoMin,400);
""")

    def test_height_normalization_keeps_legacy_chart_option(self):
        self.run_js("""
I.registerLegacy();B.register({type:'chart',minRows:4,maxRows:20,render(){}});
const list=['short','medium','tall'].map(height=>({type:'chart',options:{height}}));
for(const w of list)I.normalizeHeight(w);
assert.deepEqual(plain(list.map(w=>w.height)),['auto','auto','auto']);assert.deepEqual(plain(list.map(w=>w.options.height)),['short','medium','tall']);
const explicit={type:'chart',height:6,options:{height:'short'}};I.normalizeHeight(explicit);assert.equal(explicit.height,6);assert.deepEqual(plain(explicit.options),{height:'short'});
{const w={type:'heading',height:8,options:{}};I.normalizeHeight(w);assert.equal(w.height,'auto');}
for(const [h,want] of [[4,6],[8,8],[12,12],[30,20],['auto','auto']]){const w={type:'accounts',height:h,options:{}};I.normalizeHeight(w);assert.equal(w.height,want,'accounts: 6-20 rows or Auto');}
// A measured fit (boards.js fitAccounts) is a live minimum: clamps and packing use it; the saved height keeps its own value.
{const w={id:'fit-acc',type:'accounts',span:6,height:7,options:{}},fixed=I.rowPixels(9)-10;I.mounted.set(w.id,{fit:{min:fixed,full:I.rowPixels(11)}});
I.normalizeHeight(w);assert.equal(w.height,7,'normalizing keeps a saved height under the live minimum');assert.equal(I.clampHeight(w,7),9);assert.equal(I.heightBounds(w).min,9);assert.equal(I.heightBounds(w,true).min,6);
const p=I.packWidgets([w,{id:'fit-nb',type:'accounts',span:3,height:'auto',options:{}}],1440,x=>x.id==='fit-nb'?I.rowPixels(10)-5:0);assert.equal(p.items[0].height,9,'a box under its live minimum grows to it');assert.equal(p.lines[0].rows,10,'an Auto neighbour that needs more grows the line');
I.mounted.delete(w.id);assert.equal(I.clampHeight(w,7),7);}
for(const value of [undefined,null,true,1.5,'6']){const w={type:'tokens',height:value};I.normalizeHeight(w);assert.equal(w.height,'auto');}
const token={type:'tokens',height:3};I.normalizeHeight(token);assert.equal(token.height,4);token.height=20;I.normalizeHeight(token);assert.equal(token.height,8);
B.register({type:'heatmap',minRows:5,maxRows:12,render(){}});const calendar={type:'heatmap',height:4,options:{chart:'calendar'}};I.normalizeHeight(calendar);assert.equal(calendar.height,6);calendar.height=20;I.normalizeHeight(calendar);assert.equal(calendar.height,16);
const saved=plain(initial());saved.boards[0].widgets=[{id:'migrated',type:'chart',span:6,title:'',options:{height:'tall'}}];I.S.layout=null;B.update({layout:saved});assert.equal(B.layout.boards[0].widgets[0].height,'auto');assert.equal(B.layout.boards[0].widgets[0].options.height,'tall');assert.equal(calls.length,0);
""")

    def test_packer_stacks_after_previous_and_grows_line_without_jumping(self):
        self.run_js("""
const w=(id,span,height='auto')=>({id,type:'probe',span,height,options:{}}),places=p=>plain(p.items.map(x=>[x.w.id,x.line,x.row,x.col,x.rowSpan]));
const p=I.packWidgets([w('tall',8,7),w('short',4,4),w('stack',4,3),w('next',12)],1440);
assert.deepEqual(places(p),[['tall',0,0,0,7],['short',0,0,8,4],['stack',0,4,8,3],['next',1,0,0,1]]);
const growth=I.packWidgets([w('tall',8,6),w('short',4,4),w('stack',4,5)],1440);assert.equal(growth.lines.length,1);assert.equal(growth.lines[0].rows,9);assert.equal(growth.items[2].row,4);
const order=I.packWidgets([w('a',4,3),w('b',8,8),w('c',4,3),w('d',4,3)],1440);assert.deepEqual(places(order),[['a',0,0,0,3],['b',0,0,4,8],['c',0,3,0,3],['d',0,6,0,3]]);assert.equal(order.items[3].row,6,'never backfills an earlier hole before previous widget');
""")

    def test_packer_auto_blocks_space_and_stretches_only_in_mixed_lines(self):
        self.run_js("""
const w=(id,span,height='auto')=>({id,type:'probe',span,height,options:{}});
const p=I.packWidgets([w('fixed',8,4),w('auto',4),w('below',8,3)],1440,w=>w.id==='auto'?361:0);
assert.equal(p.lines.length,1);assert.equal(p.lines[0].rows,7);assert.equal(p.items[1].rowSpan,7);assert.equal(p.items[2].row,4);
const blocked=I.packWidgets([w('auto',8),w('fixed',4,4),w('next',8,3)],1440,()=>137);assert.equal(blocked.lines.length,2);
const ws=[w('a',5),w('b',7),w('c',3),w('d',4),w('e',6)];const auto=I.packWidgets(ws,1440,()=>251);
assert.equal(auto.tracks,'auto auto auto');assert.deepEqual(plain(auto.items.map(p=>[p.gridRow,p.col,p.rowSpan])),[[1,0,1],[1,5,1],[2,0,1],[2,3,1],[3,0,1]]);
const phone=I.packWidgets([w('a',6,8),w('b',6,4)],390);assert.equal(phone.tracks,'auto auto');assert.ok(phone.items.every(p=>p.height==='auto'));
const tablet=I.packWidgets([w('a',6,8),w('b',6,4),w('c',6,4),w('d',6,4)],900);assert.equal(tablet.cols,6);assert.deepEqual(plain(tablet.items.map(p=>[p.row,p.col,p.rowSpan])),[[0,0,8],[0,3,4],[4,3,4],[0,0,4]]);
""")

    def test_holes_have_minimum_rows_and_insert_after_widget_above(self):
        self.run_js("""
const w=(id,span,height)=>({id,type:'probe',span,height,options:{}});
let p=I.packWidgets([w('a',8,7),w('b',4,4)],1440);assert.deepEqual(plain(p.holes),[{col:8,span:4,rows:3,gridRow:5,rowSpan:3,at:2}]);
p=I.packWidgets([w('a',8,6),w('b',4,4)],1440);assert.equal(p.holes.length,0);
p=I.packWidgets([w('a',11,8)],1440);assert.equal(p.holes.length,0);
""")

    def test_hole_insertion_uses_reading_order_and_preserves_existing_positions(self):
        self.run_js("""
const w=(id,height)=>({id,type:'probe',span:4,height,options:{}});
const ws=[w('left',7),w('middle',3),w('right',10)],before=I.packWidgets(ws,1440);
const hole=before.holes.find(h=>h.col===4&&h.gridRow===4);
assert.equal(hole.at,3,'hole follows all widgets on the first row');
ws.splice(hole.at,0,w('added',hole.rows));const after=I.packWidgets(ws,1440);
const pos=p=>[p.line,p.row,p.col,p.rowSpan];
assert.deepEqual(plain(after.items.slice(0,3).map(pos)),plain(before.items.map(pos)));
assert.deepEqual(plain(pos(after.items[3])),[0,3,4,7]);
""")

    def test_requests_phone_ignores_saved_height_at_packing_breakpoint(self):
        self.run_js("""
initial();const host=node(),a=new I.Requests(host),limits=[];
const ctx={options:{rows:6,filters:false},widget:{height:20},range:{key:'today',label:'Today',since:new Date('2026-11-01T00:00:00Z')},api:async(path,params)=>{limits.push(params.limit);return {rows:Array.from({length:params.limit},(_,id)=>({id})),next:null};}};
for(const [width,want] of [[390,6],[760,6],[761,28],[1440,28],[390,6]]){
  sandbox.window.innerWidth=width;await a.render(ctx);assert.equal(limits.at(-1),want,'loaded rows at '+width);assert.match(host.innerHTML,new RegExp('<table>'+want+'</table>'),'displayed rows at '+width);
}
""")

    def test_option_height_clamp_schedules_repack_and_render(self):
        self.run_js("""
initial();B.register({type:'heatmap',minRows:5,maxRows:12,render(){}});
const w={id:'heat',type:'heatmap',span:4,height:5,options:{chart:'weekhour'}},frames=[];
sandbox.requestAnimationFrame=fn=>frames.push(fn);
I.normalized(w);assert.equal(frames.length,0,'unchanged height needs no board render');
w.options.chart='calendar';I.normalized(w);assert.equal(w.height,6);assert.equal(frames.length,1,'normalization must queue the board render that applies packing');
I.normalized(w);assert.equal(frames.length,1,'unchanged normalized height queues no extra render');
""")

    def test_height_menu_keyboard_clamping_and_size_labels(self):
        self.run_js(r"""
initial();I.registerLegacy();const w=I.S.layout.boards[0].widgets.find(w=>w.type==='tokens');w.span=4;
I.setHeight(w.id,3);assert.equal(w.height,4);I.setHeight(w.id,20);assert.equal(w.height,8);
assert.match(I.strip(w,0,1),/1\/3 × 8/);assert.match(I.strip(w,0,1),/Size: 1\/3 wide, 8 rows tall/);
I.S.overlay={type:'width',wid:w.id};I.renderOverlay();let html=nodes.get('#boards-overlay').innerHTML;
assert.match(html,/role="group" aria-label="Width"/);assert.match(html,/role="group" aria-label="Height"/);assert.match(html,/data-board-action="set-height"/);assert.match(html,/class="h-ico auto"/);assert.equal((html.split('aria-label="Height"')[1].match(/<i class="on"><\/i>/g)||[]).length,18,'4, 6 and 8 lit bars');
I.setHeight(w.id,'auto');assert.equal(w.height,'auto');assert.doesNotMatch(I.strip(w,0,1),/×/);
B.register({type:'heatmap',minRows:5,maxRows:12,render(){}});const heat={id:'heat',type:'heatmap',span:4,height:5,title:'',options:{}};I.S.layout.boards[0].widgets.push(heat);I.S.overlay={type:'width',wid:heat.id};I.renderOverlay();html=nodes.get('#boards-overlay').innerHTML;assert.match(html,/data-value="4" disabled title="Too short for this widget"/);
I.add({type:'heatmap',span:8,options:{}},1,4,3);const added=I.S.layout.boards[0].widgets[1];assert.equal(added.span,4);assert.equal(added.height,5);
const accounts=I.S.layout.boards[0].widgets[0];assert.equal(accounts.type,'accounts');I.S.overlay={type:'width',wid:accounts.id};I.renderOverlay();html=nodes.get('#boards-overlay').innerHTML;assert.match(html,/aria-label="Height"/);assert.match(html,/data-value="8"[^>]*>.*Short/,'unmeasured accounts presets');assert.doesNotMatch(html,/data-value="(8|10|14)" disabled/);
// Measured: Short is the live minimum (compact cards), Medium fits the full cards, Tall stays above both.
I.mounted.set(accounts.id,{fit:{min:I.rowPixels(9)-3,full:I.rowPixels(11)-3}});I.renderOverlay();html=nodes.get('#boards-overlay').innerHTML;I.mounted.delete(accounts.id);
assert.match(html,/data-value="9"[^>]*>.*Short<small>9 rows/);assert.match(html,/data-value="11"[^>]*>.*Medium<small>11 rows/);assert.match(html,/data-value="14"[^>]*>.*Tall<small>14 rows/);assert.doesNotMatch(html,/data-value="\d+" disabled/);
const heading=I.S.layout.boards[0].widgets.find(w=>w.type==='heading');I.S.overlay={type:'width',wid:heading.id};I.renderOverlay();assert.doesNotMatch(nodes.get('#boards-overlay').innerHTML,/aria-label="Height"/);
// Drive the real key handler with the focused grip; upward/downward edits clamp.
const handlers={};document.addEventListener=(event,fn)=>{(handlers[event]??=[]).push(fn);};sandbox.window.addEventListener=()=>{};sandbox.MutationObserver=class{observe(){}};I.S.started=false;I.start();I.S.started=false;I.S.customizing=true;
const frame={dataset:{wid:w.id}},grip={closest:()=>frame},target={closest:s=>s==='.grip'?grip:null};const key=k=>handlers.keydown[0]({key:k,shiftKey:true,target,preventDefault(){}});
I.mounted.set(w.id,{w,frame:{...node(),getBoundingClientRect:()=>({height:251}),dataset:{},getAnimations:()=>[],classList:{contains:()=>true,remove(){}}}});
key('ArrowDown');assert.equal(w.height,6);for(let i=0;i<20;i++)key('ArrowUp');assert.equal(w.height,4);for(let i=0;i<20;i++)key('ArrowDown');assert.equal(w.height,8);
""")

    def test_glide_retargets_from_current_visual_position(self):
        self.run_js("""sandbox.matchMedia=()=>({matches:false});let home=100,visual=70,active=true;const animations=[{id:'board-reorder',cancel(){active=false;}}],starts=[];const frame={classList:{contains:()=>false},getAnimations:()=>animations,getBoundingClientRect:()=>({left:active?visual:home,top:0}),animate(frames,options){starts.push(frames[0].translate);assert.equal(options.id,'board-reorder');assert.equal(options.duration,260);assert.equal(options.easing,'cubic-bezier(.2,.7,.2,1)');active=true;animations.push({id:options.id,cancel(){active=false;}});}};I.mounted.set('a',{frame});const before=I.rects();home=0;I.glide(before);assert.equal(starts[0],'70px 0px');visual=40;const next=I.rects();home=100;I.glide(next);assert.equal(starts[1],'-60px 0px');frame.classList.contains=()=>true;I.glide(I.rects());assert.equal(starts.length,2,'the lifted card never receives a glide');""")

    def test_resize_neighbour_glides_size_retarget_and_render_once(self):
        self.run_js(r"""
sandbox.matchMedia=()=>({matches:false});let clock=0,raf=0;const ticks=new Map();
// Anchor to the first rAF timestamp: a busy frame can make it trail performance.now.
sandbox.performance.now=()=>clock+40;sandbox.requestAnimationFrame=fn=>{ticks.set(++raf,fn);return raf;};sandbox.cancelAnimationFrame=id=>ticks.delete(id);
const step=ms=>{clock+=ms;const jobs=[...ticks.values()];ticks.clear();jobs.forEach(fn=>fn(clock));};
let home={left:20,top:100,width:360,height:500},renders=0;
const frame=node();frame.getAnimations=()=>[];frame.isConnected=true;
frame.getBoundingClientRect=()=>{const [x,y]=(frame.style.translate||'0 0').split(' ').map(parseFloat);return {...home,left:home.left+x,top:home.top+y,width:parseFloat(frame.style.width)||home.width,height:parseFloat(frame.style.height)||home.height};};
const w={id:'neighbour',type:'probe',height:'auto',options:{}},body=node();body.clientWidth=360;body.clientHeight=500;
B.register({type:'probe',render(){renders++;}});const m={frame,body,w,generation:0};I.mounted.set(w.id,m);
const before=I.rects();I.holdNeighbours();home={left:80,top:157,width:460,height:671};I.renderWidget(m);I.queueNatural(m);assert.equal(renders,0);assert.equal(ticks.size,0,'natural measurement is held');
I.glide(before,'width');assert.ok(frame.__resizeSettle);assert.deepEqual(plain(frame.getBoundingClientRect()),{left:20,top:100,width:360,height:500});
step(0);step(16);let current=frame.getBoundingClientRect();assert.ok(current.width>360&&current.width<460);assert.ok(current.height>500&&current.height<671);assert.ok(current.top>100&&current.top<157);assert.equal(renders,0);
const old=frame.__resizeSettle,again=I.rects();I.holdNeighbours();home={left:0,top:80,width:300,height:550};I.glide(again,'rows');assert.deepEqual(plain(frame.getBoundingClientRect()),plain(current),'retarget starts at the current box');const next=frame.__resizeSettle;old.clear();assert.equal(frame.__resizeSettle,next,'an obsolete tween cannot clear its replacement');
step(0);const heights=[];for(let i=0;i<16;i++){step(16);heights.push(frame.getBoundingClientRect().height);I.renderWidget(m);I.queueNatural(m);assert.equal(renders,0);}
assert.ok(heights.every((h,i)=>Math.abs(h-(i?heights[i-1]:current.height))/Math.abs(550-current.height)<.15));
step(16);assert.equal(renders,1);assert.equal(frame.__resizeSettle,undefined);for(const key of ['width','height','translate','marginBottom'])assert.equal(frame.style[key],'');
// Reduced motion skips tweening and renders at the final box.
sandbox.matchMedia=()=>({matches:true});const last=I.rects();I.holdNeighbours();home.height=600;I.glide(last,'rows');assert.equal(frame.__resizeSettle,undefined);assert.equal(renders,2);
""")

    def test_board_css_does_not_style_routing_confirm_buttons(self):
        css = (ROOT / 'dashboard/static/boards.css').read_text()
        self.assertNotRegex(css, r'\.confirm(?![\w-])')

    def run_js(self, body):
        result = subprocess.run(['node', '-e', HARNESS + '\n(async()=>{\n' + body + '\n})().catch(e=>{console.error(e);process.exitCode=1});'], cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v1_migrates_order_and_accounts_without_saving(self):
        self.run_js("""const l=initial();assert.equal(l.version,2);assert.deepEqual(plain(l.boards[0].widgets.filter(w=>w.type==='accounts').map(w=>w.options.provider)),['claude','codex']);assert.deepEqual(plain(l.accounts.claude),['b','a']);assert.equal(calls.length,0);""")

    def test_defaults_match_approved_layout(self):
        self.run_js("""initial();const [o,u]=B.defaults(['claude','codex']);assert.equal(o.showRange,false);assert.equal(u.showRange,true);assert.equal(u.heading,'Recorded activity');assert.deepEqual(plain(o.widgets.map(w=>w.span)),['auto','auto',12,5,7,12]);assert.deepEqual(plain(u.widgets.slice(0,4).map(w=>w.options.metric)),['requests','tokens','success_rate','cache_hit_rate']);assert.equal(u.widgets.at(-1).options.rows,50);const ids=[...o.widgets,...u.widgets].map(w=>w.id);assert.equal(new Set(ids).size,ids.length);""")

    def test_removed_provider_widget_stays_removed(self):
        self.run_js("""const l=initial();l.boards[0].widgets=l.boards[0].widgets.filter(w=>w.type!=='accounts');const saved=plain(l);B.update({layout:saved});assert.equal(B.layout.boards[0].widgets.filter(w=>w.type==='accounts').length,0);""")

    def test_new_provider_inserts_after_last_accounts(self):
        self.run_js("""initial();ps.push({id:'xai',name:'Grok',accounts:[{key:'g'}]});B.update({layout:{version:1,revision:0,providers:['claude','codex'],accounts:{codex:['c'],claude:['b','a']}}});const ws=B.layout.boards[0].widgets;assert.equal(ws[2].options.provider,'xai');assert.equal(ws[3].type,'heading');""")

    def test_normalized_inventory_detects_new_provider_and_remembers_removal(self):
        self.run_js("""initial();const saved=plain(B.layout);saved.inventory=['codex','claude'];saved.boards[0].widgets=saved.boards[0].widgets.filter(w=>w.options.provider!=='claude');ps.push({id:'xai',accounts:[{key:'g'}]});saved.accounts.xai=['g'];B.update({layout:saved});assert.deepEqual(plain(B.layout.boards[0].widgets.filter(w=>w.type==='accounts').map(w=>w.options.provider)),['codex','xai']);B.update({layout:saved});assert.equal(B.layout.boards[0].widgets.filter(w=>w.options.provider==='xai').length,1);assert.equal(B.layout.boards[0].widgets.some(w=>w.options.provider==='claude'),false);""")

    def test_first_load_uses_saved_inventory_before_normalized_accounts(self):
        self.run_js("""const saved=plain(initial());I.S.layout=null;saved.inventory=['codex','claude'];saved.boards[0].widgets=saved.boards[0].widgets.filter(w=>w.options.provider!=='claude');ps.push({id:'xai',accounts:[{key:'g'}]});saved.accounts.xai=['g'];B.update({layout:saved});assert.equal(B.layout.boards[0].widgets.filter(w=>w.options.provider==='xai').length,1);assert.equal(B.layout.boards[0].widgets.some(w=>w.options.provider==='claude'),false);""")

    def test_saving_freezes_customization_until_failure_then_allows_edits(self):
        self.run_js("""initial();I.enter();const w=B.layout.boards[0].widgets[2];I.S.overlay={type:'settings',wid:w.id};let resolve;sandbox.fetch=()=>new Promise(r=>resolve=r);const saving=I.done(),before=plain(B.layout);I.move(w.id,1);I.setWidth(w.id,6);I.editOption('$title','Lost edit');await I.saveOptions(w,{text:'Lost edit'},false);await B.cardOrder({accounts:{codex:['different']}});I.handleAction({disabled:false,dataset:{boardAction:'reset'},getAttribute(){return null},closest(){return null}});assert.deepEqual(plain(B.layout),before);assert.equal(nodes.get('#board-grid').inert,true);assert.equal(nodes.get('#boards-overlay').inert,true);assert.match(nodes.get('#customize-bar').innerHTML,/Saving…/);resolve({ok:false,status:503,json:async()=>({})});await saving;assert.equal(nodes.get('#board-grid').inert,false);assert.equal(B.customizing,true);I.move(w.id,1);assert.notDeepEqual(plain(B.layout),before);""")

    def test_reset_after_moving_default_widget_keeps_layout_ids_unique(self):
        self.run_js("""initial();I.enter();const original=B.layout.boards[0],target=B.layout.boards[1],moved=original.widgets.splice(2,1)[0];target.widgets.push(moved);I.handleAction({disabled:false,dataset:{boardAction:'reset'},getAttribute(){return null},closest(){return null}});const ids=B.layout.boards.flatMap(b=>[b.id,...b.widgets.map(w=>w.id)]);assert.equal(new Set(ids).size,ids.length);assert.equal(target.widgets.at(-1).id,moved.id);assert.ok(original.widgets.some(w=>w.type===moved.type));""")

    def test_generated_ids_avoid_loaded_ids_and_batch_collisions(self):
        self.run_js("""initial();const now=Date.now;Date.now=()=>12345;try{B.layout.boards[0].widgets.push({id:'notes-9ix-1',type:'notes',options:{}});assert.notEqual(I.newId('notes'),'notes-9ix-1');const all=[...I.template('live'),...I.template('limits'),...I.template('copy'),...I.template('overview')];const ids=all.map(w=>w.id);assert.equal(new Set(ids).size,ids.length);I.duplicateBoard();const layoutIds=B.layout.boards.flatMap(b=>[b.id,...b.widgets.map(w=>w.id)]);assert.equal(new Set(layoutIds).size,layoutIds.length);}finally{Date.now=now;}""")

    def test_only_active_board_widgets_can_mount(self):
        source = (ROOT / 'dashboard/static/boards.js').read_text()
        self.assertIn("m.board!==b.id", source)
        self.assertIn("if(system){unmount();", source)
        self.assertIn("function activeWidgets(){return (board()?.widgets||[])", source)

    def test_unknown_types_roundtrip_without_registry(self):
        self.run_js("""const l=initial();l.boards[0].widgets.push({id:'future',type:'future-widget',span:6,title:'Future',options:{x:1}});const saved=plain(l);B.update({layout:saved});assert.equal(B.layout.boards[0].widgets.at(-1).type,'future-widget');""")

    def test_registry_validation_and_compact_layout(self):
        self.run_js("""assert.throws(()=>B.register({type:'bad_type',render(){}}));B.register({type:'stat',compact:true,render(){}});const list=[{type:'stat',span:3},{type:'stat',span:4},{type:'stat',span:3},{type:'chart',span:6}];assert.deepEqual(plain(B.layoutSpans(list,390)),{cols:2,spans:[1,1,2,2]});assert.deepEqual(plain(B.layoutSpans(list,900)),{cols:6,spans:[3,3,3,3]});assert.deepEqual(plain(B.layoutSpans(list,1440)),{cols:12,spans:[3,4,3,6]});""")

    def test_tablet_lone_half_expands(self):
        self.run_js("""assert.deepEqual(plain(B.layoutSpans([{span:5},{span:8},{span:3}],900)),{cols:6,spans:[6,6,6]});""")

    def test_ranges_use_calendar_days_and_recorded_history(self):
        self.run_js("""initial();const now=new Date('2026-11-01T12:30:00Z');const today=B.resolveRange({options:{}},{range:'today'},now);assert.equal(today.since.getHours(),0);assert.equal(today.bucket,'hour');const week=B.resolveRange({options:{range:'7d'}},{range:'today'},now);assert.equal(week.overridden,true);assert.equal(week.bucket,'day');const all=B.resolveRange({options:{range:'all'}},{range:'today'},now);assert.equal(all.since.getFullYear(),2026);assert.ok(all.since<now);""")

    def test_api_deduplicates_and_sets_header(self):
        self.run_js("""reply.data={value:1};const [a,b]=await Promise.all([B.api('/api/series',{metric:'requests',group:'none'}),B.api('/api/series',{group:'none',metric:'requests'})]);assert.equal(calls.length,1);assert.equal(calls[0].opt.headers['X-Router-Request'],'1');assert.equal(calls[0].opt.method,undefined);assert.deepEqual(a,b);await B.api('/api/series',{metric:'requests',group:'none'});assert.equal(calls.length,1);""")

    def test_common_range_timezone_is_sent_only_to_timezone_aware_endpoints(self):
        self.run_js("""await B.api('/api/retry',{since:'2026-11-01T00:00:00Z',tz:'America/Santiago'});await B.api('/api/breakdown',{metric:'requests',by:'model',tz:'America/Santiago'});await B.api('/api/series',{metric:'requests',tz:'America/Santiago'});assert.equal(new URL(calls[0].url,'http://example.test').searchParams.has('tz'),false);assert.equal(new URL(calls[1].url,'http://example.test').searchParams.has('tz'),false);assert.equal(new URL(calls[2].url,'http://example.test').searchParams.get('tz'),'America/Santiago');""")

    def test_api_failures_are_retryable_and_not_cached(self):
        self.run_js("""reply.status=503;await assert.rejects(B.api('/api/series'));reply.status=200;reply.data={ok:true};assert.deepEqual(await B.api('/api/series'),{ok:true});assert.equal(calls.length,2);""")

    def test_cancel_reverts_all_boards_and_accounts(self):
        self.run_js("""const before=plain(initial());I.enter();I.S.layout.boards[0].name='Changed';I.S.layout.accounts.claude.reverse();I.S.layout.boards.push({id:'new',widgets:[]});assert.equal(I.dirty(),true);I.finish(true);assert.deepEqual(plain(B.layout),before);assert.equal(calls.length,0);assert.equal(B.customizing,false);""")

    def test_refresh_during_customize_preserves_draft(self):
        self.run_js("""initial();I.enter();I.S.layout.boards[0].name='Draft';const other=plain(I.S.snapshot);other.revision=10;other.boards[0].name='Other';B.update({layout:other});assert.equal(B.layout.boards[0].name,'Draft');assert.equal(B.layout.revision,0);""")

    def test_done_posts_once_with_revision(self):
        self.run_js("""initial();I.enter();I.S.layout.boards[0].name='Changed';reply.data={layout:{...plain(B.layout),revision:1}};await I.done();assert.equal(calls.length,1);const body=JSON.parse(calls[0].opt.body);assert.equal(body.version,2);assert.equal(body.base_revision,0);assert.equal(body.boards[0].name,'Changed');assert.equal(B.customizing,false);assert.equal(B.layout.revision,1);""")

    def test_save_failure_preserves_entire_session(self):
        self.run_js("""initial();I.enter();I.S.layout.boards[0].name='Draft';reply.status=503;await I.done();assert.equal(B.customizing,true);assert.equal(B.layout.boards[0].name,'Draft');assert.match(I.S.error,/Couldn't save/);""")

    def test_conflict_requires_explicit_force(self):
        self.run_js("""initial();I.enter();reply.status=409;reply.data={layout:{...plain(B.layout),revision:8}};await I.done();assert.equal(B.customizing,true);assert.equal(I.S.overlay.type,'conflict');assert.equal(I.S.overlay.other.revision,8);reply.status=200;reply.data={layout:{...plain(B.layout),revision:9}};await I.done(true);assert.equal(JSON.parse(calls.at(-1).opt.body).force,true);assert.equal(B.layout.revision,9);""")

    def test_card_order_is_part_of_customize_transaction(self):
        self.run_js("""initial();I.enter();await B.cardOrder({accounts:{codex:['c'],claude:['a','b']}});assert.equal(calls.length,0);assert.deepEqual(plain(B.layout.accounts.claude),['a','b']);I.finish(true);assert.deepEqual(plain(B.layout.accounts.claude),['b','a']);""")

    def test_rapid_normal_card_moves_are_optimistic_and_coalesced(self):
        self.run_js("""initial();let resolve;let n=0;sandbox.fetch=async(url,opt)=>{calls.push({url,opt});if(++n===1)await new Promise(r=>resolve=r);const body=JSON.parse(opt.body);return {ok:true,status:200,json:async()=>({layout:{...body,version:2,revision:n}})};};const first=B.cardOrder({accounts:{codex:['c'],claude:['a','b']}});const second=B.cardOrder({accounts:{codex:['c'],claude:['b','a']}});assert.deepEqual(plain(B.layout.accounts.claude),['b','a']);assert.equal(calls.length,1);resolve();await Promise.all([first,second]);assert.equal(calls.length,2);assert.equal(JSON.parse(calls[1].opt.body).base_revision,1);assert.deepEqual(plain(B.layout.accounts.claude),['b','a']);""")

    def test_browse_ranges_never_mutate_or_save_defaults(self):
        self.run_js("""initial();const b=B.layout.boards[1];await I.saveBoardRange(b,'week');await I.saveBoardRange(b,'month');assert.equal(b.range,'today');assert.equal(B.resolveRange(b.widgets[0],b).key,'month');assert.equal(calls.length,0);I.enter();I.finish(true);assert.equal(B.layout.boards[1].range,'today');assert.equal(calls.length,0);""")

    def test_stale_overview_cannot_revert_accepted_layout(self):
        self.run_js("""const stale=plain(initial());I.S.layout.revision=4;I.S.layout.boards[0].name='Accepted';B.update({layout:stale});assert.equal(B.layout.revision,4);assert.equal(B.layout.boards[0].name,'Accepted');""")

    def test_normalize_announces_and_preserves_defaults(self):
        self.run_js("""B.register({type:'chart',defaults:{chart:'stacked',metric:'requests'},render(){},normalize(o){return {options:{...o,chart:o.metric==='success_rate'?'line':o.chart},note:'Switched to line'};}});initial();const w={id:'test',type:'chart',options:{metric:'success_rate'}};I.normalized(w);assert.equal(w.options.chart,'line');await Promise.resolve();assert.equal(nodes.get('#boards-announce').textContent,'Switched to line');""")

    def test_in_place_option_save_rebases_a_widget_reference_after_another_save(self):
        self.run_js("""initial();const w=B.layout.boards[0].widgets[0];reply.data={layout:{...plain(B.layout),revision:1}};await I.persist();assert.notEqual(B.layout.boards[0].widgets[0],w);sandbox.fetch=async(url,opt)=>{calls.push({url,opt});const body=JSON.parse(opt.body);return {ok:true,status:200,json:async()=>({layout:{...body,version:2,revision:2}})};};await I.saveOptions(w,{hideRouting:true},false);assert.equal(JSON.parse(calls.at(-1).opt.body).boards[0].widgets[0].options.hideRouting,true);assert.equal(B.layout.boards[0].widgets[0].options.hideRouting,true);""")

    def test_settings_preview_cannot_persist_options(self):
        self.run_js("""initial();const w=B.layout.boards[0].widgets[0],before=plain(w.options);await I.saveOptions(w,{provider:'other'},true);assert.deepEqual(plain(w.options),before);assert.equal(calls.length,0);""")

    def test_template_ids_are_unique_and_live_has_requests(self):
        self.run_js("""initial();const live=I.template('live');assert.deepEqual(plain(live.map(w=>w.type)),['pulse','health','retry','requests']);assert.equal(live.at(-1).options.rows,10);const copy=I.template('copy');const ids=[...copy,...B.layout.boards[0].widgets].map(w=>w.id);assert.equal(ids.length,new Set(ids).size);""")

    def test_requests_render_and_duplicates_keep_separate_state(self):
        self.run_js("""initial();const host=node(),a=new I.Requests(host),b=new I.Requests(node());const ctx={options:{rows:6,filters:false},widget:{title:'Recent'},range:{key:'today',label:'Today',since:new Date('2026-11-01T00:00:00Z')},api:async()=>({rows:[{id:1}],next:null})};await a.render(ctx);await b.render(ctx);assert.match(host.innerHTML,/<table>1/);a.provider='claude';a.failed=true;assert.equal(b.provider,'');assert.equal(b.failed,false);assert.equal(a.phase,'idle');""")

    def test_live_refresh_keeps_full_request_list_until_user_requests_new(self):
        self.run_js("""initial();const a=new I.Requests(node());let id=1;const ctx={options:{rows:50,filters:true,more:true},widget:{},range:{key:'today',label:'Today',since:new Date('2026-11-01T00:00:00Z')},api:async()=>({rows:[{id}],next:'cursor'})};await a.render(ctx);id=2;await a.render(ctx);assert.equal(a.rows[0].id,1);assert.equal(a.newer,true);a.provider='claude';a.failed=true;await a.render({...ctx,range:{...ctx.range,key:'week',label:'7 days',since:new Date('2026-10-26T00:00:00Z')}});assert.equal(a.provider,'claude');assert.equal(a.failed,true);""")

    def test_requests_title_changes_render_immediately_without_option_changes(self):
        self.run_js("""initial();const host=node(),a=new I.Requests(host);const ctx={options:{rows:50,filters:true,more:true},widget:{title:'Before'},range:{key:'today',label:'Today',since:new Date('2026-11-01T00:00:00Z')},api:async()=>({rows:[{id:1}],next:null})};await a.render(ctx);await a.render(ctx);await a.render({...ctx,widget:{title:'After'}});assert.ok(host.innerHTML.includes('>After</h2>'));""")

    def test_requests_validate_malformed_pages(self):
        self.run_js("""initial();const a=new I.Requests(node());await a.render({options:{rows:6},widget:{},range:{key:'today',since:new Date()},api:async()=>({rows:'bad',next:null})});assert.equal(a.error,"Couldn't load requests.");assert.equal(a.rows.length,0);""")

    def test_requests_filter_params_include_account_until_and_reason(self):
        self.run_js("""initial();const a=new I.Requests(node());let params;a.ctx={options:{rows:20},range:{since:new Date()},api:async(path,p)=>{assert.equal(path,'/api/requests');params=p;return {rows:[],next:null};}};Object.assign(a,{provider:'claude',account:'index',failed:true,kind:'timeout',until:'2026-11-01T12:00:00Z'});await a.page('cursor');assert.equal(params.limit,20);assert.equal(params.provider,'claude');assert.equal(params.account,'index');assert.equal(params.failed,1);assert.equal(params.kind,'timeout');assert.equal(params.before,'cursor');assert.equal(params.until,a.until);""")

    def test_requests_failed_more_keeps_rows_and_cursor(self):
        self.run_js("""initial();const a=new I.Requests(node());a.ctx={options:{rows:6,more:true},widget:{},range:{key:'today',since:new Date()},api:async()=>{throw Error('offline');}};a.rows=[{id:1}];a.next='old-cursor';await a.load(false,true);assert.equal(a.rows.length,1);assert.equal(a.next,'old-cursor');assert.equal(a.moreError,true);""")

    def test_latest_clears_clickthrough_filters_and_transient_range(self):
        self.run_js("""initial();sandbox.window.RouterApp.dateTime=x=>x;const host=node(),a=new I.Requests(host);const base={options:{rows:50,filters:true},widget:{},range:{key:'today',label:'Today',since:new Date('2026-11-01T00:00:00Z')},api:async()=>({rows:[],next:null})};await a.render(base);a.jump({account:'account-id',provider:'claude',until:'2026-11-01T10:00:00Z'}, {...base.range,key:'7d',label:'7 days'});assert.match(host.innerHTML,/request-filter/);assert.match(host.innerHTML,/account-id/);a.resetLatest();assert.equal(a.account,'');assert.equal(a.provider,'');assert.equal(a.until,null);assert.equal(a.rangeOverride,null);assert.equal(a.ctx.range.key,'today');await a.page();""")

    def test_duplicate_board_names_always_have_unique_suffixes(self):
        self.run_js("""initial();const base='Scratch board name long';const first=I.copyBoardName(base);assert.equal(first,'Scratch board name copy');B.layout.boards.push({name:first});const second=I.copyBoardName(base);assert.equal(second,'Scratch board nam copy 2');assert.ok(second.length<=24);""")

    def test_placing_node_at_its_current_cursor_is_a_noop(self):
        self.run_js("""let moves=0;const parent={moveBefore(){moves++;},insertBefore(){moves++;}},a={parentElement:parent,contains(){return false}},b={parentElement:parent,contains(){return false}};a.nextElementSibling=b;b.nextElementSibling=null;I.placeNode(parent,a,a);I.placeNode(parent,b,b);assert.equal(moves,0);I.placeNode(parent,b,a);assert.equal(moves,1);""")

    def test_requests_destroy_fences_inflight_responses(self):
        self.run_js("""initial();const a=new I.Requests(node());let resolve;a.ctx={options:{rows:6},widget:{},range:{key:'today',since:new Date()},api:()=>new Promise(r=>resolve=r)};const load=a.load();a.destroy();resolve({rows:[{id:9}],next:null});await load;assert.equal(a.rows.length,0);""")

    def test_account_ids_and_label_references_follow_widget_host(self):
        self.run_js("""const appSource=fs.readFileSync('dashboard/static/app.js','utf8');vm.runInContext('const scopedAccountNodes=new Map();'+appSource.slice(appSource.indexOf('function scopeAccountHost('),appSource.indexOf('function boardAccountSection(')),c);const attrs={for:'trigger','aria-labelledby':'title trigger','aria-controls':'routing-menu'},label={dataset:{},getAttribute:k=>attrs[k]||null,setAttribute:(k,v)=>attrs[k]=v};const title={id:'title',dataset:{},getAttribute(){return null}},trigger={id:'trigger',dataset:{},getAttribute(){return null}},section={id:'provider',dataset:{},getAttribute(){return null},querySelectorAll(s){return s==='[id]'?[title,trigger]:[label]}};c.host={closest:()=>({dataset:{wid:'widget-a'}})};c.section=section;vm.runInContext('scopeAccountHost(host,section)',c);assert.equal(trigger.id,'widget-a--trigger');assert.equal(attrs.for,'widget-a--trigger');assert.equal(attrs['aria-labelledby'],'widget-a--title widget-a--trigger');assert.equal(attrs['aria-controls'],'routing-menu');c.host={closest:()=>({dataset:{wid:'widget-b'}})};vm.runInContext('scopeAccountHost(host,section)',c);assert.equal(trigger.id,'widget-b--trigger');assert.equal(attrs.for,'widget-b--trigger');assert.equal(vm.runInContext(`scopedAccountNodes.get('trigger')`,c),trigger);""")

    def test_actual_chart_catalog_registers_against_framework_contract(self):
        self.run_js("""vm.runInContext(fs.readFileSync('dashboard/static/charts.js','utf8'),c);vm.runInContext(fs.readFileSync('dashboard/static/widgets.js','utf8'),c);I.registerLegacy();initial();assert.equal(I.defs.size,17);for(const type of ['chart','stat','gauge','timeline','breakdown','heatmap','pulse','health','retry','notes','accounts','account-card','heading','tokens','usage-accounts','usage-models','requests'])assert.ok(I.defs.get(type),type);const w={id:'test-rate',type:'chart',span:6,title:'',options:{metric:'success_rate',chart:'stacked',group:'account'}};I.normalized(w);assert.equal(w.options.chart,'line');const ctx=I.context(w);assert.equal(typeof ctx.api,'function');assert.equal(typeof ctx.openSettings,'function');assert.equal(ctx.range.bucket,'hour');assert.ok(I.pickerItems().some(i=>i.name==='Top models'));""")

    def test_split_registry_picker_and_duplicate_rules(self):
        self.run_js(r"""
I.registerLegacy();initial();
const d=I.defs.get('account-card');assert.equal(d.name,'Account');assert.equal(d.span,3);assert.equal(d.min,3);assert(!d.compact);
assert.deepEqual(plain(d.defaults),{provider:'',account:'',label:''});assert.equal(I.defs.get('accounts').defaults.hideCards,false);
const items=I.pickerItems().filter(i=>i.section==='Accounts');
assert.deepEqual(plain(items.map(i=>[i.group.id,i.name,i.type])),[
 ['codex','Accounts','accounts'],['codex','Header','accounts'],['codex','c','account-card'],
 ['claude','Accounts','accounts'],['claude','Header','accounts'],['claude','b','account-card'],['claude','a','account-card']]);
const b=I.S.layout.boards[0],header=items.find(i=>i.name==='Header'&&i.options.provider==='claude'),card=items.find(i=>i.type==='account-card'&&i.options.account==='a');
assert(I.pieceConflict(b,header));assert(!I.pieceConflict(b,card));
const n=b.widgets.length;I.add(header);assert.equal(b.widgets.length,n);I.add(card,null,6,12);assert.equal(b.widgets.length,n+1);I.add(card);assert.equal(b.widgets.length,n+1);
const w=b.widgets.at(-1);assert.equal(w.span,3);assert.equal(w.height,'auto');assert.equal(B.layoutSpans([w],900).spans[0],3);assert.equal(B.layoutSpans([w],390).spans[0],2);assert(I.pieceConflict(b,card));assert(!I.pieceConflict({widgets:[]},card));
I.handleAction({dataset:{boardAction:'duplicate'},closest:()=>({dataset:{wid:w.id}}),getAttribute:()=>null},{});assert.equal(b.widgets.length,n+1);
""")

    def test_split_pullout_width_visibility_and_header_heights(self):
        self.run_js(r"""
I.registerLegacy();initial();const b=I.S.layout.boards[0],w=b.widgets.find(w=>w.type==='accounts'&&w.options.provider==='claude');
const order=JSON.stringify(I.S.layout.accounts);assert.equal(I.spanOf(w),6);assert.equal(I.shownCards(w),2);
const card={id:'split-a',type:'account-card',span:3,height:'auto',title:'',options:{provider:'claude',account:'a',label:'Saved'}};
b.widgets.push(card);assert.equal(I.spanOf(w),3);assert.equal(I.shownCards(w),1);
card.options.account='b';assert.equal(I.shownCards(w),1);assert.equal(JSON.stringify(I.S.layout.accounts),order);
b.widgets.pop();assert.equal(I.spanOf(w),6);assert.deepEqual(plain(I.orderedAccounts(ps[1]).map(a=>a.key)),['b','a']);
w.height=12;w.options.hideCards=true;I.normalizeHeight(w);assert.equal(w.height,'auto');assert(I.heightBounds(w).auto);assert.equal(I.spanOf(w),6);assert.equal(I.shownCards(w),0);
w.options.hideCards=false;I.normalizeHeight(w);assert.equal(w.height,'auto');assert(!I.heightBounds(w).auto);
b.widgets.push(card);card.options.account='gone';assert(!I.activeWidgets().includes(card));I.S.customizing=true;assert(I.activeWidgets().includes(card));
I.S.customizing=false;ps[1].accounts=[];assert(!I.activeWidgets().includes(w));I.S.customizing=true;assert(I.activeWidgets().includes(w));
assert.equal(JSON.stringify(I.S.layout.accounts),order);
""")

    def test_split_settings_group_order_missing_and_conflict(self):
        self.run_js(r"""
I.registerLegacy();initial();const b=I.S.layout.boards[0],w={id:'split-a',type:'account-card',span:3,title:'',options:{provider:'claude',account:'a',label:'Saved'}};
b.widgets.push(w,{id:'split-b',type:'account-card',options:{provider:'claude',account:'b'}});
const d=I.defs.get('account-card');let ctx=I.context(w),f=d.fields(ctx.options,ctx)[0];
assert.deepEqual(plain(f.choices.map(c=>c.group)),['Codex','Claude','Claude']);assert.deepEqual(plain(f.choices.map(c=>JSON.parse(c.value)[1])),['c','b','a']);assert(f.choices[1].disabled);assert(!f.choices[2].disabled);
const html=I.fieldControl(f,I.accountValue(w.options));assert.match(html,/<optgroup label="Claude">/);assert.match(html,/Already on this board/);
w.options.account='removed';ctx=I.context(w);f=d.fields(ctx.options,ctx)[0];assert(f.choices[0].disabled);assert.match(f.choices[0].label,/Saved \(removed\)/);assert.match(d.title(ctx.options),/Saved/);
I.S.overlay={type:'settings',wid:w.id};I.editOption('$account',JSON.stringify(['claude','b']));assert.equal(w.options.account,'removed','a conflicting account cannot be chosen');
const fields=I.defs.get('accounts').fields();assert.deepEqual(plain(fields.map(f=>f.key)),['hideCards','hideRouting']);
""")

    def test_split_missing_renderer_offers_settings_without_discarding_identity(self):
        self.run_js(r"""
I.registerLegacy();initial();let unmounted=0,opened=0;
sandbox.window.RouterApp.mountCard=()=>false;sandbox.window.RouterApp.unmountCard=()=>unmounted++;
sandbox.document.createElement=()=>{const n=node();n.append=(...children)=>n.children.push(...children);return n;};
const host=node();host.append=(...children)=>host.children.push(...children);host.replaceChildren=()=>{host.children=[];};
const options={provider:'claude',account:'gone',label:'Saved account'};
I.defs.get('account-card').render(host,{options,preview:false,openSettings:()=>opened++});
assert.equal(unmounted,1);const state=host.children[0];assert.equal(state.children[0].textContent,'Saved account was removed');assert.equal(state.children[1].textContent,'Choose another');state.children[1].onclick();assert.equal(opened,1);assert.equal(options.account,'gone');
""")

    def test_split_account_reassignment_and_measured_minimum(self):
        self.run_js(r"""
I.registerLegacy();initial();const b=I.S.layout.boards[0],w={id:'split-a',type:'account-card',span:3,height:4,title:'',options:{provider:'claude',account:'a',label:'Old'}};b.widgets.push(w);
const order=JSON.stringify(I.S.layout.accounts);I.S.overlay={type:'settings',wid:w.id};I.editOption('$account',JSON.stringify(['codex','c']));
assert.deepEqual(plain(w.options),{provider:'codex',account:'c',label:'c'});assert.equal(JSON.stringify(I.S.layout.accounts),order);
I.mounted.set(w.id,{fit:{min:310,full:400}});assert.equal(I.heightBounds(w).min,7);assert.equal(I.clampHeight(w,3),7);I.normalizeHeight(w);assert.equal(w.height,4,'live measurements never rewrite stored heights');
I.mounted.delete(w.id);const accounts=b.widgets.find(w=>w.type==='accounts');accounts.height=12;I.S.overlay={type:'settings',wid:accounts.id};I.editOption('hideCards',true);assert.equal(accounts.height,'auto');
""")

    def test_all_pulled_accounts_preview_uses_header_and_header_names_follow_the_form(self):
        self.run_js(r"""
I.registerLegacy();initial();const b=I.S.layout.boards[0],w=b.widgets.find(w=>w.options.provider==='claude');
b.widgets.push(...['a','b'].map(account=>({id:'split-'+account,type:'account-card',span:3,options:{provider:'claude',account}})));
const host=node(),classes={};host.classList.toggle=(k,v)=>classes[k]=v;sandbox.window.RouterApp.previewAccounts=()=>{};
const d=I.defs.get('accounts');d.render(host,I.context(w,host,true));assert.equal(classes['split-head'],true,'all pulled cards leave a useful header preview');
b.widgets.pop();d.render(host,I.context(w,host,true));assert.equal(classes['split-head'],false);
w.options.hideCards=true;d.render(host,I.context(w,host,true));assert.equal(classes['split-head'],true);
assert.match(I.strip(w,0,1),/Header <em>/);assert.match(I.strip(w,0,1),/Move Header/);
I.S.overlay={type:'settings',wid:w.id};I.renderOverlay();assert.match(nodes.get('#boards-overlay').innerHTML,/<h2>Header<\/h2>/);assert.match(nodes.get('#boards-overlay').innerHTML,/aria-label="Header settings"/);
w.options.hideCards=false;I.renderOverlay();assert.match(nodes.get('#boards-overlay').innerHTML,/<h2>Accounts<\/h2>/);
const thumb=node();d.preview(thumb,{provider:'claude',hideCards:true});assert.doesNotMatch(thumb.innerHTML,/2 \/ 2|2,569|99%/);
""")

    def test_overview_autoadd_counts_individual_cards(self):
        self.run_js(r"""
const w={id:'split',type:'account-card',span:3,title:'',options:{provider:'claude',account:'a'}};
const layout={version:2,revision:0,providers:['codex'],inventory:['codex'],accounts:{codex:['c']},boards:[{id:'overview',name:'Overview',widgets:[w]}]};
initial(layout);assert.equal(I.S.layout.boards[0].widgets.filter(w=>w.options.provider==='claude').length,1);
""")

    def test_load_order_and_accessibility_hooks(self):
        html = (ROOT / 'dashboard/static/index.html').read_text()
        order = [html.index('src="/' + name + '"') for name in ['app.js', 'boards.js', 'charts.js', 'widgets.js']]
        self.assertEqual(order, sorted(order))
        self.assertIn('aria-label="Customize board"', html)
        self.assertIn('id="boards-announce"', html)
        css = (ROOT / 'dashboard/static/boards.css').read_text()
        self.assertIn('max-height:120px', css)
        self.assertIn('prefers-reduced-motion', css)


if __name__ == '__main__':
    unittest.main()
