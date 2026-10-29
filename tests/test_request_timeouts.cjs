'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const {EventEmitter}=require('node:events');
const source=fs.readFileSync(path.join(__dirname,'../scripts/router-preview.mjs'),'utf8');
const callbacks=[], timers=new Map(), upstreams=[];
let timerId=0;
const config={bind:'127.0.0.1',hostname:'localhost',base_port:18740,devices:[],previews:[]};
class Upstream extends EventEmitter {
  constructor(options,reply){super();this.options=options;this.reply=reply;this.destroyed=false;}
  destroy(){if(!this.destroyed){this.destroyed=true;this.emit('error',new Error('Synthetic deadline'));}}
}
const context=vm.createContext({console,Date,Set,Math,Number,String,Object,JSON,
  fs:{readFileSync:()=>JSON.stringify(config)},net:{},process:{env:{},on(){},exit(){throw Error('Unexpected exit');}},
  setInterval:()=>({unref(){}}),setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},
  clearTimeout:id=>timers.delete(id),http:{createServer(callback){
    callbacks.push(callback);return {on(){},listen(){},close(){}};
  },request(options,reply){const upstream=new Upstream(options,reply);upstreams.push(upstream);return upstream;}}});
vm.runInContext(source.replace(/^#!.*\n/,'').replace(/^import .*;\n/gm,''),context);
assert.equal(callbacks.length,10);
function request(route){
  const incoming=new EventEmitter();
  Object.assign(incoming,{method:'POST',url:route,headers:{host:'127.0.0.1:18749'},
    socket:{remoteAddress:'127.0.0.1'},pipe(){}});
  const response=new EventEmitter();
  Object.assign(response,{headersSent:false,writeHead(status){this.status=status;this.headersSent=true;},
    end(){this.ended=true;this.emit('close');},destroy(){this.destroyed=true;this.emit('close');}});
  callbacks[9](incoming,response);
  return {incoming,response,upstream:upstreams.at(-1)};
}
// A move runs inside the routing request's server deadline, which must end before the proxy and browser give up.
const serverDeadline=Number(fs.readFileSync(path.join(__dirname,'../dashboard/request_deadline.py'),'utf8').match(/^ACCOUNT_MUTATION_SECONDS = (\d+)$/m)[1])*1000;
assert.equal(Number(source.match(/^const ACCOUNT_MUTATION_DEADLINE_MS = (\d+);$/m)[1]),serverDeadline);
assert(serverDeadline<50000,'the server deadline ends before the 50 s proxy timer below');
for(const route of ['/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/routing','/api/routing','/api/claude-routing','/api/routing-all','/api/routing-link','/api/claude/accounts/routing']){
  const {response,upstream}=request(route);
  assert.equal(upstream.options.timeout,50000);
  const deadline=[...timers.values()].find(timer=>timer.delay===50000);
  assert(deadline,'Account request needs an absolute timer, not just socket inactivity');
  for(let index=0;index<3;index++)upstream.emit('data',Buffer.from('synthetic activity'));
  deadline.callback();
  assert(upstream.destroyed);
  assert.equal(response.status,502);
  assert(response.ended);
  assert.equal(timers.size,0,'Closed response must release its deadline timer');
  const success=request(route);
  success.response.end();
  assert.equal(timers.size,0,'Successful response must release its deadline timer');
  assert(success.upstream.destroyed);
}
const other=request('/api/fast-mode');
assert.equal(other.upstream.options.timeout,30000);
assert.equal(timers.size,0,'Unrelated endpoints keep their existing timeout policy');
other.response.end();
async function browserChecks(){
  const app=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
  const recorded=[];
  const browser=vm.createContext({JSON,Error,AbortSignal:{timeout:milliseconds=>milliseconds},
    fetch:async(url,options)=>{recorded.push({url,timeout:options.signal});return {ok:true,json:async()=>({})};}});
  const definitions=app.match(/const ACCOUNT_[^;]+;/g).join('\n')+'\n'+
    app.match(/async function accountRequest\([\s\S]*?\n\}/)[0]+'\n'+
    app.match(/async function request\([\s\S]*?\n\}/)[0];
  vm.runInContext(definitions,browser);
  for(const route of ['/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/routing']){
    await browser.accountRequest(route,{});
    assert.equal(recorded.at(-1).timeout,55000);
  }
  await browser.request('/api/routing',{method:'POST',body:'{}'});
  assert.equal(recorded.at(-1).timeout,55000);
  await browser.request('/api/claude-routing',{method:'POST',body:'{}'});
  assert.equal(recorded.at(-1).timeout,55000);
  await browser.request('/api/routing-all',{method:'POST',body:'{}'});
  assert.equal(recorded.at(-1).timeout,55000);
  assert.doesNotMatch(app,/routing-move/,'the separate move endpoint is gone');
  await browser.request('/api/fast-mode',{method:'POST',body:'{}'});
  assert.equal(recorded.at(-1).timeout,75000);
  await browser.request('/api/overview');
  assert.equal(recorded.at(-1).timeout,15000);
  console.log('Request timeout enforcement and browser ordering checks passed');
}
browserChecks().catch(error=>{console.error(error);process.exitCode=1;});
