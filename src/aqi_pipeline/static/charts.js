(function (global) {
  'use strict';

  /* AqiCharts: dependency-free SVG charts for the Operations Console.
     Mark specs: 2px lines, >=8px markers with a 2px surface ring, bars <=24px with a
     4px rounded data end and a square baseline, 2px gaps between touching bars,
     solid hairline gridlines, text in text tokens (never the series colour). */

  const NS = 'http://www.w3.org/2000/svg';

  const CPCB_CATEGORIES = [
    { name: 'Good', min: 0, max: 50, range: '0–50', color: 'var(--cat-good)' },
    { name: 'Satisfactory', min: 50, max: 100, range: '51–100', color: 'var(--cat-sat)' },
    { name: 'Moderate', min: 100, max: 200, range: '101–200', color: 'var(--cat-mod)' },
    { name: 'Poor', min: 200, max: 300, range: '201–300', color: 'var(--cat-poor)' },
    { name: 'Very poor', min: 300, max: 400, range: '301–400', color: 'var(--cat-vpoor)' },
    { name: 'Severe', min: 400, max: 500, range: '401–500', color: 'var(--cat-sev)' },
  ];

  const SERIES = {
    actual: { name: 'Actual', color: 'var(--series-ink)', dash: null },
    served: { name: 'Served forecast', color: 'var(--s1)', dash: null },
    persistence: { name: 'Persistence', color: 'var(--s2)', dash: '5 4' },
    model: { name: 'Raw model', color: 'var(--s4)', dash: '1.5 3.5' },
  };

  function el(tag, attrs, parent, text) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function h(tag, cls, parent, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function fmt(v, d) {
    return v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toFixed(d == null ? 1 : d);
  }

  function catColor(catName) {
    const found = CPCB_CATEGORIES.find((c) => c.name.toLowerCase() === String(catName || '').toLowerCase());
    return found ? found.color : 'var(--cat-unk)';
  }

  function catFor(aqi) {
    if (aqi == null || Number.isNaN(Number(aqi))) return null;
    const v = Number(aqi);
    return CPCB_CATEGORIES.find((c) => v <= c.max) || CPCB_CATEGORIES[CPCB_CATEGORIES.length - 1];
  }

  function parseTs(ts) {
    const s = String(ts || '').trim();
    if (!s) return NaN;
    const iso = s.includes('T') ? s : s.replace(' ', 'T');
    return Date.parse(iso.endsWith('Z') || /[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + 'Z');
  }

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function fmtShortTs(ms) {
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return '–';
    return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()] + ' ' + String(d.getUTCHours()).padStart(2, '0') + ':00';
  }

  function fmtDay(ms) {
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return '–';
    return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()];
  }

  function fmtHumanTs(ts) {
    const ms = parseTs(ts);
    return Number.isNaN(ms) ? String(ts || '–') : fmtShortTs(ms);
  }

  function niceTicks(maxVal, n) {
    const safeMax = Number(maxVal) > 0 ? Number(maxVal) : 1;
    const raw = safeMax / Math.max(n, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [0];
    while (out[out.length - 1] < safeMax - 1e-9 && out.length < 15) {
      out.push(+(out[out.length - 1] + step).toFixed(6));
    }
    return out;
  }

  function pathOf(xs, ys, X, Y) {
    let d = '';
    let pen = false;
    for (let i = 0; i < xs.length; i++) {
      if (ys[i] == null || Number.isNaN(ys[i])) {
        pen = false;
        continue;
      }
      d += (pen ? 'L' : 'M') + X(xs[i]).toFixed(1) + ' ' + Y(ys[i]).toFixed(1);
      pen = true;
    }
    return d;
  }

  /* Column with a 4px rounded data end and a square baseline. */
  function colPath(x, y, w, hgt) {
    const r = Math.min(4, w / 2, hgt);
    const b = y + hgt;
    return 'M' + x + ' ' + b + 'V' + (y + r) + 'Q' + x + ' ' + y + ' ' + (x + r) + ' ' + y +
      'H' + (x + w - r) + 'Q' + (x + w) + ' ' + y + ' ' + (x + w) + ' ' + (y + r) + 'V' + b + 'Z';
  }

  /* Horizontal bar, rounded at the right (data) end. */
  function barPath(x, y, w, hgt) {
    const r = Math.min(4, hgt / 2, w);
    return 'M' + x + ' ' + y + 'H' + (x + w - r) + 'Q' + (x + w) + ' ' + y + ' ' + (x + w) + ' ' + (y + r) +
      'V' + (y + hgt - r) + 'Q' + (x + w) + ' ' + (y + hgt) + ' ' + (x + w - r) + ' ' + (y + hgt) + 'H' + x + 'Z';
  }

  /* Draw at the container's real width so text stays 11px on every screen. */
  function fitW(host, def, min) {
    const w = host && host.clientWidth ? Math.round(host.clientWidth) : 0;
    return w ? Math.max(min || 300, Math.min(def, w)) : def;
  }

  function anchorFor(i, x, box) {
    if (i === 0) return 'start';
    return x > box.x + box.w - 44 ? 'end' : 'middle';
  }

  function grid(svg, box, ticks, Y, fmtTick) {
    ticks.forEach((v) => {
      el('line', { x1: box.x, x2: box.x + box.w, y1: Y(v), y2: Y(v), stroke: v === 0 ? 'var(--axis)' : 'var(--grid)', 'stroke-width': 1 }, svg);
      el('text', { x: box.x - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg, fmtTick ? fmtTick(v) : String(v));
    });
  }

  function makeKey(parent, color, dash) {
    const k = h('i', 'key' + (dash ? (dash.indexOf('1.5') === 0 ? ' dotted' : ' dashed') : ''), parent);
    k.style.borderColor = color;
    return k;
  }

  /* Crosshair + tooltip card (desktop) or inline readout (narrow screens).
     Arrow keys move the crosshair when the plot has focus. */
  function attachHover(host, svg, W, box, xs, X, rowBuilder, yRange, markers) {
    if (!xs.length) return;
    const tip = h('div', 'tip', host);
    const inline = h('div', 'chart-inline-tip', host);
    const hair = el('line', { y1: yRange.y0, y2: yRange.y1, stroke: 'var(--axis)', 'stroke-width': 1, visibility: 'hidden' }, svg);
    const dots = (markers || []).map((m) =>
      el('circle', { r: 4.5, fill: m.color, stroke: 'var(--card)', 'stroke-width': 2, visibility: 'hidden' }, svg)
    );
    const hit = el(
      'rect',
      {
        class: 'hit',
        x: box.x,
        y: yRange.y0,
        width: Math.max(1, box.w),
        height: Math.max(1, yRange.y1 - yRange.y0),
        fill: 'transparent',
        tabindex: 0,
        role: 'slider',
        'aria-label': 'Chart readout. Use left and right arrow keys to move.',
        'aria-valuemin': 0,
        'aria-valuemax': xs.length - 1,
      },
      svg
    );
    let cur = xs.length - 1;

    function fill(target, info) {
      target.textContent = '';
      h('div', 't', target, info.title);
      info.rows.forEach((r) => {
        const row = h('div', 'r', target);
        const left = h('span', null, row);
        if (r.color) makeKey(left, r.color, r.dash);
        left.appendChild(document.createTextNode(r.name));
        h('b', null, row, r.value);
      });
    }

    function show(i) {
      const x = X(xs[i]);
      hair.setAttribute('x1', x);
      hair.setAttribute('x2', x);
      hair.setAttribute('visibility', 'visible');
      (markers || []).forEach((m, mi) => {
        const v = m.values[i];
        if (v == null || Number.isNaN(v)) {
          dots[mi].setAttribute('visibility', 'hidden');
          return;
        }
        dots[mi].setAttribute('cx', x);
        dots[mi].setAttribute('cy', m.Y(v));
        dots[mi].setAttribute('visibility', 'visible');
      });
      const info = rowBuilder(i);
      hit.setAttribute('aria-valuenow', i);
      hit.setAttribute('aria-valuetext', info.title);
      fill(tip, info);
      fill(inline, info);
      tip.style.display = 'block';
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? rect.width / W : 1;
      const left = x * scale + 14;
      const wTip = tip.offsetWidth || 200;
      tip.style.left = (left + wTip > rect.width ? Math.max(4, x * scale - wTip - 14) : left) + 'px';
      tip.style.top = Math.max(4, yRange.y0 * scale + 4) + 'px';
    }

    function hide() {
      tip.style.display = 'none';
      hair.setAttribute('visibility', 'hidden');
      dots.forEach((d) => d.setAttribute('visibility', 'hidden'));
    }

    function nearest(clientX) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return cur;
      const px = ((clientX - rect.left) / rect.width) * W;
      let best = 0;
      let bestDist = Infinity;
      xs.forEach((v, idx) => {
        const d = Math.abs(X(v) - px);
        if (d < bestDist) {
          bestDist = d;
          best = idx;
        }
      });
      return best;
    }

    hit.addEventListener('pointermove', (ev) => {
      cur = nearest(ev.clientX);
      show(cur);
    });
    hit.addEventListener('pointerdown', (ev) => {
      cur = nearest(ev.clientX);
      show(cur);
    });
    hit.addEventListener('pointerleave', (ev) => {
      if (ev.pointerType === 'mouse') hide();
    });
    hit.addEventListener('focus', () => show(cur));
    hit.addEventListener('blur', hide);
    hit.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight' || ev.key === 'Home' || ev.key === 'End') {
        ev.preventDefault();
        if (ev.key === 'ArrowLeft') cur = Math.max(0, cur - 1);
        if (ev.key === 'ArrowRight') cur = Math.min(xs.length - 1, cur + 1);
        if (ev.key === 'Home') cur = 0;
        if (ev.key === 'End') cur = xs.length - 1;
        show(cur);
      }
    });
    // Seed the inline readout with the latest point so phones show something useful.
    fill(inline, rowBuilder(cur));
  }

  /* ---------- AQI figure: hero number + segmented CPCB scale ---------- */
  function drawAqiGauge(host, opts) {
    host.textContent = '';
    const o = opts || {};
    const aqi = o.aqi != null && !Number.isNaN(Number(o.aqi)) ? Number(o.aqi) : null;
    const max = o.max || 500;
    const category = o.category || (aqi != null && catFor(aqi) ? catFor(aqi).name : 'Unknown');

    const fig = h('div', 'aqi-figure' + (o.compact ? ' compact' : ''), host);
    fig.setAttribute('role', 'img');
    fig.setAttribute('aria-label', aqi != null ? 'AQI ' + fmt(aqi, 0) + ', ' + category + ', on the CPCB scale of 0 to 500' : 'AQI not available');

    const top = h('div', 'aqi-top', fig);
    const left = h('div', null, top);
    h('div', 'aqi-num' + (aqi == null ? ' is-empty' : ''), left, aqi != null ? fmt(aqi, 0) : '–');
    h('div', 'aqi-unit', left, o.unitLabel || 'CPCB AQI');
    if (aqi != null || o.showCategory) {
      const pill = h('span', 'pill cat lg', top);
      const dot = h('i', 'dot-c', pill);
      dot.style.background = catColor(category);
      pill.appendChild(document.createTextNode(category));
    }

    const scale = h('div', 'aqi-scale', fig);
    const track = h('div', 'aqi-track', scale);
    const active = aqi != null ? catFor(aqi) : null;
    CPCB_CATEGORIES.forEach((c) => {
      const seg = h('i', 'aqi-seg' + (active && active.name === c.name ? ' on' : ''), track);
      seg.style.background = c.color;
      seg.style.flex = String(c.max - c.min);
      seg.title = c.name + ' (' + c.range + ')';
    });
    if (aqi != null) {
      const m = h('i', 'aqi-marker', scale);
      m.style.left = (Math.max(0, Math.min(max, aqi)) / max) * 100 + '%';
    }
    const ticks = h('div', 'aqi-ticks', fig);
    [0, 100, 200, 300, 400, 500].forEach((t) => {
      const s = h('span', null, ticks, String(t));
      s.style.left = (t / max) * 100 + '%';
    });
  }

  /* ---------- Spark bars (KPI micro-viz) ---------- */
  function drawSpark(host, values, opts) {
    host.textContent = '';
    const vals = (values || []).filter((v) => v != null && !Number.isNaN(Number(v))).map(Number);
    if (!vals.length) return;
    const o = opts || {};
    const maxV = Math.max(o.floor || 0, Math.max.apply(null, vals)) || 1;
    const peak = Math.max.apply(null, vals);
    const peakIdx = vals.indexOf(peak);
    vals.forEach((v, i) => {
      const bar = h('i', i === peakIdx ? 'peak' : v >= (o.hotAt || Infinity) ? 'hot' : null, host);
      bar.style.height = Math.max(8, (v / maxV) * 100) + '%';
    });
    if (o.ariaLabel) host.setAttribute('aria-label', o.ariaLabel);
  }

  /* ---------- Forecast chart ---------- */
  function drawForecastChart(host, series, opts) {
    host.textContent = '';
    if (!series || !series.length) {
      h('div', 'empty-msg', host, 'No hourly observations or forecasts yet. Run a cycle and they appear here.');
      return;
    }
    const o = opts || {};
    const show = {
      actual: o.showActual !== false,
      served: o.showServed !== false,
      model: Boolean(o.showModel),
      persistence: o.showPersistence !== false,
    };
    const xs = series.map((r) => parseTs(r.timestamp));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 3600000 : xs[xs.length - 1];
    const W = fitW(host, 1120);
    const narrow = W < 640;
    const H = narrow ? 260 : 320;
    const box = { x: 40, y: 22, w: W - 40 - (narrow ? 30 : 64), h: H - 68 };
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;

    let maxAqi = 100;
    series.forEach((r) => {
      [show.actual ? r.actual_aqi : null, show.served ? r.forecast_aqi : null, show.model ? r.model_aqi : null, show.persistence ? r.persistence_aqi : null].forEach((v) => {
        if (v != null && v > maxAqi) maxAqi = v;
      });
    });

    const ticks = niceTicks(Math.min(500, Math.ceil(maxAqi * 1.08)), 5);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(Math.max(v, 0), top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Hourly actual AQI against served forecast' }, host);

    if (o.showBands) {
      CPCB_CATEGORIES.forEach((cat) => {
        if (cat.min >= top) return;
        const yHi = Y(Math.min(cat.max, top));
        const yLo = Y(cat.min);
        el('rect', { x: box.x, y: yHi, width: box.w, height: Math.max(0, yLo - yHi), fill: cat.color, opacity: 0.08, class: 'band' }, svg);
        if (!narrow && yLo - yHi > 14) {
          el('text', { x: box.x + box.w + 8, y: (yHi + yLo) / 2 + 4 }, svg, cat.name);
        }
      });
    }

    grid(svg, box, ticks, Y);

    const firstFutureIdx = series.findIndex((r) => r.is_future);
    if (firstFutureIdx >= 0) {
      const xNow = X(xs[Math.max(0, firstFutureIdx - 1)]);
      el('rect', { x: xNow, y: box.y, width: Math.max(0, box.x + box.w - xNow), height: box.h, fill: 'var(--future-band)' }, svg);
      el('line', { x1: xNow, x2: xNow, y1: box.y - 8, y2: box.y + box.h, stroke: 'var(--ink-2)', 'stroke-width': 1 }, svg);
      if (xNow - box.x > 40) el('text', { x: xNow - 6, y: box.y - 10, 'text-anchor': 'end', class: 'lab' }, svg, 'Now');
      el('text', { x: xNow + 6, y: box.y - 10, class: 'lab' }, svg, 'Forecast →');
    }

    const lines = [
      ['persistence', 'persistence_aqi', 1.75],
      ['model', 'model_aqi', 2],
      ['actual', 'actual_aqi', 2],
      ['served', 'forecast_aqi', 2],
    ];
    const markers = [];
    lines.forEach(([key, field, width]) => {
      if (!show[key]) return;
      const vals = series.map((r) => r[field]);
      const d = pathOf(xs, vals, X, Y);
      if (!d) return;
      const attrs = { d, fill: 'none', stroke: SERIES[key].color, 'stroke-width': width, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' };
      if (SERIES[key].dash) attrs['stroke-dasharray'] = SERIES[key].dash;
      el('path', attrs, svg);
      markers.push({ color: SERIES[key].color, values: vals, Y });
    });

    // End label on the served forecast: the value people act on.
    if (show.served) {
      for (let i = series.length - 1; i >= 0; i--) {
        if (series[i].forecast_aqi != null) {
          const x = X(xs[i]);
          const y = Y(series[i].forecast_aqi);
          el('circle', { cx: x, cy: y, r: 4.5, fill: 'var(--s1)', stroke: 'var(--card)', 'stroke-width': 2 }, svg);
          el('text', { x: x + 9, y: y + 4, class: 'end-label' }, svg, fmt(series[i].forecast_aqi, 0));
          break;
        }
      }
    }

    const stepIdx = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(box.w / 150))));
    for (let i = 0; i < xs.length; i += stepIdx) {
      const x = X(xs[i]);
      el('line', { x1: x, x2: x, y1: box.y + box.h, y2: box.y + box.h + 4, stroke: 'var(--axis)' }, svg);
      el('text', { x, y: box.y + box.h + 18, 'text-anchor': anchorFor(i, x, box) }, svg, fmtShortTs(xs[i]));
    }

    attachHover(
      host,
      svg,
      W,
      box,
      xs,
      X,
      (i) => {
        const r = series[i];
        const rows = [];
        if (show.actual) rows.push({ name: 'Actual', color: SERIES.actual.color, value: r.actual_aqi != null ? fmt(r.actual_aqi, 0) + (r.actual_category ? ' · ' + r.actual_category : '') : '–' });
        if (show.served) rows.push({ name: 'Served', color: SERIES.served.color, value: r.forecast_aqi != null ? fmt(r.forecast_aqi, 0) + (r.category ? ' · ' + r.category : '') : '–' });
        if (show.persistence) rows.push({ name: 'Persistence', color: SERIES.persistence.color, dash: SERIES.persistence.dash, value: fmt(r.persistence_aqi, 0) });
        if (show.model) rows.push({ name: 'Raw model', color: SERIES.model.color, dash: SERIES.model.dash, value: fmt(r.model_aqi, 0) });
        if (r.source) rows.push({ name: 'Served by', value: r.source });
        return { title: fmtShortTs(xs[i]) + (r.is_future ? ' · forecast' : ''), rows };
      },
      { y0: box.y, y1: box.y + box.h },
      markers
    );
  }

  /* ---------- Horizontal bars (single series) ---------- */
  function drawHorizontalBars(host, items, opts) {
    host.textContent = '';
    if (!items || !items.length) {
      h('div', 'empty-msg', host, (opts && opts.emptyText) || 'No data yet.');
      return;
    }
    const o = opts || {};
    const W = fitW(host, 560);
    const rowH = 28;
    const barH = 14;
    const x0 = Math.min(128, Math.round(W * 0.3));
    const H = items.length * rowH + 4;
    const maxV = Math.max.apply(null, items.map((d) => Number(d.value) || 0)) || 1;
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': o.ariaLabel || 'Bar chart' }, host);
    const fv = (v) => (o.fmtVal ? o.fmtVal(v) : fmt(v, 3));
    items.forEach((it, idx) => {
      const yMid = idx * rowH + rowH / 2;
      const val = Number(it.value) || 0;
      const barW = Math.max(2, (val / maxV) * (W - x0 - 64));
      const g = el('g', { class: 'bar-row', tabindex: 0, role: 'img', 'aria-label': it.label + ': ' + fv(val) }, svg);
      el('rect', { x: x0, y: yMid - barH / 2, width: W - x0 - 64, height: barH, rx: 4, fill: 'var(--muted-bg)', opacity: 0.6 }, g);
      el('path', { d: barPath(x0, yMid - barH / 2, barW, barH), fill: idx < 3 ? 'var(--s1)' : 'var(--quiet)' }, g);
      el('text', { x: x0 - 10, y: yMid + 4, 'text-anchor': 'end', class: 'lab' }, g, it.label);
      el('text', { x: x0 + barW + 8, y: yMid + 4, class: 'strong' }, g, fv(val));
      el('title', {}, g, it.label + ': ' + fv(val));
    });
  }

  /* ---------- Grouped columns (monthly MAE by strategy) ---------- */
  const METHODS = [
    { key: 'pipeline', name: 'This pipeline', color: 'var(--s1)' },
    { key: 'scheduled', name: 'Fixed monthly', color: 'var(--s2)' },
    { key: 'persistence', name: 'Persistence', color: 'var(--s3)' },
    { key: 'static', name: 'Never retrained', color: 'var(--quiet)' },
  ];

  function drawGroupedBars(host, monthlyRows) {
    host.textContent = '';
    if (!monthlyRows || !monthlyRows.length) {
      h('div', 'empty-msg', host, 'Monthly comparison appears once a month of forecasts has been scored.');
      return;
    }
    const methods = METHODS.filter((m) => monthlyRows.some((r) => r[m.key] != null));
    if (!methods.length) {
      h('div', 'empty-msg', host, 'No monthly comparison metrics yet.');
      return;
    }

    const W = fitW(host, 560);
    const H = 240;
    const box = { x: 36, y: 12, w: W - 36 - 8, h: 190 };
    let maxMae = 0;
    monthlyRows.forEach((r) => methods.forEach((m) => {
      if (r[m.key] != null && r[m.key] > maxMae) maxMae = r[m.key];
    }));
    const ticks = niceTicks(maxMae, 4);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(v, top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Monthly mean absolute error by strategy' }, host);
    grid(svg, box, ticks, Y);

    const groupW = box.w / monthlyRows.length;
    const gap = 2;
    const barW = Math.max(2, Math.min(24, (groupW * 0.72 - gap * (methods.length - 1)) / methods.length));
    const xs = monthlyRows.map((_, i) => box.x + (i + 0.5) * groupW);
    const labelEvery = Math.ceil(monthlyRows.length / Math.max(2, Math.floor(box.w / 64)));

    monthlyRows.forEach((row, idx) => {
      const groupSpan = barW * methods.length + gap * (methods.length - 1);
      const gx = box.x + idx * groupW + (groupW - groupSpan) / 2;
      methods.forEach((m, mi) => {
        const val = row[m.key];
        if (val == null) return;
        const yVal = Y(val);
        el('path', { d: colPath(gx + mi * (barW + gap), yVal, barW, Math.max(1, box.y + box.h - yVal)), fill: m.color }, svg);
      });
      if (idx % labelEvery === 0) {
        const ms = parseTs(String(row.month) + '-01 00:00:00');
        const lbl = Number.isNaN(ms) ? String(row.month) : MONTHS[new Date(ms).getUTCMonth()] + ' ' + String(new Date(ms).getUTCFullYear()).slice(2);
        el('text', { x: xs[idx], y: box.y + box.h + 18, 'text-anchor': 'middle' }, svg, lbl);
      }
    });

    attachHover(
      host,
      svg,
      W,
      box,
      xs,
      (x) => x,
      (i) => {
        const r = monthlyRows[i];
        return {
          title: r.month + ' · ' + (r.n || '–') + ' forecasts · mean AQI ' + fmt(r.mean_aqi, 0),
          rows: methods.map((m) => ({ name: m.name, color: m.color, value: fmt(r[m.key], 1) })),
        };
      },
      { y0: box.y, y1: box.y + box.h }
    );
  }

  /* ---------- Cycle signals: three small multiples, shared x ---------- */
  function drawCycleSignals(host, cycles, thresholds) {
    host.textContent = '';
    if (!cycles || !cycles.length) {
      h('div', 'empty-msg', host, 'No monitoring cycles yet. Each run-cycle adds one day here.');
      return;
    }
    const thr = thresholds || {};
    const xs = cycles.map((c) => parseTs(c.date + ' 00:00:00'));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 86400000 : xs[xs.length - 1];
    const W = fitW(host, 1080);
    const ph = 86;
    const gap = 40;
    const box = { x: 40, w: W - 40 - (W < 640 ? 64 : 84) };
    const panels = [
      {
        title: 'Error ratio · model error ÷ expected error',
        key: 'ratio',
        cap: 3.5,
        rules: [
          { v: thr.perf_warn, c: 'var(--warn)', l: 'warn' },
          { v: thr.perf_critical, c: 'var(--critical)', l: 'critical' },
        ],
      },
      {
        title: 'Input drift · importance-weighted',
        key: 'drift',
        cap: 1.0,
        rules: [
          { v: thr.drift_warn, c: 'var(--warn)', l: 'warn' },
          { v: thr.drift_alert, c: 'var(--critical)', l: 'alert' },
        ],
      },
      {
        title: 'CUSUM · accumulated excess error',
        key: 'cusum',
        rules: [{ v: thr.cusum_alarm, c: 'var(--critical)', l: 'alarm' }],
      },
    ];
    const bottomY = 24 + (panels.length - 1) * (ph + gap) + ph;
    const H = bottomY + 26;
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Monitoring signals per cycle' }, host);
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;

    // Retrain decisions: faint accent rules through every panel.
    cycles.forEach((c, i) => {
      if (c.action !== 'retrain') return;
      el('line', { x1: X(xs[i]), x2: X(xs[i]), y1: 22, y2: bottomY, stroke: 'var(--accent)', 'stroke-width': 1, opacity: 0.28 }, svg);
    });

    const markers = [];
    panels.forEach((p, idx) => {
      const y0 = 24 + idx * (ph + gap);
      let max = 0;
      cycles.forEach((c) => {
        if (c[p.key] != null && c[p.key] > max) max = c[p.key];
      });
      p.rules.forEach((r) => {
        if (r.v != null && r.v > max) max = r.v;
      });
      if (p.cap && max > 0) max = Math.min(max, p.cap);
      const ticks = niceTicks(max, 3);
      const top = ticks[ticks.length - 1];
      const Y = (v) => y0 + ph - (Math.min(Math.max(v, 0), top) / top) * ph;

      el('text', { x: box.x, y: y0 - 9, class: 'lab' }, svg, p.title);
      grid(svg, { x: box.x, w: box.w }, ticks, Y);
      let lastLabelY = Infinity;
      p.rules
        .slice()
        .sort((a, b) => (a.v || 0) - (b.v || 0))
        .forEach((r) => {
          if (r.v == null) return;
          const ry = Y(r.v);
          el('line', { x1: box.x, x2: box.x + box.w, y1: ry, y2: ry, stroke: r.c, 'stroke-width': 1, 'stroke-dasharray': '4 3' }, svg);
          const ly = Math.min(ry + 4, lastLabelY - 13);
          el('text', { x: box.x + box.w + 8, y: ly }, svg, r.l + ' ' + r.v);
          lastLabelY = ly;
        });
      const vals = cycles.map((c) => c[p.key]);
      const dPath = pathOf(xs, vals, X, Y);
      if (dPath) el('path', { d: dPath, fill: 'none', stroke: 'var(--series-ink)', 'stroke-width': 2, 'stroke-linejoin': 'round' }, svg);
      markers.push({ color: 'var(--series-ink)', values: vals, Y });
    });

    const stepIdx = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(box.w / 120))));
    for (let i = 0; i < xs.length; i += stepIdx) {
      el('text', { x: X(xs[i]), y: bottomY + 18, 'text-anchor': anchorFor(i, X(xs[i]), box) }, svg, fmtDay(xs[i]));
    }
    const retrains = cycles.filter((c) => c.action === 'retrain').length;
    if (retrains && W >= 640) {
      el('line', { x1: box.x + box.w - 136, x2: box.x + box.w - 122, y1: 6, y2: 6, stroke: 'var(--accent)', 'stroke-width': 2 }, svg);
      el('text', { x: box.x + box.w, y: 10, 'text-anchor': 'end' }, svg, retrains + ' retrain' + (retrains === 1 ? '' : 's'));
    }

    attachHover(
      host,
      svg,
      W,
      box,
      xs,
      X,
      (i) => {
        const c = cycles[i];
        return {
          title: c.date + ' · champion ' + (c.champion || '–'),
          rows: [
            { name: 'Decision', value: c.action },
            { name: 'Error ratio', value: fmt(c.ratio, 2) },
            { name: 'Drift', value: fmt(c.drift, 2) },
            { name: 'CUSUM', value: fmt(c.cusum, 2) },
          ],
        };
      },
      { y0: 24, y1: bottomY },
      markers
    );
  }

  /* ---------- Batch trajectory ---------- */
  function drawBatchChart(host, predictions) {
    host.textContent = '';
    if (!predictions || !predictions.length) {
      h('div', 'empty-msg', host, 'Score a batch to see the predicted trajectory against current AQI.');
      return;
    }
    const xs = predictions.map((r) => parseTs(r.made_at));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 3600000 : xs[xs.length - 1];
    const W = fitW(host, 640);
    const H = 230;
    const box = { x: 36, y: 14, w: W - 36 - 12, h: 180 };
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;
    let maxAqi = 100;
    predictions.forEach((p) => {
      if (p.aqi != null && p.aqi > maxAqi) maxAqi = p.aqi;
      if (p.current_aqi != null && p.current_aqi > maxAqi) maxAqi = p.current_aqi;
    });
    const ticks = niceTicks(Math.min(500, Math.ceil(maxAqi * 1.08)), 4);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(Math.max(v, 0), top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Batch predicted AQI against current AQI' }, host);
    grid(svg, box, ticks, Y);
    const curVals = predictions.map((r) => r.current_aqi);
    const predVals = predictions.map((r) => r.aqi);
    const pCur = pathOf(xs, curVals, X, Y);
    if (pCur) el('path', { d: pCur, fill: 'none', stroke: 'var(--series-ink)', 'stroke-width': 1.75, 'stroke-dasharray': '5 4' }, svg);
    const pPred = pathOf(xs, predVals, X, Y);
    if (pPred) el('path', { d: pPred, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2, 'stroke-linejoin': 'round' }, svg);

    const stepIdx = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(box.w / 140))));
    for (let i = 0; i < xs.length; i += stepIdx) {
      el('text', { x: X(xs[i]), y: box.y + box.h + 18, 'text-anchor': anchorFor(i, X(xs[i]), box) }, svg, fmtShortTs(xs[i]));
    }

    attachHover(
      host,
      svg,
      W,
      box,
      xs,
      X,
      (i) => {
        const r = predictions[i];
        return {
          title: fmtHumanTs(r.made_at) + ' → ' + fmtHumanTs(r.target_time),
          rows: [
            { name: 'Predicted', color: 'var(--s1)', value: fmt(r.aqi, 0) + (r.category ? ' · ' + r.category : '') },
            { name: 'Current', color: 'var(--series-ink)', dash: '5 4', value: fmt(r.current_aqi, 0) },
            { name: 'Inputs', value: r.inputs_clean ? 'Clean' : 'Flagged' },
          ],
        };
      },
      { y0: box.y, y1: box.y + box.h },
      [
        { color: 'var(--s1)', values: predVals, Y },
        { color: 'var(--series-ink)', values: curVals, Y },
      ]
    );
  }

  global.AqiCharts = {
    CPCB_CATEGORIES,
    SERIES,
    METHODS,
    catColor,
    catFor,
    fmt,
    parseTs,
    fmtShortTs,
    fmtHumanTs,
    drawAqiGauge,
    drawSpark,
    drawForecastChart,
    drawHorizontalBars,
    drawGroupedBars,
    drawCycleSignals,
    drawBatchChart,
  };
})(window);
