'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const nodes=new Map();
function node(id='') {
  const el={id,dataset:{},value:'',hidden:false,disabled:false,textContent:'',options:[],children:[],
    classList:{toggle(){},remove(){}},style:{setProperty(){}},addEventListener(){},removeAttribute(){},setAttribute(){},
    focus(){document.activeElement=el;},querySelector:()=>node(),querySelectorAll:()=>[]};
  let markup='';
  Object.defineProperty(el,'innerHTML',{get:()=>markup,set(value){markup=value;
    if(id.endsWith('-mode'))el.options=[...value.matchAll(/<option value="([^"]+)"/g)].map(m=>({value:m[1]}));
  }});
  return el;
}
const document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null,
  getElementById(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);}};
document.activeElement=document.body;
const c=vm.createContext({document,Date,JSON,Math,localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const get=id=>document.getElementById(id),dialog=get('routing-dialog');
dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};
const account=(key,email)=>({key,provider:'claude',email,label:email});
const a=account('a.json','a@example.test'),b=account('b.json','b@example.test');
c.fixture={providers:[{id:'claude',routable:true,accounts:[a,b]}],watch_accounts:[],gateway:{online:true,move_available:true,
  routing_linked:false,claude_preference:{policy:'round-robin',saved_mode:'round-robin',status:'active'}}};
vm.runInContext('current=fixture;failed=false',c);
// Exercise the real option/poll rendering while limiting unrelated linked-row DOM handling.
c.renderRoutingControls=()=>c.renderRouting(c.fixture.gateway,'claude');
let posts=[];c.request=async(url,options)=>{posts.push({url,body:JSON.parse(options.body)});return {status:'active'};};c.load=async()=>{};
const render=()=>{c.renderProviders(c.fixture);c.renderRoutingControls();};
(async()=>{
  for(const poll of [true,false]) {
    c.fixture.providers[0].accounts=[a,b];render();
    get('claude-routing-mode').value=b.key;c.openRoutingChoice('claude',b.key);assert.equal(dialog.open,true);
    // Replace B with the same label/email and a different gateway filename.
    c.fixture.providers[0].accounts=[a,{...b,key:'replacement.json'}];
    if(poll){render();assert.equal(dialog.open,false,'a poll must invalidate the removed account choice');
      assert.equal(get('claude-routing-mode').value,'round-robin');}
    await c.routingChoiceApply('claude','move');
    await c.saveRouting(b.key,'claude',true);
    await c.saveRouting('','claude',true);
    assert.equal(posts.length,0,'stale choices must never post a fallback policy, even before option refresh');
    assert.equal(dialog.open,false);assert.equal(get('claude-routing-mode').value,'round-robin');
  }
  // Legacy email-valued options must also invalidate if the account key changes.
  c.fixture.providers[0].accounts=[a,b];get('claude-routing-mode').options=[{value:'round-robin'},{value:b.email}];
  c.openRoutingChoice('claude',b.email);assert.equal(dialog.open,true);
  c.fixture.providers[0].accounts=[a,{...b,key:'replacement.json'}];c.renderRoutingControls();
  assert.equal(dialog.open,false);assert.equal(posts.length,0);
  // A still-available choice retains its exact preferred filename and Move flag.
  render();get('claude-routing-mode').value='replacement.json';c.openRoutingChoice('claude','replacement.json');
  await c.routingChoiceApply('claude','move');
  assert.deepEqual(posts,[{url:'/api/claude-routing',body:{mode:'preferred-account',preferred_key:'replacement.json',move:true}}]);
  // Linked routing must also notice a changed counterpart, even though its selected Codex key survives.
  posts=[];c.fixture.gateway.routing_linked=true;c.fixture.gateway.preference={policy:'round-robin',status:'active'};
  c.fixture.providers.push({id:'codex',routable:true,accounts:[{...b,provider:'codex',key:'codex-b.json'}]});
  render();get('routing-all-mode').value='codex-b.json';c.openRoutingChoice('all','codex-b.json');
  assert.equal(dialog.open,true);c.fixture.providers[0].accounts=[a,{...b,key:'another-replacement.json'}];
  await c.routingChoiceApply('all','move');
  assert.equal(posts.length,0);assert.equal(dialog.open,false);assert.equal(get('routing-all-mode').value,'round-robin');
  console.log('Routing inventory modal: removed/replaced choices close and never save a fallback; valid Move retains its key');
})().catch(error=>{console.error(error);process.exitCode=1;});
