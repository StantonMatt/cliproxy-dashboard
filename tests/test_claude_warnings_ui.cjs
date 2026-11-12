'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
let clock=Date.parse('2026-10-30T19:51:00Z');
class FakeDate extends Date {constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
function harness(){
  const elements=new Map(),document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null};
  document.activeElement=document.body;
  const get=id=>{if(!elements.has(id))elements.set(id,{id,hidden:true,innerHTML:'',addEventListener(){},classList:{toggle(){}},focus(){}});return elements.get(id);};
  document.getElementById=get;
  const c=vm.createContext({document,Date:FakeDate,localStorage:{getItem:()=>null},JSON,Math});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dashboard/static/billing.js'),'utf8'),c);
  vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
  const row=(id,email,kind='five_hour',left=8)=>({id:id.repeat(24),account_id:email,email,kind,limit:kind==='five_hour'?'5-hour limit':'Weekly · Fable',remaining:left,resets_at:clock/1000+7200});
  const data={gateway:{online:true,accounts:[],claude_accounts:[],claude_warnings:{low:[row('a','a@example.test')],out:null}}};
  c.fixture=data;vm.runInContext('current=fixture;failed=false',c);
  return {c,get,data,row,run:s=>vm.runInContext(s,c),render:()=>{c.renderClaudeWarnings(data);return get('claude-warnings').innerHTML;}};
}
(async()=>{
  // An initial overview failure must expose both the notice and its hidden parent.
  let h=harness();h.run('current=null');
  h.c.request=async()=>{throw Error('desktop unreachable');};
  await h.c.load();
  assert.equal(h.get('notice').hidden,false);
  assert.match(h.get('notice').textContent,/Connecting to your Linux desktop/);
  assert.equal(h.get('banner-stack').hidden,false);
  h=harness();let html=h.render();
  assert.match(html,/Claude running low/);assert.match(html,/8% left/);
  // Narrow screens keep the countdown and the short limit name; the verb, clock and suffix can hide, the tooltip keeps the exact time.
  assert.match(html,/<span class="allowance-limit">5-hour<span class="limit-more"> limit<\/span><\/span>/);
  assert.match(html,/<span class="allowance-verb">resets in <\/span><span title="resets in 2h 0m · [^"]+[AP]M">2h 0m<\/span><span class="allowance-clock"> · <time>/);
  assert.match(html,/id="claude-low-dismiss"/);assert.equal(h.get('banner-stack').hidden,false);
  h.data.gateway.claude_warnings.low.push(h.row('b','a@example.test','weekly_model',4));
  html=h.render();assert.match(html,/<li class="cont"><span class="allowance-account same"/);
  h.data.gateway.claude_warnings.low[0].remaining=0;
  assert.match(h.render(),/Used up/);assert.match(h.render(),/back in/);
  // An account with any limit used up is out, not running low.
  assert.match(h.render(),/id="low-title">Claude account out of usage</);assert.doesNotMatch(h.render(),/running low/);
  h.data.gateway.claude_warnings.low.push(h.row('d','c@example.test'));
  assert.match(h.render(),/id="low-title">Claude account out of usage · another running low</);
  h.data.gateway.claude_warnings.low.pop();
  h.data.gateway.claude_warnings.low.forEach(r=>r.dismissed=true);
  assert.equal(h.render(),'');assert.equal(h.get('banner-stack').hidden,true);
  h.get('notice').hidden=false;h.render();assert.equal(h.get('banner-stack').hidden,false);
  h.data.gateway.claude_warnings.low.push(h.row('c','b@example.test'));
  html=h.render();assert.match(html,/b@example.test/);assert.doesNotMatch(html,/a@example.test/);
  clock+=7200001;assert.equal(h.render(),'');clock-=7200001;
  h=harness();h.data.read_only=true;assert.match(h.render(),/aria-label="Dismiss Claude warning" disabled/);
  h=harness();h.run('failed=true');assert.match(h.render(),/aria-label="Dismiss Claude warning" disabled/);
  h=harness();h.data.gateway.claude_warnings.out={codex_ready:true,resets_at:clock/1000+360,rows:[
    {email:'b@example.test',limit:'5-hour limit',resets_at:clock/1000+360,note:'Weekly 2% left'},
    {email:'a@example.test',limit:'Weekly · all models',resets_at:clock/1000+7200},
    {email:'off@example.test',limit:'Routing off',off:true,remaining:29}]};
  html=h.render();assert.match(html,/role="alert"/);assert.match(html,/Switch to OpenAI/);assert.match(html,/>6m</);
  assert.match(html,/Weekly 2% left/);assert.match(html,/Routing off/);assert.match(html,/29% left/);
  assert.doesNotMatch(html,/Claude running low|reset-close|<button/);
  h.data.gateway.claude_warnings.out.codex_ready=false;assert.match(h.render(),/Codex has no ready account either/);
  clock+=361000;assert.match(h.render(),/Reset due · checking/);assert.doesNotMatch(h.render(),/refreshing/);clock-=361000;
  h.data.gateway.claude_warnings.out.resets_at=null;assert.doesNotMatch(h.render(),/out-back|Back in/);
  h.data.gateway.claude_warnings.out.rows[0].email='<img src=x onerror=alert(1)>';
  assert.match(h.render(),/&lt;img/);assert.doesNotMatch(h.render(),/<img/);
  // Errors/staleness retain priority, and cue copy moves freshness to the tooltip.
  h=harness();const a={status:'ok',email:'a@example.test',key:'a',auth_index:'a',checked_at:new FakeDate().toISOString(),near_limit:true,
    windows:[{kind:'five_hour',remaining:8,used:92,resets_at:clock/1000+7200},{kind:'seven_day',remaining:29,used:71,resets_at:clock/1000+200000}]};
  h.data.gateway.claude_accounts=[{key:'a',auth_index:'a',email:a.email,near_limit:true},
    {key:'b',auth_index:'b',email:'b@example.test',routing_enabled:true,near_limit:false,runtime_available:true}];
  let state=h.c.claudeCardState(a,h.data);assert.equal(state.text,'Near limit');assert.match(state.title,/new sessions go to b@example.test/);assert.match(state.title,/Updated/);
  h.data.gateway.claude_accounts[1].near_limit=true;
  h.data.gateway.claude_preference={status:'active',actual_priorities:{'a@example.test':100,'b@example.test':0}};
  h.data.gateway.claude_accounts[0].routing_enabled=true;
  assert.match(h.c.claudeCardState(a,h.data).title,/All Claude accounts are near their limits/);
  delete h.data.gateway.claude_preference;
  assert.match(h.c.claudeCardState(a,h.data).title,/All Claude accounts are near their limits/);
  a.windows[0].remaining=0;state=h.c.claudeCardState(a,h.data);assert.match(state.text,/Out · back in/);
  a.error='http_429';assert.match(h.c.claudeCardState(a,h.data).text,/Rate-limited/);
  h.run('failed=true');assert.equal(h.c.claudeCardState(a,h.data).text,'Status stale');h.run('failed=false');
  a.error=null;a.windows[0].resets_at=clock/1000-1;assert.match(h.c.claudeCardState(a,h.data).text,/Stale/);
  assert.match(h.c.limitRow('5-hour limit',{remaining:8},true),/class="low">8% remaining/);
  assert.doesNotMatch(h.c.limitRow('5-hour limit',{remaining:10},true),/class="low"/);
  // Low allowance colour belongs to Claude, for both the hero and shared limit rows.
  h=harness();
  const lowWindows=[{kind:'five_hour',label:'5-hour',seconds:18000,remaining:4,used:96,resets_at:clock/1000+7200},
    {kind:'seven_day',label:'Weekly',seconds:604800,remaining:8,used:92,resets_at:clock/1000+200000}];
  h.data.gateway.accounts=[{id:'codex',email:'codex@example.test',windows:lowWindows}];
  h.c.renderAccounts(h.data.gateway);
  const codex=h.get('account-grid').innerHTML;
  assert.match(codex,/92% used/);assert.match(codex,/4% remaining/);
  assert.doesNotMatch(codex,/class="low"/);
  h.data.watch_accounts=[{id:'claude',key:'claude',email:'claude@example.test',status:'ok',
    checked_at:new FakeDate().toISOString(),windows:lowWindows}];
  h.c.renderWatch(h.data);
  const claude=h.get('watch-grid').innerHTML;
  assert.match(claude,/class="used-figure low">92% used/);
  assert.match(claude,/class="low">4% remaining/);
  // Dismiss exactly the displayed rows; preserve warnings on a failed write.
  h=harness();const sent=[];h.c.load=async()=>{};
  h.c.request=async(url,options)=>{sent.push([url,JSON.parse(options.body)]);};
  await h.c.dismissClaudeWarnings();assert.equal(sent[0][0],'/api/claude-warnings/dismiss');
  assert.deepEqual(sent[0][1],{rows:['a'.repeat(24)]});assert.equal(h.render(),'');
  h=harness();h.c.request=async()=>{throw Error('disk full');};await h.c.dismissClaudeWarnings();
  assert.match(h.render(),/Warning not dismissed. Try again/);assert.match(h.render(),/8% left/);
  h=harness();h.data.read_only=true;h.c.request=()=>{throw Error('should not write');};await h.c.dismissClaudeWarnings();
  // #3: a near-limit card never names itself as a destination, even when preferred.
  h=harness();const own={...a,error:null,windows:lowWindows,near_limit:true};
  h.data.gateway.claude_accounts=[{key:own.key,auth_index:own.auth_index,email:own.email,routing_enabled:true,near_limit:true,runtime_available:true}];
  assert.doesNotMatch(h.c.claudeCardState(own,h.data).title,/new sessions go to a@example.test/);
  // #4: subscription problems take the plan badge's place in their own tone; the badge stays the billing link.
  for(const [status,tone,label,action] of [['unpaid','warn','Payment due','Pay'],['expired','down','Subscription ended','Renew']]){
    const sub=h.c.subscriptionHtml({plan:'Max (5x)',subscription:{subscription_status:status}});
    assert.match(sub,new RegExp(`^<a class="small-badge plan-badge ${tone}" href="https://claude\\.ai/settings/billing"`));
    assert.match(sub,new RegExp(`aria-label="${label} · last known · ${action}">${label}<svg`));
    assert.match(sub,new RegExp(`title="Max · ${label} · last known`));
  }
  const active=h.c.subscriptionHtml({plan:'Max (5x)',subscription:{subscription_status:'active'}});
  assert.match(active,/^<a class="small-badge plan-badge" [^>]*>Max<svg/);assert.doesNotMatch(active,/Payment due|Subscription ended/);
  // #5: a primary exhaustion mutes even a nonempty weekly hero.
  h=harness();h.data.watch_accounts=[{...own,windows:lowWindows.map(w=>({...w,remaining:w.kind==='five_hour'?0:80,used:w.kind==='five_hour'?100:20}))}];
  h.c.renderWatch(h.data);assert.match(h.get('watch-grid').innerHTML,/claude-card is-out/);
  // #6: signed-out account rows and cards explain the next action; no unknown countdown hero.
  h.data.watch_accounts[0].auth_state='sign_in_required';h.c.renderWatch(h.data);
  assert.match(h.get('watch-grid').innerHTML,/Sign in required/);
  h.data.gateway.claude_warnings.out={codex_ready:true,resets_at:null,rows:[{email:'a@example.test',limit:'Signed out',resets_at:null}]};
  html=h.render();assert.match(html,/Signed out[\s\S]*Sign in on your computer/);
  assert.doesNotMatch(html,/Back in|out-back|Return time unknown/);
  // #7 and #9: countdowns have an explicit clock separator, due resets have no past clock.
  assert.match(h.c.allowanceWhen(clock/1000+120),/back in <\/span><span title="back in 2m · [^"]+">2m<\/span><span class="allowance-clock"> · <time>/);
  assert.equal(h.c.allowanceLimit('Weekly · all models'),'Weekly<span class="limit-more"> · all models</span>');
  assert.equal(h.c.allowanceLimit('Weekly · Fable'),'Weekly · Fable');assert.equal(h.c.allowanceLimit('Routing off'),'Routing off');
  assert.equal(h.c.allowanceWhen(clock/1000-1),'Reset due · checking');
  h.data.gateway.claude_warnings.out.resets_at=clock/1000-1;
  html=h.render();assert.match(html,/<div class="out-back checking"><span>Reset due · checking<\/span><\/div>/);
  assert.doesNotMatch(html,/<strong>|<time>/);
  const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
  // #4, #5, #8, #9, #10: focused style contracts, also checked as computed styles in the browser.
  assert.match(css,/\.account-card \.plan-badge\.warn\{color:var\(--amber\);/);
  assert.match(css,/\.account-card \.plan-badge\.down\{color:var\(--danger\);/);
  assert.match(css,/\.claude-card.is-out \.quota-value\{color:var\(--faint\)\}/);
  assert.match(css,/\.claude-card.is-out \.quota-track>span:not\(\.pace-bar\)\{background:var\(--faint\)\}/);
  assert.match(css,/\.out-switch\{[^}]*color:var\(--muted\);font-weight:400/);
  assert.match(css,/\.out-back.checking>span\{font-size:12px\}/);
  assert.match(css,/\.allowance-when time\{color:var\(--muted\)\}/);
  // The stack is outside page sections, so all three pages keep its order.
  const index=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
  assert.ok(index.indexOf('id="notice"')<index.indexOf('id="claude-warnings"'));
  assert.ok(index.indexOf('id="claude-warnings"')<index.indexOf('id="overview-view"'));
  console.log('Claude warnings UI: banners, card cues, expiry, dismissal, escaping and page stack passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
