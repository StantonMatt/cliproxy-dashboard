'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const clock=Date.parse('2026-10-16T12:00:00Z');
class FakeDate extends Date {
  constructor(...args){super(...(args.length?args:[clock]));}
  static now(){return clock;}
}
const elements=new Map(),get=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',style:{},addEventListener(){},classList:{toggle(){}}});
  return elements.get(id);
};
const document={documentElement:{dataset:{}},getElementById:get,querySelectorAll:()=>[]};
const c=vm.createContext({document,Date:FakeDate,JSON,Math,localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const row=(requests,total_tokens)=>({requests,total_tokens,failures:0,input_tokens:total_tokens,output_tokens:0,cached_tokens:0});
const usage=()=>({available:true,since:'2026-10-15T12:00:00Z',accounts:[row(2,20)],models:[],recent:[],
  hours:[{hour:'2026-10-15T12:00:00Z',auth_index:'a',requests:2}],
  claude:{since:'2026-10-02T12:00:00Z',accounts:[row(3,30)],models:[],
    hours:[{hour:'2026-10-02T12:00:00Z',auth_index:'ca',requests:3}]}});
c.fixture={gateway:{online:true,checked_at:new FakeDate().toISOString(),accounts:[{id:'a',email:'a@example.test'}],
  claude_accounts:[{auth_index:'ca',email:'a@example.test'}]},watch_accounts:[{id:'claude',email:'a@example.test'}],health:{},usage:usage()};
vm.runInContext('current=fixture',c);
const slots=html=>html.match(/class="bar-slot"/g)?.length||0;
const tests=[];
const test=(name,fn)=>tests.push([name,fn]);
test('all recorded starts at earlier Claude history and includes both providers',()=>{
  c.renderChart('chart',usage(),'all');
  assert.equal(slots(get('chart').innerHTML),15);
  assert.match(get('chart').innerHTML,/Claude a@example.test 3/);
  assert.match(get('chart').innerHTML,/Codex a@example.test 2/);
});
test('Codex can still supply the earliest history bound',()=>{
  const u=usage();[u.since,u.claude.since]=[u.claude.since,u.since];
  c.renderChart('chart',u,'all');assert.equal(slots(get('chart').innerHTML),15);
});
test('Claude-only history uses its recorded start',()=>{
  const u=usage();u.since=null;u.accounts=[];u.hours=[];
  c.renderChart('chart',u,'all');assert.equal(slots(get('chart').innerHTML),15);
  assert.match(get('chart').innerHTML,/Claude a@example.test 3/);
});
test('all recorded keeps the latest 30 days while totals retain older requests',()=>{
  const u=usage();u.claude.since='2026-08-01T12:00:00Z';
  u.claude.hours.unshift({hour:u.claude.since,auth_index:'ca',requests:7});
  u.claude.accounts=[row(10,100)];c.fixture.usage=u;
  vm.runInContext("range='all'",c);c.renderUsage(c.fixture);
  assert.equal(slots(get('usage-chart').innerHTML),30);
  assert.doesNotMatch(get('usage-chart').innerHTML,/Claude a@example.test 7/);
  assert.match(get('usage-summary').innerHTML,/<strong>12<\/strong>/);
  assert.equal(get('chart-caption').textContent,'By day · up to the latest 30 days');
});
test('week and today retain their range boundaries and count both providers',()=>{
  const u=usage();u.hours.push({hour:new FakeDate().toISOString(),auth_index:'a',requests:4});
  u.claude.hours.push({hour:new FakeDate().toISOString(),auth_index:'ca',requests:5});
  for(const [range,length] of [['week',7],['today',24]]){
    c.renderChart('chart',u,range);const html=get('chart').innerHTML;
    assert.equal(slots(html),length);assert.match(html,/Claude a@example.test 5/);assert.match(html,/Codex a@example.test 4/);
    assert.doesNotMatch(html,/Claude a@example.test 3/);
  }
  u.accounts=[row(4,40)];u.claude.accounts=[row(5,50)];c.renderToday({usage:u});
  assert.equal(slots(get('overview-chart').innerHTML),24);
  assert.equal(get('today-requests').textContent,'4');assert.equal(get('claude-today-requests').textContent,'5');
  assert.equal(get('tokens-today').textContent,'90');
});
test('history note and footer use the earliest provider, with legacy and empty fallbacks',()=>{
  const u=usage();c.fixture.usage=u;
  for(const name of ['renderOperations','renderWatch','renderAccounts','renderRoutingAll','renderLegend','renderResetModal','renderSystem'])c[name]=()=>{};
  c.render();
  const dateTime=value=>new FakeDate(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  assert.ok(get('history-note').textContent.startsWith(`History starts ${dateTime(u.claude.since)}.`));
  assert.ok(get('footer-note').textContent.endsWith(new FakeDate(u.claude.since).toLocaleDateString(undefined,{month:'short',day:'numeric'})));
  delete u.claude.since;c.render();
  assert.ok(get('history-note').textContent.startsWith(`History starts ${dateTime(u.since)}.`));
  u.since=null;c.render();assert.ok(get('footer-note').textContent.endsWith('setup'));
});
const split=(r,read,readCount,write,writeCount)=>({...r,cache_read_tokens:read,cache_read_requests:readCount,
  cache_creation_tokens:write,cache_creation_requests:writeCount});
const cells=id=>[...get(id).innerHTML.matchAll(/<tr class="[^"]+">(.*?)<\/tr>/g)].map(m=>[...m[1].matchAll(/<td>(.*?)<\/td>/g)].map(c=>c[1]));
const usageRows=(codex,claude)=>{
  const u=usage();u.accounts=codex;u.claude.accounts=claude;c.fixture.usage=u;
  vm.runInContext("range='today'",c);c.renderUsage(c.fixture);
  return {summary:get('usage-summary').innerHTML,rows:cells('usage-accounts')};
};
test('account table and summary show a known cache read/write split',()=>{
  const {summary,rows}=usageRows([split(row(3,30),10,3,25,3)],[split(row(2,20),4000,2,1500,2)]);
  assert.match(get('usage-accounts').innerHTML,/<th>Cached input<\/th><th>Cache read<\/th><th>Cache write<\/th><th>Output tokens<\/th>/);
  assert.deepEqual(rows.map(r=>r.slice(5,7)),[['10','25'],['4,000','1,500']]);
  assert.match(summary,/<small class="metric-detail"><span>Read 4K<\/span><span>Write 1.5K<\/span><\/small>/);
  assert.doesNotMatch(summary+get('usage-accounts').innerHTML,/≥|partial/);
});
test('unknown split renders as "—", never as zero',()=>{
  const unknown=split(row(3,30),null,0,null,0);
  for(const codex of [[unknown],[row(3,30)],[split(row(3,30),0,0,0,0)]]){
    const {summary,rows}=usageRows(codex,[]);
    assert.deepEqual(rows[0].slice(5,7),['<span title="Not recorded">—</span>','<span title="Not recorded">—</span>']);
    assert.match(summary,/Read <span title="Not recorded">—<\/span><\/span><span>Write <span title="Not recorded">—<\/span>/);
  }
  // Unknown rows do not dilute or zero a known one; the summary covers known values only.
  const {summary}=usageRows([unknown],[split(row(2,20),40,2,7,2)]);
  assert.match(summary,/<span>Read <span class="partial" title="Recorded for 2 of 5 requests">≥40<\/span><\/span>/);
  // No requests at all: everything is unknown.
  assert.match(usageRows([],[]).summary,/Read <span title="Not recorded">—<\/span>/);
});
test('partial coverage is marked as a lower bound with its coverage',()=>{
  const {summary,rows}=usageRows([split(row(4,40),12,2,0,3)],[split(row(5,50),3000,5,9000,1)]);
  assert.deepEqual(rows.map(r=>r.slice(5,7)),[
    ['<span class="partial" title="Recorded for 2 of 4 requests">≥12</span>','<span class="partial" title="Recorded for 3 of 4 requests">≥0</span>'],
    ['3,000','<span class="partial" title="Recorded for 1 of 5 requests">≥9,000</span>']]);
  assert.match(summary,/Read <span class="partial" title="Recorded for 7 of 9 requests">≥3K<\/span>/);
  assert.match(summary,/Write <span class="partial" title="Recorded for 4 of 9 requests">≥9K<\/span>/);
});
test('Codex observed zero writes and Claude cold-cache rows keep their zeros',()=>{
  const {rows}=usageRows([{...split(row(6,60),5000,6,0,6),cached_tokens:5200}],[split(row(2,20),0,2,41200,2)]);
  assert.deepEqual(rows.map(r=>r.slice(5,7)),[['5,000','0'],['0','41,200']]);
  assert.equal(rows[0][4],'5,200');  // Cached input keeps its own continuity figure.
});
test('Cached input keeps using cached_tokens over input tokens',()=>{
  const r={...split(row(2,100),10,2,5,2),cached_tokens:25};
  assert.match(usageRows([r],[]).summary,/<span>Cached input<\/span><strong>25%<\/strong>/);
});
test('overview shows today\'s split, unknown and partial values, and the per-request split',()=>{
  const u=usage();
  u.accounts=[split(row(3,30),9000,3,0,3)];u.claude.accounts=[split(row(2,20),3000,1,6000,2)];
  u.recent=[{provider:'claude',auth_index:'ca',model:'claude-opus-5-5',total_tokens:52900,cache_read_tokens:0,cache_creation_tokens:41200,timestamp:new FakeDate().toISOString()},
    {provider:'codex',auth_index:'a',model:'gpt-6.1-sol',total_tokens:41600,cache_read_tokens:36000,cache_creation_tokens:null,timestamp:new FakeDate().toISOString()},
    {provider:'codex',auth_index:'a',model:'gpt-6.1-sol',total_tokens:41600,timestamp:new FakeDate().toISOString()}];
  c.renderToday({usage:u});
  assert.equal(get('cache-split').innerHTML,'<span class="partial" title="Recorded for 4 of 5 requests">≥12K</span> · 6K');
  const html=get('recent-table').innerHTML;
  assert.match(html,/<th>Tokens<\/th><th>Cache read · write<\/th><th>Result<\/th>/);
  assert.match(html,/<span class="mono" title="Read 0 · Write 41,200">0 · 41.2K<\/span>/);
  assert.match(html,/<span class="mono" title="Read 36,000 · Write —">36K · —<\/span>/);
  assert.match(html,/<span class="mono" title="Read — · Write —">— · —<\/span>/);
  u.accounts=[row(3,30)];u.claude.accounts=[row(2,20)];c.renderToday({usage:u});
  assert.equal(get('cache-split').innerHTML,'<span title="Not recorded">—</span> · <span title="Not recorded">—</span>');
  c.renderToday({usage:{available:false}});
  assert.equal(get('cache-split').textContent,'—');
});
let failures=0;
for(const [name,fn] of tests){try{fn();}catch(error){failures++;console.error(name,error);}}
console.log(`Usage history UI: ${tests.length-failures}/${tests.length} passed`);
if(failures)process.exitCode=1;
