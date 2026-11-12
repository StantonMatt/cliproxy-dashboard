'use strict';
// Shared deterministic harness for the production registry path, without singleton DOM fixtures.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ROOT=path.resolve(__dirname,'../..');
function install(c){
 c.window ||= {};c.document.addEventListener ||= ()=>{};
 c.setTimeout ||= ()=>0;c.clearTimeout ||= ()=>{};c.setInterval ||= ()=>0;c.clearInterval ||= ()=>{};c.requestAnimationFrame ||= ()=>0;
 c.location ||= {hash:'#overview',origin:'http://example.test'};
 const app=fs.readFileSync(path.join(ROOT,'dashboard/static/app.js'),'utf8');
 vm.runInContext(app.slice(app.indexOf('window.RouterApp={')),c);
 let code=fs.readFileSync(path.join(ROOT,'dashboard/static/boards.js'),'utf8').replace('window.RouterBoards={','window.boardTest={registerLegacy,Requests,defs,S};window.RouterBoards={');
 vm.runInContext(code,c);c.window.boardTest.registerLegacy();
 for(const f of ['charts.js','widgets.js'])vm.runInContext(fs.readFileSync(path.join(ROOT,'dashboard/static',f),'utf8'),c);
 return c.window;
}
function context(c,type,options={},data){
 const d=c.window.boardTest.defs.get(type),o={...d.defaults,...options};data ||= vm.runInContext('current',c);
 const since=new c.Date();since.setHours(0,0,0,0);
 return {widget:{id:'test-'+type,type,title:'',options:o},options:o,overview:data,range:{key:'today',since,until:new c.Date(),bucket:'hour',label:'Today'},board:{id:'overview',range:'today'},
  preview:false,customizing:false,width:600,tz:'UTC',state(host,kind,info){host.innerHTML='<div class="empty-state">'+info.message+'</div>';},
  api:async()=>data,accountColor:c.window.RouterApp.accountColor,accountName:c.window.RouterApp.accountName,accentOf:c.accentOf};
}
function historyKeys(c){
 if(!c.window?.RouterWidgets)install(c);
 const data=vm.runInContext('current',c),rows=Object.entries(c.usageProviders(data.usage)).flatMap(([provider,u])=>[...(u.accounts||[]),...(u.hours||[]).filter(r=>!(u.accounts||[]).some(a=>a.auth_index===r.auth_index&&a.key===r.key))].filter(r=>r.requests>0).map(r=>({key:'account:'+provider+':'+(r.auth_index||r.key),provider,auth_index:r.auth_index,account_key:r.key,email:r.email,values:[r.requests],total:r.requests})));
 const ctx=context(c,'chart');ctx.overview={...data,providers:c.providerData()};
 return c.window.RouterWidgets.seriesKeys(ctx,{group:'account',series:rows},ctx.options);
}
module.exports={install,context,historyKeys};
