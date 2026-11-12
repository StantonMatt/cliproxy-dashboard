'use strict';
// All lifecycle assertions run the widget-scoped controller used by every Requests widget.
const assert=require('node:assert/strict'),setup=require('./helpers/requests.cjs');
const row=(id,provider='codex')=>({id,provider,auth_index:'a',model:'m',timestamp:new Date().toISOString(),failed:false,total_tokens:10});
(async()=>{
 const {c,host,calls,ctx,r,flush}=setup();let run=r.render(ctx);assert.equal(calls.length,1);assert.equal(calls[0].params.limit,50);
 // A later range wins even if an earlier response arrives last.
 const next={...ctx,range:{...ctx.range,key:'week',label:'7 days',since:new Date(+ctx.range.since-6*86400000)}};let newer=r.render(next);calls[1].resolve({rows:[row(3)],next:'cursor'});await newer;calls[0].resolve({rows:[row(1)],next:null});await run;assert.equal(r.rows[0].id,3);
 run=r.load(false,true);assert.equal(calls[2].params.before,'cursor');calls[2].resolve({rows:[row(2)],next:null});await run;assert.deepEqual(Array.from(r.rows,x=>x.id),[3,2]);assert(!host.innerHTML.includes('data-request="more"'));
 r.next='again';run=r.load(false,true);calls[3].reject(Error('503'));await run;assert.equal(r.rows.length,2);assert(r.moreError);assert.match(host.innerHTML,/Try again/);run=r.load(false,true);assert.equal(calls[4].params.before,'again');calls[4].resolve({rows:[row(1)],next:null});await run;
 // A live refresh holds the full list until Show new requests, and detects late rows below the top.
 run=r.render(next);calls[5].resolve({rows:[row(3),row(9)],next:null});await run;assert(r.newer);assert.deepEqual(Array.from(r.rows,x=>x.id),[3,2,1]);assert.match(host.innerHTML,/Show new requests/);
 r.provider='claude';r.account='specific';r.failed=true;r.until=new Date(Date.now()-3600000).toISOString();r.kind='rate_limit';run=r.load();assert.equal(calls[6].params.provider,'claude');assert.equal(calls[6].params.account,'specific');assert.equal(calls[6].params.kind,'rate_limit');assert.equal(calls[6].params.failed,1);assert.equal(calls[6].params.until,r.until);calls[6].resolve({rows:[],next:null});await run;
 run=r.render(next);calls[7].resolve({rows:[row(10)],next:null});await run;assert(!host.innerHTML.includes('Show new requests'));
 // Jump filter and epoch state are independent between duplicated widgets.
 const another=new c.window.boardTest.Requests({...host});assert.equal(another.provider,'');assert.equal(another.account,'');assert(!another.failed);
 r.jump({provider:'codex',account:'a',failed:true,until:new Date(Date.now()-1000)});assert.equal(calls[8].params.account,'a');calls[8].resolve({rows:[row(7)],next:null});await flush();assert.equal(r.rows[0].id,7);
 // A failed first page is retryable, malformed responses never render as success.
 r.rows=[];run=r.load();calls[9].resolve({rows:'invalid',next:null});await run;assert.match(r.error,/load requests/);assert.match(host.innerHTML,/Try again/);run=r.render(next);calls[10].resolve({rows:[row(8)],next:null});await run;assert(!r.error);
 r.archived=new Date(+next.range.since+86400000).toISOString();r.rows=[];r.next=null;r.draw();assert.match(host.innerHTML,/Older requests archived/);
 // The shared formatter preserves phone cells, unknown fields, dates, seconds, cache split and outcome text.
 const html=c.requestTable([row(1),{...row(2,'claude'),failed:true,failure:{kind:'rate_limit',status_code:429},cache_read_tokens:0,cache_creation_tokens:41200}],true,true);
 assert.equal((html.match(/<col class="c-/g)||[]).length,8);assert.equal((html.match(/<td role="cell"/g)||[]).length,16);assert.match(html,/c-reasoning unset/);assert.match(html,/Read 0 · Write 41,200/);assert.match(html,/Rate limited/);assert.match(html,/HTTP 429/);assert.match(html,/\d{1,2}:\d{2}:\d{2}/);
 // Time jumps include the picked minute, reject out-of-range times, and reset to latest.
 r.ctx=ctx;r.account='';r.provider='';r.kind='';r.failed=false;const time=new Date(Date.now()-3600000);const input={value:String(time.getHours()).padStart(2,'0')+':'+String(time.getMinutes()).padStart(2,'0'),focus(){}};host.querySelector=s=>s==='[data-request-time-input]'?input:null;
 let n=calls.length;r.event({type:'submit',target:{matches:()=>true},preventDefault(){}});assert.equal(calls.length,n+1);assert.equal(new Date(calls[n].params.until).getSeconds(),59);assert.equal(new Date(calls[n].params.until).getMilliseconds(),999);calls[n].resolve({rows:[row(12)],next:null});await flush();
 r.ctx={...ctx,range:{...ctx.range,since:new Date()}};n=calls.length;r.event({type:'submit',target:{matches:()=>true},preventDefault(){}});assert.equal(calls.length,n);assert.match(r.jumpError,/Choose a time/);
 r.ctx=ctx;r.until=null;r.rows=[row(12)];n=calls.length;r.event({type:'click',target:{closest:()=>({dataset:{request:'new'}})},preventDefault(){}});calls[n].resolve({rows:[row(12),row(13)],next:null});await flush();assert(r.fresh.has(13));assert(!r.fresh.has(12));assert.match(host.innerHTML,/row-new/);
 r.destroy();another.destroy();console.log('Widget Requests: stale epochs, pagination/retry, refresh, duplicate state, account filters, archive and row formatting passed');
})().catch(e=>{console.error(e);process.exitCode=1});
