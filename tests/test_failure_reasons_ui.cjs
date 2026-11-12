'use strict';
const assert=require('node:assert/strict'),setup=require('./helpers/requests.cjs');
(async()=>{
 const {c,r,ctx,data,calls,host}=setup();
 for(const [kind,text] of Object.entries({rate_limit:'Rate limited',capacity:'At capacity',auth:'Sign-in issue',server_error:'Server error',timeout:'Timed out',cancelled:'Cancelled',other:'Failed',unknown:'Failed'})){
  const html=c.outcomeCell({failed:true,failure:{kind,status_code:kind==='rate_limit'?429:null,code:'safe_code'}});assert(html.includes(text));assert(html.includes(kind==='cancelled'?'outcome cancelled':'outcome failed'));
 }
 assert.match(c.outcomeCell({failed:true,failure:{kind:'<script>',code:'<script>'}}),/Other failure · &lt;script&gt;/);
 const run=r.render(ctx);calls[0].resolve({rows:[],next:null});await run;assert(!host.innerHTML.includes('Failure reason'));r.failed=true;r.draw();assert.match(host.innerHTML,/Failure reason/);assert.match(host.innerHTML,/All reasons/);assert.match(host.innerHTML,/Rate limited/);assert(!host.innerHTML.includes('At capacity'));
 r.kind='rate_limit';let p=r.load();assert.equal(calls[1].params.kind,'rate_limit');calls[1].resolve({rows:[],next:null});await p;
 data.usage.failure_counts.rate_limit=0;p=r.load();assert.equal(calls[2].params.kind,undefined,'an absent reason resets to All');calls[2].resolve({rows:[],next:null});await p;assert.equal(r.kind,'');
 r.provider='claude';data.usage.providers.claude={accounts:[{auth_index:'key',failures:1}],failure_counts:data.usage.failure_counts};r.account='key';assert.equal(r.counts(),null,'account-filtered counts cannot use provider totals');
 r.destroy();console.log('Widget failure reasons: escaped outcomes, picks, absent reasons and scoped counts passed');
})().catch(e=>{console.error(e);process.exitCode=1});
