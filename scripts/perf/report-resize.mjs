#!/usr/bin/env node
// Postprocess a completed bench-board run; no browser or server operations.
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {distribution,sum} from './trace.mjs';
const dir=path.resolve(process.argv[2]),r=JSON.parse(await fs.readFile(path.join(dir,'results.json'),'utf8'));
const names=['S5a','S5b','S5c','S5d'],fmt=n=>n==null?'n/a':n.toFixed(3);
const scored=r.runs.filter(x=>!x.warmup),values=(name,get)=>scored.map(x=>get(x.scenarios[name]));
const cell=d=>`${fmt(d.median)} [${fmt(d.min)}, ${fmt(d.max)}]`;
const rows=r.summary.filter(x=>names.includes(x.metric.split('.')[0])).map(x=>({...x,
  stability:x.A.median==null||x.B.median==null?'not available':x.stability}));
const lines=['# Resize A/A spread','',`Source: ${path.join(dir,'results.json')}`,'',
  'All times ms. Cells are median [min,max]; range is max−min. Stable means full per-arm range ≤5% of median, or exactly zero. This is observed repeatability, not a guarantee.',
  '', '| Metric | A | B | A range | B range | Classification |','|---|---:|---:|---:|---:|---|'];
for(const x of rows)lines.push(`| ${x.metric} | ${cell(x.A)} | ${cell(x.B)} | ${fmt(x.A.spread)} | ${fmt(x.B.spread)} | ${x.stability} |`);
const diagnosis={startedAt:r.startedAt,host:r.host,browser:r.browserVersion,checkouts:r.checkouts,pass:r.pass,
  gates:{passes:r.runs.length+Number(!!r.diagnostic),checks:sum([...r.runs,r.diagnostic].filter(Boolean).map(x=>Object.keys(x.gates).length)),
    failures:[...r.runs,r.diagnostic].filter(Boolean).flatMap(x=>Object.entries(x.gates).filter(([,g])=>!g.pass).map(([name,g])=>({label:x.label,iteration:x.iteration,name,...g})))},
  pollActivity:[...r.runs,r.diagnostic].filter(Boolean).flatMap(run=>names.map(name=>{
    const s=run.scenarios[name],polls=s.raw.events.filter(e=>e.kind==='timer'&&e.intervalMs===15000&&e.source.includes('/app.js'));
    return {label:run.label,iteration:run.iteration,warmup:run.warmup,profile:run.profile,scenario:name,
      motion:polls.filter(e=>e.start>=s.raw.motionAt&&e.start<s.raw.dropAt).length,
      release:polls.filter(e=>e.start>=s.raw.dropAt).length};
  })),
  headline:Object.fromEntries(names.map(name=>[name,Object.fromEntries([
    ['motionMs',s=>s.metrics.motion.durationMs],['motionWorkMs',s=>s.metrics.motion.workMs],['p95',s=>s.metrics.frames.p95],
    ['max',s=>s.metrics.frames.max],['over33',s=>s.metrics.frameIntervalsOver33],['releaseMs',s=>s.metrics.releaseToSettledMs],
    ['targetSVG',s=>s.metrics.motion.resizedWidgetSVG],['neighbourSVG',s=>s.metrics.motion.neighbourSVG],
    ['packing',s=>s.metrics.motion.applyPacking],['probes',s=>s.metrics.motion.naturalProbes],
    ['moves',s=>s.raw.events.filter(e=>e.kind==='pointermove'&&e.source.includes('/boards.js')).length],
  ].map(([key,get])=>[key,distribution(values(name,get))]))])),diagnostics:{}};
for(const name of ['S5a','S5c']) {
  const s=r.diagnostic.scenarios[name],data=diagnosis.diagnostics[name]={metrics:s.metrics,trace:s.tracePath,cpu:s.cpuProfilePath,
    topSelfTime:s.topSelfTime.slice(0,30),motionSelfTime:s.motionSelfTime.slice(0,30),releaseSelfTime:s.releaseSelfTime.slice(0,30),
    forced:{},coverage:[]};
  for(const phase of ['motion','release']) {
    const sources=new Map();
    for(const event of s[`${phase}Timeline`].forcedEvents) {
      const frame=event.stack[0]||event.initiator||{};
      const key=`${frame.functionName||'(anonymous)'} ${frame.url||'unknown'}:${frame.lineNumber??'?'}:${frame.columnNumber??'?'}`;
      const item=sources.get(key)||{source:key,layouts:0,styles:0,ms:0};
      item[event.kind==='Layout'?'layouts':'styles']++;item.ms+=event.ms;sources.set(key,item);
    }
    data.forced[phase]=[...sources.values()].sort((a,b)=>b.ms-a.ms);
  }
  for(const script of s.coverage) {
    const file=path.basename(new URL(script.url).pathname);
    // The detached baseline has been removed; read its recorded commit object.
    // Candidate changes must not shift the diagnostic baseline's file locations.
    const checkout=fileURLToPath(new URL('../../',import.meta.url));
    const text=execFileSync('git',['-C',checkout,'show',`${r.checkouts.A.revision}:dashboard/static/${file}`],{encoding:'utf8'});
    for(const f of script.functions)if(['applyPacking','measureNatural','queueNatural','updateResize','render','resizeReadout',
      'fitAccounts','holdNeighbours','drawTime','size','tick'].includes(f.functionName)) {
      const range=f.ranges[0];data.coverage.push({function:f.functionName,file,line:text.slice(0,range.startOffset).split('\n').length,calls:range.count});
    }
  }
}
await fs.writeFile(path.join(dir,'resize-summary.md'),lines.join('\n')+'\n');
await fs.writeFile(path.join(dir,'resize-diagnosis.json'),JSON.stringify(diagnosis,null,2)+'\n');
// Fluid chart with an explicit range whisker; all 10 scored A/A runs combined.
const max=Math.max(...names.map(n=>diagnosis.headline[n].motionWorkMs.max));
const bars=names.map((n,i)=>{const d=diagnosis.headline[n].motionWorkMs,left=d.min/max*100,right=d.max/max*100;
  return `<div class="row"><b>${n}</b><div class="track"><div class="bar" style="width:${d.median/max*100}%;background:var(--chart-${i+1})"></div><span class="range" style="left:${left}%;width:${right-left}%"></span></div><span>${(d.median/1000).toFixed(2)}s</span></div>`;}).join('');
const html=`<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:var(--font-sans);color:var(--foreground)} p{color:var(--muted-foreground)}
.row{display:grid;grid-template-columns:44px minmax(0,1fr) 56px;gap:12px;align-items:center;margin:18px 0;font-size:14px}
.track{position:relative;height:26px}.bar{height:26px;border-radius:4px}.range{position:absolute;top:11px;height:3px;background:var(--foreground);border-left:2px solid var(--foreground);border-right:2px solid var(--foreground)}
small{color:var(--muted-foreground)}</style></head><body>
<p>Resize motion main-thread work · main/main · 4× CPU</p>${bars}<small>Median of 10 scored runs; whiskers show min–max. S5a width chart · S5b height chart · S5c corner chart · S5d width health with chart neighbours. Script + layout + style + paint; CPU profile excluded.</small></body></html>`;
await fs.writeFile(path.join(dir,'resize-summary.html'),html);
console.log(`Wrote resize-summary.md, resize-diagnosis.json and resize-summary.html in ${dir}`);
