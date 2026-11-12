'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {install,context}=require('./board-widgets.cjs');
module.exports=function(){
 const nodes=new Map(),document={activeElement:{},documentElement:{dataset:{}},querySelectorAll:()=>[],querySelector:()=>null,getElementById(id){if(!nodes.has(id))nodes.set(id,{style:{},classList:{toggle(){}},addEventListener(){}});return nodes.get(id);}};
 const c=vm.createContext({document,Date,JSON,Math,Intl,localStorage:{getItem:()=>null}}),s=fs.readFileSync(path.join(__dirname,'../../dashboard/static/app.js'),'utf8');vm.runInContext(s.slice(0,s.indexOf("window.addEventListener('hashchange'")),c);install(c);
 const data={gateway:{accounts:[]},providers:[{id:'codex',name:'Codex',accounts:[]},{id:'claude',name:'Claude',accounts:[]}],usage:{available:true,providers:{},failure_counts:{rate_limit:2,capacity:0,auth:0,server_error:1,timeout:0,cancelled:0,other:0,unknown:0}}};c.fixture=data;vm.runInContext('current=fixture;today=fixture',c);
 const host={innerHTML:'',contains:()=>false,addEventListener(){},removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],closest:()=>null};
 const calls=[],ctx=context(c,'requests',{rows:50,filters:true,more:true},data);ctx.api=(url,params)=>new Promise((resolve,reject)=>calls.push({url,params,resolve,reject}));
 const r=new c.window.boardTest.Requests(host),flush=()=>new Promise(resolve=>setImmediate(resolve));
 return {c,document,host,calls,ctx,r,flush,data};
};
