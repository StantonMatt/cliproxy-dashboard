"""Board widget checks (charts.js, widgets.js) in the dashboard's Node VM UI style.

Deterministic: the widgets load into a VM with a RouterBoards stub and fake ctx; nothing connects to the dashboard,
the gateway or the management service.
"""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'dashboard' / 'static'
HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const defs=new Map();
const sandbox={document:{addEventListener(){}},Intl,Date,JSON,Math,Map,Set,Promise,console,setTimeout,clearTimeout,setInterval,clearInterval,requestAnimationFrame(){},cancelAnimationFrame(){}};
sandbox.window=sandbox;sandbox.RouterBoards={register(d){defs.set(d.type,d);}};
const c=vm.createContext(sandbox);
for(const f of ['charts.js','widgets.js'])vm.runInContext(fs.readFileSync('dashboard/static/'+f,'utf8').replace('window.RouterWidgets = {','window.RouterWidgets = {allEmpty, retryData,'),c,{filename:f});
const RC=sandbox.RouterCharts,RW=sandbox.RouterWidgets;
const plain=x=>JSON.parse(JSON.stringify(x));
const overview={providers:[
  {id:'codex',name:'Codex',accounts:[{key:'codex-a.json',auth_index:'ca',email:'one@example.test',windows:[{slot:'primary_window',label:'Weekly',seconds:604800,used:84,remaining:16,resets_at:Date.now()/1000+3*86400,measured_at:new Date().toISOString(),forecast:{state:'runs_out',runs_out_at:Date.now()/1000+7200}}]}]},
  {id:'claude',name:'Claude',accounts:[{key:'claude-b.json',auth_index:'cb',email:'one@example.test',windows:[{kind:'seven_day',label:'Weekly · all models',seconds:604800,used:100,remaining:0,resets_at:Date.now()/1000+86400,measured_at:new Date(Date.now()-3*3600e3).toISOString(),forecast:{state:'out'}}]},
                                      {key:'claude-c.json',auth_index:'cc',email:'two@example.test',windows:[]}]},
  {id:'xai',name:'Grok',accounts:[{key:'xai-d.json',auth_index:'xd',email:'one@example.test',windows:[]}]}],
  gateway:{online:true,gateway_version:'8.0.47',checked_at:new Date().toISOString()},
  health:{codex:{status:'healthy'},usage_collection:{status:'healthy',last_success_at:new Date().toISOString()},backup:{status:'healthy',last_success_at:new Date(Date.now()-3600e3).toISOString()}},
  notifications:{available:true,items:[{kind:'gateway_release',message:'Gateway 8.0.50 is available (running 8.0.47)'}]}};
function ctxFor(type,options={},extra={}){
  const d=defs.get(type),calls=[];
  const ctx={widget:{id:'w1',type,span:d.span,title:'',options},options:{...d.defaults,...options},board:{id:'b',name:'B',range:'today'},
    range:{key:'today',since:new Date(new Date().setHours(0,0,0,0)),until:new Date(),label:'Today',overridden:false,bucket:'hour'},
    preview:false,customizing:false,width:600,overview,tz:'America/Sao_Paulo',calls,
    api:async(path,params)=>{calls.push({path,params});return (extra.replies||{})[path]||{};},
    state(){},accountColor:r=>({ca:'var(--codex)',cb:'var(--claude-2)',cc:'var(--claude)',xd:'var(--xai)'})[r.auth_index]||'var(--blue)',
    accountName:r=>r.email||'Removed account',accentOf:p=>p,openRequests(){},saveOptions:async()=>{},announce(){},...extra};
  return ctx;
}
"""


class WidgetsUI(unittest.TestCase):
    def run_js(self, body):
        script = HARNESS + '\n(async()=>{\n' + body + '\n})().catch(e=>{console.error(e);process.exitCode=1});'
        result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registers_every_w3_type_with_spec_sizes(self):
        self.run_js(r"""
const want={chart:[6,4,false,'Charts'],stat:[3,2,true,'Numbers'],gauge:[3,3,false,'Limits'],timeline:[8,6,false,'Limits'],breakdown:[4,3,false,'Charts'],
  heatmap:[8,4,false,'Charts'],pulse:[4,3,true,'Charts'],health:[4,3,true,'Other'],retry:[3,3,true,'Numbers'],notes:[3,2,true,'Other']};
assert.deepEqual([...defs.keys()].sort(),Object.keys(want).sort());
const kinds=new Set(['text','textarea','tiles','segmented','select','switch','range']);
for(const [type,[span,min,compact,section]] of Object.entries(want)){
  const d=defs.get(type);
  assert.equal(d.span,span,type);assert.equal(d.min,min,type);assert.equal(!!d.compact,compact,type);assert.equal(d.section,section,type);
  assert.equal(typeof d.name,'string');assert.equal(typeof d.defaults,'object');
  for(const fn of ['title','fields','render','preview','table'])assert.equal(typeof d[fn],'function',type+'.'+fn);
  const ctx=ctxFor(type);assert.equal(typeof d.title(ctx.options,ctx),'string');
  for(const f of d.fields(ctx.options,ctx)){assert(kinds.has(f.kind),type+' '+f.kind);assert.equal(typeof f.key,'string');}
  for(const v of Object.values(d.defaults))assert(v==null||['string','number','boolean'].includes(typeof v),type);
}
const top=defs.get('breakdown').presets.find(p=>p.name==='Top models');
assert.equal(top.options.chart,'bars');assert.equal(top.options.by,'model');
assert.deepEqual(plain(defs.get('chart').defaults),{chart:'stacked',metric:'requests',group:'account',range:'board',compare:false});
""")

    def test_height_rows_per_type_and_no_chart_height_option(self):
        # Spec §6: min and max rows per type (the calendar's own 6-16 is boards.js heightBounds). The chart's
        # Short/Medium/Tall option is gone from settings and new-chart defaults; a legacy options.height is kept as is.
        self.run_js(r"""
const want={stat:[3,8],retry:[3,8],notes:[3,20],health:[4,12],gauge:[4,8],pulse:[4,12],breakdown:[4,16],heatmap:[5,12],chart:[4,20],timeline:[5,16]};
for(const [type,[lo,hi]] of Object.entries(want)){
  const d=defs.get(type);
  assert.equal(d.minRows,lo,type+' minRows');assert.equal(d.maxRows,hi,type+' maxRows');
  assert(Number.isInteger(d.minRows)&&Number.isInteger(d.maxRows)&&3<=d.minRows&&d.minRows<=d.maxRows&&d.maxRows<=20,type);
  assert.equal(d.autoHeightOnly,undefined,type);
}
const d=defs.get('chart');
assert(!d.fields(d.defaults).some(f=>f.key==='height'),'no Height field in chart settings');
assert(!('height' in d.defaults));
for(const h of [undefined,'short','medium','tall','bogus'])assert.equal(d.normalize({...d.defaults,...(h?{height:h}:{})}).options.height,h,'normalize keeps a legacy height as it is');
""")

    def test_auto_height_keeps_natural_sizes(self):
        # No frame, or a frame at Auto, draws exactly as before heights; a fixed frame sizes to its box.
        self.run_js(r"""
const noop=()=>{};let markup='',svg=null;
const fakeSVG={replaceWith:noop,setAttribute:noop,removeAttribute:noop,querySelectorAll:()=>[],querySelector:()=>null};
sandbox.document.createElement=()=>({content:{firstChild:fakeSVG},set innerHTML(v){markup=v;}});
sandbox.getComputedStyle=()=>({paddingTop:'12px'});
const frame=h=>({dataset:{height:h}});
const host=(h,fr)=>({clientWidth:600,clientHeight:h,classList:{add:noop},contains:()=>false,addEventListener:noop,querySelector:s=>s===':scope > svg'?svg:null,prepend(n){svg=n;},
  closest:s=>s==='[data-height]'?fr:null,set innerHTML(v){markup=v;},get innerHTML(){return markup;}});
assert.equal(RC.fills(host(10,null)),false);assert.equal(RC.fills(host(10,frame('auto'))),false);assert.equal(RC.fills({}),false);
assert.equal(RC.fills(host(10,frame('5'))),true);
const svgH=()=>+/<svg width="\d+" height="(\d+)"/.exec(markup)[1];
const cfg={kind:'bars',keys:[{key:'a',color:'red',name:'A'}],rows:[{a:1},{a:2}],labels:[],xTicks:[],unit:'count',height:190};
for(const fr of [null,frame('auto')]){RC.time(host(60,fr),cfg);assert.equal(svgH(),190,'Auto keeps the natural 190 px plot');}
RC.time(host(300,frame('auto')),cfg);assert.equal(svgH(),300,'Auto still stretches with its line');
RC.time(host(60,frame('4')),cfg);assert.equal(svgH(),60,'a fixed box draws at the plot height');
RC.time(host(12,frame('4')),cfg);assert.equal(svgH(),40,'with a 40 px floor');
// sparkline: 30 px natural; a fixed box fills its slot (minus the slot padding); too small draws nothing
RC.spark(host(42,null),[1,2,3],{});assert.equal(svgH(),30);
RC.spark(host(120,frame('6')),[1,2,3],{});assert.equal(svgH(),108);
RC.spark(host(20,frame('6')),[1,2,3],{});assert.equal(markup,'');
RC.spark(host(400,frame('8')),[1,2,3],{});assert.equal(svgH(),120,'tall stat caps the drawn sparkline');
// reset timeline: 36 px lanes at Auto; a fixed box compresses them to 28 px, then scrolls; taller spaces them up to 48
const lanes=Array.from({length:6},(_,i)=>({name:'a'+i,sub:'Codex · Weekly',color:'red',state:'lasts',resetAt:Date.now()/1000+3600*(i+1),resetText:'1h'}));
const tl=(h,fr)=>{const x=host(h,fr);x.clientWidth=900;RC.timeline(x,lanes,{hours:168});return svgH();};
assert.equal(tl(10,null),22+6*36+4);assert.equal(tl(10,frame('auto')),22+6*36+4);
assert.equal(tl(900,frame('16')),22+6*48+4,'taller boxes space lanes out up to 48 px');assert.equal(tl(22+6*40+4,frame('9')),22+6*40+4,'between, lanes share the box');
assert.equal(tl(22+6*30+4,frame('6')),22+6*30+4,'lanes compress to fit');
assert.equal(tl(60,frame('4')),22+6*28+4,'then stop at 28 px and scroll');
// usage chart and pulse publish their natural minimums as CSS variables (charts.css drops them for a fixed box)
const props={},el=()=>({style:{setProperty(k,v){props[k]=v;}},classList:{add:noop,toggle:noop},querySelector:()=>null,querySelectorAll:()=>[],append:noop,replaceChildren:noop,dataset:{}});
const body=el(),root={...el(),dataset:{shell:'chart:card'},querySelector:s=>s==='.rc-body'?body:s==='.rc-legend'?{replaceChildren:noop}:{textContent:''}};
const chost={__rcw:{},isConnected:true,querySelector:()=>root,querySelectorAll:()=>[],closest:()=>null};
// an Auto chart's natural plot: legacy short/tall as before, medium when absent or unknown
for(const [h,px] of [[undefined,'190px'],['short','120px'],['medium','190px'],['tall','290px'],['bogus','190px']]){
  await defs.get('chart').render(chost,ctxFor('chart',h?{height:h}:{}));assert.equal(props['--rc-body-min'],px,String(h));}
await defs.get('chart').render(chost,ctxFor('chart',{height:'tall'},{preview:true}));assert.equal(props['--rc-body-min'],'92px');
// a fixed layout height ignores options.height: the plot draws at its box (40 px floor), never at the legacy 290
RC.time(host(150,frame('6')),{...cfg,height:290});assert.equal(svgH(),150);
""")

    def test_requests_option_is_requests_loaded(self):
        # The Requests widget's row count option is renamed so "rows" only means height; its key stays `rows`.
        boards = (STATIC / 'boards.js').read_text()
        start = boards.index("register({type:'requests'")
        fields = boards[start:boards.index('preview:', start)]
        self.assertIn("{key:'rows',label:'Requests loaded'", fields)
        self.assertNotIn("label:'Rows'", fields)

    def test_failed_notes_draft_survives_refresh_remount_and_retries(self):
        self.run_js(r"""
function noteHost(){
  let ta=null;const meta={textContent:'',hidden:false,classList:{add(){},remove(){}},replaceChildren(...ns){this.children=ns;this.textContent=ns.map(n=>n.textContent).join('');}},title={textContent:''};
  const body={querySelector:s=>s==='textarea'?ta:null,replaceChildren(n){ta=n;}};
  const root={dataset:{shell:'notes:edit'},style:{setProperty(){}},querySelector:s=>s==='.rc-body'?body:s==='.rc-note-meta'?meta:s==='.rc-title'?title:null};
  return {__rcw:{},isConnected:true,querySelector:()=>root,ta:()=>ta,meta};
}
sandbox.document.createElement=tag=>({tag,textContent:'',value:'',style:{},classList:{add(){},remove(){}},listeners:{},setAttribute(){},addEventListener(k,fn){this.listeners[k]=fn;},append(){},replaceChildren(){}});
let fail=true,saves=0;const ctx=ctxFor('notes',{text:'Original'},{saveOptions:async patch=>{saves++;if(fail)throw Error('503');ctx.options={...ctx.options,...patch};}});
const d=defs.get('notes'),h=noteHost();await d.render(h,ctx);h.ta().value='Keep this failed draft';h.ta().listeners.input();h.ta().listeners.blur();await new Promise(r=>setImmediate(r));
assert.match(h.meta.textContent,/save your note/);await d.render(h,ctx);assert.equal(h.ta().value,'Keep this failed draft');
const remount=noteHost();await d.render(remount,ctx);assert.equal(remount.ta().value,'Keep this failed draft');assert.match(remount.meta.textContent,/save your note/);
const retry=remount.meta.children.find(n=>n.textContent==='Try again');assert(retry,'visible retry action');fail=false;retry.listeners.click();await new Promise(r=>setImmediate(r));assert.equal(saves,2);assert.equal(ctx.options.text,'Keep this failed draft');
const saved=noteHost();await d.render(saved,ctx);assert.equal(saved.ta().value,'Keep this failed draft');
// A fresh failed edit can also be explicitly discarded.
saved.ta().value='Discard me';saved.ta().listeners.input();fail=true;saved.ta().listeners.blur();await new Promise(r=>setImmediate(r));saved.meta.children.find(n=>n.textContent==='Discard').listeners.click();assert.equal(saved.ta().value,'Keep this failed draft');
// An edit blurred during an in-flight write is serialized and saved after it.
let finish;const values=[];ctx.saveOptions=async patch=>{values.push(patch.text);if(values.length===1)await new Promise(r=>finish=r);ctx.options={...ctx.options,...patch};};
saved.ta().value='First edit';saved.ta().listeners.input();saved.ta().listeners.blur();saved.ta().value='Latest edit';saved.ta().listeners.input();saved.ta().listeners.blur();finish();await new Promise(r=>setImmediate(r));assert.deepEqual(values,['First edit','Latest edit']);assert.equal(ctx.options.text,'Latest edit');

""")

    def test_chart_option_rules(self):
        self.run_js(r"""
const d=defs.get('chart'),n=o=>d.normalize({...d.defaults,...o});
let r=n({metric:'success_rate',chart:'stacked'});assert.equal(r.options.chart,'line');assert.match(r.note,/Rates read best as a line/);
for(const m of ['success_rate','cache_hit_rate','avg_duration'])for(const c of ['stacked','grouped','area','sarea'])assert.equal(n({metric:m,chart:c}).options.chart,'line');
assert.equal(n({group:'none',chart:'grouped'}).options.chart,'stacked');
assert.equal(n({group:'none',chart:'sarea'}).options.chart,'area');
r=n({metric:'tokens',group:'outcome'});assert.equal(r.options.group,'account');assert(r.note);
assert.equal(n({metric:'requests',group:'outcome'}).options.group,'outcome');
assert.equal(n({}).note,undefined);
let f=d.fields({...d.defaults,metric:'success_rate'});
const tiles=f.find(x=>x.key==='chart').choices;
for(const c of tiles)assert.equal(!!c.disabled,c.value!=='line',c.value);
assert(tiles.filter(c=>c.disabled).every(c=>c.reason==='Rates read best as a line'));
f=d.fields({...d.defaults,group:'none'});const t2=f.find(x=>x.key==='chart').choices;
assert.equal(t2.find(c=>c.value==='stacked').label,'Bars');
for(const v of ['grouped','sarea']){const c=t2.find(c=>c.value===v);assert(c.disabled);assert.equal(c.reason,'Needs a Group by');}
const outcome=d.fields({...d.defaults,metric:'tokens'}).find(x=>x.key==='group').choices.find(c=>c.value==='outcome');
assert(outcome.disabled);assert.equal(outcome.reason,'Only for Requests');
assert(!d.fields({...d.defaults}).find(x=>x.key==='group').choices.find(c=>c.value==='outcome').disabled);
assert.equal(RW.kindFor({metric:'requests',group:'none',chart:'stacked'}),'bars');
""")

    def test_other_option_rules(self):
        self.run_js(r"""
const b=defs.get('breakdown');let r=b.normalize({...b.defaults,by:'failure_reason',metric:'tokens',top:'8'});
assert.equal(r.options.metric,'failures');assert.equal(r.options.top,8);
assert(b.fields(r.options).find(f=>f.key==='metric').hidden);
const s=defs.get('stat');r=s.normalize({...s.defaults,metric:'retry_recoveries',filter:'provider:codex',spark:true});
assert.equal(r.options.filter,'all');assert.equal(r.options.spark,false);
const sf=s.fields(r.options,ctxFor('stat'));assert(sf.find(f=>f.key==='filter').hidden);assert(sf.find(f=>f.key==='spark').hidden);
assert(sf.length&&s.fields(s.defaults,ctxFor('stat')).find(f=>f.key==='filter').choices.some(c=>c.value==='account:claude:cc'));
const h=defs.get('heatmap');assert(h.fields({...h.defaults,chart:'calendar'}).find(f=>f.key==='range').hidden);
// option values stay within the layout's 80-character string limit
const long={key:'claude-'+'x'.repeat(90)+'@example.test.json',auth_index:'abcdef0123456789'};
const v=RW.limitValue({id:'claude'},long,{kind:'seven_day'});assert(v.length<=80);assert(v.startsWith('abcdef0123456789#'));
for(const c of defs.get('gauge').fields({},ctxFor('gauge')).find(f=>f.key==='limit').choices)assert(String(c.value).length<=80);
""")

    def test_data_requests_use_each_endpoints_parameters(self):
        self.run_js(r"""
const series={metric:'requests',group:'none',bucket:'hour',unit:'count',buckets:[new Date(new Date().setHours(0,0,0,0)).toISOString()],current:0,first_recorded:'2026-09-22T16:00:00Z',series:[{key:'total',values:[3],total:3}],previous:null};
const replies={'/api/series':series,'/api/breakdown':{unit:'count',total:4,rows:[{key:'failure_reason:rate_limit',label:'Rate limited',value:4,share:100}],other:null},'/api/retry':{available:true,recovered_requests:6,evidence_since:'2026-10-25T00:00:00Z',last_scanned_at:new Date().toISOString()},'/api/heatmap':{cells:[],days:[]}};
const all=[];
for(const [type,options] of [['chart',{}],['stat',{metric:'success_rate'}],['breakdown',{}],['retry',{}],['heatmap',{}],['pulse',{}]]){
  const ctx=ctxFor(type,options,{replies});await defs.get(type).table(ctx);all.push(...ctx.calls);
}
const allowed={'/api/series':['since','until','tz','metric','group','bucket','compare','provider','account'],'/api/breakdown':['since','until','metric','by','top','provider','account'],'/api/retry':['since','until'],'/api/heatmap':['since','until','tz','mode','metric']};
assert(all.length>=6);
for(const {path,params} of all){assert(allowed[path],path);for(const k of Object.keys(params))assert(allowed[path].includes(k),path+' '+k);assert.match(params.since,/T\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);}
assert(all.some(c=>c.path==='/api/series'&&c.params.tz==='America/Sao_Paulo'));
const retry=await defs.get('retry').table(ctxFor('retry',{},{replies}));assert.deepEqual(plain(retry.rows[0]),['6','4','60%']);
""")

    def test_series_model_today_padding_and_history_caption(self):
        self.run_js(r"""
const day=new Date(new Date().setHours(0,0,0,0)),buckets=[...Array(16)].map((_,i)=>new Date(+day+i*3600e3).toISOString());
const resp={metric:'requests',group:'account',bucket:'hour',unit:'count',buckets,current:15,first_recorded:'2026-09-22T16:00:00Z',
  series:[{key:'account:claude:cb',provider:'claude',auth_index:'cb',email:'one@example.test',account_key:'claude-b.json',values:buckets.map((_,i)=>i),total:120},
          {key:'account:codex:ca',provider:'codex',auth_index:'ca',email:'one@example.test',account_key:'codex-a.json',values:buckets.map(()=>2),total:32}],previous:null};
const ctx=ctxFor('chart');let m=RW.chartModel(ctx,resp,ctx.options);
assert.equal(m.rows.length,24);assert.equal(m.rows[23],null);assert.equal(m.labels[15].tip.endsWith(' · so far'),true);
assert.equal(m.caption,'By hour · your local time');assert.deepEqual(plain(RW.xTicks(24)),[0,7,15,23]);
// every connected account is listed in provider order, coloured by ctx.accountColor, labelled by provider in tooltips
assert.deepEqual(plain(m.keys.map(k=>k.key)),['account:codex:ca','account:claude:cb','account:claude:cc','account:xai:xd']);
assert.equal(m.keys[1].color,'var(--claude-2)');assert.equal(m.keys[1].tip,'Claude · one');
// a range longer than recorded history says where it starts
const c2=ctxFor('chart',{},{range:{key:'all',since:new Date('2026-09-22T00:00:00'),until:new Date(),label:'All recorded',bucket:'day'}});
const m2=RW.chartModel(c2,{...resp,bucket:'day',current:null,buckets:['2026-09-22T00:00:00-03:00','2026-09-23T00:00:00-03:00']},c2.options);
assert.match(m2.caption,/^By day · since /);
// models fold past four per family into "Other models"
const models=['a','b','c','d','e'].map((x,i)=>({key:'model:codex:'+x,label:x,provider:'codex',values:[5-i],total:5-i}));
const mk=RW.seriesKeys(ctx,{group:'model',series:models},{group:'model',metric:'requests'});
assert.equal(mk.length,5);assert.equal(mk.at(-1).name,'Other models');assert.equal(mk.at(-1).fold.length,1);
assert.equal(RW.chartEmptyText(ctx,{metric:'failures'}),'No failed requests today.');
assert.equal(RW.chartEmptyText(ctxFor('chart',{},{range:{key:'7d',since:new Date(),label:'7 days',bucket:'day'}}),{metric:'requests'}),'No requests in the last 7 days.');
""")

    def test_zero_rates_are_data_but_null_rates_and_zero_counts_are_empty(self):
        self.run_js(r"""
for(const metric of ['success_rate','cache_hit_rate','avg_duration']){
 const ctx=ctxFor('chart',{metric,group:'none'}),resp={bucket:'hour',buckets:[new Date().toISOString()],unit:metric==='avg_duration'?'ms':'percent',series:[{key:'total',values:[0],total:0}]};
 assert.equal(RW.allEmpty(RW.chartModel(ctx,resp,ctx.options)),false,metric+' zero');
 resp.series[0].values=[null];assert.equal(RW.allEmpty(RW.chartModel(ctx,resp,ctx.options)),true,metric+' null');
}
assert.equal(RW.allEmpty({unit:'count',rows:[{total:0}]}),true);
assert.equal(RW.allEmpty({unit:'tokens',rows:[{total:0}]}),true);
""")

    def test_model_rates_and_durations_never_fold_but_counts_and_tokens_do(self):
        self.run_js(r"""
const series=Array.from({length:6},(_,i)=>({key:'model:codex:m'+i,label:'m'+i,provider:'codex',values:[100],total:100}));
for(const metric of ['success_rate','cache_hit_rate','avg_duration','requests','tokens']){
 const ctx=ctxFor('chart',{metric,group:'model'}),m=RW.chartModel(ctx,{group:'model',bucket:'hour',buckets:[new Date().toISOString()],series,previous:{series}},ctx.options),rate=RW.METRICS[metric].rate;
 assert.equal(m.keys.some(k=>k.fold),!rate,metric);assert.equal(m.keys.length,rate?6:5);
 if(rate){assert(m.keys.every(k=>k.color));assert.equal(m.rows[0]['model:codex:m5'],100);}
 assert.equal(m.rows[0]['model:other'],rate?undefined:200);assert.equal(m.prev[0]['model:other'],rate?undefined:200);
}
""")

    def test_retry_partial_evidence_withholds_share_in_tile_and_table(self):
        self.run_js(r"""
const body={innerHTML:'',classList:{toggle(){}},querySelector(){return null}},title={textContent:''};
const root={dataset:{shell:'retry:tile'},style:{setProperty(){}},querySelector:s=>s==='.rc-body'?body:s==='.rc-stat-label'?title:null};
const host={__rcw:{drawn:true,seq:0},isConnected:true,querySelector:()=>root};
const range={key:'7d',since:new Date('2026-10-25T00:00:00Z'),until:new Date('2026-11-01T00:00:00Z'),label:'7 days'};
for(const evidence of [
 {evidence_since:'2026-10-28T00:00:00Z',last_scanned_at:'2026-11-01T00:00:00Z'},
 {evidence_since:'2026-10-24T00:00:00Z',last_scanned_at:'2026-10-31T00:00:00Z'},
 {evidence_since:null,last_scanned_at:null}]){
 const ctx=ctxFor('retry',{},{range,replies:{'/api/retry':{available:true,recovered_requests:6,...evidence},'/api/breakdown':{rows:[{key:'failure_reason:rate_limit',value:4}]}}});
 await defs.get('retry').render(host,ctx);assert.doesNotMatch(body.innerHTML,/>60%</);assert.match(body.innerHTML,/partial/i);assert.doesNotMatch(body.innerHTML,/class="rc-seg"/);
 const table=await defs.get('retry').table(ctx);assert.match(table.rows[0][2],/partial/i);assert.doesNotMatch(table.rows[0][2],/%/);
}
const ctx=ctxFor('retry',{},{range,replies:{'/api/retry':{available:true,recovered_requests:6,evidence_since:'2026-10-24T00:00:00Z',last_scanned_at:'2026-11-01T00:00:00Z'},'/api/breakdown':{rows:[{key:'failure_reason:rate_limit',value:4}]}}});
await defs.get('retry').render(host,ctx);assert.match(body.innerHTML,/>60%</);assert.equal((await defs.get('retry').table(ctx)).rows[0][2],'60%');
""")

    def test_allowances_health_and_removed_references(self):
        self.run_js(r"""
const ctx=ctxFor('gauge');
const fc=RW.forecastOf(overview.providers[0].accounts[0].windows[0],16);assert.match(fc.text,/^Runs out in /);assert.equal(fc.state,'runs_out');
assert.match(RW.forecastOf(overview.providers[1].accounts[0].windows[0],0).text,/^Out · back in /);
// a reading whose reset is still ahead is fresh, as on the account cards
assert.equal(RW.allowanceStale(ctx,overview.providers[1].accounts[0].windows[0],overview.providers[1].accounts[0]),0);
assert(RW.allowanceStale(ctx,{remaining:50,measured_at:new Date(Date.now()-7200e3).toISOString(),resets_at:Date.now()/1000-60},{})>0);
assert.equal(RW.findLimit(ctx,'').limit.a.key,'codex-a.json');
assert.equal(RW.findLimit(ctx,'gone.json#seven_day').missing,'account');
assert.equal(defs.get('gauge').title({limit:''},ctx),'Codex weekly');
const f=RW.statFilter(ctxFor('stat'),{filter:'account:xai:xd'});assert.equal(f.label,'Grok');assert.equal(f.removed,false);
assert.equal(RW.statFilter(ctxFor('stat'),{filter:'account:claude:zz'}).removed,true);
const rows=RW.healthRows(ctx);assert.deepEqual(plain(rows.map(r=>r.name)),['Gateway','Codex server','Usage collection','Backup','Gateway releases']);
assert.equal(rows[0].word,'Online · 8.0.47');assert.equal(rows[4].word,'8.0.50 available');assert.equal(rows[4].tone,'warn');
""")

    def test_chart_primitives(self):
        self.run_js(r"""
assert.equal(RC.fmt(12345,'count'),new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(12345));
assert.equal(RC.fmt(9999,'count'),new Intl.NumberFormat().format(9999));
assert.equal(RC.fmt(99.96,'percent'),'99.9%');assert.equal(RC.fmt(100,'percent'),'100%');assert.equal(RC.fmt(1500,'ms'),'1.5s');assert.equal(RC.fmt(null,'count'),'—');
assert.deepEqual(plain(RC.yScale([99.2,100],'percent',190).ticks),[98,99,100]);
assert.deepEqual(plain(RC.yScale([0,2],'count',120).ticks),[0,1,2]);
assert.deepEqual(plain(RC.yScale([2130],'count',120).ticks),[0,500,1000,1500,2000,2500]);
assert.equal(RC.share(1,300),'0.3%');assert.equal(RC.share(0,0),'0%');
assert.match(RW.iso(new Date()),/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
""")

    def test_rate_paths_break_at_null_buckets_in_both_periods(self):
        self.run_js(r"""
let markup='',svg=null;const noop=()=>{};
const fakeSVG={replaceWith:noop,setAttribute:noop,removeAttribute:noop,querySelectorAll:()=>[],querySelector:()=>null};
sandbox.document.createElement=()=>({content:{firstChild:fakeSVG},set innerHTML(v){markup=v;}});
const host={clientWidth:600,clientHeight:190,classList:{add:noop},contains:()=>false,addEventListener:noop,querySelector:s=>s===':scope > svg'?svg:null,prepend(n){svg=n;}};
const cfg={kind:'line',keys:[{key:'a',color:'red',name:'A'}],rows:[{a:99},{a:100},{a:null},{a:98},{a:99}],prev:[{a:98},{a:99},{a:null},{a:97},{a:98}],labels:[],xTicks:[],unit:'percent'};
RC.time(host,cfg);const paths=[...markup.matchAll(/<path d="([^"]+)"[^>]*stroke="red"/g)].map(m=>m[1]);assert.equal(paths.length,4);assert(paths.every(p=>(p.match(/L/g)||[]).length===1), 'a line bridges an empty bucket');
cfg.kind='area';RC.time(host,cfg);assert.equal((markup.match(/fill-opacity=".13"/g)||[]).length,2,'area bridges an empty bucket');
""")

    def test_source_invariants(self):
        charts = (STATIC / 'charts.js').read_text()
        widgets = (STATIC / 'widgets.js').read_text()
        css = (STATIC / 'charts.css').read_text()
        # The tooltip is built with DOM nodes and textContent, never HTML strings.
        show = charts[charts.index('function show('):charts.index('function hideTip(')]
        tip_lines = [line for line in show.splitlines() if 'tip' in line and 'innerHTML' in line]
        self.assertEqual(tip_lines, [])
        self.assertIn("el('span', '', k.tip || k.name)", show)
        self.assertIn('textContent = text', charts)
        # Widget content is host-scoped: no element ids in widget markup (one type can appear twice on a board).
        self.assertIsNone(re.search(r'''\bid=["']''', widgets))
        self.assertNotIn('getElementById', widgets + charts)
        # Read-only data: only the documented GET endpoints, never the management usage queue.
        for source in (charts, widgets):
            self.assertNotIn('usage-queue', source)
            self.assertNotIn('/v0/management', source)
            self.assertNotIn("method:", source)
        self.assertEqual(set(re.findall(r"ctx\.api\('(/api/[a-z]+)'", widgets)),
                         {'/api/series', '/api/breakdown', '/api/heatmap', '/api/retry', '/api/overview'})
        # Accessibility: charts are images with a summary, focusable for keyboard; reduced motion is honoured.
        self.assertIn('role="img"', charts)
        self.assertIn('tabindex=', charts)
        self.assertIn("aria-pressed", charts)
        self.assertIn('prefers-reduced-motion', css)
        self.assertIn('@container', css)
        # Fixed heights are keyed on the frame's data-height and never apply at Auto; overflow fades only where it scrolls.
        self.assertIn('.w[data-height]:not([data-height=auto]) .rc{container-type:size}', css)
        self.assertIn('animation-timeline:scroll(self block)', css)
        self.assertIn('@property --rc-fade', css)


if __name__ == '__main__':
    unittest.main()
