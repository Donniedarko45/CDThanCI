(function (global) {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';

  const CPCB_CATEGORIES = [
    { name: 'Good', min: 0, max: 50, range: '0–50', color: 'var(--cat-good)' },
    { name: 'Satisfactory', min: 50, max: 100, range: '51–100', color: 'var(--cat-sat)' },
    { name: 'Moderate', min: 100, max: 200, range: '101–200', color: 'var(--cat-mod)' },
    { name: 'Poor', min: 200, max: 300, range: '201–300', color: 'var(--cat-poor)' },
    { name: 'Very poor', min: 300, max: 400, range: '301–400', color: 'var(--cat-vpoor)' },
    { name: 'Severe', min: 400, max: 500, range: '401–500', color: 'var(--cat-sev)' },
  ];

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
    return (v == null || Number.isNaN(Number(v))) ? '–' : Number(v).toFixed(d == null ? 1 : d);
  }

  function catColor(catName) {
    const found = CPCB_CATEGORIES.find(
      (c) => c.name.toLowerCase() === String(catName || '').toLowerCase()
    );
    return found ? found.color : 'var(--cat-unk)';
  }

  function parseTs(ts) {
    const s = String(ts || '').trim();
    if (!s) return NaN;
    const iso = s.includes('T') ? s : s.replace(' ', 'T');
    return Date.parse(iso.endsWith('Z') || /[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + 'Z');
  }

  function fmtShortTs(ms) {
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return '–';
    return d.toISOString().slice(5, 16).replace('T', ' ');
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

  function attachHover(host, svg, W, box, xs, X, rowBuilder, yRange) {
    if (!xs.length) return;
    const tip = h('div', 'tip', host);
    const hair = el(
      'line',
      {
        y1: yRange.y0,
        y2: yRange.y1,
        stroke: 'var(--muted)',
        'stroke-width': 1,
        'stroke-dasharray': '3 3',
        visibility: 'hidden',
      },
      svg
    );
    const hit = el(
      'rect',
      {
        x: box.x,
        y: yRange.y0,
        width: Math.max(1, box.w),
        height: Math.max(1, yRange.y1 - yRange.y0),
        fill: 'transparent',
        tabindex: 0,
      },
      svg
    );
    let cur = xs.length - 1;

    function show(i) {
      const x = X(xs[i]);
      hair.setAttribute('x1', x);
      hair.setAttribute('x2', x);
      hair.setAttribute('visibility', 'visible');
      tip.textContent = '';
      const info = rowBuilder(i);
      h('div', 't', tip, info.title);
      info.rows.forEach((r) => {
        const row = h('div', 'r', tip);
        const left = h('span', null, row);
        if (r.color) {
          const k = h('i', 'key', left);
          k.style.borderColor = r.color;
        }
        left.appendChild(document.createTextNode(r.name));
        h('b', null, row, r.value);
      });
      tip.style.display = 'block';
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? rect.width / W : 1;
      const left = x * scale + 12;
      const wTip = tip.offsetWidth || 180;
      tip.style.left =
        (left + wTip > rect.width ? Math.max(4, x * scale - wTip - 12) : left) + 'px';
      tip.style.top = Math.max(4, yRange.y0 * scale) + 'px';
    }

    function hide() {
      tip.style.display = 'none';
      hair.setAttribute('visibility', 'hidden');
    }

    hit.addEventListener('pointermove', (ev) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      let best = 0;
      let bestDist = Infinity;
      xs.forEach((v, idx) => {
        const d = Math.abs(X(v) - px);
        if (d < bestDist) {
          bestDist = d;
          best = idx;
        }
      });
      cur = best;
      show(best);
    });
    hit.addEventListener('pointerleave', hide);
    hit.addEventListener('focus', () => show(cur));
    hit.addEventListener('blur', hide);
    hit.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowLeft') {
        cur = Math.max(0, cur - 1);
        show(cur);
      }
      if (ev.key === 'ArrowRight') {
        cur = Math.min(xs.length - 1, cur + 1);
        show(cur);
      }
    });
  }

  function arcPath(cx, cy, r, startAngle, endAngle) {
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const large = Math.abs(endAngle - startAngle) > Math.PI ? 1 : 0;
    return 'M ' + x1.toFixed(2) + ' ' + y1.toFixed(2) + ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 ' + x2.toFixed(2) + ' ' + y2.toFixed(2);
  }

  function drawAqiGauge(host, opts) {
    host.textContent = '';
    const aqi = opts && opts.aqi != null ? Number(opts.aqi) : null;
    const category = (opts && opts.category) || 'Unknown';
    const max = (opts && opts.max) || 500;
    const W = 260;
    const H = 152;
    const cx = 130;
    const cy = 122;
    const r = 92;
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'CPCB AQI gauge' }, host);

    // Background segmented CPCB bands from PI to 2*PI
    CPCB_CATEGORIES.forEach((cat) => {
      const a0 = Math.PI + (cat.min / max) * Math.PI;
      const a1 = Math.PI + (cat.max / max) * Math.PI;
      el(
        'path',
        {
          d: arcPath(cx, cy, r, a0, a1),
          fill: 'none',
          stroke: cat.color,
          'stroke-width': 11,
          opacity: 0.22,
        },
        svg
      );
    });

    if (aqi != null && !Number.isNaN(aqi)) {
      const clamped = Math.max(0, Math.min(max, aqi));
      const endA = Math.PI + (clamped / max) * Math.PI;
      if (clamped > 0.5) {
        el(
          'path',
          {
            d: arcPath(cx, cy, r, Math.PI, endA),
            fill: 'none',
            stroke: catColor(category),
            'stroke-width': 13,
            'stroke-linecap': 'round',
          },
          svg
        );
      }
      const px = cx + r * Math.cos(endA);
      const py = cy + r * Math.sin(endA);
      el('circle', { cx: px, cy: py, r: 6, fill: catColor(category), stroke: 'var(--surface)', 'stroke-width': 2 }, svg);
    }

    el(
      'text',
      { x: cx, y: cy - 22, 'text-anchor': 'middle', class: 'strong', style: 'font-size:34px;font-weight:750;' },
      svg,
      aqi != null ? fmt(aqi, 1) : '–'
    );
    el(
      'text',
      { x: cx, y: cy + 2, 'text-anchor': 'middle', class: 'lab', style: 'font-size:12.5px;font-weight:650;' },
      svg,
      category
    );
    el('text', { x: cx - r, y: cy + 18, 'text-anchor': 'middle' }, svg, '0');
    el('text', { x: cx, y: cy - r - 10, 'text-anchor': 'middle' }, svg, '250');
    el('text', { x: cx + r, y: cy + 18, 'text-anchor': 'middle' }, svg, '500');
  }

  function drawForecastChart(host, series, opts) {
    host.textContent = '';
    if (!series || !series.length) {
      h('div', 'empty-msg', host, 'No hourly observations or forecasts available yet.');
      return;
    }
    const o = opts || {};
    const xs = series.map((r) => parseTs(r.timestamp));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 3600000 : xs[xs.length - 1];
    const W = 1120;
    const H = 300;
    const box = { x: 48, y: 18, w: W - 48 - 22, h: 240 };
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;

    let maxAqi = 100;
    series.forEach((r) => {
      [
        o.showActual !== false ? r.actual_aqi : null,
        o.showServed !== false ? r.forecast_aqi : null,
        o.showModel ? r.model_aqi : null,
        o.showPersistence !== false ? r.persistence_aqi : null,
      ].forEach((v) => {
        if (v != null && v > maxAqi) maxAqi = v;
      });
    });

    const ticks = niceTicks(Math.min(500, Math.ceil(maxAqi * 1.1)), 5);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(Math.max(v, 0), top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Hourly actual vs forecast AQI' }, host);

    if (o.showBands) {
      CPCB_CATEGORIES.forEach((cat) => {
        if (cat.min >= top) return;
        const yHi = Y(Math.min(cat.max, top));
        const yLo = Y(cat.min);
        el(
          'rect',
          { x: box.x, y: yHi, width: box.w, height: Math.max(0, yLo - yHi), fill: cat.color, opacity: 0.06 },
          svg
        );
      });
    }

    const firstFutureIdx = series.findIndex((r) => r.is_future);
    if (firstFutureIdx >= 0) {
      const xNow = X(xs[Math.max(0, firstFutureIdx - 1)]);
      el('rect', { x: xNow, y: box.y, width: Math.max(0, box.x + box.w - xNow), height: box.h, fill: 'var(--band)' }, svg);
      el('line', { x1: xNow, x2: xNow, y1: box.y, y2: box.y + box.h, stroke: 'var(--s1)', 'stroke-width': 1, 'stroke-dasharray': '4 3' }, svg);
      el('text', { x: Math.min(xNow + 6, box.x + box.w - 92), y: box.y + 13, class: 'lab' }, svg, 'Forecast horizon');
    }

    ticks.forEach((v) => {
      el('line', { x1: box.x, x2: box.x + box.w, y1: Y(v), y2: Y(v), stroke: v === 0 ? 'var(--axis)' : 'var(--grid)' }, svg);
      el('text', { x: box.x - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg, String(v));
    });
    [50, 100, 200, 300, 400].forEach((edge, idx) => {
      if (edge <= top) {
        el('rect', { x: box.x - 4, y: Y(edge) - 2, width: 4, height: 4, rx: 1, fill: CPCB_CATEGORIES[idx].color }, svg);
      }
    });

    if (o.showPersistence !== false) {
      const pPers = pathOf(xs, series.map((r) => r.persistence_aqi), X, Y);
      if (pPers) el('path', { d: pPers, fill: 'none', stroke: 'var(--s3)', 'stroke-width': 1.5, 'stroke-dasharray': '4 4', opacity: 0.85 }, svg);
    }
    if (o.showModel) {
      const pMod = pathOf(xs, series.map((r) => r.model_aqi), X, Y);
      if (pMod) el('path', { d: pMod, fill: 'none', stroke: 'var(--s4)', 'stroke-width': 1.7, 'stroke-dasharray': '2 3' }, svg);
    }
    if (o.showActual !== false) {
      const pAct = pathOf(xs, series.map((r) => r.actual_aqi), X, Y);
      if (pAct) el('path', { d: pAct, fill: 'none', stroke: 'var(--ink2)', 'stroke-width': 2, 'stroke-linejoin': 'round' }, svg);
    }
    if (o.showServed !== false) {
      const pFc = pathOf(xs, series.map((r) => r.forecast_aqi), X, Y);
      if (pFc) el('path', { d: pFc, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2.3, 'stroke-linejoin': 'round' }, svg);
    }

    const stepIdx = Math.max(1, Math.floor(xs.length / 6));
    for (let i = 0; i < xs.length; i += stepIdx) {
      const x = X(xs[i]);
      el('line', { x1: x, x2: x, y1: box.y + box.h, y2: box.y + box.h + 4, stroke: 'var(--axis)' }, svg);
      el('text', { x: x, y: box.y + box.h + 18, 'text-anchor': i === 0 ? 'start' : 'middle' }, svg, fmtShortTs(xs[i]));
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
        return {
          title: r.timestamp + (r.is_future ? ' (Future)' : ''),
          rows: [
            { name: 'Actual AQI', color: 'var(--ink2)', value: r.actual_aqi != null ? fmt(r.actual_aqi) + ' · ' + (r.actual_category || '') : '–' },
            { name: 'Served Forecast', color: 'var(--s1)', value: r.forecast_aqi != null ? fmt(r.forecast_aqi) + ' · ' + (r.category || '') : '–' },
            { name: 'Raw Model', color: 'var(--s4)', value: fmt(r.model_aqi) },
            { name: 'Persistence', color: 'var(--s3)', value: fmt(r.persistence_aqi) },
            { name: 'Served by', value: r.source || '–' },
          ],
        };
      },
      { y0: box.y, y1: box.y + box.h }
    );
  }

  function drawHorizontalBars(host, items, opts) {
    host.textContent = '';
    if (!items || !items.length) {
      h('div', 'empty-msg', host, (opts && opts.emptyText) || 'No data available yet.');
      return;
    }
    const o = opts || {};
    const W = 520;
    const rowH = 26;
    const x0 = 135;
    const H = items.length * rowH + 8;
    const maxV = Math.max.apply(null, items.map((d) => Number(d.value) || 0)) || 1;
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': o.ariaLabel || 'Horizontal bar chart' }, host);
    el('line', { x1: x0, x2: x0, y1: 2, y2: H - 2, stroke: 'var(--axis)' }, svg);
    items.forEach((it, idx) => {
      const y = idx * rowH + rowH / 2;
      const val = Number(it.value) || 0;
      const barW = Math.max(2, (val / maxV) * (W - x0 - 65));
      el('text', { x: x0 - 8, y: y + 4, 'text-anchor': 'end', class: 'lab' }, svg, it.label);
      const rect = el('rect', { x: x0, y: y - 7, width: barW, height: 14, rx: 3, fill: it.color || 'var(--s1)' }, svg);
      el('title', {}, rect, it.label + ': ' + (o.fmtVal ? o.fmtVal(val) : fmt(val, 3)));
      el('text', { x: x0 + barW + 6, y: y + 4, class: 'strong' }, svg, o.fmtVal ? o.fmtVal(val) : fmt(val, 3));
    });
  }

  function drawGroupedBars(host, monthlyRows) {
    host.textContent = '';
    if (!monthlyRows || !monthlyRows.length) {
      h('div', 'empty-msg', host, 'No monthly accuracy comparison available yet.');
      return;
    }
    const methods = [
      { key: 'pipeline', name: 'This pipeline', color: 'var(--s1)' },
      { key: 'scheduled', name: 'Fixed monthly', color: 'var(--s2)' },
      { key: 'persistence', name: 'Persistence', color: 'var(--s3)' },
      { key: 'static', name: 'Never retrained', color: 'var(--quiet)' },
    ].filter((m) => monthlyRows.some((r) => r[m.key] != null));

    if (!methods.length) {
      h('div', 'empty-msg', host, 'No monthly comparison metrics available.');
      return;
    }

    const W = 560;
    const H = 225;
    const box = { x: 42, y: 14, w: W - 42 - 12, h: 170 };
    let maxMae = 0;
    monthlyRows.forEach((r) => methods.forEach((m) => { if (r[m.key] != null && r[m.key] > maxMae) maxMae = r[m.key]; }));
    const ticks = niceTicks(maxMae, 4);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(v, top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Monthly MAE comparison by method' }, host);
    ticks.forEach((tv) => {
      el('line', { x1: box.x, x2: box.x + box.w, y1: Y(tv), y2: Y(tv), stroke: tv === 0 ? 'var(--axis)' : 'var(--grid)' }, svg);
      el('text', { x: box.x - 6, y: Y(tv) + 4, 'text-anchor': 'end' }, svg, String(tv));
    });

    const groupW = box.w / monthlyRows.length;
    const barW = Math.max(2, Math.min(10, (groupW - 6) / methods.length));
    const xs = monthlyRows.map((_, i) => box.x + (i + 0.5) * groupW);

    monthlyRows.forEach((row, idx) => {
      const gx = box.x + idx * groupW + (groupW - barW * methods.length) / 2;
      methods.forEach((m, mi) => {
        const val = row[m.key];
        if (val == null) return;
        const yVal = Y(val);
        el(
          'rect',
          { x: gx + mi * barW, y: yVal, width: Math.max(1.5, barW - 1), height: Math.max(1, box.y + box.h - yVal), rx: 1.5, fill: m.color },
          svg
        );
      });
      if (monthlyRows.length <= 12 || idx % 2 === 0) {
        el('text', { x: xs[idx], y: box.y + box.h + 15, 'text-anchor': 'middle' }, svg, String(row.month).slice(2));
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
          title: r.month + ' (n=' + (r.n || '–') + ', mean AQI ' + fmt(r.mean_aqi, 0) + ')',
          rows: methods.map((m) => ({ name: m.name, color: m.color, value: fmt(r[m.key], 1) })),
        };
      },
      { y0: box.y, y1: box.y + box.h }
    );
  }

  function drawCycleSignals(host, cycles, thresholds) {
    host.textContent = '';
    if (!cycles || !cycles.length) {
      h('div', 'empty-msg', host, 'No monitoring cycles recorded yet.');
      return;
    }
    const thr = thresholds || {};
    const xs = cycles.map((c) => parseTs(c.date + ' 00:00:00'));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 86400000 : xs[xs.length - 1];
    const W = 1080;
    const ph = 90;
    const gap = 36;
    const box = { x: 44, w: W - 44 - 92 };
    const panels = [
      {
        title: 'Accuracy ratio (model error / expected error)',
        key: 'ratio',
        cap: 3.5,
        rules: [
          { v: thr.perf_warn, c: 'var(--warn)', l: 'warn' },
          { v: thr.perf_critical, c: 'var(--critical)', l: 'crit' },
        ],
      },
      {
        title: 'Input drift score (importance-weighted)',
        key: 'drift',
        cap: 1.0,
        rules: [
          { v: thr.drift_warn, c: 'var(--warn)', l: 'warn' },
          { v: thr.drift_alert, c: 'var(--critical)', l: 'alert' },
        ],
      },
      {
        title: 'Accumulated excess error (CUSUM)',
        key: 'cusum',
        rules: [{ v: thr.cusum_alarm, c: 'var(--critical)', l: 'alarm' }],
      },
    ];
    const H = panels.length * (ph + gap) + 8;
    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Cycle monitoring signals' }, host);
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;

    panels.forEach((p, idx) => {
      const y0 = 20 + idx * (ph + gap);
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

      el('text', { x: box.x, y: y0 - 6, class: 'lab' }, svg, p.title);
      ticks.forEach((tv) => {
        el('line', { x1: box.x, x2: box.x + box.w, y1: Y(tv), y2: Y(tv), stroke: tv === 0 ? 'var(--axis)' : 'var(--grid)' }, svg);
        el('text', { x: box.x - 6, y: Y(tv) + 4, 'text-anchor': 'end' }, svg, String(tv));
      });
      let lastLabelY = Infinity;
      p.rules
        .slice()
        .sort((a, b) => (a.v || 0) - (b.v || 0))
        .forEach((r) => {
          if (r.v == null) return;
          const ry = Y(r.v);
          el('line', { x1: box.x, x2: box.x + box.w, y1: ry, y2: ry, stroke: r.c, 'stroke-dasharray': '3 3' }, svg);
          const ly = Math.min(ry + 3, lastLabelY - 12);
          el('text', { x: box.x + box.w + 6, y: ly, class: 'lab' }, svg, r.l + ' ' + r.v);
          lastLabelY = ly;
        });
      const dPath = pathOf(xs, cycles.map((c) => c[p.key]), X, Y);
      if (dPath) el('path', { d: dPath, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 1.9 }, svg);
    });

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
          title: c.date + ' (' + (c.champion || '–') + ')',
          rows: [
            { name: 'Decision', value: c.action },
            { name: 'Accuracy ratio', color: 'var(--s1)', value: fmt(c.ratio, 2) },
            { name: 'Drift score', color: 'var(--s1)', value: fmt(c.drift, 2) },
            { name: 'CUSUM', color: 'var(--s1)', value: fmt(c.cusum, 2) },
          ],
        };
      },
      { y0: 20, y1: H - gap + 8 }
    );
  }

  function drawBatchChart(host, predictions) {
    host.textContent = '';
    if (!predictions || !predictions.length) {
      h('div', 'empty-msg', host, 'Run batch scoring to preview predicted AQI trajectory.');
      return;
    }
    const xs = predictions.map((r) => parseTs(r.made_at));
    const t0 = xs[0];
    const t1 = xs[xs.length - 1] === t0 ? t0 + 3600000 : xs[xs.length - 1];
    const W = 760;
    const H = 220;
    const box = { x: 44, y: 16, w: W - 44 - 16, h: 170 };
    const X = (t) => box.x + ((t - t0) / (t1 - t0)) * box.w;
    let maxAqi = 100;
    predictions.forEach((p) => {
      if (p.aqi != null && p.aqi > maxAqi) maxAqi = p.aqi;
      if (p.current_aqi != null && p.current_aqi > maxAqi) maxAqi = p.current_aqi;
    });
    const ticks = niceTicks(Math.min(500, Math.ceil(maxAqi * 1.1)), 4);
    const top = ticks[ticks.length - 1];
    const Y = (v) => box.y + box.h - (Math.min(Math.max(v, 0), top) / top) * box.h;

    const svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Batch prediction trajectory' }, host);
    ticks.forEach((tv) => {
      el('line', { x1: box.x, x2: box.x + box.w, y1: Y(tv), y2: Y(tv), stroke: tv === 0 ? 'var(--axis)' : 'var(--grid)' }, svg);
      el('text', { x: box.x - 6, y: Y(tv) + 4, 'text-anchor': 'end' }, svg, String(tv));
    });
    const pCur = pathOf(xs, predictions.map((r) => r.current_aqi), X, Y);
    if (pCur) el('path', { d: pCur, fill: 'none', stroke: 'var(--ink2)', 'stroke-width': 1.6, 'stroke-dasharray': '3 3' }, svg);
    const pPred = pathOf(xs, predictions.map((r) => r.aqi), X, Y);
    if (pPred) el('path', { d: pPred, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2.2 }, svg);

    const stepIdx = Math.max(1, Math.floor(xs.length / 5));
    for (let i = 0; i < xs.length; i += stepIdx) {
      el('text', { x: X(xs[i]), y: box.y + box.h + 16, 'text-anchor': i === 0 ? 'start' : 'middle' }, svg, fmtShortTs(xs[i]));
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
          title: 'Made ' + r.made_at + ' -> ' + r.target_time,
          rows: [
            { name: 'Predicted AQI', color: 'var(--s1)', value: fmt(r.aqi) + ' · ' + (r.category || '') },
            { name: 'Current AQI', color: 'var(--ink2)', value: fmt(r.current_aqi) },
            { name: 'Clean inputs', value: r.inputs_clean ? 'Yes' : 'Flagged' },
          ],
        };
      },
      { y0: box.y, y1: box.y + box.h }
    );
  }

  global.AqiCharts = {
    CPCB_CATEGORIES,
    catColor,
    fmt,
    drawAqiGauge,
    drawForecastChart,
    drawHorizontalBars,
    drawGroupedBars,
    drawCycleSignals,
    drawBatchChart,
  };
})(window);
