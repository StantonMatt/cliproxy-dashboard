'use strict';
// Cache/history regressions now exercise the registered Tokens, stat and usage table widgets.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {install,context}=require('./helpers/board-widgets.cjs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../dashboard/static/app.js'),'utf8');
const nodes=new Map(),document={documentElement:{dataset:{}},querySelectorAll:()=>[],querySelector:()=>null,getElementById(id){if(!nodes.has(id))nodes.set(id,{style:{},classList:{toggle(){}},addEventListener(){}});return nodes.get(id);}};
const c=vm.createContext({document,Date,JSON,Math,Intl,localStorage:{getItem:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);install(c);
const row=(requests,total_tokens)=>({requests,total_tokens,failures:0,input_tokens:total_tokens,output_tokens:0,cached_tokens:0});
const split=(r,read,rc,write,wc)=>({...r,cache_read_tokens:read,cache_read_requests:rc,cache_creation_tokens:write,cache_creation_requests:wc});
const data=(codex=[],claude=[])=>({gateway:{accounts:[]},usage:{available:true,since:'2026-10-15T12:00:00Z',accounts:codex,models:[],claude:{since:'2026-10-02T12:00:00Z',accounts:claude,models:[]}}});
const host=()=>({innerHTML:'',classList:{add(){},remove(){}}});
async function render(d){c.fixture=d;vm.runInContext('current=fixture;today=fixture',c);const tokens=host(),accounts=host();await c.window.boardTest.defs.get('tokens').render(tokens,context(c,'tokens'));await c.window.boardTest.defs.get('usage-accounts').render(accounts,context(c,'usage-accounts'));return {tokens:tokens.innerHTML,accounts:accounts.innerHTML};}
(async()=>{
 let v=await render(data([split(row(3,30),10,3,25,3)],[split(row(2,20),4000,2,1500,2)]));assert.match(v.tokens,/4K · 1.5K/);assert.match(v.accounts,/>4,000</);assert.match(v.accounts,/>1,500</);
 for(const r of [split(row(3,30),null,0,null,0),row(3,30),split(row(3,30),0,0,0,0)]){v=await render(data([r]));assert.match(v.tokens,/Not recorded/);assert.doesNotMatch(v.tokens,/width:NaN/);}
 v=await render(data([split(row(4,40),12,2,0,3)],[split(row(5,50),3000,5,9000,1)]));assert.match(v.tokens,/Recorded for 7 of 9 requests">≥3K/);assert.match(v.tokens,/Recorded for 4 of 9 requests">≥9K/);assert.match(v.accounts,/Recorded for 2 of 4 requests">≥12/);
 v=await render(data([{...split(row(6,60),5000,6,0,6),cached_tokens:5200}],[split(row(2,20),0,2,41200,2)]));assert.match(v.accounts,/>5,200</);assert.match(v.accounts,/>41,200</);assert.doesNotMatch(v.tokens,/Not recorded/);
 const codex={...split(row(2843,262657437),242599808,2843,0,2843),input_tokens:261358580,output_tokens:1298857,cached_tokens:242599808};
 const claude={...split(row(1960,305294699),286354414,1960,15473237,1960),input_tokens:1117954,output_tokens:2349094,cached_tokens:289723396};
 v=await render(data([codex],[claude]));assert.match(v.tokens,/>93\.7%</);assert.match(v.tokens,/564,304,185 input · 3,647,951 output tokens/);assert.match(v.accounts,/302,945,605/);
 v=await render(data([],[{...split(row(1,1150),0,1,1000,1),input_tokens:100,output_tokens:50,cached_tokens:1000}]));assert.match(v.tokens,/>0%</);assert.match(v.accounts,/>1,100</);
 v=await render(data([],[{...row(2,400),input_tokens:10,output_tokens:20,cached_tokens:300}]));assert.match(v.accounts,/>380</);assert.match(v.tokens,/Not recorded/);
 v=await render(data([{...row(1,100),cached_tokens:80}],[{...split(row(3,520),80,1,20,1),input_tokens:20,output_tokens:30,cached_tokens:380}]));assert.match(v.tokens,/Recorded for 2 of 4 requests">≥27\.1%/);assert.match(v.accounts,/>490</);
 const d=data();d.usage.providers=Object.fromEntries(['codex','xai','xai-api-key','codex-api-key','openai-compatibility','unknown'].map(p=>[p,{accounts:[{...split(row(1,120),80,1,0,1),input_tokens:100,output_tokens:20,cached_tokens:80}]}]));d.usage.providers['claude-api-key']={accounts:[{...split(row(1,120),80,1,20,1),input_tokens:10,output_tokens:10,cached_tokens:80}]};v=await render(d);assert.match(v.tokens,/>78\.9%</);assert.match(v.accounts,/>110</);
 v=await render(data());assert.match(v.tokens,/width:0%/);assert.match(v.tokens,/>—</);
 v=await render(data([{...row(1,10),cached_tokens:20}]));assert.match(v.tokens,/width:100%/);assert.match(v.tokens,/>100%</);
 const u=data();assert.equal(c.usageSince(u.usage),u.usage.claude.since);delete u.usage.claude.since;assert.equal(c.usageSince(u.usage),u.usage.since);u.usage.since=null;assert.equal(c.usageSince(u.usage),null);
 // Stat details use the real /series + overview shapes and preserve partial cache coverage.
 const ctx=context(c,'stat',{metric:'cache_hit_rate'},data([split(row(3,30),12,2,0,3)]));ctx.api=async()=>({series:[{total:40}],unit:'percent'});const stat=await c.window.boardTest.defs.get('stat').table(ctx);assert(stat.rows.some(r=>r[0]==='Cache read'&&r[1]==='≥12'));
 const partial=context(c,'stat',{metric:'cache_hit_rate'},data([{...row(1,100),cached_tokens:80}],[{...split(row(3,520),80,1,20,1),input_tokens:20,output_tokens:30,cached_tokens:380}]));partial.api=ctx.api;const share=await c.window.boardTest.defs.get('stat').table(partial);assert.equal(share.rows[0][1],'≥27.1%');
 console.log('Widget usage history: 13 cache/history/summary cases passed');
})().catch(e=>{console.error(e);process.exitCode=1});
