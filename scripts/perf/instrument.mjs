// Read only after the scenario trace ends: correctness geometry must not enter
// the measured motion/release windows. Keep this function self-contained for evaluate().
export function collectChartsFinal() {
  const grid = document.querySelector('#board-grid');
  const selectors = {chart:'.rc-plot', pulse:'.rc-plot', timeline:'.rc-tl',
    heatmap:'.rc-heat-host', breakdown:'.rc-tree,.rc-donut,.rc-ranked', stat:'.rc-spark'};
  const model = window.RouterBoards.layout.boards[0].widgets;
  const missing = [];
  for (const w of model) {
    if (!selectors[w.type] || w.type === 'stat' && !w.options.spark) continue;
    const frame = [...grid.children].find(el => el.dataset.wid === w.id);
    if (!frame?.querySelector(selectors[w.type])) missing.push({widget:w.id,type:w.type});
  }
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {width:r.width,height:r.height};
  };
  const content = el => {
    const s = getComputedStyle(el), r = rect(el), n = key => parseFloat(s[key]) || 0;
    return {width:r.width-n('borderLeftWidth')-n('borderRightWidth')-n('paddingLeft')-n('paddingRight'),
      height:r.height-n('borderTopWidth')-n('borderBottomWidth')-n('paddingTop')-n('paddingBottom')};
  };
  const visible = el => !!el && el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const hosts = [...grid.querySelectorAll('.rc-plot,.rc-tl,.rc-heat-host,.rc-tree,.rc-donut,.rc-ranked,.rc-spark')]
    .filter(el => el.closest('.w')?.parentElement === grid).map(host => {
      const frame = host.closest('.w'), fixed = !!frame.dataset.height && frame.dataset.height !== 'auto';
      const type = ['plot','tl','heat-host','tree','donut','ranked','spark'].find(t => host.classList.contains('rc-'+t));
      const box = content(host), checks = [];
      const compare = (name,actual,expected) => checks.push({name,actual,expected});
      let marks = [], svgDetails = null;
      if (['plot','tl','spark','donut'].includes(type)) {
        const svg = host.querySelector(':scope > svg');
        if (svg) {
          const drawn = rect(svg), vb = svg.viewBox.baseVal;
          svgDetails = {rendered:drawn,width:svg.getAttribute('width'),height:svg.getAttribute('height'),
            viewBox:{x:vb.x,y:vb.y,width:vb.width,height:vb.height}};
          // Pixel charts have three independently observable sizes. Checking only
          // CSS width would let an old viewBox stretched to 100% pass.
          compare('rendered.width',drawn.width,box.width);
          if (type === 'donut') {
            // donut() uses a 132-square coordinate system; CSS scales its dial.
            compare('rendered.height',drawn.height,box.height);
            compare('viewBox.width',vb.width,132);compare('viewBox.height',vb.height,132);
          } else {
            compare('attribute.width',Number(svg.getAttribute('width')),box.width);
            compare('viewBox.width',vb.width,box.width);
            // Even Auto plots can stretch with their grid-row neighbours: size()
            // fills their current box with a cfg.height minimum (40px when fixed).
            // Timeline lanes and natural sparks have content-driven heights.
            // A fixed spark subtracts top padding and caps ink height at 120px.
            if (type === 'plot' || fixed && type === 'spark') {
              const h = type === 'spark' ? Math.min(120,Math.floor(box.height)) :
                Math.max(fixed ? 40 : host.__rct?.cfg.height || 120,box.height);
              compare('rendered.height',drawn.height,h);
              compare('attribute.height',Number(svg.getAttribute('height')),h);
              compare('viewBox.height',vb.height,h);
            }
          }
          const selector = type === 'plot' ? 'path:not(.ghost-line):not(.sep),[data-k]' :
            type === 'tl' ? 'circle,rect:not(defs rect),path:not(defs path)' : 'path,circle';
          marks = [...svg.querySelectorAll(selector)].filter(el => {
            const b = el.getBBox(), s = getComputedStyle(el);
            return !el.closest('defs') && (b.width > 0 || b.height > 0) && s.display !== 'none' &&
              s.visibility !== 'hidden' && Number(s.opacity) > 0 && (s.fill !== 'none' || s.stroke !== 'none');
          });
          if (!visible(svg) || drawn.width <= 0 || drawn.height <= 0) marks = [];
        }
      } else if (type === 'heat-host') {
        // Heatmaps are CSS grids, not SVG; rows can be capped/centred vertically.
        const heat = host.querySelector('.rc-heat-grid,.rc-cal-grid');
        if (heat) {
          const maxWidth = parseFloat(getComputedStyle(heat).maxWidth);
          compare('grid.width',rect(heat).width,Math.min(box.width,maxWidth || Infinity));
          marks = [...heat.querySelectorAll('.cells i')].filter(el => visible(el) && rect(el).width > 0 && rect(el).height > 0);
        }
      } else if (type === 'tree') {
        // squarify() places HTML tiles with a 1px inset on each edge.
        marks = [...host.children].filter(el => visible(el) && rect(el).width > 0 && rect(el).height > 0);
        const edge = axis => Math.max(0,...marks.map(el => parseFloat(el.style[axis === 'width' ? 'left' : 'top']) + parseFloat(el.style[axis]))) + 1;
        compare('tiles.width',edge('width'),box.width);
        compare('tiles.height',edge('height'),Math.max(fixed ? 40 : 180,box.height));
      } else if (type === 'ranked') {
        // Ranked bars are a CSS grid, with natural-height rows and no SVG.
        marks = [...host.querySelectorAll('.bar')].filter(el => visible(el) && rect(el).width > 0 && rect(el).height > 0);
        const cells = [...host.querySelectorAll('.n,.trk,b')].map(el => el.getBoundingClientRect());
        if (cells.length) compare('rows.width',Math.max(...cells.map(r => r.right))-Math.min(...cells.map(r => r.left)),box.width);
      }
      return {widget:frame.dataset.wid,type,fixed,contentBox:box,svg:svgDetails,
        nonEmpty:visible(host) && box.width > 0 && box.height > 0 && marks.length > 0,marks:marks.length,checks};
    });
  return {hosts,missing};
}

// Pure assessment is also used by the browser-free harness regression checks.
export function chartsFinalGate({hosts,missing}, tolerancePx = 2) {
  const details = hosts.map(host => {
    const checks = host.checks.map(check => ({...check,delta:Math.abs(check.actual-check.expected),
      pass:Number.isFinite(check.actual) && Number.isFinite(check.expected) && Math.abs(check.actual-check.expected) <= tolerancePx}));
    return {...host,checks,pass:host.nonEmpty && checks.length > 0 && checks.every(c => c.pass)};
  });
  return {pass:missing.length === 0 && details.length > 0 && details.every(h => h.pass),tolerancePx,
    hostCount:details.length,worstDeltaPx:Math.max(0,...details.flatMap(h => h.checks.map(c => c.delta))),missing,hosts:details};
}

// Installed before dashboard scripts. Instrumentation is identical in both arms.
export function instrument({epoch}) {
  const NativeDate = Date, origin = performance.now();
  class FixtureDate extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [FixtureDate.now()])); }
    static now() { return epoch * 1000 + performance.now() - origin; }
  }
  window.Date = FixtureDate;
  localStorage.setItem('router-theme','light');
  const p = window.__boardPerf = {active: null, events: [], frames: [], longTasks: [], animations: [],
    dirtyAt: 0, running: 0, settled: false, readyAt: null, phase: '', phaseStart: 0,pendingFetches:0};
  p.begin = name => {
    p.active = name; p.events = []; p.frames = []; p.longTasks = []; p.animations = [];
    p.phaseStart = performance.now(); p.previousFrame = null; p.dragFrames=[]; p.phase=''; p.settled=false;
    p.dropAt=null;p.liftAt=null;p.motionAt=null;p.resizeAnimations=[];
    p.dirtyAt=performance.now();
    performance.mark(`perf:${name}:start`);
  };
  p.end = () => {
    performance.mark(`perf:${p.active}:end`);
    if(p.active==='S1')performance.mark('perf:S1-drop:end');
    if(p.active?.startsWith('S5'))performance.mark(`perf:${p.active}-release:end`);
    const result = {name:p.active, elapsedMs:performance.now()-p.phaseStart,
      frames:p.frames, longTasks:p.longTasks, events:p.events, animations:p.animations,
      readyAt:p.readyAt, dragFrames:p.dragFrames, dropAt:p.dropAt,liftAt:p.liftAt,startAt:p.phaseStart,motionAt:p.motionAt,resizeAnimations:p.resizeAnimations,resizeTarget:p.resizeTarget};
    p.active = null; return result;
  };
  new PerformanceObserver(list => {
    if (p.active) for (const e of list.getEntries()) if(e.startTime >= p.phaseStart && e.duration>50)
      p.longTasks.push({start:e.startTime, duration:e.duration});
  }).observe({type:'longtask', buffered:true});
  document.addEventListener('pointerup',()=>{if(p.active?.startsWith('S5')){
    p.dropAt=performance.now();p.phase='release';
    performance.mark(`perf:${p.active}-motion:end`);performance.mark(`perf:${p.active}-release:start`);
  }else if(p.active==='S1'){
    p.dropAt=performance.now();p.phase='drop';
    performance.mark('perf:S1-motion:end');performance.mark('perf:S1-drop:start');
  }},true);
  const fetch=window.fetch;
  window.fetch=function(...args) {
    p.pendingFetches++;
    const done=()=>{p.pendingFetches--;p.dirtyAt=performance.now();};
    return fetch.apply(this,args).then(response=>{done();return response;},error=>{done();throw error;});
  };
  const add = EventTarget.prototype.addEventListener, remove = EventTarget.prototype.removeEventListener;
  const wrapped = new WeakMap();
  EventTarget.prototype.addEventListener = function(type, callback, options) {
    if(type !== 'pointermove' || !callback) return add.call(this,type,callback,options);
    let fn = wrapped.get(callback);
    if(!fn) {
      const source = new Error().stack;
      fn = function(...args) {
        if(!p.active) return typeof callback === 'function' ? callback.apply(this,args) : callback.handleEvent(...args);
        const start = performance.now();
        try { return typeof callback === 'function' ? callback.apply(this,args) : callback.handleEvent(...args); }
        finally { p.events.push({kind:'pointermove', source, start, duration:performance.now()-start}); }
      };
      wrapped.set(callback,fn);
    }
    return add.call(this,type,fn,options);
  };
  EventTarget.prototype.removeEventListener = function(type, callback, options) {
    return remove.call(this,type,type === 'pointermove' ? wrapped.get(callback) || callback : callback, options);
  };
  const RO = ResizeObserver;
  window.ResizeObserver = class extends RO {
    constructor(callback) {
      const source = new Error().stack;
      super((...args) => {
        if(!p.active) return callback(...args);
        const start=performance.now();
        try { return callback(...args); }
        finally { p.events.push({kind:'resizeObserver',source,start,duration:performance.now()-start,
          widgets:[...new Set(args[0].map(e=>e.target.closest?.('[data-wid]')?.dataset.wid).filter(Boolean))]}); }
      });
    }
  };
  const interval = window.setInterval;
  window.setInterval = function(callback, ms, ...args) {
    if(typeof callback !== 'function') return interval(callback,ms,...args);
    const source = new Error().stack;
    return interval(function(...values) {
      const poll=ms===15000 && source.includes('/app.js') && p.armPoll;
      if(poll) {
        p.armPoll=false;p.begin('S3');p.pollStarted=true;p.pollFinished=false;
      }
      if(!p.active) return callback(...values);
      const start=performance.now();
      try {
        const result=callback(...values);
        if(poll)Promise.resolve(result).then(()=>{p.pollFinished=true;p.dirtyAt=performance.now();},
          error=>{p.pollError=String(error);p.pollFinished=true;p.dirtyAt=performance.now();});
        return result;
      }
      finally { p.events.push({kind:'timer',source,intervalMs:ms,start,duration:performance.now()-start}); }
    },ms,...args);
  };
  const animate = Element.prototype.animate;
  const liveAnimations=new Set();
  Element.prototype.animate = function(frames, options) {
    const animation = animate.call(this,frames,options);
    if(options?.id==='board-reorder') {
      liveAnimations.add(animation);
      animation.finished.then(()=>liveAnimations.delete(animation),()=>liveAnimations.delete(animation));
    }
    if(options?.id === 'board-reorder' && p.active) {
      const event = {widget:this.dataset.wid, start:performance.now(), duration:options.duration, observedRunning:false};
      p.animations.push(event);
      const sample=()=>{
        event.observedRunning = animation.playState === 'running' && animation.currentTime > 0 &&
          animation.effect.getComputedTiming().progress > 0;
        if(!event.observedRunning && ['running','pending'].includes(animation.playState))requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    }
    return animation;
  };
  // Successful measureNatural probes and packing entry, without replacing app functions.
  // --cols is written once by applyPacking; cloneNode(true) on a live frame is
  // the probe path. Early-return measureNatural calls are diagnostic coverage only.
  const clone=Node.prototype.cloneNode;
  Node.prototype.cloneNode=function(deep) {
    if(p.active && deep && this.matches?.('#board-grid > .w'))
      p.events.push({kind:'naturalProbe',widget:this.dataset.wid,start:performance.now(),duration:0});
    return clone.call(this,deep);
  };
  const setProperty=CSSStyleDeclaration.prototype.setProperty;
  CSSStyleDeclaration.prototype.setProperty=function(name,...args) {
    if(p.active && name==='--cols')p.events.push({kind:'applyPacking',start:performance.now(),duration:0});
    return setProperty.call(this,name,...args);
  };
  const raf=window.requestAnimationFrame;
  window.requestAnimationFrame=function(callback) {
    // No geometry getters or Error().stack in the high-frequency rAF path.
    const text=Function.prototype.toString.call(callback);
    const kind=text.includes('updateResize(r)')?'resizeRAF':null;
    if(!kind)return raf.call(this,callback);
    return raf.call(this,function(now){
      const start=performance.now();
      try{return callback(now);}finally{if(p.active)p.events.push({kind,start,duration:performance.now()-start});}
    });
  };
  // Count each SVG insertion record, not unique widgets per observer batch.
  // Retain widget IDs to distinguish the resized host from reflowing neighbours.
  document.addEventListener('DOMContentLoaded', () => {
    const grid=document.querySelector('#board-grid');
    new MutationObserver(records => {
      p.dirtyAt=performance.now();
      if(p.active==='S1' && p.liftAt==null && document.body.classList.contains('reordering'))p.liftAt=performance.now();
      if(p.active) {
        for(const r of records) if(r.type==='childList' && [...r.addedNodes].some(n=>n.nodeName?.toLowerCase()==='svg')) {
          const frame=r.target.closest?.('.w-chart,.w-pulse,.w-timeline,.w-heatmap,.w-breakdown');
          if(frame && !frame.classList.contains('natural-probe'))p.events.push({kind:'chartSVG',widget:frame.dataset.wid,start:performance.now(),duration:0});
        }
      }
    }).observe(grid,{subtree:true,childList:true,characterData:true,attributes:true});
    new RO(()=>{p.dirtyAt=performance.now();}).observe(grid);
  });
  function frame(now) {
    if(p.active && p.previousFrame != null) {
      p.frames.push(now-p.previousFrame);
      if(p.phase==='drag'||p.phase==='motion'&&p.previousFrame>=p.motionAt)p.dragFrames.push(now-p.previousFrame);
    }
    p.previousFrame=now;
    p.running=[...liveAnimations].filter(a=>a.playState==='running').length;
    const grid=document.querySelector('#board-grid');
    if(p.active?.startsWith('S5') && p.phase==='release')for(const el of grid?.children||[]) {
      if(!el.__resizeSettle || el.__resizeSettle.neighbour || !el.classList.contains('resize-settling'))continue;
      let event=p.resizeAnimations.find(a=>a.widget===el.dataset.wid);
      const style=[el.style.width,el.style.height,el.style.translate].join('|');
      if(!event){event={widget:el.dataset.wid,start:performance.now(),samples:0,firstStyle:style,observedRunning:false};p.resizeAnimations.push(event);}
      event.samples++;event.observedRunning ||= event.samples>1 && style!==event.firstStyle;
    }
    const full=p.readyAt!=null || grid?.querySelectorAll(':scope > .w').length===15 &&
      [...grid.querySelectorAll(':scope > .w > .w-body')].every(body=>body.__rcw?.drawn ||
        body.closest('.w-account-card') && body.querySelector('.account-card')) &&
      grid.querySelectorAll('.w-chart .rc-plot svg').length>=4 &&
      grid.querySelector('.w-heatmap svg') && grid.querySelector('.w-timeline svg') &&
      grid.querySelectorAll('.account-card').length>=4 &&
      !grid.querySelector('.widget-state.error,.rc-state.err,.widget-state.loading,.rc-state.loading');
    p.settled=!!full && !p.running && !p.pendingFetches &&
      (p.active!=='S3'||p.pollFinished) && !document.body.classList.contains('reordering') && !grid?.querySelector('.resizing,.resize-settling') && now-p.dirtyAt>150;
    if(p.settled && p.readyAt==null) p.readyAt=performance.now();
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  p.begin('S2');
}
