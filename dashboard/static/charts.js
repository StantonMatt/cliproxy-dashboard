'use strict';
/* Chart primitives for the dashboard's boards (window.RouterCharts): plain SVG, no libraries. Colours are style.css custom
   properties (family tokens, --blue, --mint, --danger…), so both themes come for free and a series keeps its colour when
   the theme changes. Text that comes from data (names, labels) is escaped in SVG markup and set with textContent in the
   tooltip. Every function draws into a host element it owns; drawing again replaces what it drew. */
(function () {
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nf = new Intl.NumberFormat();
const cf = new Intl.NumberFormat(undefined, {notation:'compact', maximumFractionDigits:1});
const num = v => nf.format(Math.round(v));
const isNum = v => typeof v === 'number' && Number.isFinite(v);

// Values: counts and tokens are exact below 10K and compact from there (1.2B, 24.6M); rates keep one decimal.
function fmt(v, unit) {
  if (!isNum(v)) return '—';
  if (unit === 'percent') return (v >= 99.95 && v < 100 ? '99.9' : v >= 100 ? '100' : (Math.round(v * 10) / 10).toFixed(1)) + '%';
  if (unit === 'ms') return v < 1000 ? Math.round(v) + 'ms' : (v / 1000).toFixed(1) + 's';
  return Math.abs(v) >= 1e4 ? cf.format(v) : num(v);
}
function axisFmt(v, unit, step) {
  if (unit === 'percent') return (step < 1 ? v.toFixed(1) : Math.round(v)) + '%';
  if (unit === 'ms') return (step < 1000 ? (v / 1000).toFixed(1) : Math.round(v / 1000)) + 's';
  return Math.abs(v) >= 1e4 ? cf.format(v) : num(v);
}
function niceStep(range, n) {
  const raw = range / n, p = Math.pow(10, Math.floor(Math.log10(raw || 1))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
// Clean y ticks from zero; rates zoom in (98–100%) because they live near the top.
function yScale(values, unit, height) {
  const n = height >= 120 ? 5 : 3;
  if (unit === 'percent') {
    const mn = values.length ? Math.min(...values) : 100;
    let lo = Math.max(0, Math.floor(mn - Math.max(1, (100 - mn) * .25)));
    if (lo >= 100) lo = 99;
    const step = niceStep(100 - lo, 2);
    lo = Math.max(0, Math.floor(lo / step) * step);
    const ticks = [];
    for (let v = lo; v <= 100 + 1e-9; v += step) ticks.push(+v.toFixed(6));
    return {lo, hi: 100, step, ticks};
  }
  const mx = values.length ? Math.max(0, ...values) : 0;
  let step = niceStep(mx || 1, n);
  if (unit !== 'ms') step = Math.max(1, step);
  const hi = Math.max(step, Math.ceil((mx || 1) / step - 1e-9) * step), ticks = [];
  for (let v = 0; v <= hi + 1e-9; v += step) ticks.push(v);
  return {lo: 0, hi, step, ticks};
}

// During a board resize, keep the existing chart between coarse redraws. Release/settle and all other size changes
// use the normal next-frame draw at true pixel size. Keep the latest observer box instead of forcing another layout read.
function observe(host, draw) {
  const st = host.__rcObs || (host.__rcObs = {});
  st.draw = draw;
  if (st.ro || typeof ResizeObserver !== 'function') return;
  const redraw = () => {
    st.raf = null;
    if (host.isConnected) st.draw(st.box);
  };
  st.ro = new ResizeObserver(entries => {
    const box = entries[0].contentRect, w = Math.round(box.width), h = Math.round(box.height);
    if (w === st.w && h === st.h) return;
    st.w = w; st.h = h; st.box = box;
    if (window.RouterBoards?.resizing) {
      if (st.timer == null) st.timer = setTimeout(() => {
        st.timer = null;
        cancelAnimationFrame(st.raf);
        st.raf = requestAnimationFrame(redraw);
      }, 120);
      return;
    }
    clearTimeout(st.timer); st.timer = null;
    cancelAnimationFrame(st.raf);
    st.raf = requestAnimationFrame(redraw);
  });
  st.ro.observe(host);
}
function unobserve(host) {
  const st = host?.__rcObs;
  if (st?.ro) { st.ro.disconnect(); cancelAnimationFrame(st.raf); clearTimeout(st.timer); }
  if (host) delete host.__rcObs;
}
function size(host, minH, fallbackW = 320, box) {
  const st = host.__rcObs;
  const w = box ? Math.round(box.width) : host.clientWidth, h = box ? Math.round(box.height) : host.clientHeight;
  if (st) {
    st.w = w; st.h = h;
    // A data-driven draw already used the current layout; do not replay an older queued observer box over it.
    if (!box) { cancelAnimationFrame(st.raf); clearTimeout(st.timer); st.raf = st.timer = null; st.box = null; }
  }
  return {W: Math.max(120, w || fallbackW), H: Math.max(minH || 0, h || 0)};
}
// A widget with a fixed height (its board frame has data-height="<rows>") fills that box: plots and lanes size to it instead of
// their natural height. Read at draw time, so a change of height redraws correctly even without a new render.
function fills(host) {
  const frame = host?.closest?.('[data-height]');
  return !!frame && !!frame.dataset.height && frame.dataset.height !== 'auto';
}
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

/* ---------- time charts ----------
   cfg: { kind: 'stacked'|'bars'|'grouped'|'line'|'area'|'sarea', keys: [{key,name,color}], hidden: Set, rows: [{key:value}|null],
          prev: [...]|null, labels: [{label, tip}], xTicks: [i], current: i|null, unit, height, label, pickable,
          onPick(i, key|null), prevName, pickText, axis: false (no y axis), hover: false }
   Rows that are null are buckets with nothing to draw yet (later today). */
function barPath(x, y, w, h, r, color, opacity, key) {
  const k = key ? ` data-k="${esc(key)}"` : '';
  if (r <= 0 || h < r * 2) return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${w.toFixed(2)}" height="${h.toFixed(2)}" fill="${color}" opacity="${opacity}"${k}/>`;
  return `<path d="M${x.toFixed(2)},${(y + h).toFixed(2)}V${(y + r).toFixed(2)}Q${x.toFixed(2)},${y.toFixed(2)} ${(x + r).toFixed(2)},${y.toFixed(2)}H${(x + w - r).toFixed(2)}Q${(x + w).toFixed(2)},${y.toFixed(2)} ${(x + w).toFixed(2)},${(y + r).toFixed(2)}V${(y + h).toFixed(2)}Z" fill="${color}" opacity="${opacity}"${k}/>`;
}
const BAR_KINDS = ['stacked', 'bars', 'grouped'];
function time(host, cfg) {
  const st = host.__rct || (host.__rct = {});
  st.cfg = cfg;
  host.classList.add('rc-plot');
  drawTime(host);
  observe(host, box => drawTime(host, box));
  if (!st.wired) wireTime(host);
}
function drawTime(host, box) {
  const st = host.__rct, cfg = st.cfg;
  const hadFocus = host.contains(document.activeElement) && document.activeElement?.tagName?.toLowerCase() === 'svg';
  const {W, H} = size(host, fills(host) ? 40 : cfg.height || 120, cfg.width, box);
  const hidden = cfg.hidden || new Set();
  let keys = cfg.keys.filter(k => !hidden.has(k.key));
  if (!keys.length) keys = cfg.keys.slice();
  const rows = cfg.rows, n = Math.max(1, rows.length), unit = cfg.unit, kind = cfg.kind;
  const stacked = kind === 'stacked' || kind === 'sarea' || kind === 'bars';
  const total = r => keys.reduce((s, k) => s + (isNum(r[k.key]) ? r[k.key] : 0), 0);
  const totals = rows.map(r => r ? total(r) : null);
  const prevT = cfg.prev ? cfg.prev.map(p => p ? total(p) : null) : null;
  const values = [];
  for (const [set, tot] of [[rows, totals], [cfg.prev || [], prevT || []]]) set.forEach((r, i) => {
    if (!r) return;
    if (stacked) values.push(tot[i]); else for (const k of keys) if (isNum(r[k.key])) values.push(r[k.key]);
  });
  const sc = yScale(values, unit, H);
  const axis = cfg.axis !== false;
  const labels = axis ? sc.ticks.map(v => axisFmt(v, unit, sc.step)) : [];
  const padL = axis ? Math.max(24, ...labels.map(s => s.length * 5.5 + 9)) : 0;
  const padT = 6, padB = cfg.xTicks?.length ? 18 : 2, padR = BAR_KINDS.includes(kind) ? 2 : 6;
  const pw = W - padL - padR, ph = Math.max(10, H - padT - padB), slot = pw / n, base = padT + ph;
  const y = v => base - (Math.max(sc.lo, Math.min(sc.hi, v)) - sc.lo) / ((sc.hi - sc.lo) || 1) * ph;
  const cx = i => padL + slot * (i + .5);
  let s = `<svg width="${W}" height="${H}" style="width:${W}px;height:${H}px" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.label || 'Chart')}" tabindex="${cfg.hover === false ? -1 : 0}" focusable="${cfg.hover === false ? 'false' : 'true'}">`;
  s += `<rect class="hit" x="${padL}" y="0" width="${Math.max(0, pw)}" height="${base}" fill="transparent"/>`;
  sc.ticks.forEach((v, j) => {
    const yy = Math.round(y(v)) + .5;
    if (v !== sc.lo) s += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>`;
    if (axis) s += `<text class="tk-y" x="${padL - 8}" y="${yy + 3}">${esc(labels[j])}</text>`;
  });
  s += `<line class="base" x1="${padL}" x2="${W - padR}" y1="${base + .5}" y2="${base + .5}"/>`;
  for (const t of xTickPlan(cfg.xTicks || [], cfg.labels, slot, padL, cx))
    s += `<text class="tk-x" x="${t.x.toFixed(1)}" y="${H - 3}" text-anchor="${t.anchor}">${esc(t.label)}</text>`;
  if (BAR_KINDS.includes(kind)) {
    const bw = Math.max(1.5, Math.min(24, slot * (kind === 'bars' ? .62 : .64)));
    // Grouped bars give a slot only to series with something in this range (idle accounts stay in the legend).
    const gkeys = kind === 'grouped' ? keys.filter(key => rows.some(r => r && isNum(r[key.key]) && r[key.key] > 0) || cfg.prev?.some(r => r && r[key.key] > 0)) : keys;
    const k = Math.max(1, gkeys.length), gap = 2, gw = Math.min(slot * .8, k * 10 + (k - 1) * gap), w = Math.max(1, (gw - (k - 1) * gap) / k);
    rows.forEach((r, i) => {
      if (!r) return;
      const op = i === cfg.current ? '.5' : '.9';
      let g = `<g data-i="${i}">`;
      if (kind === 'grouped') {
        gkeys.forEach((key, j) => {
          const v = r[key.key];
          if (!isNum(v) || v <= 0) return;
          const x = cx(i) - gw / 2 + j * (w + gap), h = Math.max(1, base - y(v));
          g += barPath(x, base - h, w, h, Math.min(2, w / 2), key.color, op, key.key);
        });
        if (cfg.prev?.[i]) gkeys.forEach((key, j) => {
          const pv = cfg.prev[i][key.key];
          if (!isNum(pv) || pv <= 0) return;
          const x = cx(i) - gw / 2 + j * (w + gap), yy = y(pv).toFixed(1);
          g += `<line class="ghost-cap" x1="${(x - 1.5).toFixed(1)}" x2="${(x + w + 1.5).toFixed(1)}" y1="${yy}" y2="${yy}"/>`;
        });
      } else {
        let cum = 0;
        const list = keys.filter(key => isNum(r[key.key]) && r[key.key] > 0);
        list.forEach((key, j) => {
          const v = r[key.key], y0 = y(sc.lo + cum), y1 = y(sc.lo + cum + v);
          cum += v;
          let h = y0 - y1;
          if (j > 0) h -= 2;
          if (h < .8) h = .8;
          g += barPath(cx(i) - bw / 2, y1, bw, h, j === list.length - 1 ? Math.min(2, bw / 2) : 0, key.color, op, key.key);
        });
        if (prevT && isNum(prevT[i]) && prevT[i] > 0) {
          const yy = y(prevT[i]).toFixed(1);
          g += `<line class="ghost-cap" x1="${(cx(i) - bw / 2 - 3).toFixed(1)}" x2="${(cx(i) + bw / 2 + 3).toFixed(1)}" y1="${yy}" y2="${yy}"/>`;
        }
      }
      s += g + '</g>';
    });
  } else {
    const valid = rows.map((r, i) => r ? i : -1).filter(i => i >= 0);
    const pt = (i, v) => `${cx(i).toFixed(1)},${y(v).toFixed(1)}`;
    if (kind === 'sarea') {
      if (prevT) { const pts = valid.filter(i => isNum(prevT[i])).map(i => pt(i, prevT[i])); if (pts.length > 1) s += `<path class="ghost-line" d="M${pts.join('L')}"/>`; }
      let lower = rows.map(() => sc.lo);
      for (const key of keys) {
        const upper = rows.map((r, i) => r ? lower[i] + (isNum(r[key.key]) ? r[key.key] : 0) : null);
        if (valid.length) {
          const top = valid.map(i => pt(i, upper[i])), bot = valid.slice().reverse().map(i => pt(i, lower[i]));
          s += `<path d="M${top.join('L')}L${bot.join('L')}Z" fill="${key.color}" fill-opacity=".82"/>`;
          s += `<path class="sep" d="M${top.join('L')}"/>`;
        }
        lower = upper.map(v => v ?? sc.lo);
      }
    } else {
      const segments = (data,key) => {const out=[];let run=[];for(let i=0;i<data.length;i++){if(isNum(data[i]?.[key]))run.push(i);else if(run.length){out.push(run);run=[];}}if(run.length)out.push(run);return out;};
      if (cfg.prev) for (const key of keys) for(const idx of segments(cfg.prev,key.key)) {
        const pts = idx.map(i => pt(i, cfg.prev[i][key.key]));
        if (pts.length > 1) s += `<path d="M${pts.join('L')}" fill="none" stroke="${key.color}" stroke-opacity=".38" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`;
      }
      for (const key of keys) for(const idx of segments(rows,key.key)) {
        const pts = idx.map(i => pt(i, rows[i][key.key]));
        if (kind === 'area') s += `<path d="M${pts.join('L')}L${cx(idx[idx.length - 1]).toFixed(1)},${base}L${cx(idx[0]).toFixed(1)},${base}Z" fill="${key.color}" fill-opacity=".13"/>`;
        const partial = cfg.current != null && idx[idx.length - 1] === cfg.current && pts.length > 1;
        const line = list => `<path d="M${list.join('L')}" fill="none" stroke="${key.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
        if (partial) s += line(pts.slice(0, -1)) + `<path d="M${pts[pts.length - 2]}L${pts[pts.length - 1]}" fill="none" stroke="${key.color}" stroke-opacity=".45" stroke-width="2" stroke-linecap="round"/>`;
        else if (pts.length > 1) s += line(pts);
        const [lx, ly] = pts[pts.length - 1].split(',');
        s += `<circle cx="${lx}" cy="${ly}" r="4" fill="${key.color}" stroke="var(--surface)" stroke-width="2"/>`;
      }
    }
  }
  s += '<g class="rc-hover"></g></svg>';
  const old = host.querySelector(':scope > svg');
  const tpl = document.createElement('template');
  tpl.innerHTML = s;
  const svg = tpl.content.firstChild;
  if (old) old.replaceWith(svg); else host.prepend(svg);
  st.geo = {padL, padT, slot, n, base, W, keys, rows, totals, prevT, stacked, unit, cx, y, cfg};
  if (hadFocus) svg.focus({preventScroll: true});
  if (st.hi != null && (st.pinned || hadFocus || st.hovering)) show(host, Math.min(st.hi, n - 1), st.pinned);
  else hide(host);
}
// X ticks: the first starts at its bucket and the last ends at its bucket; when labels would collide (narrow widgets, dates),
// fall back to fewer ticks: four, three, then the two ends.
function xTickPlan(ticks, labels, slot, padL, cx) {
  const n = labels.length;
  const place = list => list.filter(i => labels[i]).map((i, j, all) => {
    const anchor = all.length > 1 && j === 0 ? 'start' : all.length > 1 && j === all.length - 1 ? 'end' : 'middle';
    const edge = slot > 20 ? slot / 2 - 6 : 0, label = String(labels[i].label ?? ''), w = label.length * 5.6;
    const x = anchor === 'start' ? padL + slot * i + edge : anchor === 'end' ? padL + slot * (i + 1) - edge : cx(i);
    return {x, anchor, label, l: anchor === 'start' ? x : anchor === 'end' ? x - w : x - w / 2, r: anchor === 'start' ? x + w : anchor === 'end' ? x : x + w / 2};
  });
  const fits = list => list.every((t, j) => j === 0 || t.l - list[j - 1].r >= 8);
  const options = [ticks, [0, Math.floor((n - 1) / 3), Math.floor(2 * (n - 1) / 3), n - 1], [0, Math.floor((n - 1) / 2), n - 1], [0, n - 1], [n - 1]]
    .map(list => [...new Set(list)].filter(i => i >= 0 && i < n));
  for (const list of options) { const plan = place(list); if (fits(plan)) return plan; }
  return [];
}
function lastIndex(rows) { for (let i = rows.length - 1; i >= 0; i--) if (rows[i]) return i; return 0; }
function show(host, i, pinned = false) {
  const st = host.__rct, g = st.geo;
  if (!g) return;
  const cfg = g.cfg, r = g.rows[i], svg = host.querySelector(':scope > svg');
  if (!svg) return;
  const layer = svg.querySelector('.rc-hover');
  const bars = BAR_KINDS.includes(cfg.kind);
  svg.querySelectorAll('[data-i]').forEach(e => e.classList.toggle('dim', +e.dataset.i !== i));
  if (!r) { hideTip(host); layer.innerHTML = ''; st.hi = i; return; }
  if (!bars) {
    let h = `<line class="xhair" x1="${Math.round(g.cx(i)) + .5}" x2="${Math.round(g.cx(i)) + .5}" y1="${g.padT}" y2="${g.base}"/>`;
    let cum = g.stacked ? 0 : null;
    for (const k of g.keys) {
      const v = r[k.key];
      if (!isNum(v)) continue;
      if (cum != null) cum += v;
      h += `<circle cx="${g.cx(i).toFixed(1)}" cy="${g.y(cum != null ? cum : v).toFixed(1)}" r="4" fill="${k.color}" stroke="var(--surface)" stroke-width="2"/>`;
    }
    layer.innerHTML = h;
  } else layer.innerHTML = '';
  let tip = host.querySelector(':scope > .rc-tip');
  if (!tip) { tip = el('div', 'rc-tip'); tip.setAttribute('role', 'tooltip'); tip.id = 'rc-tip-' + Math.random().toString(36).slice(2, 9); host.appendChild(tip); }
  tip.replaceChildren();
  const lab = cfg.labels[i] || {};
  const pv = cfg.prev?.[i];
  const head = el('div', 'tt-h');
  head.append(el('span', '', lab.tip || lab.label || ''));
  if (pv) head.append(el('span', 'tt-prevh', cfg.prevName || 'Previous'));
  tip.append(head);
  const counted = g.unit === 'count' || g.unit === 'tokens';
  if (g.keys.length > 1 && counted) {
    const row = el('div', 'tt-tot'), right = el('span');
    row.append(el('span', '', 'Total'));
    right.append(el('b', '', fmt(g.totals[i], g.unit)));
    if (pv && isNum(g.prevT?.[i])) right.append(el('em', '', fmt(g.prevT[i], g.unit)));
    row.append(right);
    tip.append(row);
  }
  const list = g.keys;
  for (const k of list) {
    const v = r[k.key];
    if ((!isNum(v) || v === 0) && g.keys.length > 3) continue;
    const row = el('div', 'tt-r'), sw = el('i');
    sw.style.background = k.color;
    row.append(sw, el('b', '', fmt(v, g.unit)), el('span', '', k.tip || k.name));
    row.lastChild.title = k.title || k.name;
    row.append(el('em', '', pv && isNum(pv[k.key]) ? fmt(pv[k.key], g.unit) : ''));
    tip.append(row);
  }
  if (cfg.pickable) {
    if (pinned) {
      const b = el('button', 'tt-see', 'See requests');
      b.type = 'button';
      b.addEventListener('click', e => { e.stopPropagation(); pick(host, i, st.pinKey ?? null); });
      tip.append(b);
    } else {
      const f = el('div', 'tt-f', cfg.pickText || 'Click to see these requests');
      tip.append(f);
    }
  }
  tip.classList.toggle('pinned', pinned);
  tip.hidden = false;
  svg.setAttribute('aria-describedby', tip.id);
  const hostW = host.clientWidth || g.W, tw = Math.min(tip.offsetWidth, hostW), x = g.cx(i);
  let left = x + 14;
  if (left + tw > hostW) left = x - tw - 14;
  if (left < 0) left = Math.max(0, Math.min(hostW - tw, x - tw / 2));
  tip.style.left = left.toFixed(0) + 'px';
  tip.style.top = Math.max(0, g.padT - 4) + 'px';
  st.hi = i;
  st.pinned = pinned;
}
function hideTip(host) {
  const tip = host.querySelector(':scope > .rc-tip');
  if (tip) { tip.hidden = true; tip.classList.remove('pinned'); }
}
function hide(host) {
  const st = host.__rct, svg = host.querySelector(':scope > svg');
  if (svg) {
    svg.querySelectorAll('[data-i].dim').forEach(e => e.classList.remove('dim'));
    const layer = svg.querySelector('.rc-hover');
    if (layer) layer.innerHTML = '';
    svg.removeAttribute('aria-describedby');
  }
  hideTip(host);
  if (st) { st.hi = null; st.pinned = false; st.pinKey = null; }
}
function pick(host, i, key) {
  const st = host.__rct, g = st?.geo;
  if (!g || !g.cfg.pickable || !g.rows[i] || typeof g.cfg.onPick !== 'function') return;
  if (key == null && g.keys.length === 1) key = g.keys[0].key;
  g.cfg.onPick(i, key);
}
function wireTime(host) {
  const st = host.__rct;
  st.wired = true;
  const index = e => {
    const g = st.geo, svg = host.querySelector(':scope > svg');
    if (!g || !svg) return null;
    const b = svg.getBoundingClientRect(), x = e.clientX - b.left;
    if (x < g.padL - 2) return null;
    return Math.max(0, Math.min(g.n - 1, Math.floor((x - g.padL) / g.slot)));
  };
  const inTip = e => e.target?.closest?.('.rc-tip');
  host.addEventListener('pointermove', e => {
    if (inTip(e) || st.pinned || st.geo?.cfg.hover === false || e.pointerType === 'touch') return;
    const i = index(e);
    st.hovering = i != null;
    if (i == null) hide(host); else if (i !== st.hi) show(host, i);
  });
  host.addEventListener('pointerleave', e => {
    st.hovering = false;
    if (!st.pinned && e.pointerType !== 'touch' && !host.contains(document.activeElement)) hide(host);
  });
  host.addEventListener('pointerdown', e => {
    st.pointer = e.pointerType;
    if (e.pointerType !== 'touch' || inTip(e) || st.geo?.cfg.hover === false) return;
    const i = index(e);
    if (i == null) { hide(host); return; }
    st.pinKey = e.target?.closest?.('[data-k]')?.dataset.k ?? null;
    show(host, i, true);
    if (!st.outside) {
      st.outside = ev => { if (!host.contains(ev.target)) { hide(host); document.removeEventListener('pointerdown', st.outside, true); st.outside = null; } };
      document.addEventListener('pointerdown', st.outside, true);
    }
  });
  host.addEventListener('click', e => {
    if (inTip(e) || e.pointerType === 'touch' || e.sourceCapabilities?.firesTouchEvents || st.pointer === 'touch' || st.pinned) return;
    const i = index(e);
    if (i == null) return;
    pick(host, i, e.target?.closest?.('[data-k]')?.dataset.k ?? null);
  });
  host.addEventListener('keydown', e => {
    if (e.target?.tagName?.toLowerCase() !== 'svg' || !st.geo) return;
    const g = st.geo;
    let i = st.hi ?? (g.cfg.current ?? lastIndex(g.rows));
    if (e.key === 'ArrowRight') i = Math.min(g.n - 1, i + 1);
    else if (e.key === 'ArrowLeft') i = Math.max(0, i - 1);
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = lastIndex(g.rows);
    else if (e.key === 'Escape') { if (st.hi != null) { e.stopPropagation(); hide(host); } return; }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (st.hi != null) pick(host, st.hi, null); return; }
    else return;
    e.preventDefault();
    show(host, i);
  });
  host.addEventListener('focusin', e => {
    if (e.target?.tagName?.toLowerCase() !== 'svg' || !st.geo || st.geo.cfg.hover === false) return;
    if (st.hi == null) show(host, st.geo.cfg.current ?? lastIndex(st.geo.rows));
  });
  host.addEventListener('focusout', e => {
    if (host.contains(e.relatedTarget) || st.pinned) return;
    if (!st.hovering) hide(host);
  });
}

/* ---------- legend ----------
   Items are buttons (aria-pressed). The first click isolates a series, later clicks add or remove one; when everything or
   nothing is hidden the legend resets. Show all appears while anything is hidden. Colours never move.
   cfg: { keys: [{key, name, short, color, title, group: {id, name, accent}}], hidden: Set, line: bool, extra: [{name, line, color}],
          onChange(hiddenSet), interactive: true } */
function legend(host, cfg) {
  const focusKey = host.contains(document.activeElement) ? document.activeElement.dataset?.key || (document.activeElement.classList.contains('rc-showall') ? '*' : null) : null;
  const hidden = cfg.hidden || new Set();
  host.classList.add('legend', 'rc-legend');
  host.replaceChildren();
  const interactive = cfg.interactive !== false && cfg.keys.length > 1;
  const groups = [];
  for (const k of cfg.keys) {
    const id = k.group?.id ?? '';
    let g = groups[groups.length - 1];
    if (!g || g.id !== id) groups.push(g = {id, info: k.group, keys: []});
    g.keys.push(k);
  }
  for (const g of groups) {
    const box = el('span', 'legend-group');
    if (g.info?.accent) box.classList.add(g.info.accent);
    if (g.info?.name) box.append(el('b', '', g.info.name));
    for (const k of g.keys) {
      const off = hidden.has(k.key);
      const item = el(interactive ? 'button' : 'span', 'rc-lgi');
      if (interactive) { item.type = 'button'; item.setAttribute('aria-pressed', String(!off)); item.dataset.key = k.key; }
      item.title = k.title || k.name;
      const sw = el('i', cfg.line ? 'ln' : '');
      sw.style.background = k.color;
      item.append(sw, el('span', '', k.short || k.name));
      box.append(item);
    }
    host.append(box);
  }
  if (cfg.extra?.length) {
    const box = el('span', 'legend-group');
    for (const x of cfg.extra) {
      const item = el('span', 'rc-lgi static');
      const sw = el('i', x.line ? 'ln' : '');
      sw.style.background = x.color || 'var(--faint)';
      item.append(sw, el('span', '', x.name));
      box.append(item);
    }
    host.append(box);
  }
  if (interactive && hidden.size) {
    const all = el('button', 'rc-showall', 'Show all');
    all.type = 'button';
    all.addEventListener('click', () => cfg.onChange?.(new Set()));
    host.append(all);
  }
  if (interactive) host.querySelectorAll('button[data-key]').forEach(b => b.addEventListener('click', () => {
    const key = b.dataset.key, all = cfg.keys.map(k => k.key);
    let next = new Set(hidden);
    if (!next.size) next = new Set(all.filter(k => k !== key));
    else if (next.has(key)) next.delete(key); else next.add(key);
    if (next.size >= all.length) next = new Set();
    cfg.onChange?.(next);
  }));
  host.hidden = !host.childElementCount;
  if (focusKey) {
    const target = focusKey === '*' ? host.querySelector('button[data-key]') : [...host.querySelectorAll('button[data-key]')].find(b => b.dataset.key === focusKey);
    target?.focus({preventScroll: true});
  }
}

/* ---------- shares: donut, ranked bars, treemap ----------
   parts: [{key, name, value, color, title}] already cut to Top N with an "Other (n)" part last. */
function share(v, total) { if (!total) return '0%'; const p = v / total * 100; return (p > 0 && p < 1 ? p.toFixed(1) : Math.round(p)) + '%'; }
function donut(host, parts, opts = {}) {
  const total = opts.total ?? parts.reduce((s, p) => s + (p.value || 0), 0);
  const size = 132, r = size / 2 - 2, ri = r - 15, c = size / 2;
  let a0 = -Math.PI / 2, arcs = '';
  const sum = parts.reduce((s, p) => s + Math.max(0, p.value || 0), 0) || 1;
  const P = (rad, a) => `${(c + rad * Math.cos(a)).toFixed(2)},${(c + rad * Math.sin(a)).toFixed(2)}`;
  parts.forEach((p, i) => {
    const a1 = a0 + Math.max(0, p.value || 0) / sum * Math.PI * 2;
    if (a1 - a0 < .002) { a0 = a1; return; }
    if (a1 - a0 > Math.PI * 2 - .001) arcs += `<circle cx="${c}" cy="${c}" r="${(r + ri) / 2}" fill="none" stroke="${p.color}" stroke-width="${r - ri}" data-n="${i}"/>`;
    else {
      const large = a1 - a0 > Math.PI ? 1 : 0;
      arcs += `<path d="M${P(r, a0)}A${r},${r} 0 ${large} 1 ${P(r, a1)}L${P(ri, a1)}A${ri},${ri} 0 ${large} 0 ${P(ri, a0)}Z" fill="${p.color}" stroke="var(--surface)" stroke-width="2" stroke-linejoin="round" data-n="${i}"><title>${esc(p.title || p.name)} · ${esc(fmt(p.value, opts.unit))}</title></path>`;
    }
    a0 = a1;
  });
  host.classList.add('rc-donut-wrap');
  host.style.setProperty('--rc-n', parts.length);
  host.innerHTML = `<div class="rc-donut"><svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${esc(opts.label || 'Share')}">${arcs || `<circle cx="${c}" cy="${c}" r="${(r + ri) / 2}" fill="none" stroke="var(--track)" stroke-width="${r - ri}"/>`}</svg><div class="val"><b>${esc(fmt(total, opts.unit))}</b><span>${esc(opts.centerLabel || '')}</span></div></div><div class="rc-blist">${parts.map((p, i) => `<div data-n="${i}"><i style="background:${p.color}"></i><span class="n" title="${esc(p.title || p.name)}">${esc(p.name)}</span><b>${esc(fmt(p.value, opts.unit))}</b><small>${esc(share(p.value, total))}</small></div>`).join('')}</div>`;
  const hl = n => host.querySelectorAll('[data-n]').forEach(e => e.classList.toggle('dim', n != null && e.dataset.n !== n));
  host.onpointerover = e => { const t = e.target.closest?.('[data-n]'); hl(t ? t.dataset.n : null); };
  host.onpointerleave = () => hl(null);
}
function ranked(host, parts, opts = {}) {
  const mx = Math.max(0, ...parts.map(p => p.value || 0)) || 1;
  host.classList.add('rc-ranked');
  host.style.setProperty('--rc-n', parts.length);
  host.setAttribute('role', 'list');
  host.innerHTML = parts.map(p => `<div role="listitem"><span class="n" title="${esc(p.title || p.name)} · ${esc(fmt(p.value, opts.unit))}">${esc(p.name)}</span><span class="trk"><span class="bar" style="width:${Math.max(.6, (p.value || 0) / mx * 100).toFixed(2)}%;background:${p.color}"></span></span><b>${esc(fmt(p.value, opts.unit))}</b></div>`).join('');
}
function squarify(items, x, y, w, h) {
  const out = [], totalV = items.reduce((s, i) => s + i.value, 0);
  if (!totalV || w <= 0 || h <= 0) return out;
  const worst = (row, s) => { const sum = row.reduce((a, i) => a + i.a, 0), mx = Math.max(...row.map(i => i.a)), mn = Math.min(...row.map(i => i.a)); return Math.max(s * s * mx / (sum * sum), sum * sum / (s * s * mn)); };
  let rest = items.map(i => ({...i, a: i.value * w * h / totalV}));
  while (rest.length) {
    const short = Math.min(w, h);
    let row = [rest[0]], best = worst(row, short), k = 1;
    while (k < rest.length) { const t = [...row, rest[k]], wv = worst(t, short); if (wv > best) break; row = t; best = wv; k++; }
    const sum = row.reduce((s, i) => s + i.a, 0);
    if (w >= h) { const cw = sum / h; let yy = y; for (const it of row) { const ch = it.a / cw; out.push({...it, x, y: yy, w: cw, h: ch}); yy += ch; } x += cw; w -= cw; }
    else { const ch = sum / w; let xx = x; for (const it of row) { const cw = it.a / ch; out.push({...it, x: xx, y, w: cw, h: ch}); xx += cw; } y += ch; h -= ch; }
    rest = rest.slice(k);
  }
  return out;
}
function resolveColor(css) {
  const p = document.createElement('span');
  p.style.color = css; p.style.display = 'none';
  document.body.appendChild(p);
  const c = getComputedStyle(p).color;
  p.remove();
  return c;
}
// Treemap labels are the one place text sits on a series colour: white or near-black, by the fill's luminance.
function inkFor(css) {
  const m = (resolveColor(css).match(/[\d.]+/g) || [128, 128, 128]).map(Number);
  const L = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
  return .2126 * L(m[0]) + .7152 * L(m[1]) + .0722 * L(m[2]) > .32 ? '#0b0e13' : '#ffffff';
}
function treemap(host, parts, opts = {}) {
  host.classList.add('rc-tree');
  const draw = box => {
    const list = parts.filter(p => p.value > 0), total = list.reduce((s, p) => s + p.value, 0);
    const {W, H} = size(host, fills(host) ? 40 : opts.height || 180, 320, box);
    const rects = squarify(list, 0, 0, W, H);
    host.setAttribute('role', 'img');
    host.setAttribute('aria-label', opts.label || 'Share');
    host.innerHTML = rects.map(r => {
      const fits = r.w > 74 && r.h > 34, ink = inkFor(r.color);
      return `<div style="left:${(r.x + 1).toFixed(1)}px;top:${(r.y + 1).toFixed(1)}px;width:${Math.max(0, r.w - 2).toFixed(1)}px;height:${Math.max(0, r.h - 2).toFixed(1)}px;background:${r.color};color:${ink}" title="${esc(r.title || r.name)} · ${esc(fmt(r.value, opts.unit))} · ${esc(share(r.value, total))}">${fits ? `<b>${esc(share(r.value, total))}</b><span>${esc(r.name)}</span>` : ''}</div>`;
    }).join('');
  };
  draw();
  observe(host, draw);
}

/* ---------- heatmaps ---------- one sequential hue: --blue mixed into --raised at 0/22/42/64/85/100%. */
const HEAT = [22, 42, 64, 85, 100];
function heatColor(v, max) {
  if (!isNum(v) || v <= 0 || !max) return 'var(--raised)';
  return `color-mix(in srgb,var(--blue) ${HEAT[Math.min(4, Math.floor(v / max * 5))]}%,var(--raised))`;
}
function heatKey() {
  return `<div class="rc-heat-key" aria-hidden="true">Less${[0, ...HEAT].map(p => `<i style="background:${p ? `color-mix(in srgb,var(--blue) ${p}%,var(--raised))` : 'var(--raised)'}"></i>`).join('')}More</div>`;
}
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const hourName = h => new Date(2000, 0, 1, h).toLocaleTimeString(undefined, {hour: 'numeric'});
function weekHour(host, cells, opts = {}) {
  const max = opts.max ?? Math.max(0, ...cells.flat().filter(isNum));
  const what = opts.what || 'requests';
  const fmtV = v => fmt(v >= 10 || v === 0 ? Math.round(v) : Math.round(v * 10) / 10, opts.unit);
  host.classList.add('rc-heat');
  host.innerHTML = `<div class="rc-heat-grid" role="img" aria-label="${esc(opts.label || 'Activity by hour of the week')}">${cells.map((row, d) => `<span class="rl">${DAYS[d]}</span><div class="cells">${row.map((v, h) => isNum(v) ? `<i style="background:${heatColor(v, max)}" title="${DAYS[d]} ${esc(hourName(h))} · ${esc(fmtV(v))} ${esc(what)} on average"></i>` : `<i class="none" title="${DAYS[d]} ${esc(hourName(h))} · not recorded"></i>`).join('')}</div>`).join('')}<span></span><div class="cols"><span>00</span><span>06</span><span>12</span><span>18</span></div></div>${heatKey()}`;
}
function calendar(host, days, opts = {}) {
  const max = opts.max ?? Math.max(0, ...days.map(d => d.value).filter(isNum));
  const what = opts.what || 'requests';
  const todayIso = opts.today;
  const weeks = Math.ceil(days.length / 7);
  const months = [];
  for (let w = 0; w < weeks; w++) {
    const d = days[w * 7];
    const m = d ? new Date(d.date + 'T12:00:00').toLocaleDateString(undefined, {month: 'short'}) : '';
    months.push(months.length && months[months.length - 1].m === m ? {m, l: ''} : {m, l: m});
  }
  const cell = d => {
    const label = new Date(d.date + 'T12:00:00').toLocaleDateString(undefined, {weekday: 'short', month: 'short', day: 'numeric'});
    if (d.future) return '<i class="future"></i>';
    if (!isNum(d.value)) return `<i class="none" title="${esc(label)} · before history starts"></i>`;
    return `<i style="background:${heatColor(d.value, max)}"${d.date === todayIso ? ' class="today"' : ''} title="${esc(label)} · ${esc(fmt(d.value, opts.unit))} ${esc(what)}"></i>`;
  };
  host.classList.add('rc-cal');
  host.innerHTML = `<div class="rc-cal-grid" role="img" aria-label="${esc(opts.label || 'Daily activity')}"><span></span><div class="months" style="grid-template-columns:repeat(${weeks},minmax(0,1fr))">${months.map(m => `<span>${esc(m.l)}</span>`).join('')}</div><div class="rl">${['M', '', 'W', '', 'F', '', 'S'].map(x => `<span>${x}</span>`).join('')}</div><div class="cells" style="grid-template-columns:repeat(${weeks},minmax(0,1fr))">${days.map(cell).join('')}</div></div>${heatKey()}`;
}

/* ---------- sparkline ---------- */
function spark(host, values, opts = {}) {
  host.classList.add('rc-spark');
  const draw = box => {
    const v = values.map((x, i) => [i, x]).filter(p => isNum(p[1]));
    if (v.length < 2) { host.innerHTML = ''; return; }
    const {W} = size(host, 0, 160, box);
    const H = fills(host) ? Math.min(120, Math.floor(host.clientHeight - (parseFloat(getComputedStyle(host).paddingTop) || 0))) : opts.height || 30;
    if (H < 16) { host.innerHTML = ''; return; }
    const ys = v.map(p => p[1]);
    const pct = opts.unit === 'percent';
    const mn = pct ? Math.min(98, ...ys) : Math.min(...ys), mx = pct ? 100 : Math.max(...ys);
    const n = values.length;
    const xx = i => 4 + (W - 8) * i / Math.max(1, n - 1), yy = x => 4 + (H - 8) * (1 - (mx === mn ? .5 : (x - mn) / (mx - mn)));
    const pts = v.map(([i, x]) => `${xx(i).toFixed(1)},${yy(x).toFixed(1)}`);
    const [li, lv] = v[v.length - 1];
    host.innerHTML = `<svg width="${W}" height="${H}" style="width:${W}px;height:${H}px" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path d="M${pts.join('L')}" fill="none" stroke="var(--muted)" stroke-opacity=".75" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${xx(li).toFixed(1)}" cy="${yy(lv).toFixed(1)}" r="3.5" fill="var(--blue)" stroke="var(--surface)" stroke-width="2"${li === opts.current ? ' opacity=".6"' : ''}/></svg>`;
  };
  draw();
  observe(host, draw);
}

/* ---------- gauges ---------- remaining and pace are percentages (pace = share of the window still to come). */
function ringSVG({remaining, pace, color, size = 112}) {
  const r = 46, C = 2 * Math.PI * r, rem = Math.max(0, Math.min(100, remaining || 0));
  let tick = '';
  if (isNum(pace)) {
    const a = (100 - Math.max(0, Math.min(100, pace))) / 100 * Math.PI * 2;
    const p = d => `${(56 + d * Math.cos(a)).toFixed(2)}`, q = d => `${(56 + d * Math.sin(a)).toFixed(2)}`;
    tick = `<line x1="${p(r - 8)}" y1="${q(r - 8)}" x2="${p(r + 8)}" y2="${q(r + 8)}" stroke="var(--text)" stroke-opacity=".6" stroke-width="2" stroke-linecap="round"/>`;
  }
  return `<svg viewBox="0 0 112 112" width="${size}" height="${size}" aria-hidden="true"><circle cx="56" cy="56" r="${r}" fill="none" stroke="var(--track)" stroke-width="9"/>${rem ? `<circle cx="56" cy="56" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${(C * rem / 100).toFixed(2)} ${C.toFixed(2)}"/>` : ''}${tick}</svg>`;
}

/* ---------- reset timeline ----------
   lanes: [{name, sub, color, remaining, state, resetAt, runsOutAt, resetText, title, muted}], times in epoch seconds.
   opts: {now, hours, forecast}. A limit whose reset falls past the window runs to the edge with its countdown there. */
function timeline(host, lanes, opts = {}) {
  host.classList.add('rc-tl');
  const draw = box => {
    const {W} = size(host, 0, 520, box);
    const narrow = W < 520, labW = narrow ? 0 : Math.min(176, Math.max(120, W * .24)), top = 22;
    let laneH = narrow ? 46 : 36;
    // A fixed-height box fits its lanes: they compress (36 → 28 px; below that the name and limit lines would touch), then
    // scroll; a tall box spaces them out up to 48 px (narrow: 46 → 56), and the key follows the last lane.
    const grow = narrow ? 56 : 48;
    if (fills(host)) host.parentElement?.style.setProperty('--rc-tl-natural', `${top + lanes.length * grow + 4}px`);
    if (fills(host)) laneH = Math.max(narrow ? 36 : 28, Math.min(grow, Math.floor((host.clientHeight - top - 4) / Math.max(1, lanes.length))));
    const subY = laneH < 34 ? 10 : 11;
    const H = top + lanes.length * laneH + 4, span = (opts.hours || 168) * 3600, now = opts.now || Date.now() / 1000;
    const x0 = labW + (narrow ? 0 : 6) + .5, x1 = W - 58;
    const X = t => x0 + (x1 - x0) * Math.max(0, Math.min(span, t - now)) / span;
    const uid = host.__rcId || (host.__rcId = Math.random().toString(36).slice(2, 8));
    let s = `<svg width="${W}" height="${H}" style="width:${W}px;height:${H}px" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || 'Next reset for each limit')}"><defs><pattern id="tl-out-${uid}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.6" height="5" fill="var(--danger)" fill-opacity=".8"/></pattern><pattern id="tl-dry-${uid}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="1.6" height="5" fill="var(--amber)" fill-opacity=".85"/></pattern></defs>`;
    // Day lines at local midnight (hours window: every 6 hours), labelled at the top.
    const ticks = [];
    if (span <= 86400 * 1.01) { const d = new Date(now * 1000); d.setMinutes(0, 0, 0); d.setHours(Math.floor(d.getHours() / 6) * 6 + 6); for (let t = d.getTime() / 1000; t < now + span; t += 6 * 3600) ticks.push({t, label: new Date(t * 1000).toLocaleTimeString(undefined, {hour: 'numeric'})}); }
    else {
      const step = span > 86400 * 14 ? 7 : 1, d = new Date(now * 1000); d.setHours(24, 0, 0, 0);
      if (step === 7) d.setDate(d.getDate() + ((8 - d.getDay()) % 7));
      for (let t = d.getTime() / 1000; t < now + span; t = new Date(new Date(t * 1000).setDate(new Date(t * 1000).getDate() + step)).getTime() / 1000) {
        const dt = new Date(t * 1000);
        ticks.push({t, label: step === 7 ? dt.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : dt.toLocaleDateString(undefined, {weekday: 'short'}) + ' ' + dt.getDate()});
      }
    }
    let lastLabel = -1e9;
    for (const tk of ticks) {
      const xx = Math.round(X(tk.t)) + .5;
      s += `<line class="day" x1="${xx}" x2="${xx}" y1="${top - 6}" y2="${H}"/>`;
      if (xx - lastLabel > 46 && xx < W - 30) { s += `<text x="${xx + 4}" y="10">${esc(tk.label)}</text>`; lastLabel = xx; }
    }
    s += `<line class="now" x1="${x0}" x2="${x0}" y1="${top - 8}" y2="${H}"/>${narrow ? '' : `<text class="now-t" x="${x0 - 4}" y="10" text-anchor="end">Now</text>`}`;
    lanes.forEach((l, i) => {
      const yc = top + i * laneH + (narrow ? laneH - 16 : laneH / 2);
      s += narrow ? `<text class="lane-name" x="0" y="${yc - 14}">${esc(l.name)}<tspan class="lane-sub" dx="6">${esc(l.sub)}</tspan></text>`
        : `<text class="lane-name" x="0" y="${yc - 2}">${esc(l.name)}</text><text class="lane-sub" x="0" y="${yc + subY}">${esc(l.sub)}</text>`;
      const reset = isNum(l.resetAt) && l.resetAt > now ? l.resetAt : null;
      const rx = reset ? X(reset) : x1, past = reset && reset - now > span;
      if (l.state === 'out') s += `<rect x="${x0}" y="${yc - 5}" width="${Math.max(2, rx - x0).toFixed(1)}" height="10" rx="3" fill="url(#tl-out-${uid})" stroke="var(--danger)" stroke-opacity=".5"/>`;
      else if (l.state === 'runs_out' && opts.forecast !== false && isNum(l.runsOutAt) && l.runsOutAt > now && (!reset || l.runsOutAt < reset)) {
        const ox = X(l.runsOutAt);
        s += `<rect x="${x0}" y="${yc - 2}" width="${Math.max(2, ox - x0).toFixed(1)}" height="4" rx="2" fill="${l.color}"/>`;
        if (rx - ox > 6) s += `<rect x="${(ox + 2).toFixed(1)}" y="${yc - 5}" width="${(rx - ox - 4).toFixed(1)}" height="10" rx="3" fill="url(#tl-dry-${uid})" stroke="var(--amber)" stroke-opacity=".55"/>`;
      } else s += `<rect x="${x0}" y="${yc - 2}" width="${Math.max(2, rx - x0).toFixed(1)}" height="4" rx="2" fill="${l.color}"${l.muted ? ' fill-opacity=".45"' : ''}/>`;
      if (reset && !past) s += `<circle cx="${rx.toFixed(1)}" cy="${yc}" r="5.5" fill="var(--text)" stroke="var(--surface)" stroke-width="2.5"><title>${esc(l.title || '')}</title></circle>`;
      else if (past) s += `<path d="M${(rx - 1).toFixed(1)},${yc - 4}l5,4l-5,4" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><title>${esc(l.title || '')}</title></path>`;
      s += `<text class="when ${l.state === 'out' ? 'bad' : ''}" x="${(rx + 10).toFixed(1)}" y="${yc + 3}">${esc(l.resetText || '')}</text>`;
    });
    host.innerHTML = s + '</svg>';
  };
  draw();
  observe(host, draw);
}

// Range change / refetch: keep the previous render at 45% until new data arrives.
function dim(node, on) { node?.classList.toggle('rc-refetch', !!on); }
function destroy(host) {
  if (!host) return;
  host.querySelectorAll('*').forEach(n => { if (n.__rcObs) unobserve(n); if (n.__rct?.outside) document.removeEventListener('pointerdown', n.__rct.outside, true); });
  unobserve(host);
}

window.RouterCharts = {fmt, axisFmt, niceStep, yScale, esc, share, time, legend, donut, ranked, treemap, squarify, inkFor, heatColor,
  weekHour, calendar, spark, ringSVG, timeline, dim, destroy, hourName, show, hide, fills};
})();
