import assert from 'node:assert/strict';
import {analyseTrace,analyseProfile,distribution} from '../scripts/perf/trace.mjs';
const e=(name,ts,dur,args={})=>({name,ts,dur,args,pid:1,tid:2,ph:'X'});
const trace={traceEvents:[{name:'thread_name',ph:'M',pid:1,tid:2,args:{name:'CrRendererMain'}},
  {name:'perf:S1:start',ts:0,pid:1,tid:2},{name:'perf:S1:end',ts:200000,pid:1,tid:2},
  e('EventDispatch',1000,100000),e('FunctionCall',2000,90000,{data:{functionName:'follow',url:'boards.js',lineNumber:617}}),
  e('UpdateLayoutTree',3000,10000),e('Layout',14000,20000),e('Paint',110000,5000),
  e('Layout',120000,10000)]};
const result=analyseTrace(trace,'S1');
assert.deepEqual(result.mainThreadMs,{script:70,layout:30,recalcStyle:10,paint:5});
assert.equal(result.forcedLayouts,1);assert.equal(result.forcedStyleRecalcs,1);
assert.match(result.forcedSources[0].source,/follow boards.js:617/);
assert.equal(result.forcedSources[0].ms,30);
const crossing=structuredClone(trace);
crossing.traceEvents.find(e=>e.name==='perf:S1:start').ts=10000;
const clipped=analyseTrace(crossing,'S1');
assert.deepEqual(clipped.mainThreadMs,{script:68,layout:30,recalcStyle:3,paint:5});
assert.equal(clipped.forcedLayouts,1);
assert.equal(clipped.forcedStyleRecalcs,1);
assert.deepEqual(distribution([20,10,30,40,50]),{count:5,median:30,p95:50,min:10,max:50,spread:40});
assert.equal(distribution([]).median,null);
assert.throws(()=>analyseTrace({traceEvents:[]},'S1'),/missing scenario markers/);
const profile=analyseProfile({nodes:[{id:1,callFrame:{functionName:'follow',url:'boards.js',lineNumber:616,columnNumber:0}}],
  samples:[1,1],timeDeltas:[1000,2000]});
assert.equal(profile[0].selfMs,3);assert.match(profile[0].source,/boards.js:617:1/);
// Resize phase boundaries, multiple redraws in one widget, and target/neighbour attribution.
const {resizePhase}=await import('../scripts/perf/resize-metrics.mjs');
const phase=resizePhase({events:[
  {kind:'chartSVG',start:9,widget:'target'},
  {kind:'chartSVG',start:10,widget:'target'}, {kind:'chartSVG',start:11,widget:'target'},
  {kind:'chartSVG',start:12,widget:'neighbour'}, {kind:'chartSVG',start:20,widget:'target'},
  {kind:'resizeObserver',start:11,source:'/charts.js',widgets:['target']},
  {kind:'resizeObserver',start:12,source:'/charts.js',widgets:['neighbour']},
  {kind:'resizeObserver',start:12,source:'/boards.js',widgets:['target']},
  {kind:'naturalProbe',start:13,widget:'neighbour'}, {kind:'applyPacking',start:14},
  {kind:'resizeRAF',start:15,duration:2}],longTasks:[{start:-50,duration:65},{start:19,duration:70},
  {start:20,duration:80}]},10,20,'target',{windowMs:10,mainThreadMs:{script:2,layout:3,recalcStyle:4,paint:5},
  forcedLayouts:1,forcedStyleRecalcs:2});
assert.equal(phase.chartSVGReplacements,3);
assert.equal(phase.resizedWidgetSVG,2);assert.equal(phase.neighbourSVG,1);
assert.deepEqual(phase.svgByWidget,{neighbour:1,target:2});
assert.equal(phase.chartResizeCallbacks,2);assert.equal(phase.resizedWidgetResizeCallbacks,1);
assert.equal(phase.neighbourResizeCallbacks,1);assert.equal(phase.boardResizeCallbacks,1);
assert.equal(phase.naturalProbes,1);assert.equal(phase.applyPacking,1);
assert.equal(phase.longTasks,2);assert.equal(phase.longTaskTotalMs,6);
assert.equal(phase.workMs,14);assert.equal(phase.resizeRAF.count,1);
const clippedProfile=analyseProfile({startTime:100,nodes:[{id:1,callFrame:{functionName:'resize',url:'boards.js',lineNumber:597,columnNumber:0}}],
  samples:[1,1],timeDeltas:[1000,2000]},{startTime:600,endTime:1600});
assert.equal(clippedProfile[0].selfMs,1);assert.equal(clippedProfile[0].samples,2);
// Chromium can emit samples out of timestamp order (negative deltas). Rebuild
// absolute sample times, sort, then weight non-overlapping chronological gaps.
const unordered=analyseProfile({startTime:0,endTime:2000,
  nodes:[{id:1,callFrame:{functionName:'resize',url:'boards.js',lineNumber:597,columnNumber:0}},
    {id:2,callFrame:{functionName:'other',url:'charts.js',lineNumber:50,columnNumber:0}}],
  samples:[1,2,1,1],timeDeltas:[1000,1000,-500,500]});
assert.equal(unordered.reduce((total,x)=>total+x.selfMs,0),2);
assert.deepEqual(Object.fromEntries(unordered.map(x=>[x.source.split(' ')[0],x.selfMs])),{resize:1.5,other:.5});
// A stretched CSS box must not hide stale SVG coordinates; all neighbours and
// missing/blank hosts participate, and the 2px tolerance is inclusive.
const {chartsFinalGate}=await import('../scripts/perf/instrument.mjs');
const host=(widget,actual=300)=>({widget,nonEmpty:true,checks:[{name:'viewBox.width',actual,expected:300}]});
const gate=hosts=>chartsFinalGate({hosts,missing:[]});
assert.equal(gate([host('target'),host('neighbour',298)]).pass,true);
const stale=gate([host('target'),host('neighbour',297.9)]);
assert.equal(stale.pass,false);assert.equal(stale.hosts[1].pass,false);
assert.ok(Math.abs(stale.worstDeltaPx-2.1)<1e-9);
assert.equal(gate([{...host('blank'),nonEmpty:false}]).pass,false);
assert.equal(gate([host('invalid',NaN)]).pass,false);
assert.equal(gate([]).pass,false);
assert.equal(chartsFinalGate({hosts:[host('target')],missing:[{widget:'neighbour'}]}).pass,false);
assert.equal(gate([{...host('unchecked'),checks:[]}]).pass,false);
console.log('PASS: 31 trace/distribution/profile/resize assertions; 9 final-chart gate assertions');
