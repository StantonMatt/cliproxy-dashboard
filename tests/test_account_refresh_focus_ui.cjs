'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map(),documentListeners={};
const document={addEventListener(type,fn){(documentListeners[type] ||= []).push(fn);},documentElement:{dataset:{}},body:{},activeElement:null,querySelectorAll:()=>[],querySelector:s=>s.includes('[data-generic-provider=')?section:null,
 getElementById(id){if(!elements.has(id))elements.set(id,node(id));return elements.get(id);}};
function node(id){return {id,dataset:{},listeners:{},style:{setProperty(){}},value:'',innerHTML:'',classList:{toggle(){}},addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},removeAttribute(){},setAttribute(){},querySelector(){return null},matches(){return false},focus(){document.activeElement=this;}};}
const section=node('generic-section');section.dataset.genericProvider='xai';
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
const source=fs.readFileSync('dashboard/static/app.js','utf8');
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const data={gateway:{online:true,checked_at:new Date().toISOString(),accounts:[]},watch_accounts:[{id:'claude',email:'test@example.test',status:'ok',windows:[]}]};
c.fixture=data;vm.runInContext('current=fixture;reserveCardRows=()=>{};syncStateTip=()=>{}',c);
function checkRefresh(kind,render,host,canonical){
 const prior={id:'widget--'+canonical,dataset:{appPart:canonical},closest:s=>s===kind?host:null,matches:()=>false};document.activeElement=prior;
 let replacement;Object.defineProperty(host,'innerHTML',{configurable:true,get:()=>'',set(){elements.delete(prior.id);replacement=node(canonical);elements.set(canonical,replacement);document.activeElement=document.body;}});
 render();assert.equal(document.activeElement,replacement,kind+' refresh restores canonical focus after replacing controls');
}
checkRefresh('.watch-grid',()=>c.renderWatch(data),document.getElementById('watch-grid'),'claude-route-claude');
checkRefresh('[data-generic-provider]',()=>c.renderProviders({gateway:data.gateway,providers:[{id:'xai',name:'Grok',accounts:[]}]}),section,'generic-route-xai-account');
console.log('Account refresh focus: Claude and generic provider controls retain canonical identity.');

// A pulled-out card is replaced outside its original provider grid. Restore to
// the fresh widget-scoped node, using canonical identity before its DOM path.
const card={dataset:{provider:'claude',accountKey:'linked.json'},children:[],querySelectorAll:()=>[]};
const oldControl={id:'split--route',dataset:{appPart:'route'},parentElement:card,closest:s=>s==='.w-account-card .account-card'?card:null,selectionStart:2,selectionEnd:4};
card.children=[oldControl];document.activeElement=oldControl;
const saved=c.splitCardFocus();assert.equal(saved.part,'route');
const replacement={id:'split--route',dataset:{appPart:'route'},focus(){document.activeElement=this;},setSelectionRange(a,b){this.selection=[a,b];}};
card.children=[replacement];card.querySelectorAll=()=>[replacement];document.activeElement=document.body;document.querySelectorAll=s=>s==='.w-account-card .account-card'?[card]:[];
c.restoreSplitFocus(saved);assert.equal(document.activeElement,replacement);assert.deepEqual(replacement.selection,[2,4]);
// Controls without IDs use their location within the account card.
const noId={dataset:{},parentElement:card,closest:oldControl.closest};card.children=[noId];document.activeElement=noId;
const path=c.splitCardFocus();const freshNoId={focus(){document.activeElement=this;}};card.children=[freshNoId];card.querySelectorAll=()=>[];document.activeElement=document.body;
c.restoreSplitFocus(path);assert.equal(document.activeElement,freshNoId);
console.log('Single-card refresh focus: canonical controls, DOM path fallback and text selection passed.');

// Bubble through the original grid or a pulled-out shell, then the document.
// Run the real registered handlers; spy only on their action entry points.
const actions=[];c.record=(name,...args)=>actions.push([name,...args]);
vm.runInContext(`
 saveClaudeAccountRouting=(...a)=>record('claude-route',...a);
 saveAccountRouting=(...a)=>record('codex-route',...a);
 startSignin=id=>record('signin',id);cancelSignin=id=>record('cancel',id);copySignin=id=>record('copy',id);
 speedShow=()=>{};speedChoose=input=>record('speed',input.dataset.speedAccount,input.value);
 openResetModal=(id,opener,watch=false)=>record('resets',id,watch);
`,c);
function control(dataset,parentElement,classes=[]){return {dataset,parentElement,value:'2',checked:false,disabled:false,classList:{contains:()=>false},
 matches(selector){return selector.split(',').some(s=>{if(s.trim()==='.account-card')return classes.includes('account-card');const m=s.trim().match(/^\[data-([a-z-]+)\]$/);return !!m&&Object.hasOwn(dataset,m[1].replace(/-([a-z])/g,(_,a)=>a.toUpperCase()));});},
 closest(selector){for(let n=this;n;n=n.parentElement)if(n.matches?.(selector))return n;return null;}};}
function bubble(type,target){const event={type,target,key:'',preventDefault(){}};for(let n=target;n;n=n.parentElement)for(const fn of n.listeners?.[type]||[])fn(event);for(const fn of documentListeners[type]||[])fn(event);}
const shell={parentElement:document.body},claudeCard=control({provider:'claude'},document.getElementById('watch-grid'),['account-card']);
const claudeToggle=control({claudeRoute:'claude-id'},claudeCard);
bubble('change',claudeToggle);assert.deepEqual(actions.pop(),['claude-route','claude-id',false],'routing works in its original grid');
claudeCard.parentElement=shell;bubble('change',claudeToggle);assert.deepEqual(actions.pop(),['claude-route','claude-id',false],'routing still works after the card leaves its grid');
const codexCard=control({provider:'codex'},shell,['account-card']);
for(const [data,type,expected] of [
 [{routeAccount:'codex-id'},'change',['codex-route','codex-id',false]],
 [{accessFocus:'codex-id:start',signinStart:'codex-id'},'click',['signin','codex-id']],
 [{accessFocus:'codex-id:cancel',signinCancel:'codex-id'},'click',['cancel','codex-id']],
 [{accessFocus:'codex-id:copy',signinCopy:'codex-id'},'click',['copy','codex-id']],
 [{speedAccount:'codex-id'},'input',['speed','codex-id','2']],
 [{resetOpen:'codex-id'},'click',['resets','codex-id',false]]]){
 actions.length=0;bubble(type,control(data,codexCard));assert.deepEqual(actions,[expected],type+' acts exactly once from a pulled-out card');
}
actions.length=0;bubble('click',control({resetOpen:'claude-id'},claudeCard));assert.deepEqual(actions,[['resets','claude-id',true]],'Claude Resets dispatches once in watch mode');
console.log('Pulled-out controls: Claude/Codex routing, sign-in, cancel, copy, speed and both Resets actions passed.');
