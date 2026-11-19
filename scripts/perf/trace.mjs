import fs from 'node:fs/promises';
import {gzip, gunzip} from 'node:zlib';
import {promisify} from 'node:util';
const zip=promisify(gzip), unzip=promisify(gunzip);
export const sum = values => values.reduce((a,b)=>a+b,0);
export function distribution(values) {
  const s=[...values].sort((a,b)=>a-b), at=q=>s.length?s[Math.max(0,Math.ceil(s.length*q)-1)]:null;
  return {count:s.length, median:at(.5), p95:at(.95), min:s[0]??null, max:s.at(-1)??null,
    spread:s.length?s.at(-1)-s[0]:null};
}
export async function saveJSON(file,value) {
  const body=JSON.stringify(value,null,file.endsWith('.gz')?0:2);
  await fs.writeFile(file,file.endsWith('.gz')?await zip(body):body);
}
export async function startTrace(cdp) {
  await cdp.send('Tracing.start',{transferMode:'ReturnAsStream',traceConfig:{recordMode:'recordContinuously',
    includedCategories:['devtools.timeline','v8','blink.user_timing','disabled-by-default-devtools.timeline',
      'disabled-by-default-devtools.timeline.stack']}});
}
export async function stopTrace(cdp,file) {
  const complete=new Promise(resolve=>cdp.once('Tracing.tracingComplete',resolve));
  await cdp.send('Tracing.end');
  const {stream}=await complete;
  const chunks=[];
  for(;;) {
    const r=await cdp.send('IO.read',{handle:stream});
    chunks.push(r.base64Encoded?Buffer.from(r.data,'base64'):Buffer.from(r.data));
    if(r.eof)break;
  }
  await cdp.send('IO.close',{handle:stream});
  const body=Buffer.concat(chunks), trace=JSON.parse(body);
  await fs.writeFile(file,await zip(body));
  return trace;
}
// Exclusive time prevents nested FunctionCall/EventDispatch/Layout double counting.
// Forced layout = Layout or UpdateLayoutTree synchronously nested under JS on
// the renderer main thread, not every geometry read and not DevTools' >30ms warning.
export function analyseTrace(trace,scenario) {
  const events=trace.traceEvents;
  const start=events.find(e=>e.name===`perf:${scenario}:start`)?.ts;
  const end=events.find(e=>e.name===`perf:${scenario}:end`)?.ts;
  if(start==null || end==null)throw Error(`Trace missing scenario markers: ${scenario}`);
  const main=events.find(e=>e.ph==='M' && e.name==='thread_name' && e.args?.name==='CrRendererMain' &&
    events.some(m=>m.name===`perf:${scenario}:start` && m.pid===e.pid && m.tid===e.tid));
  if(!main)throw Error('No renderer main thread');
  const names={Layout:'layout',UpdateLayoutTree:'recalcStyle',Paint:'paint',PrePaint:'paint',
    FunctionCall:'script',EvaluateScript:'script',EventDispatch:'script',FireAnimationFrame:'script',
    TimerFire:'script',RunMicrotasks:'script',V8Console:'script'};
  // Keep enclosing script tasks that straddle a mark. Dropping them loses both
  // their exclusive time and the JS ancestry of nested forced layout events.
  const slices=events.filter(e=>e.pid===main.pid && e.tid===main.tid && e.ph==='X' && e.dur>0 &&
    e.ts<end && e.ts+e.dur>start && names[e.name])
    .map(e=>({...e,originalTs:e.ts,ts:Math.max(start,e.ts),dur:Math.min(end,e.ts+e.dur)-Math.max(start,e.ts)}))
    .sort((a,b)=>a.ts-b.ts||b.dur-a.dur||a.originalTs-b.originalTs);
  const totals={script:0,layout:0,recalcStyle:0,paint:0}, stack=[], forced=[];
  for(const e of slices) {
    while(stack.length && stack.at(-1).e.ts+stack.at(-1).e.dur<=e.ts)stack.pop();
    stack.push({e});
    // Calculate exclusive duration with a second pass below; record initiators here.
    if(['Layout','UpdateLayoutTree'].includes(e.name) && stack.slice(0,-1).some(s=>names[s.e.name]==='script')) {
      const initiator=[...stack].reverse().find(s=>s.e.name==='FunctionCall')?.e.args?.data;
      forced.push({kind:e.name,ms:e.dur/1000,initiator:initiator||null,
        stack:e.args?.beginData?.stackTrace||e.args?.data?.stackTrace||[],ts:e.ts});
    }
  }
  // Direct-child intervals are disjoint under the timeline nesting contract.
  const all=[], parents=[];
  for(const e of slices) {
    while(parents.length && parents.at(-1).e.ts+parents.at(-1).e.dur<=e.ts)parents.pop();
    if(parents.length)parents.at(-1).child+=e.dur;
    const item={e,child:0};all.push(item);parents.push(item);
  }
  for(const {e,child} of all)totals[names[e.name]]+=Math.max(0,e.dur-child)/1000;
  const bySource=new Map();
  for(const f of forced) {
    const s=f.stack[0]||f.initiator||{};
    const key=`${s.functionName||'(anonymous)'} ${s.url||'unknown'}:${s.lineNumber??'?'}:${s.columnNumber??'?'}`;
    const a=bySource.get(key)||{source:key,count:0,ms:0};a.count++;a.ms+=f.ms;bySource.set(key,a);
  }
  return {windowStartUs:start,windowEndUs:end,windowMs:(end-start)/1000,mainThreadMs:totals,
    forcedLayouts:forced.filter(f=>f.kind==='Layout').length,
    forcedStyleRecalcs:forced.filter(f=>f.kind==='UpdateLayoutTree').length,
    forcedSources:[...bySource.values()].sort((a,b)=>b.ms-a.ms),forcedEvents:forced};
}
export function analyseProfile(profile,{startTime=-Infinity,endTime=Infinity}={}) {
  const nodes=new Map(profile.nodes.map(n=>[n.id,n])), totals=new Map();
  const origin=profile.startTime||0;
  let sampleTime=origin;
  // V8 samples can arrive out of timestamp order; negative deltas describe
  // those backsteps, not negative self time. Reconstruct chronology first.
  const samples=profile.samples.map((id,i)=>({id,time:sampleTime+=profile.timeDeltas?.[i]??1000}));
  samples.sort((a,b)=>a.time-b.time);
  let previous=origin;
  for(const sample of samples) {
    const until=Math.max(origin,Math.min(profile.endTime??Infinity,sample.time));
    const overlap=Math.min(endTime,until)-Math.max(startTime,previous);
    previous=until;
    if(overlap<=0)continue;
    const n=nodes.get(sample.id),f=n.callFrame;
    const key=`${f.functionName||'(anonymous)'} ${f.url}:${f.lineNumber+1}:${f.columnNumber+1}`;
    const row=totals.get(key)||{source:key,selfMs:0,samples:0};
    row.selfMs+=overlap/1000;row.samples++;totals.set(key,row);
  }
  return [...totals.values()].sort((a,b)=>b.selfMs-a.selfMs);
}
if(process.argv[1]?.endsWith('/trace.mjs')) {
  const body=await fs.readFile(process.argv[2]);
  const trace=JSON.parse(process.argv[2].endsWith('.gz')?await unzip(body):body);
  console.log(JSON.stringify(analyseTrace(trace,process.argv[3]||'S1'),null,2));
}
