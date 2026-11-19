import {sum,distribution} from './trace.mjs';
export function resizePhase(raw,start,end,target,timeline) {
  const events=raw.events.filter(e=>e.start>=start&&e.start<end);
  // Long-task entries can start before capture-phase pointerup but do almost
  // all their work in release. Clip durations; count entries overlapping each
  // phase. A crossing entry appears in both counts, never twice in duration.
  const tasks=raw.longTasks.filter(e=>e.start<end&&e.start+e.duration>start);
  const taskMs=e=>Math.min(end,e.start+e.duration)-Math.max(start,e.start);
  const svg=events.filter(e=>e.kind==='chartSVG');
  const ro=events.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/charts.js'));
  const counts=items=>Object.fromEntries([...new Set(items)].sort().map(id=>[id,items.filter(x=>x===id).length]));
  return {durationMs:timeline.windowMs,mainThreadMs:timeline.mainThreadMs,
    workMs:sum(Object.values(timeline.mainThreadMs)),forcedLayouts:timeline.forcedLayouts,
    forcedStyleRecalcs:timeline.forcedStyleRecalcs,longTasks:tasks.length,longTaskTotalMs:sum(tasks.map(taskMs)),
    chartSVGReplacements:svg.length,resizedWidgetSVG:svg.filter(e=>e.widget===target).length,
    neighbourSVG:svg.filter(e=>e.widget!==target).length,svgByWidget:counts(svg.map(e=>e.widget)),
    chartResizeCallbacks:ro.length,resizedWidgetResizeCallbacks:ro.filter(e=>e.widgets.includes(target)).length,
    neighbourResizeCallbacks:ro.filter(e=>e.widgets.some(id=>id!==target)).length,
    resizeCallbacksByWidget:counts(ro.flatMap(e=>e.widgets)),
    boardResizeCallbacks:events.filter(e=>e.kind==='resizeObserver'&&e.source.includes('/boards.js')).length,
    naturalProbes:events.filter(e=>e.kind==='naturalProbe').length,
    probesByWidget:counts(events.filter(e=>e.kind==='naturalProbe').map(e=>e.widget)),
    applyPacking:events.filter(e=>e.kind==='applyPacking').length,
    resizeRAF:distribution(events.filter(e=>e.kind==='resizeRAF').map(e=>e.duration))};
}
