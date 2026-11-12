'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
const nodes=new Map(),generic=new Map();
function node(id='') {return {id,dataset:{},innerHTML:'',textContent:'',hidden:false,value:'',className:'',title:'',style:{setProperty(k,v){this[k]=v;}},options:[],listeners:{},
  classList:{toggle(){}},addEventListener(type,fn){this.listeners[type]=fn;},focus(){},remove(){},
  insertBefore(el){generic.set(el.dataset.genericProvider,el);}};}
const document={documentElement:{dataset:{}},body:{},activeElement:null,
  getElementById(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);},
  createElement:()=>node(),querySelectorAll:selector=>selector==='[data-generic-provider]'?[...generic.values()]:[],
  querySelector:selector=>generic.get(selector.match(/data-generic-provider="([^"]+)"/)?.[1]) || null};
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const ago=s=>new Date(Date.now()-s*1000).toISOString();
const report=(indicator,extra={})=>({source:'Anthropic',indicator,description:'Partially degraded service',url:'https://status.claude.com/',
  checked_at:ago(130),incident:null,...extra});
const status=(s,id='claude')=>c.officialStatus({provider_status:{[id]:s}},id);
const tone=markup=>markup.match(/class="status-item official (\w+)"/)?.[1];
const label=markup=>markup.match(/<span>([^<]*)<\/span>/)?.[1];
const details=markup=>[...markup.matchAll(/<span class="state-detail" id="([^"]+)" hidden>([^<]*)<\/span>/g)].map(m=>[m[1],m[2]]);
vm.runInContext('failed=false',c);

// Each indicator: a short word after the source, in the header's existing tones.
for(const [indicator,word,t] of [['operational','operational','ok'],['minor','degraded','warn'],['maintenance','maintenance','warn'],
  ['major','partial outage','down'],['critical','major outage','down']]) {
  const markup=status(report(indicator));
  assert.equal(label(markup),'Anthropic '+word,indicator);assert.equal(tone(markup),t,indicator);
}
// Unknown, or an unrecognised indicator, is a quiet link to the page.
for(const s of [report('unknown',{checked_at:null,description:'Status unavailable'}),report('unknown'),report('degraded-ish')]) {
  const markup=status(s);
  assert.equal(label(markup),'Anthropic status');assert.equal(tone(markup),'neutral');
  assert.match(markup,/href="https:\/\/status\.claude\.com\/"/);assert.match(markup,/<use href="#arrow"\/>/);
}
assert.deepEqual(details(status(report('unknown',{checked_at:null,description:'Status unavailable'}))).map(d=>d[1]),['Status unavailable']);
// A report older than half an hour, or one this tab cannot refresh, is not shown as current.
assert.equal(label(status(report('operational',{checked_at:ago(31*60)}))),'Anthropic status');
assert.equal(label(status(report('major',{checked_at:'not a date'}))),'Anthropic status');
vm.runInContext('failed=true',c);
assert.equal(tone(status(report('operational'))),'neutral');
vm.runInContext('failed=false',c);

// Link: a new tab without an opener, the popover hook and its details (incident, then the check time).
const degraded=status(report('minor',{incident:'Elevated errors on Claude Opus 5.5'}));
const open=degraded.match(/^<a [^>]*>/)[0];
assert.match(open,/ id="status-claude"/);assert.match(open,/ href="https:\/\/status\.claude\.com\/"/);
assert.match(open,/ target="_blank"/);assert.match(open,/ rel="noopener noreferrer"/);assert.match(open,/ data-state-tip/);
assert.match(open,/ aria-describedby="status-claude-0 status-claude-1"/);
assert.deepEqual(details(degraded),[['status-claude-0','Elevated errors on Claude Opus 5.5'],['status-claude-1','Checked 2m ago']]);
// Without an incident a problem keeps its description; operational needs no line beyond the check time.
assert.deepEqual(details(status(report('minor'))).map(d=>d[1]),['Partially degraded service','Checked 2m ago']);
assert.deepEqual(details(status(report('operational',{description:'All systems operational'}))).map(d=>d[1]),['Checked 2m ago']);
// Text and URLs are escaped; only http(s) pages are linked; a known state without one stays plain text.
const odd=status(report('major',{source:'<b>"x"</b>',incident:'a & b'}));
assert.match(odd,/<span>&lt;b&gt;&quot;x&quot;&lt;\/b&gt; partial outage<\/span>/);assert.match(odd,/>a &amp; b</);
assert.equal(status(report('minor',{url:'javascript:alert(1)'})),'<span class="status-item official warn">Anthropic degraded</span>');
assert.equal(status(report('unknown',{url:'javascript:alert(1)'})),'');assert.equal(status(report('minor',{url:null})),'<span class="status-item official warn">Anthropic degraded</span>');
// Missing source falls back to the provider's name; a missing key or a missing provider_status renders nothing.
assert.match(status(report('operational',{source:''}),'xai'),/<span>xai operational<\/span>/);
assert.equal(c.officialStatus({provider_status:{codex:report('operational')}},'claude'),'');
assert.equal(c.officialStatus({provider_status:{claude:null}},'claude'),'');
assert.equal(c.officialStatus({},'claude'),'');assert.equal(c.officialStatus({provider_status:null},'claude'),'');

// The popover reads the label and every detail line; tones map onto the popover's.
const fake=(cls,text,lines)=>({classList:{contains:k=>cls.split(' ').includes(k)},firstElementChild:{textContent:text},
  querySelectorAll:()=>lines.map(textContent=>({textContent})),closest:()=>null,lastElementChild:null});
assert.deepEqual(JSON.parse(JSON.stringify(c.stateTipContent(fake('status-item official warn','Anthropic degraded',['Elevated errors','Checked 2m ago'])))),
  {items:[{text:'Anthropic degraded',detail:['Elevated errors','Checked 2m ago']}],tone:'warn'});
assert.equal(c.stateTipContent(fake('status-item official neutral','Meta status',[])).tone,'off');
assert.equal(c.stateTipContent(fake('status-item official down','x',[])).tone,'down');
// Header status lines keep to one line by stepping down to shorter forms (fitStatus); a shortened link's popover keeps
// the full label.
const forms=text=>JSON.parse(JSON.stringify(c.statusForms(text)));
assert.deepEqual(forms('All accounts ready'),['All accounts ready','All ready','Ready']);
assert.deepEqual(forms('One account ready'),['One account ready','One ready']);
assert.deepEqual(forms('No accounts ready'),['No accounts ready','None ready']);
assert.deepEqual(forms('2 of 3 ready'),['2 of 3 ready','2/3 ready']);
assert.deepEqual(forms('Status unavailable · checking'),['Status unavailable · checking','Checking']);
assert.deepEqual(forms('OpenAI operational'),['OpenAI operational','OpenAI ok','OpenAI']);
assert.deepEqual(forms('xAI partial outage'),['xAI partial outage','xAI outage']);
assert.deepEqual(forms('Anthropic degraded'),['Anthropic degraded']);assert.deepEqual(forms('Meta status'),['Meta status','Meta']);
const shortened={...fake('status-item official ok','OpenAI ok',[]),querySelector:selector=>selector==='.status-full' ? {textContent:'OpenAI operational'} : null};
assert.equal(c.stateTipContent(shortened).items[0].text,'OpenAI operational');
// A click on the link leaves navigation to the browser instead of pinning the popover.
const link={id:'status-claude',href:'https://status.claude.com/'};
nodes.get('gateways').listeners.click({target:{closest:selector=>selector==='[data-state-tip]' ? link : null}});
assert.equal(vm.runInContext('stateTip',c),null);

// Headers: Codex (the renamed local server and OpenAI's status), Claude and generic providers.
const account=(provider,i)=>({key:provider+'-'+i+'.json',provider,id:provider+'-'+i,auth_index:provider+'-'+i,email:provider+i+'@example.test',
  label:provider+i+'@example.test',status:'active',disabled:false,active:true,availability:{state:'ready'},windows:[],
  routing_id:'a'.repeat(64),routing_enabled:true,runtime_available:true,today:{requests:1,hours:[]}});
const data={providers:[{id:'codex',name:'Codex',accounts:[account('codex',0)]},{id:'claude',name:'Claude',routable:true,accounts:[account('claude',0)]},
  {id:'xai',name:'Grok',accent:'xai',accounts:[account('xai',0)]},{id:'meta',name:'Meta',accounts:[account('meta',0)]}],
  gateway:{online:true,checked_at:new Date().toISOString(),accounts:[],claude_accounts:[]},health:{codex:{status:'healthy'},backup:{}},
  usage:{available:true,providers:{}},watch_accounts:[],
  provider_status:{codex:{...report('operational'),source:'OpenAI',url:'https://status.openai.com/'},claude:report('minor',{incident:'Elevated errors'}),
    xai:{...report('major'),source:'xAI',url:'https://status.x.ai/'}}};
c.fixture=data;vm.runInContext('current=fixture;today=fixture;',c);
// The local Codex server is desktop health, shown in the footer with the backup, not in the Codex header.
assert.match(html,/<div id="codex-status" class="gateway-status" role="status"><span id="pool-availability" class="status-item" hidden><\/span><span id="codex-official" class="status-slot"><\/span><\/div>/);
for(const [server,word] of [['healthy','Healthy'],['down','Down'],['unknown','Unknown'],['stale','Stale'],['failed','Failed']]) {
  data.health.codex.status=server;c.renderOperations(data);
  assert.match(nodes.get('health-panel').innerHTML,new RegExp(`^<span class="health-item health-${server}" title="[^"]*your Linux desktop[^"]*"><i aria-hidden="true"></i><span>Codex server</span><b>${word}</b></span><span class="health-item health-`));
}
assert.match(nodes.get('codex-official').innerHTML,/^<a class="status-item official ok" id="status-codex" href="https:\/\/status\.openai\.com\/"[^>]*><span>OpenAI operational<\/span>/);
c.renderWatch(data);
assert.match(nodes.get('claude-status').innerHTML,/^<span class="status-item \w+">[^<]+<\/span><a class="status-item official warn" id="status-claude"[^>]*><span>Anthropic degraded<\/span>/);
c.renderProviders(data);
assert.match(generic.get('xai').innerHTML,/<div class="gateway-status"><span class="status-item ok">All accounts ready<\/span><a class="status-item official down" id="status-xai"[^>]*><span>xAI partial outage<\/span>/);
assert.match(generic.get('meta').innerHTML,/<div class="gateway-status"><span class="status-item ok">All accounts ready<\/span><\/div>/);
// Today's backend sends no provider_status: nothing new renders.
delete data.provider_status;
c.renderOperations(data);c.renderWatch(data);c.renderProviders(data);
assert.equal(nodes.get('codex-official').innerHTML,'');assert.doesNotMatch(nodes.get('claude-status').innerHTML,/official/);
assert.doesNotMatch(generic.get('xai').innerHTML,/official|status-xai/);
console.log('Provider status UI: indicators, unknown and stale reports, links, popover, short forms, local Codex server and missing data passed');
