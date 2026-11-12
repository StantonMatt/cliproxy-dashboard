'use strict';
// Browse ranges use the board engine; they never start a layout write or replace its default.
const assert=require('node:assert/strict'),setup=require('./helpers/requests.cjs');
(async()=>{
 const {c,r,ctx,calls}=setup(),B=c.window.RouterBoards;
 for(const key of ['today','week','month','all']){const b={id:'usage',range:key};const range=B.resolveRange({options:{}},b);assert.equal(range.key,key);assert(range.since<=range.until);assert(range.until-range.since<=366*86400000);}
 const a=r.render(ctx),b=r.render({...ctx,range:{...ctx.range,key:'week',since:new Date(+ctx.range.since-6*86400000)}});calls[1].resolve({rows:[{id:2}],next:null});await b;calls[0].resolve({rows:[{id:1}],next:null});await a;assert.equal(r.rows[0].id,2);
 r.destroy();console.log('Board ranges and stale request response checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
