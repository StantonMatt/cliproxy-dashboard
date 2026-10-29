'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'fast_mode_fixture.py'),'utf8');
// F13: scenario flags must reach the complete renderer on first and later snapshots.
const script=source.match(/FIXTURE_JS = b'''([\s\S]*?)'''/)[1];
for(const scenario of ['read-only','offline']){
  let timer,draws=[];
  const context=vm.createContext({URLSearchParams,location:{search:'?scenario='+scenario},
    document:{getElementById:()=>null},current:{gateway:{online:true}},fastPending:new Map(),fastErrors:new Map(),Date,
    setInterval:fn=>{timer=fn;return 1;},clearInterval(){},
    render(){draws.push([context.current.read_only,context.current.gateway.online]);},
    renderAccounts(){throw Error('Scenario must render the complete overview');}});
  vm.runInContext(script,context);timer();
  assert.equal(draws.length,1);
  context.current={gateway:{online:true}};vm.runInContext('render()',context);
  assert.deepEqual(draws,scenario==='read-only'?[[true,true],[true,true]]:[[undefined,false],[undefined,false]]);
}
console.log('Fixture frontend: F13 persistent full-overview scenario flags passed');
