#!/usr/bin/env node
// Full run: ~/.local/bin/heavy node scripts/perf/bench-board.mjs --baseline-ref main --aa
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {spawn,spawnSync,execFileSync} from 'node:child_process';
import {resizePhase} from './resize-metrics.mjs';
import {instrument,collectChartsFinal,chartsFinalGate} from './instrument.mjs';
import {distribution,sum,saveJSON,startTrace,stopTrace,analyseTrace,analyseProfile} from './trace.mjs';

const here=path.dirname(fileURLToPath(import.meta.url)), task=path.resolve(here,'../..');
const cache=path.join(os.homedir(),'.cache/agent-scratch/personal-codex-router/perf');
const options={iterations:5,warmups:1,cpu:4,'port-a':8390,'port-b':8391,candidate:task};
const flags=new Set(['aa','skip-tests','help']);
for(let i=2;i<process.argv.length;i++) {
  const key=process.argv[i].replace(/^--/,'');
  if(!['baseline','candidate','baseline-ref','iterations','warmups','cpu','port-a','port-b','output',...flags].includes(key))throw Error(`Unknown option: ${key}`);
  options[key]=flags.has(key)?true:process.argv[++i];
}
if(options.help) {
  console.log('bench-board.mjs (--baseline DIR | --baseline-ref REF) [--candidate DIR | --aa] [--iterations 5] [--warmups 1] [--cpu 4] [--port-a 8390] [--port-b 8391] [--output CACHE_SUBDIR]');
  process.exit(0);
}
if(!!options.baseline===!!options['baseline-ref'])throw Error('Choose exactly one of --baseline or --baseline-ref');
for(const key of ['iterations','warmups','cpu','port-a','port-b']) {
  options[key]=Number(options[key]);
  if(!Number.isInteger(options[key]) || options[key]<(key==='warmups'?0:1))throw Error(`Invalid ${key}`);
}
if(![options['port-a'],options['port-b']].every(p=>p>=8390&&p<=8399) || options['port-a']===options['port-b'])throw Error('Use two distinct isolated ports in 8390–8399');
const output=path.resolve(options.output||path.join(cache,new Date().toISOString().replace(/[:.]/g,'-')));
if(!output.startsWith(cache+path.sep))throw Error(`Output must be below ${cache}`);
await fs.mkdir(output,{recursive:true});
const require=createRequire(import.meta.url);
let playwright;
for(const module of [process.env.PLAYWRIGHT_MODULE,'playwright',path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')].filter(Boolean)) {
  try { playwright=require(module);break; } catch(error) { if(error.code!=='MODULE_NOT_FOUND')throw error; }
}
if(!playwright)throw Error('Set PLAYWRIGHT_MODULE to an installed Playwright package');
const git=(dir,args)=>execFileSync('git',['-C',dir,...args],{encoding:'utf8'}).trim();
const servers=[];let temporary,browser,closing=false;
async function cleanup() {
  if(closing)return;closing=true;
  await browser?.close().catch(()=>{});
  for(const child of servers) {
    if(child.exitCode!==null || child.signalCode!==null)continue;
    const exited=new Promise(resolve=>child.once('exit',resolve));
    child.kill('SIGTERM');
    const timer=setTimeout(()=>child.kill('SIGKILL'),3000);
    await exited;clearTimeout(timer);console.log(`Stopped synthetic PID ${child.pid}`);
  }
  if(temporary && results.temporaryCreated) {
    git(task,['worktree','remove',temporary]);git(task,['worktree','prune']);
    console.log(`Removed detached baseline ${temporary}`);
  }
}
for(const signal of ['SIGINT','SIGTERM'])process.once(signal,async()=>{await cleanup();process.exit(130);});
const results={schemaVersion:2,startedAt:new Date().toISOString(),options,output,runs:[],tests:[],diagnostic:null};
async function server(checkout,port) {
  const child=spawn('python3',['-B',path.join(here,'fake-dashboard.py'),'--fake-data','--checkout',checkout,'--port',String(port)],{cwd:checkout,stdio:['ignore','pipe','pipe']});
  servers.push(child);
  let stderr='';child.stderr.on('data',s=>{stderr+=s;});
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{reject(Error(`Synthetic server startup timeout: ${stderr}`));},10000);
    child.once('error',error=>{clearTimeout(timer);reject(error);});
    child.once('exit',code=>{clearTimeout(timer);reject(Error(`Synthetic server exited ${code}: ${stderr}`));});
    child.stdout.once('data',()=>{clearTimeout(timer);resolve();});
  });
  console.log(`Synthetic ${checkout}: http://127.0.0.1:${port}, PID ${child.pid}`);
  return `http://127.0.0.1:${port}`;
}
function tests(checkout,label,checks=[['node',['tests/test_reorder_ui.cjs']],['python3',['-B','tests/test_widgets_ui.py']],['python3',['-B','tests/test_boards_ui.py']]]) {
  for(const [cmd,args] of checks) {
    const name=args.at(-1), row={label,checkout,command:[cmd,...args],pass:false};
    const result=spawnSync(cmd,args,{cwd:checkout,encoding:'utf8',timeout:120000,stdio:'pipe'});
    row.log=String(result.stdout||'')+String(result.stderr||'');row.pass=result.status===0;
    results.tests.push(row);console.log(`${label} ${name}: ${row.pass?'PASS':'FAIL'}`);
  }
}
const order=page=>page.evaluate(()=>[...document.querySelectorAll('#board-grid > .w')].map(n=>n.dataset.wid));
const settled=page=>page.waitForFunction(()=>window.__boardPerf?.settled,{},{timeout:30000,polling:'raf'});
const begin=(page,name)=>page.evaluate(name=>window.__boardPerf.begin(name),name);
async function finish(page,cdp,name,prefix) {
  // Let the long-task observer receive the just-completed task before collecting.
  await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
  const raw=await page.evaluate(()=>window.__boardPerf.end());
  const tracePath=path.join(output,`${prefix}-${name}.trace.json.gz`);
  const trace=await stopTrace(cdp,tracePath), timeline=analyseTrace(trace,name);
  const motion=name==='S1'?analyseTrace(trace,'S1-motion'):null;
  const drop=name==='S1'?analyseTrace(trace,'S1-drop'):null;
  const moves=raw.events.filter(e=>e.kind==='pointermove'&&e.source.includes('/boards.js'));
  const isResize=name.startsWith('S5');
  const frames=name==='S1'||isResize?raw.dragFrames:raw.frames;
  const metrics={durationMs:raw.elapsedMs,frames:distribution(frames),
    frameIntervalsOver16_7:frames.filter(n=>n>16.7).length,frameIntervalsOver33:frames.filter(n=>n>33).length,
    longTasks:raw.longTasks.length,longTaskTotalMs:sum(raw.longTasks.map(e=>e.duration)),
    mainThreadMs:timeline.mainThreadMs,forcedLayouts:timeline.forcedLayouts,
    ...(motion?{dragMainThreadMs:motion.mainThreadMs,dragForcedLayouts:motion.forcedLayouts,
      dragForcedStyleRecalcs:motion.forcedStyleRecalcs,dragWorkMs:sum(Object.values(motion.mainThreadMs)),
      dragDurationMs:motion.windowMs,dropMainThreadMs:drop.mainThreadMs}:{}),
    forcedStyleRecalcs:timeline.forcedStyleRecalcs,pointermoveMs:distribution(moves.map(e=>e.duration)),
    pointermoveTotalMs:sum(moves.map(e=>e.duration)),
    ...(name==='S2'?{firstFullBoardMs:raw.readyAt}:{}),
    chartSVGReplacements:raw.events.filter(e=>e.kind==='chartSVG').length,
    chartResizeCallbacks:raw.events.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/charts.js')).length,
    boardResizeCallbacks:raw.events.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/boards.js')).length,
    widgetTimerCallbacks:raw.events.filter(e=>e.kind==='timer'&&e.source.includes('/widgets.js')).length};
  if(name==='S1') {
    const moving=raw.events.filter(e=>e.start>=raw.liftAt&&e.start<raw.dropAt);
    metrics.duringDrag={chartSVGReplacements:moving.filter(e=>e.kind==='chartSVG').length,
      chartResizeCallbacks:moving.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/charts.js')).length,
      boardResizeCallbacks:moving.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/boards.js')).length,
      widgetTimerCallbacks:moving.filter(e=>e.kind==='timer'&&e.source.includes('/widgets.js')).length,
      longTasks:raw.longTasks.filter(e=>e.start>=raw.liftAt&&e.start<raw.dropAt).length};
  }
  let resizeTimelines;
  if(isResize) {
    const motion=analyseTrace(trace,`${name}-motion`),release=analyseTrace(trace,`${name}-release`);
    metrics.motion=resizePhase(raw,raw.motionAt,raw.dropAt,raw.resizeTarget,motion);
    metrics.release=resizePhase(raw,raw.dropAt,Infinity,raw.resizeTarget,release);
    resizeTimelines={motionTimeline:motion,releaseTimeline:release};
  }
  return {metrics,tracePath,...resizeTimelines,timeline,...(motion?{motionTimeline:motion,dropTimeline:drop}:{}),raw};
}
async function run(label,url,iteration,{warmup=false,profile=false}={}) {
  const prefix=`${profile?'diagnostic':warmup?'warmup':'run'}-${iteration}-${label}`;
  const context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1,
    timezoneId:'UTC',locale:'en-US',reducedMotion:'no-preference',colorScheme:'light'});
  // The fixture page cannot accidentally contact a gateway, production dashboard,
  // preview proxy or the internet, including links/images introduced by a candidate.
  await context.route('**/*',route=>route.request().url().startsWith(url+'/')?route.continue():route.abort('blockedbyclient'));
  const page=await context.newPage(), errors=[],badResponses=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  page.on('response',r=>{if(r.status()>=400)badResponses.push({url:r.url(),status:r.status()});});
  const cdp=await context.newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate',{rate:options.cpu});
  await page.addInitScript(instrument,{epoch:1793620800});
  const reset=await context.request.post(url+'/__perf/reset',{headers:{'X-Router-Request':'1'},data:{}});
  if(!reset.ok())throw Error(`Fixture reset failed ${reset.status()}`);
  const row={label,iteration,warmup,profile,scenarios:{},gates:{}};
  try {
    await startTrace(cdp);
    await page.goto(url+'/',{waitUntil:'domcontentloaded'});
    await settled(page);
    row.scenarios.S2=await finish(page,cdp,'S2',prefix);

    await startTrace(cdp);await begin(page,'S4');
    await page.locator('#customize-board').click();
    await page.waitForFunction(()=>window.RouterBoards.customizing);
    await settled(page);
    await page.locator('[data-board-action="cancel"]').click();
    await page.waitForFunction(()=>!window.RouterBoards.customizing);
    await settled(page);
    row.scenarios.S4=await finish(page,cdp,'S4',prefix);

    // Wait for the real 15s poll: its wrapper marks S3 just before load(), so
    // idle waiting is excluded and the widget API cache really has expired.
    await startTrace(cdp);
    await page.evaluate(()=>{const p=window.__boardPerf;p.armPoll=true;p.pollFinished=false;p.pollBefore=current.generated_at;});
    await page.waitForFunction(()=>window.__boardPerf.pollStarted,{},{timeout:20000,polling:'raf'});
    await settled(page);
    row.scenarios.S3=await finish(page,cdp,'S3',prefix);
    row.gates.pollRefresh=await page.evaluate(()=>({pass:__boardPerf.pollFinished && !__boardPerf.pollError &&
      current.generated_at!==__boardPerf.pollBefore,before:__boardPerf.pollBefore,after:current.generated_at,
      pendingFetches:__boardPerf.pendingFetches,error:__boardPerf.pollError||null}));

    await page.locator('#customize-board').click();await settled(page);
    const initial=await order(page);
    const expected=[initial[1],initial[2],initial[0],...initial.slice(3)];
    const source=await page.locator(`[data-wid="${initial[0]}"] .grip`).boundingBox();
    const destination=await page.locator(`[data-wid="${initial[2]}"] .grip`).boundingBox();
    const x=source.x+source.width/2,y=source.y+source.height/2,dx=destination.x-source.x;
    await page.mouse.move(x,y);
    await startTrace(cdp);
    if(profile){await cdp.send('Profiler.enable');await cdp.send('Profiler.setSamplingInterval',{interval:1000});await cdp.send('Profiler.start');}
    await begin(page,'S1');
    await page.mouse.down();await page.waitForTimeout(400);
    await page.evaluate(()=>{window.__boardPerf.phase='drag';performance.mark('perf:S1-motion:start');});
    // 120 points over 2 seconds; a monotonic sweep crosses two neighbours. Mouse
    // press waits 400ms, then the real >4px movement threshold lifts the widget.
    const sweepStart=performance.now();
    for(let i=1;i<=120;i++) {
      await page.mouse.move(x+dx*i/120,y+Math.sin(i/120*Math.PI)*14);
      const remaining=sweepStart+i*1000/60-performance.now();
      if(remaining>0)await page.waitForTimeout(remaining);
    }
    await page.mouse.up();await settled(page);
    const settledAt=await page.evaluate(()=>performance.now());
    row.scenarios.S1=await finish(page,cdp,'S1',prefix);
    row.scenarios.S1.metrics.dropToSettledMs=settledAt-row.scenarios.S1.raw.dropAt;
    if(profile) {
      const {profile:cpu}=await cdp.send('Profiler.stop');
      const profilePath=path.join(output,`${prefix}-S1.cpuprofile`);
      await saveJSON(profilePath,cpu);row.cpuProfilePath=profilePath;row.topSelfTime=analyseProfile(cpu);
    }
    const actual=await order(page), model=await page.evaluate(()=>window.RouterBoards.layout.boards[0].widgets.map(w=>w.id));
    row.gates.order={pass:JSON.stringify(actual)===JSON.stringify(expected)&&JSON.stringify(model)===JSON.stringify(expected),expected,actual,model};
    row.gates.animations={pass:row.scenarios.S1.raw.animations.some(a=>a.observedRunning),
      started:row.scenarios.S1.raw.animations.length,observedRunning:row.scenarios.S1.raw.animations.filter(a=>a.observedRunning).length};
    await page.locator('[data-board-action="done"]').click();
    await page.waitForFunction(()=>!window.RouterBoards.customizing);
    const saved=await context.request.get(url+'/api/overview',{headers:{'X-Router-Request':'1'}});
    const persisted=(await saved.json()).layout.boards[0].widgets.map(w=>w.id);
    row.gates.persistedOrder={pass:JSON.stringify(persisted)===JSON.stringify(expected),actual:persisted};
    await resizeScenarios({page,context,cdp,url,row,prefix,profile});
    row.gates.console={pass:!errors.length,errors};row.gates.http={pass:!badResponses.length,responses:badResponses};
    row.gates.boardReady={pass:row.scenarios.S2.metrics.firstFullBoardMs>0};
    console.log(`${prefix}: S1 p95 ${row.scenarios.S1.metrics.frames.p95?.toFixed(1)}ms, >33ms ${row.scenarios.S1.metrics.frameIntervalsOver33}, pointer ${row.scenarios.S1.metrics.pointermoveTotalMs.toFixed(1)}ms; gates ${Object.values(row.gates).every(g=>g.pass)?'PASS':'FAIL'}`);
    await saveJSON(path.join(output,`${prefix}.json`),row);
    return row;
  } catch(error) {
    console.error('Page errors:',errors,'HTTP errors:',badResponses);
    console.error(await page.evaluate(()=>({widgets:[...document.querySelectorAll('#board-grid > .w')].map(w=>({type:w.className,svgs:w.querySelectorAll('svg').length,cards:w.querySelectorAll('.account-card').length,state:w.querySelector('.wstate,.rc-state')?.textContent})),notice:document.querySelector('#notice')?.textContent,animations:document.getAnimations().map(a=>({id:a.id,name:a.animationName,iterations:a.effect?.getTiming().iterations,target:a.effect?.target?.className})),perf:window.__boardPerf&&{ready:__boardPerf.readyAt,running:__boardPerf.running,dirtyAt:__boardPerf.dirtyAt,now:performance.now()}})).catch(()=>({})));
    throw error;
  } finally {await context.close();}
}
async function resizeScenarios({page,context,cdp,url,row,prefix,profile}) {
  for(const [name,kind,target] of [['S5a','width','perf-00'],['S5b','height','perf-00'],
    ['S5c','corner','perf-00'],['S5d','width','perf-07']]) {
    // Fresh fixture/model for every resize; setup/render/Customize are unmeasured.
    const reset=await context.request.post(url+'/__perf/reset',{headers:{'X-Router-Request':'1'},
      data:target==='perf-07'?{arrangement:'resize-non-chart'}:{}});
    if(!reset.ok())throw Error(`Resize fixture reset failed ${reset.status()}`);
    await page.reload({waitUntil:'domcontentloaded'});await settled(page);
    await page.locator('#customize-board').click();await settled(page);
    const frame=page.locator(`#board-grid > [data-wid="${target}"]`);
    const setup=await frame.evaluate(el=>{const r=el.getBoundingClientRect(),gr=el.parentElement.getBoundingClientRect();
      const w=RouterBoards.layout.boards[0].widgets.find(w=>w.id===el.dataset.wid);
      return {span:w.span,height:w.height,rows:Math.round((r.height+34)/57),step:(gr.width+20)/12,rect:{width:r.width,height:r.height},
        neighbours:[...el.parentElement.querySelectorAll(':scope > .w-chart')].map(n=>n.dataset.wid)};});
    const grip=await frame.locator(kind==='width'?'.resize-grip':kind==='height'?'.height-grip':'.corner-grip').boundingBox();
    if(!grip)throw Error(`Missing ${name} grip`);
    const x=grip.x+grip.width/2,y=grip.y+grip.height/2;
    // Final fractional overshoot exercises a visible snap to the expected integer.
    // Width: 4→8→12→8→4→6. Height: natural→+3→+5→+1→+2.
    const widthPath=[0,4,8,4,0,2.22],heightPath=[0,2,4,1,0,2.22];
    const expected={span:kind==='height'?setup.span:6,height:kind==='width'?setup.height:setup.rows+2};
    await page.mouse.move(x,y);await startTrace(cdp);
    const diagnostic=profile&&['S5a','S5c'].includes(name);
    if(diagnostic){await cdp.send('Profiler.enable');await cdp.send('Profiler.setSamplingInterval',{interval:1000});
      await cdp.send('Profiler.startPreciseCoverage',{callCount:true,detailed:false});await cdp.send('Profiler.start');}
    await begin(page,name);
    await page.evaluate(target=>{__boardPerf.resizeTarget=target;},target);
    await page.mouse.down();
    const started=await frame.evaluate(el=>el.classList.contains('resizing'));
    await page.evaluate(name=>{const p=__boardPerf;p.phase='motion';p.motionAt=performance.now();p.previousFrame=null;
      performance.mark(`perf:${name}-motion:start`);},name);
    // 240 absolute-cadence points, 60/sec, five linear segments over 4 seconds.
    const sweepStart=performance.now();
    for(let i=1;i<=240;i++) {
      const segment=Math.min(4,Math.floor((i-1)/48)),t=(i-segment*48)/48;
      const at=path=>path[segment]+(path[segment+1]-path[segment])*t;
      await page.mouse.move(x+(kind==='height'?0:at(widthPath)*setup.step),y+(kind==='width'?0:at(heightPath)*57));
      const remaining=sweepStart+i*1000/60-performance.now();
      if(remaining>0)await page.waitForTimeout(remaining);
    }
    await page.mouse.up();await settled(page);
    const settledAt=await page.evaluate(()=>performance.now());
    const result=row.scenarios[name]=await finish(page,cdp,name,prefix);
    result.setup=setup;result.metrics.releaseToSettledMs=settledAt-result.raw.dropAt;
    if(diagnostic){const {profile:cpu}=await cdp.send('Profiler.stop');
      result.cpuProfilePath=path.join(output,`${prefix}-${name}.cpuprofile`);await saveJSON(result.cpuProfilePath,cpu);
      result.topSelfTime=analyseProfile(cpu);
      result.motionSelfTime=analyseProfile(cpu,{startTime:result.motionTimeline.windowStartUs,endTime:result.motionTimeline.windowEndUs});
      result.releaseSelfTime=analyseProfile(cpu,{startTime:result.releaseTimeline.windowStartUs,endTime:result.releaseTimeline.windowEndUs});
      result.coverage=(await cdp.send('Profiler.takePreciseCoverage')).result
        .filter(s=>s.url.endsWith('/boards.js')||s.url.endsWith('/charts.js'));
      await cdp.send('Profiler.stopPreciseCoverage');}
    const actual=await frame.evaluate(el=>{const w=RouterBoards.layout.boards[0].widgets.find(w=>w.id===el.dataset.wid);
      return {span:w.span,height:w.height,domSpan:Number(el.style.getPropertyValue('--span')),domHeight:el.dataset.height,
        resizing:el.classList.contains('resizing'),settling:!!el.__resizeSettle};});
    row.gates[`${name}Size`]={pass:started&&actual.span===expected.span&&actual.height===expected.height&&
      actual.domSpan===expected.span&&actual.domHeight===String(expected.height)&&!actual.resizing&&!actual.settling,started,expected,actual};
    row.gates[`${name}Animation`]={pass:result.raw.resizeAnimations.some(a=>a.widget===target&&a.observedRunning),
      animations:result.raw.resizeAnimations};
    row.gates[`${name}ChartsFinal`]=chartsFinalGate(await page.evaluate(collectChartsFinal));
    await page.locator('[data-board-action="done"]').click();await page.waitForFunction(()=>!RouterBoards.customizing);await settled(page);
    const saved=await context.request.get(url+'/api/overview',{headers:{'X-Router-Request':'1'}});
    const persisted=(await saved.json()).layout.boards[0].widgets.find(w=>w.id===target);
    row.gates[`${name}Persisted`]={pass:persisted.span===expected.span&&(persisted.height??'auto')===expected.height,
      expected,actual:{span:persisted.span,height:persisted.height??'auto'}};
    await saveJSON(path.join(output,`${prefix}-${name}.json`),{...result,gates:Object.fromEntries(Object.entries(row.gates).filter(([k])=>k.startsWith(name)))});
    console.log(`${prefix} ${name}: motion ${result.metrics.motion.durationMs.toFixed(1)}ms, work ${result.metrics.motion.workMs.toFixed(1)}ms, `+
      `SVG ${result.metrics.motion.chartSVGReplacements}, probes ${result.metrics.motion.naturalProbes}, release ${result.metrics.releaseToSettledMs.toFixed(1)}ms; `+
      `ChartsFinal ${row.gates[name+'ChartsFinal'].pass?'PASS':'FAIL'} (${row.gates[name+'ChartsFinal'].hostCount} hosts, worst Δ ${row.gates[name+'ChartsFinal'].worstDeltaPx.toFixed(2)}px); `+
      `gates ${['Size','Animation','ChartsFinal','Persisted'].every(k=>row.gates[name+k].pass)?'PASS':'FAIL'}`);
  }
}
const metricPaths=['S1.frames.median','S1.frames.p95','S1.frames.max','S1.frameIntervalsOver16_7','S1.frameIntervalsOver33',
  'S1.longTasks','S1.longTaskTotalMs','S1.mainThreadMs.script','S1.mainThreadMs.layout','S1.mainThreadMs.recalcStyle',
  'S1.mainThreadMs.paint','S1.forcedLayouts','S1.pointermoveTotalMs','S1.dropToSettledMs',
  'S1.dragWorkMs','S1.dragMainThreadMs.script','S1.dragMainThreadMs.layout','S1.dragMainThreadMs.recalcStyle',
  'S1.dragMainThreadMs.paint','S1.dragForcedLayouts',
  'S2.firstFullBoardMs','S3.durationMs','S4.durationMs'];
for(const name of ['S5a','S5b','S5c','S5d']) {
  for(const key of ['frames.median','frames.p95','frames.max','frameIntervalsOver16_7','frameIntervalsOver33','releaseToSettledMs'])metricPaths.push(`${name}.${key}`);
  for(const phase of ['motion','release'])for(const key of ['durationMs','workMs','longTasks','longTaskTotalMs',
    'mainThreadMs.script','mainThreadMs.layout','mainThreadMs.recalcStyle','mainThreadMs.paint','forcedLayouts','forcedStyleRecalcs',
    'chartSVGReplacements','resizedWidgetSVG','neighbourSVG','chartResizeCallbacks','resizedWidgetResizeCallbacks','neighbourResizeCallbacks',
    'boardResizeCallbacks','naturalProbes','applyPacking','resizeRAF.count','resizeRAF.p95'])metricPaths.push(`${name}.${phase}.${key}`);
}
function summarise() {
  const rows=[];
  for(const key of metricPaths) {
    const [scenario,...parts]=key.split('.');
    const values=label=>results.runs.filter(r=>r.label===label&&!r.warmup).map(r=>parts.reduce((o,k)=>o?.[k],r.scenarios[scenario].metrics));
    const a=distribution(values('A')),b=distribution(values('B'));
    rows.push({metric:key,A:a,B:b,delta:b.median-a.median,deltaPercent:a.median?(b.median-a.median)/a.median*100:null,
      stability:[a,b].every(s=>s.spread===0)?'deterministic in observed runs':[a,b].every(s=>s.median>0&&s.spread/s.median<=.05)?'near-deterministic (range <=5%)':'variable',
      maxArmSpreadPercent:Math.max(...[a,b].map(s=>s.median?s.spread/s.median*100:0))});
  }
  const fmt=n=>n==null?'n/a':n.toFixed(2);
  const primary=rows.find(r=>r.metric==='S1.dragWorkMs');
  results.score={metric:'S1.dragWorkMs',lowerIsBetter:true,baseline:primary.A.median,candidate:primary.B.median,
    improvementPercent:primary.deltaPercent==null?null:-primary.deltaPercent,
    observedNoiseMs:Math.max(primary.A.spread,primary.B.spread),
    interpretation:Math.abs(primary.delta)<=Math.max(primary.A.spread,primary.B.spread)?'inconclusive within observed run spread':primary.delta<0?'candidate lower; confirm against saved A/A noise and secondary metrics':'candidate higher; confirm against saved A/A noise and secondary metrics'};
  const lines=['# Board benchmark',`A: ${results.checkouts.A.path} @ ${results.checkouts.A.revision}`,
    `B: ${results.checkouts.B.path} @ ${results.checkouts.B.revision}`,
    `CPU ${options.cpu}×; 1440×900; ${options.iterations} scored runs per arm; ${options.warmups} warmups per arm.`,
    'Each cell is median [min, max]. Spread is full run-to-run range, not a confidence interval.',
    `Primary score: drag main-thread work (script + layout + style + paint), lower is better. ${results.score.interpretation}.`,
    '| Metric | Baseline A | Candidate B | B − A | Delta % |', '|---|---:|---:|---:|---:|'];
  for(const r of rows)lines.push(`| ${r.metric} | ${fmt(r.A.median)} [${fmt(r.A.min)}, ${fmt(r.A.max)}] | ${fmt(r.B.median)} [${fmt(r.B.min)}, ${fmt(r.B.max)}] | ${fmt(r.delta)} | ${fmt(r.deltaPercent)}% |`);
  results.summary=rows;
  results.pass=results.tests.every(t=>t.pass)&&!options['skip-tests']&&results.runs.every(r=>Object.values(r.gates).every(g=>g.pass))&&(!results.diagnostic||Object.values(results.diagnostic.gates).every(g=>g.pass));
  lines.push('',`Correctness gates: ${results.pass?'PASS':'FAIL (or tests skipped)'}`,`Results: ${path.join(output,'results.json')}`);
  return lines.join('\n')+'\n';
}
try {
  if(options['baseline-ref']) {
    const parent=path.join(os.homedir(),'.worktrees/personal-codex-router');
    await fs.mkdir(parent,{recursive:true});
    temporary=path.join(parent,`perf-baseline-${process.pid}`);
    git(task,['worktree','add','--detach',temporary,options['baseline-ref']]);
    results.temporaryCreated=true;
    options.baseline=temporary;
  }
  const a=await fs.realpath(options.baseline),b=await fs.realpath(options.aa?a:options.candidate);
  for(const checkout of new Set([a,b]))await fs.access(path.join(checkout,'dashboard/static/boards.js'));
  results.checkouts={A:{path:a,revision:git(a,['rev-parse','HEAD']),status:git(a,['status','--short'])},B:{path:b,revision:git(b,['rev-parse','HEAD']),status:git(b,['status','--short'])}};
  if(!options['skip-tests']) {
    tests(task,'harness',[['python3',['-B','tests/test_perf_board_fixture.py']],['node',['tests/test_perf_board_trace.mjs']]]);
    for(const [label,checkout] of [['A',a],['B',b]])tests(checkout,label);
  }
  if(!options['skip-tests']&&results.tests.some(t=>!t.pass))throw Error('Focused correctness tests failed');
  const urls={A:await server(a,options['port-a']),B:await server(b,options['port-b'])};
  browser=await playwright.chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
  results.browserVersion=browser.version();results.host={platform:os.platform(),arch:os.arch(),cpus:os.cpus()[0]?.model};
  // Paired A/B/A/B schedule, with an equal number of discarded warmups.
  for(let i=-options.warmups;i<options.iterations;i++)for(const label of ['A','B'])results.runs.push(await run(label,urls[label],i+1,{warmup:i<0}));
  // Separate unscored diagnostic avoids profiler sampling contaminating A/A noise.
  results.diagnostic=await run('A',urls.A,1,{profile:true});
  const fingerprints={};
  for(const file of ['bench-board.mjs','instrument.mjs','trace.mjs','resize-metrics.mjs','fake-dashboard.py']) {
    const {createHash}=await import('node:crypto');fingerprints[file]=createHash('sha256').update(await fs.readFile(path.join(here,file))).digest('hex');
  }
  await saveJSON(path.join(output,'harness-sha256.json'),fingerprints);
  const markdown=summarise();
  await saveJSON(path.join(output,'results.json'),results);await fs.writeFile(path.join(output,'summary.md'),markdown);
  console.log(markdown);if(!results.pass)process.exitCode=1;
} catch(error) {
  results.error=String(error.stack||error);await saveJSON(path.join(output,'failed-results.json'),results);throw error;
} finally {await cleanup();}
