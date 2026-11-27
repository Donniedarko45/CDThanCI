(function () {
  'use strict';

  const C = window.AqiCharts;
  const $ = (id) => document.getElementById(id);

  const ROUTES = ['overview', 'diagnostics', 'simulator', 'registry', 'pipeline'];
  const ROUTE_TITLES = {
    overview: 'Overview',
    diagnostics: 'Drift & health',
    simulator: 'What-if & batch',
    registry: 'Registry & audit',
    pipeline: 'Architecture & config',
  };

  const PLAYGROUND_FIELDS = [
    { key: 'pm25', label: 'PM2.5', unit: 'µg/m³', min: 0, max: 500, step: 1, def: 65, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'pm10', label: 'PM10', unit: 'µg/m³', min: 0, max: 600, step: 1, def: 120, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'o3', label: 'O₃', unit: 'µg/m³', min: 0, max: 350, step: 1, def: 45, pLo: 0, pHi: 800, scale: true, stuck: true },
    { key: 'no2', label: 'NO₂', unit: 'µg/m³', min: 0, max: 300, step: 1, def: 38, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'temperature', label: 'Temperature', unit: '°C', min: -10, max: 50, step: 0.5, def: 24, pLo: -30, pHi: 55, scale: false, stuck: true },
    { key: 'humidity', label: 'Humidity', unit: '%', min: 5, max: 100, step: 1, def: 60, pLo: 0, pHi: 100, scale: false, stuck: true },
    { key: 'wind_speed', label: 'Wind speed', unit: 'm/s', min: 0.2, max: 20, step: 0.1, def: 2.2, pLo: 0, pHi: 60, scale: true, stuck: false },
    { key: 'fc_wind_speed', label: 'Forecast wind', unit: 'm/s', min: 0.2, max: 20, step: 0.1, def: 2.4, pLo: 0, pHi: 60, scale: true, stuck: false },
    { key: 'fc_temperature', label: 'Forecast temp', unit: '°C', min: -10, max: 50, step: 0.5, def: 24.5, pLo: -30, pHi: 55, scale: false, stuck: false },
  ];

  const ACTION_LABEL = { none: 'No action', watch: 'Watch', hold: 'Hold', retrain: 'Retrain' };
  const ACTION_CLS = { none: 'good', watch: 'info', hold: 'warn', retrain: 'warn' };
  const LEVEL_LABEL = { ok: 'On track', warn: 'Elevated', critical: 'Critical', alert: 'Alert', unknown: 'Unknown' };
  const LEVEL_CLS = { ok: 'good', warn: 'warn', critical: 'critical', alert: 'critical' };

  const state = {
    route: 'overview',
    hours: 72,
    data: null,
    analytics: null,
    config: null,
    events: [],
    baseObservations: [],
    obsSource: 'none',
    stuckFaultActive: false,
    batchResult: null,
    autoTimer: null,
    countdownTimer: null,
    nextRefreshAt: 0,
    reqSeq: 0,
    abortCtrl: null,
    driftSort: { key: 'ratio', dir: -1 },
    selectedStageId: null,
  };

  /* ---------- DOM & UI helpers ---------- */
  function h(tag, cls, parent, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function setPill(node, text, cls) {
    node.className = 'pill' + (cls ? ' ' + cls : '');
    node.textContent = text;
  }

  function pill(text, cls) {
    return h('span', 'pill' + (cls ? ' ' + cls : ''), null, text);
  }

  function setMeta(node, label, value, cls) {
    node.className = 'meta-item' + (cls ? ' ' + cls : '');
    node.textContent = '';
    h('span', 'live-dot', node);
    node.appendChild(document.createTextNode(label + ' '));
    h('b', null, node, value);
  }

  function makeCatPill(catName, suffix) {
    const label = catName || 'Unknown';
    const span = h('span', 'pill cat');
    const dot = h('i', 'dot-c', span);
    dot.style.background = C.catColor(label);
    span.appendChild(document.createTextNode(label + (suffix || '')));
    return span;
  }

  function twoLine(main, sub) {
    const frag = document.createDocumentFragment();
    h('span', null, frag, main);
    if (sub) h('span', 'sub', frag, sub);
    return frag;
  }

  function pct(v, d) {
    return v == null || Number.isNaN(Number(v)) ? '–' : (Number(v) * 100).toFixed(d == null ? 0 : d) + '%';
  }

  function showToast(msg, cls) {
    const host = $('toastHost');
    const t = h('div', 'toast' + (cls ? ' ' + cls : ''), host, msg);
    setTimeout(() => {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 3200);
  }

  function setBusy(btn, busy) {
    btn.disabled = busy;
    btn.classList.toggle('is-busy', busy);
    btn.setAttribute('aria-busy', String(busy));
  }

  function downloadBlob(filename, content, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function toCsv(headers, rows) {
    const esc = (v) => {
      const s = v == null ? '' : String(v);
      return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    return [headers.map(esc).join(',')]
      .concat(rows.map((r) => r.map(esc).join(',')))
      .join('\n');
  }

  function renderTable(host, cols, rows, emptyText, opts) {
    host.textContent = '';
    if (!rows || !rows.length) {
      h('div', 'empty-msg', host, emptyText || 'No records yet.');
      return;
    }
    const o = opts || {};
    const tbl = h('table', null, host);
    const trHead = h('tr', null, h('thead', null, tbl));
    cols.forEach((c) => {
      const th = h('th', (c.num ? 'num ' : '') + (c.onSort ? 'sortable' : ''), trHead, c.name);
      th.scope = 'col';
      if (c.sort) th.setAttribute('aria-sort', c.sort);
      if (c.onSort) {
        th.tabIndex = 0;
        th.addEventListener('click', c.onSort);
        th.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            c.onSort();
          }
        });
      }
    });
    const tbody = h('tbody', null, tbl);
    rows.forEach((r, ri) => {
      const tr = h('tr', o.rowClass ? o.rowClass(r, ri) : null, tbody);
      cols.forEach((c) => {
        const td = h('td', [c.num ? 'num' : '', c.wrap ? 'wrap' : ''].join(' ').trim() || null, tr);
        const val = c.get(r);
        if (val instanceof Node) td.appendChild(val);
        else td.textContent = val == null ? '–' : String(val);
      });
    });
  }

  function setMeter(wrapId, fraction, color, ticks) {
    const wrap = $(wrapId);
    if (!wrap) return;
    let bar = wrap.querySelector('i');
    if (!bar) bar = h('i', null, wrap);
    bar.style.width = Math.max(0, Math.min(1, fraction || 0)) * 100 + '%';
    bar.style.background = color || 'var(--ink)';
    wrap.querySelectorAll('b').forEach((b) => b.remove());
    (ticks || []).forEach((t) => {
      if (t == null || Number.isNaN(t) || t <= 0 || t >= 1) return;
      const b = h('b', null, wrap);
      b.style.left = t * 100 + '%';
    });
  }

  /* ---------- Theme ---------- */
  function readSavedTheme() {
    try {
      return localStorage.getItem('aqi-theme');
    } catch (_) {
      return null;
    }
  }

  function effectiveTheme() {
    const saved = readSavedTheme();
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  const ICON_MOON = 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z';
  const ICON_SUN = 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z';

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = $('themeToggleBtn');
    const isDark = theme === 'dark';
    btn.setAttribute('aria-pressed', String(isDark));
    btn.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
    const icon = $('themeIcon');
    if (icon) {
      icon.textContent = '';
      const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', isDark ? ICON_SUN : ICON_MOON);
      icon.appendChild(p);
    }
  }

  function toggleTheme() {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem('aqi-theme', next);
    } catch (_) {}
    applyTheme(next);
  }

  applyTheme(effectiveTheme());
  $('themeToggleBtn').addEventListener('click', toggleTheme);

  /* ---------- Drawer & hash routing ---------- */
  function closeDrawer() {
    $('sidebar').classList.remove('open');
    $('drawerBackdrop').classList.remove('open');
    $('menuBtn').setAttribute('aria-expanded', 'false');
  }

  $('menuBtn').addEventListener('click', () => {
    $('sidebar').classList.add('open');
    $('drawerBackdrop').classList.add('open');
    $('menuBtn').setAttribute('aria-expanded', 'true');
    const first = $('navList').querySelector('.nav-item');
    if (first) first.focus();
  });
  $('closeDrawerBtn').addEventListener('click', closeDrawer);
  $('drawerBackdrop').addEventListener('click', closeDrawer);

  function routeFromHash() {
    const raw = (window.location.hash || '#/overview').replace(/^#\/?/, '').toLowerCase();
    return ROUTES.includes(raw) ? raw : 'overview';
  }

  function setActiveRoute(route) {
    state.route = ROUTES.includes(route) ? route : 'overview';
    ROUTES.forEach((r) => {
      const sec = $('view-' + r);
      if (sec) sec.hidden = r !== state.route;
    });
    $('navList')
      .querySelectorAll('.nav-item')
      .forEach((a) => {
        const on = a.getAttribute('data-route') === state.route;
        a.classList.toggle('active', on);
        if (on) a.setAttribute('aria-current', 'page');
        else a.removeAttribute('aria-current');
      });
    const title = ROUTE_TITLES[state.route];
    $('pageTitle').textContent = title;
    $('crumbCurrent').textContent = title;
    document.title = title + ' · AQI Pipeline';
    closeDrawer();
  }

  $('emptyStateAction').addEventListener('click', () => {
    window.location.hash = '#/simulator';
  });

  /* Charts draw at their container width, so redraw when it changes. */
  function redrawCharts() {
    if (!state.data) return;
    const D = state.data;
    if (state.route === 'overview') renderForecastChartOnly();
    if (state.route === 'diagnostics') renderDiagnostics(D, state.analytics);
    if (state.route === 'simulator' && state.batchResult) C.drawBatchChart($('batchChartHost'), state.batchResult.predictions || []);
  }
  let resizeTimer = null;
  let lastWidth = window.innerWidth;
  window.addEventListener('resize', () => {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawCharts, 150);
  });

  window.addEventListener('hashchange', () => {
    setActiveRoute(routeFromHash());
    redrawCharts();
  });
  setActiveRoute(routeFromHash());

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      closeDrawer();
      return;
    }
    const tag = ev.target && ev.target.tagName ? ev.target.tagName.toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key >= '1' && ev.key <= '5') {
      window.location.hash = '#/' + ROUTES[Number(ev.key) - 1];
    } else if (ev.key === 'r' || ev.key === 'R') {
      loadAllData();
      showToast('Refreshing', 'info');
    } else if (ev.key === 't' || ev.key === 'T') {
      toggleTheme();
    }
  });

  /* ---------- CPCB legend ---------- */
  (function buildCpcbLegend() {
    const host = $('cpcbLegend');
    C.CPCB_CATEGORIES.forEach((c) => {
      const s = h('span', null, host);
      h('i', null, s).style.background = c.color;
      s.appendChild(document.createTextNode(c.name + ' ' + c.range));
    });
  })();

  /* ---------- Page head, summary & banners ---------- */
  function forecastStats(D) {
    const fcs = D.forecast && D.forecast.forecasts ? D.forecast.forecasts : [];
    const valid = fcs.filter((f) => f.aqi != null);
    if (!valid.length) return null;
    const peakItem = valid.reduce((a, b) => (b.aqi > a.aqi ? b : a), valid[0]);
    const mean = valid.reduce((a, b) => a + b.aqi, 0) / valid.length;
    return { fcs, valid, peakItem, peak: peakItem.aqi, mean };
  }

  function buildSummary(D) {
    const st = D.status || {};
    if (!st.bootstrapped) {
      return 'No champion model in this run directory yet. Run a simulation or bootstrap the pipeline to start forecasting.';
    }
    const parts = [];
    const cur = st.current;
    if (cur && cur.aqi != null) {
      parts.push(
        'Air is ' + String(cur.category || 'unknown').toLowerCase() + ' at ' + C.fmt(cur.aqi, 0) +
          (cur.dominant ? ', driven by ' + cur.dominant.toUpperCase() : '') + '.'
      );
    }
    const fs = forecastStats(D);
    if (fs) {
      parts.push(
        'The next ' + fs.valid.length + ' hours peak at ' + C.fmt(fs.peak, 0) + ' (' + String(fs.peakItem.category || '').toLowerCase() +
          ') around ' + C.fmtHumanTs(fs.peakItem.target_time) + '.'
      );
    }
    const cyc = st.latest_cycle;
    if (st.safe_mode) {
      parts.push('Safe mode is on, so persistence is being served instead of the model.');
    } else if (cyc) {
      const pf = cyc.performance || {};
      const dr = cyc.drift || {};
      const perfTxt = pf.ratio != null ? 'Forecast error is ' + C.fmt(pf.ratio, 2) + '× expected' : 'Forecast error is not scored yet';
      const driftTxt = dr.level === 'alert' ? 'inputs have drifted' : dr.level === 'warn' ? 'inputs are starting to drift' : 'inputs look stable';
      parts.push(perfTxt + ' and ' + driftTxt + '.');
    }
    return parts.join(' ');
  }

  function renderShell(D) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const dec = cyc && cyc.decision ? cyc.decision : null;
    const meta = st.champion_meta || {};

    $('stationLocation').textContent = st.location || D.location || 'Unknown station';
    $('simBadge').hidden = !D.simulated;

    if (!st.bootstrapped) setPill($('servingPill'), 'Not bootstrapped', '');
    else if (st.safe_mode) setPill($('servingPill'), 'Safe mode · persistence', 'warn');
    else setPill($('servingPill'), 'Model serving', 'info');
    $('pageSummary').textContent = buildSummary(D);

    const champ = st.champion ? st.champion + (meta.kind ? ' · ' + meta.kind : '') : 'none';
    setMeta($('badgeChampion'), 'Champion', champ, st.champion ? 'info' : 'warn');
    setMeta($('badgeShadow'), 'Shadow', st.shadow ? st.shadow + ' · probation' : 'none', st.shadow ? 'warn' : '');
    setMeta($('badgeSafeMode'), 'Safe mode', st.safe_mode ? 'on' : 'off', st.safe_mode ? 'warn' : 'good');
    const action = dec ? dec.action : 'none';
    setMeta($('badgeDecision'), 'Decision', cyc ? (ACTION_LABEL[action] || action) : '–', cyc ? ACTION_CLS[action] || '' : '');
    $('badgeDecision').title = dec ? dec.reason : 'No cycle run yet';
    $('lastObsTime').textContent = st.last_observation ? C.fmtHumanTs(st.last_observation) : '–';
    $('lastCycleTime').textContent = cyc ? String(cyc.cycle_time || '').slice(0, 10) : '–';

    $('sideChampion').textContent = st.champion || 'none';
    $('sideHorizon').textContent = (st.horizon_hours || 24) + 'h';
    $('sideCycles').textContent = String((D.cycles || []).length);
    $('predHorizonLbl').textContent = String(st.horizon_hours || 24);
    $('fcTitle').textContent = 'Next ' + (st.horizon_hours || 24) + ' hours';

    $('emptyStateTitle').textContent = 'Nothing trained in this run directory yet';
    $('emptyStateText').textContent =
      'Run aqi-pipeline simulate for a 15-month demo, or bootstrap then run-cycle for a live station. What-if works now with synthetic sample data.';
    $('emptyStateBanner').hidden = Boolean(st.bootstrapped);

    if (dec && dec.action !== 'none') {
      $('decisionBanner').hidden = false;
      $('decisionBannerTitle').textContent = 'Latest cycle decided to ' + (ACTION_LABEL[dec.action] || dec.action).toLowerCase();
      $('decisionBannerReason').textContent = dec.reason || '';
      $('decisionBannerTime').textContent = String(cyc.cycle_time || '').slice(0, 16);
    } else {
      $('decisionBanner').hidden = true;
    }
  }

  /* ---------- View 1: Overview ---------- */
  const POLLUTANTS = [
    ['PM2.5', 'pm25', 'µg/m³', 0, 60, 120],
    ['PM10', 'pm10', 'µg/m³', 0, 100, 250],
    ['NO₂', 'no2', 'µg/m³', 0, 80, 180],
    ['SO₂', 'so2', 'µg/m³', 0, 80, 380],
    ['CO', 'co', 'mg/m³', 2, 2, 10],
    ['O₃', 'o3', 'µg/m³', 0, 100, 168],
  ];
  const WEATHER = [
    ['Temperature', 'temperature', '°C', 1],
    ['Humidity', 'humidity', '%', 0],
    ['Wind', 'wind_speed', 'm/s', 1],
    ['Rain', 'precipitation', 'mm', 1],
  ];

  function renderReadings(r) {
    const pHost = $('pollutantCards');
    pHost.textContent = '';
    POLLUTANTS.forEach(([label, key, unit, dec, warnAt, highAt]) => {
      const val = r[key];
      const lvl = val == null ? '' : val >= highAt ? 'lvl-high' : val >= warnAt ? 'lvl-warn' : '';
      const card = h('div', 'reading ' + lvl, pHost);
      const k = h('div', 'reading-k', card);
      h('span', null, k, label);
      if (lvl) h('span', 'reading-flag', k, lvl === 'lvl-high' ? 'High' : 'Elevated');
      const v = h('div', 'reading-v', card, val != null ? C.fmt(val, dec) : '–');
      h('small', null, v, unit);
      const bar = h('div', 'reading-bar', card);
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', label + ' at ' + (val != null ? Math.round((val / highAt) * 100) : 0) + '% of the poor breakpoint');
      h('i', null, bar).style.width = val != null ? Math.min(100, (val / highAt) * 100) + '%' : '0%';
    });

    const wHost = $('weatherCards');
    wHost.textContent = '';
    WEATHER.forEach(([label, key, unit, dec]) => {
      const val = r[key];
      const card = h('div', 'reading', wHost);
      h('div', 'reading-k', card, label);
      const v = h('div', 'reading-v', card, val != null ? C.fmt(val, dec) : '–');
      h('small', null, v, unit);
    });
  }

  function renderOverview(D) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const cur = st.current;
    const thr = D.thresholds || {};

    if (cur && cur.aqi != null) {
      C.drawAqiGauge($('aqiGaugeHost'), { aqi: cur.aqi, category: cur.category });
      $('gaugeSub').textContent = C.fmtHumanTs(cur.timestamp);
      $('gaugeFooter').textContent = cur.dominant
        ? 'Dominant pollutant ' + cur.dominant.toUpperCase() + ' · CPCB 24h / 8h averaging'
        : 'CPCB 24h / 8h averaging';
    } else {
      C.drawAqiGauge($('aqiGaugeHost'), { aqi: null });
      $('gaugeSub').textContent = st.last_observation ? C.fmtHumanTs(st.last_observation) : '–';
      $('gaugeFooter').textContent = st.last_observation
        ? 'Not enough valid PM hours in the last day to compute a CPCB AQI.'
        : 'Waiting for the first observations.';
    }

    const r = cur && cur.readings ? cur.readings : {};
    if (cur && cur.qc_notes) setPill($('qcLatestPill'), 'QC · ' + cur.qc_notes, 'warn');
    else setPill($('qcLatestPill'), cur ? 'QC passed' : 'QC –', cur ? 'good' : '');
    renderReadings(r);

    // Forecast tile
    const fs = forecastStats(D);
    const fcs = fs ? fs.fcs : (D.forecast && D.forecast.forecasts) || [];
    $('kpiFcCat').textContent = '';
    if (fs) {
      const v = $('kpiFcVal');
      v.textContent = C.fmt(fs.peak, 0);
      h('small', null, v, 'peak');
      $('kpiFcCat').appendChild(makeCatPill(fs.peakItem.category));
      const sources = Array.from(new Set(fcs.map((f) => f.source).filter(Boolean))).join(' + ') || 'model';
      $('kpiFcMeta').textContent = 'Mean ' + C.fmt(fs.mean, 0) + ' · peak ' + C.fmtHumanTs(fs.peakItem.target_time) + ' · ' + sources;
      C.drawSpark($('kpiFcSpark'), fs.fcs.map((f) => f.aqi), {
        floor: 100,
        hotAt: 200,
        ariaLabel: fs.valid.length + ' hourly forecasts from ' + C.fmt(Math.min.apply(null, fs.valid.map((f) => f.aqi)), 0) + ' to ' + C.fmt(fs.peak, 0),
      });
    } else {
      $('kpiFcVal').textContent = '–';
      $('kpiFcMeta').textContent = st.bootstrapped ? 'Run a cycle to generate forecasts' : 'No trained model yet';
      $('kpiFcSpark').textContent = '';
    }

    // Error tile
    const perf = cyc && cyc.performance ? cyc.performance : null;
    if (perf && perf.ratio != null) {
      const v = $('kpiPerfVal');
      v.textContent = C.fmt(perf.ratio, 2) + '×';
      h('small', null, v, 'expected');
      setPill($('kpiPerfBadge'), LEVEL_LABEL[perf.level] || perf.level, LEVEL_CLS[perf.level] || '');
      $('kpiPerfMeta').textContent = 'MAE ' + C.fmt(perf.mae) + ' vs ' + C.fmt(perf.expected_mae) + ' expected · persistence ' + C.fmt(perf.mae_naive);
      const color = perf.level === 'critical' ? 'var(--critical)' : perf.level === 'warn' ? 'var(--warn)' : 'var(--ink)';
      setMeter('kpiPerfMeter', perf.ratio / 2, color, [0.5]);
    } else {
      $('kpiPerfVal').textContent = '–';
      $('kpiPerfBadge').textContent = '';
      $('kpiPerfBadge').className = '';
      $('kpiPerfMeta').textContent = 'Waiting for forecasts to mature into labels';
      setMeter('kpiPerfMeter', 0, null, [0.5]);
    }

    // Drift tile
    const dr = cyc && cyc.drift ? cyc.drift : null;
    const cu = cyc && cyc.cusum ? cyc.cusum : null;
    $('kpiDriftThr').textContent = 'warn ' + C.fmt(thr.drift_warn, 2) + ' · alert ' + C.fmt(thr.drift_alert, 2);
    if (dr) {
      $('kpiDriftVal').textContent = C.fmt(dr.score, 2);
      const lvl = cu && cu.alarm ? 'critical' : dr.level;
      setPill($('kpiDriftBadge'), (LEVEL_LABEL[dr.level] || dr.level) + (cu && cu.alarm ? ' · CUSUM' : ''), LEVEL_CLS[lvl] || '');
      const n = dr.drifted ? dr.drifted.length : 0;
      $('kpiDriftMeta').textContent = (n ? n + ' feature' + (n === 1 ? '' : 's') + ' drifted' : 'No features drifted') + ' · CUSUM ' + C.fmt(cu ? cu.g : 0, 2);
      const color = dr.level === 'alert' ? 'var(--critical)' : dr.level === 'warn' ? 'var(--warn)' : 'var(--ink)';
      setMeter('kpiDriftMeter', dr.score, color, [thr.drift_warn, thr.drift_alert]);
    } else {
      $('kpiDriftVal').textContent = '–';
      $('kpiDriftBadge').textContent = '';
      $('kpiDriftBadge').className = '';
      $('kpiDriftMeta').textContent = 'No monitoring cycle yet';
      setMeter('kpiDriftMeter', 0, null, [thr.drift_warn, thr.drift_alert]);
    }

    // Forecast chart
    const servedSources = Array.from(new Set(fcs.map((f) => f.source).filter(Boolean)));
    if (st.safe_mode) setPill($('forecastServedBadge'), 'Serving persistence · safe mode', 'warn');
    else if (servedSources.length) setPill($('forecastServedBadge'), 'Serving ' + servedSources.join(' + '), servedSources.includes('persistence') ? 'warn' : 'info');
    else setPill($('forecastServedBadge'), st.bootstrapped ? 'Nothing served yet' : 'No forecast yet', '');

    $('forecastSubtitle').textContent =
      'Last ' + (state.hours >= 168 ? '7 days' : state.hours + ' hours') + ' of hourly station AQI against what was served, then the next ' +
      (st.horizon_hours || 24) + ' hours' + (st.last_observation ? ' from ' + C.fmtHumanTs(st.last_observation) : '') + '.';

    renderForecastChartOnly();

    // Upcoming table
    $('upcomingCount').textContent = String(fcs.length);
    const catHost = $('upcomingCatSummary');
    catHost.textContent = '';
    const counts = {};
    fcs.forEach((f) => {
      const c = f.category || 'Unknown';
      counts[c] = (counts[c] || 0) + 1;
    });
    C.CPCB_CATEGORIES.map((c) => c.name)
      .concat(Object.keys(counts).filter((k) => !C.CPCB_CATEGORIES.some((c) => c.name === k)))
      .forEach((cat) => {
        if (counts[cat]) catHost.appendChild(makeCatPill(cat, ' · ' + counts[cat] + 'h'));
      });

    const peakTarget = fs ? fs.peakItem.target_time : null;
    renderTable(
      $('forecastTableHost'),
      [
        { name: 'Target hour', get: (row) => twoLine(C.fmtHumanTs(row.target_time), 'issued ' + C.fmtHumanTs(row.made_at)) },
        { name: 'Served AQI', num: true, get: (row) => C.fmt(row.aqi, 0) },
        { name: 'Category', get: (row) => makeCatPill(row.category) },
        { name: 'Model', num: true, get: (row) => C.fmt(row.model_aqi, 0) },
        { name: 'Persistence', num: true, get: (row) => C.fmt(row.persistence_aqi, 0) },
        { name: 'Source', get: (row) => row.source || '–' },
        { name: 'Inputs', get: (row) => (row.inputs_clean ? 'Clean' : pill('Flagged', 'warn')) },
      ],
      fcs,
      st.bootstrapped ? 'No upcoming forecasts are being served.' : 'Bootstrap and run a cycle to produce forecasts.',
      { rowClass: (row) => (row.target_time === peakTarget ? 'is-current' : null) }
    );
  }

  function renderForecastChartOnly() {
    if (!state.data) return;
    C.drawForecastChart($('forecastChartHost'), state.data.hourly_series || [], {
      showActual: $('togActual').checked,
      showServed: $('togServed').checked,
      showModel: $('togModel').checked,
      showPersistence: $('togPersistence').checked,
      showBands: $('togBands').checked,
    });
  }

  ['togActual', 'togServed', 'togModel', 'togPersistence', 'togBands'].forEach((id) => {
    $(id).addEventListener('change', renderForecastChartOnly);
  });

  $('exportForecastCsvBtn').addEventListener('click', () => {
    const fcs = state.data && state.data.forecast ? state.data.forecast.forecasts : [];
    if (!fcs.length) {
      showToast('No upcoming forecasts to export', 'warn');
      return;
    }
    const csv = toCsv(
      ['target_time', 'made_at', 'aqi', 'category', 'model_aqi', 'persistence_aqi', 'source', 'inputs_clean'],
      fcs.map((f) => [f.target_time, f.made_at, f.aqi, f.category, f.model_aqi, f.persistence_aqi, f.source, f.inputs_clean])
    );
    downloadBlob('aqi_forecasts.csv', csv, 'text/csv;charset=utf-8');
    showToast('Exported ' + fcs.length + ' forecast rows', 'good');
  });

  /* ---------- View 2: Drift & health ---------- */
  function renderDriftFeatureTable(dr) {
    const colRows = Object.entries((dr && dr.columns) || {}).map(([name, info]) => ({ name, ...info }));
    const { key, dir } = state.driftSort;
    colRows.sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      if (typeof va === 'string' || typeof vb === 'string') return dir * String(va || '').localeCompare(String(vb || ''));
      return dir * ((Number(va) || 0) - (Number(vb) || 0));
    });
    const sortBy = (k) => () => {
      if (state.driftSort.key === k) state.driftSort.dir *= -1;
      else state.driftSort = { key: k, dir: k === 'name' ? 1 : -1 };
      renderDriftFeatureTable(dr);
      const th = $('driftFeatureTable').querySelector('th[aria-sort]');
      if (th) th.focus();
    };
    const arrow = (k) => (state.driftSort.key === k ? (state.driftSort.dir > 0 ? ' ↑' : ' ↓') : '');
    const sortAttr = (k) => (state.driftSort.key === k ? (state.driftSort.dir > 0 ? 'ascending' : 'descending') : null);
    renderTable(
      $('driftFeatureTable'),
      [
        { name: 'Feature' + arrow('name'), sort: sortAttr('name'), onSort: sortBy('name'), get: (r) => r.name },
        { name: 'PSI' + arrow('psi'), sort: sortAttr('psi'), num: true, onSort: sortBy('psi'), get: (r) => C.fmt(r.psi, 3) },
        { name: 'Limit' + arrow('threshold'), sort: sortAttr('threshold'), num: true, onSort: sortBy('threshold'), get: (r) => C.fmt(r.threshold, 3) },
        { name: 'Ratio' + arrow('ratio'), sort: sortAttr('ratio'), num: true, onSort: sortBy('ratio'), get: (r) => C.fmt(r.ratio, 2) },
        { name: 'Novel' + arrow('novel'), sort: sortAttr('novel'), num: true, onSort: sortBy('novel'), get: (r) => pct(r.novel, 1) },
        { name: 'Weight' + arrow('weight'), sort: sortAttr('weight'), num: true, onSort: sortBy('weight'), get: (r) => pct(r.weight, 1) },
        { name: 'Status' + arrow('drifted'), sort: sortAttr('drifted'), onSort: sortBy('drifted'), get: (r) => pill(r.drifted ? 'Drifted' : 'Stable', r.drifted ? 'critical' : 'good') },
      ],
      colRows,
      'Per-feature drift appears after the first monitoring cycle.',
      { rowClass: (r) => (r.drifted ? 'is-current' : null) }
    );
  }

  function renderDiagnostics(D, A) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const thr = D.thresholds || {};

    if (!cyc) {
      $('healthSummaryPill').textContent = 'No cycles recorded';
      setPill($('driftLevelPill'), '–', '');
      $('driftScoreNum').textContent = '–';
      setMeter('driftMeterWrap', 0, null, [thr.drift_warn, thr.drift_alert]);
      $('driftRefMeta').textContent = 'Warn at ' + C.fmt(thr.drift_warn, 2) + ' · alert at ' + C.fmt(thr.drift_alert, 2);
      $('driftedFeaturesList').textContent = '';
      setPill($('perfLevelPill'), '–', '');
      $('perfRatioNum').textContent = '–';
      setMeter('perfMeterWrap', 0, null, [0.5]);
      $('perfDetailMeta').textContent = 'No scored cycle yet';
      $('perfCovMeta').textContent = '';
      setPill($('cusumAlarmPill'), '–', '');
      $('cusumGNum').textContent = '–';
      setMeter('cusumMeterWrap', 0, null, []);
      $('cusumMeta').textContent = 'Alarm at ' + C.fmt(thr.cusum_alarm, 1);
      $('qualityMeta').textContent = 'Quality –';
      renderDriftFeatureTable(null);
    } else {
      const dr = cyc.drift || {};
      const pf = cyc.performance || {};
      const cu = cyc.cusum || {};
      const act = (cyc.decision || {}).action || 'none';
      $('healthSummaryPill').textContent = 'Cycle ' + cyc.cycle_time + ' decided ' + (ACTION_LABEL[act] || act);

      setPill($('driftLevelPill'), LEVEL_LABEL[dr.level] || dr.level || '–', LEVEL_CLS[dr.level] || 'good');
      $('driftScoreNum').textContent = C.fmt(dr.score, 3);
      setMeter('driftMeterWrap', dr.score, dr.level === 'alert' ? 'var(--critical)' : dr.level === 'warn' ? 'var(--warn)' : 'var(--ink)', [thr.drift_warn, thr.drift_alert]);
      $('driftRefMeta').textContent =
        'Against ' + (dr.reference || 'reference') + ' · ' + (dr.reference_rows || 0) + ' rows · warn ' + C.fmt(thr.drift_warn, 2) + ' / alert ' + C.fmt(thr.drift_alert, 2);

      const dList = $('driftedFeaturesList');
      dList.textContent = '';
      const drifted = dr.drifted || [];
      if (!drifted.length) {
        dList.appendChild(pill('No features drifted', 'good'));
      } else {
        drifted.forEach((col) => {
          const info = dr.columns && dr.columns[col] ? dr.columns[col] : {};
          dList.appendChild(pill(col + ' · PSI ' + C.fmt(info.psi, 2) + (info.novel ? ' · ' + Math.round(info.novel * 100) + '% novel' : ''), 'critical'));
        });
      }
      renderDriftFeatureTable(dr);

      setPill($('perfLevelPill'), LEVEL_LABEL[pf.level] || pf.level || 'Unknown', LEVEL_CLS[pf.level] || '');
      const pr = $('perfRatioNum');
      if (pf.ratio != null) {
        pr.textContent = C.fmt(pf.ratio, 2) + '×';
        h('small', null, pr, 'of expected');
      } else pr.textContent = '–';
      setMeter('perfMeterWrap', pf.ratio != null ? pf.ratio / 2 : 0, pf.level === 'critical' ? 'var(--critical)' : pf.level === 'warn' ? 'var(--warn)' : 'var(--ink)', [0.5]);
      $('perfDetailMeta').textContent =
        pf.mae != null ? 'MAE ' + C.fmt(pf.mae) + ' · persistence ' + C.fmt(pf.mae_naive) + ' · relative ' + C.fmt(pf.rel_mae, 2) : 'Not enough matured hours in the window';
      $('perfCovMeta').textContent =
        'Coverage ' + pct(pf.coverage) + (pf.category_accuracy != null ? ' · category accuracy ' + pct(pf.category_accuracy) : '');

      setPill($('cusumAlarmPill'), cu.alarm ? 'Alarm' : 'Normal', cu.alarm ? 'critical' : 'good');
      $('cusumGNum').textContent = C.fmt(cu.g, 2);
      const cMax = thr.cusum_alarm || 4.0;
      setMeter('cusumMeterWrap', (cu.g || 0) / (cMax * 1.25), cu.alarm ? 'var(--critical)' : 'var(--ink)', [1 / 1.25]);
      $('cusumMeta').textContent = 'Alarm at ' + C.fmt(cMax, 1) + ' · error streak ' + (cyc.perf_streak || 0) + ' · drift streak ' + (cyc.drift_streak || 0);
      const q = cyc.quality || {};
      $('qualityMeta').textContent =
        'Quality ' + (q.status || '–') + ' · ' + (cyc.quality_bad_fraction != null ? pct(cyc.quality_bad_fraction, 1) + ' of core readings flagged' : 'no flag rate');
    }

    const impList = (A && A.feature_importance) || [];
    C.drawHorizontalBars(
      $('importanceChartHost'),
      impList.slice(0, 12).map((it) => ({ label: it.feature, value: it.importance })),
      {
        emptyText: 'Train or bootstrap a champion to see what it relies on.',
        ariaLabel: 'Champion feature importance',
        fmtVal: (v) => (v * 100).toFixed(1) + '%',
      }
    );

    const monthlyRows = (A && A.monthly_mae) || D.monthly_mae || [];
    const mLeg = $('monthlyMaeLegend');
    mLeg.textContent = '';
    C.METHODS.forEach((m) => {
      if (!monthlyRows.some((r) => r[m.key] != null)) return;
      const sp = h('span', null, mLeg);
      h('i', 'swatch', sp).style.background = m.color;
      sp.appendChild(document.createTextNode(m.name));
    });
    C.drawGroupedBars($('monthlyMaeChartHost'), monthlyRows);

    C.drawCycleSignals($('cycleChartHost'), D.cycles || [], thr);
    const revCycles = (D.cycles || []).slice().reverse().slice(0, 60);
    renderTable(
      $('cycleTimelineHost'),
      [
        { name: 'Date', get: (r) => r.date },
        { name: 'Decision', get: (r) => pill(ACTION_LABEL[r.action] || r.action, ACTION_CLS[r.action] || '') },
        { name: 'Error ×', num: true, get: (r) => C.fmt(r.ratio, 2) },
        { name: 'Drift', num: true, get: (r) => C.fmt(r.drift, 2) },
        { name: 'CUSUM', num: true, get: (r) => C.fmt(r.cusum, 2) },
        { name: 'Champion', get: (r) => r.champion || '–' },
      ],
      revCycles,
      'Decisions appear here after the first cycle.'
    );
  }

  /* ---------- View 3: What-if & batch ---------- */
  const controlInputs = {};

  function baseValue(f) {
    const last = state.baseObservations[state.baseObservations.length - 1];
    const raw = last ? last[f.key] : null;
    return raw != null && !Number.isNaN(Number(raw)) ? Number(raw) : f.def;
  }

  function markChanged() {
    PLAYGROUND_FIELDS.forEach((f) => {
      const ci = controlInputs[f.key];
      const v = Number(ci.num.value);
      ci.box.classList.toggle('changed', !Number.isNaN(v) && Math.abs(v - baseValue(f)) > 1e-9);
    });
  }

  function setActivePreset(name) {
    document.querySelectorAll('#presetGroup button[data-preset]').forEach((b) => {
      const on = b.getAttribute('data-preset') === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', String(on));
    });
  }

  (function buildPlaygroundControls() {
    const host = $('playgroundControls');
    PLAYGROUND_FIELDS.forEach((f) => {
      const box = h('div', 'ctrl-box', host);
      const top = h('div', 'ctrl-top', box);
      const lbl = h('label', null, top, f.label + ' ');
      h('span', 'muted', lbl, f.unit);
      lbl.setAttribute('for', 'num-' + f.key);
      const num = h('input', null, top);
      num.type = 'number';
      num.id = 'num-' + f.key;
      num.min = f.min;
      num.max = f.max;
      num.step = f.step;
      num.value = f.def;
      num.inputMode = 'decimal';

      const rng = h('input', null, box);
      rng.type = 'range';
      rng.id = 'rng-' + f.key;
      rng.min = f.min;
      rng.max = f.max;
      rng.step = f.step;
      rng.value = f.def;
      rng.setAttribute('aria-label', f.label + ' (' + f.unit + ')');

      rng.addEventListener('input', () => {
        state.stuckFaultActive = false;
        setActivePreset(null);
        num.value = rng.value;
        markChanged();
      });
      num.addEventListener('input', () => {
        state.stuckFaultActive = false;
        setActivePreset(null);
        const v = Number(num.value);
        if (!Number.isNaN(v)) rng.value = Math.min(f.max, Math.max(f.min, v));
        markChanged();
      });
      controlInputs[f.key] = { num, rng, box, def: f.def };
    });
    C.drawAqiGauge($('predictGaugeHost'), { aqi: null, compact: true, unitLabel: 'Predicted CPCB AQI' });
  })();

  function syncControlsFromLastRow(obs) {
    if (!obs || !obs.length) return;
    const last = obs[obs.length - 1];
    PLAYGROUND_FIELDS.forEach((f) => {
      const val = last[f.key];
      const setVal = val != null && !Number.isNaN(Number(val)) ? Number(val) : f.def;
      controlInputs[f.key].num.value = setVal;
      controlInputs[f.key].rng.value = Math.min(f.max, Math.max(f.min, setVal));
    });
    markChanged();
  }

  function applyPreset(name) {
    state.stuckFaultActive = name === 'stuck';
    setActivePreset(name);
    if (name === 'baseline') {
      syncControlsFromLastRow(state.baseObservations);
      showToast('Back to the station baseline', 'info');
      return;
    }
    const presets = {
      smoke: { pm25: 245, pm10: 360, no2: 95, o3: 28, temperature: 12.0, humidity: 78, wind_speed: 0.9, fc_wind_speed: 1.0, fc_temperature: 12.5 },
      ozone: { pm25: 62, pm10: 115, no2: 42, o3: 195, temperature: 38.5, humidity: 34, wind_speed: 2.1, fc_wind_speed: 2.0, fc_temperature: 39.0 },
      stuck: { pm25: 150, pm10: 190 },
    };
    const p = presets[name];
    if (!p) return;
    Object.keys(p).forEach((k) => {
      if (!controlInputs[k]) return;
      controlInputs[k].num.value = p[k];
      controlInputs[k].rng.value = p[k];
    });
    markChanged();
    showToast(name === 'stuck' ? 'PM2.5 will flatline for the last 12 hours' : 'Scenario applied · score it to compare', 'info');
  }

  document.querySelectorAll('button[data-preset]').forEach((btn) => {
    btn.setAttribute('aria-pressed', 'false');
    btn.addEventListener('click', () => applyPreset(btn.getAttribute('data-preset')));
  });

  async function loadPlaygroundObservations(forceSample) {
    const url = forceSample ? '/observations/recent?hours=216&sample=true' : '/observations/recent?hours=216';
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    let payload = await res.json();
    if (!forceSample && (!payload.observations || payload.observations.length < 24)) {
      const sRes = await fetch('/observations/recent?hours=216&sample=true');
      if (sRes.ok) payload = await sRes.json();
    }
    state.baseObservations = payload.observations || [];
    state.obsSource = payload.source || 'none';
    state.stuckFaultActive = false;
    setActivePreset(null);
    const range = C.fmtHumanTs(payload.start) + ' – ' + C.fmtHumanTs(payload.end);
    const label = (state.obsSource === 'station' ? 'Station history · ' : 'Synthetic sample · ') + payload.count + 'h · ' + range;
    setPill($('playgroundSourcePill'), label, state.obsSource === 'station' ? 'good' : 'info');
    syncControlsFromLastRow(state.baseObservations);
    return payload;
  }

  function buildAdjustedObservations() {
    if (!state.baseObservations.length) return [];
    const copy = state.baseObservations.map((r) => ({ ...r }));
    const lastIdx = copy.length - 1;
    if (state.stuckFaultActive) {
      const stuckVal = Number(controlInputs.pm25.num.value) || 150;
      for (let i = Math.max(0, copy.length - 12); i <= lastIdx; i++) {
        copy[i].pm25 = stuckVal;
      }
      return copy;
    }

    const scope = Number($('adjustScope').value) || 24;
    const startIdx = Math.max(0, copy.length - scope);
    const baseLast = state.baseObservations[lastIdx];

    PLAYGROUND_FIELDS.forEach((f) => {
      const targetVal = Number(controlInputs[f.key].num.value);
      if (Number.isNaN(targetVal)) return;
      const rawBase = baseLast[f.key];
      const baseVal = rawBase != null && !Number.isNaN(Number(rawBase)) ? Number(rawBase) : f.def;
      if (Math.abs(targetVal - baseVal) < 1e-9) return;

      if (scope <= 1) {
        copy[lastIdx][f.key] = targetVal;
        return;
      }
      const useRatio = f.scale && baseVal >= 1 && targetVal > 0;
      const ratio = useRatio ? targetVal / baseVal : 1;
      const delta = targetVal - baseVal;

      for (let i = startIdx; i < lastIdx; i++) {
        const origRaw = copy[i][f.key];
        const orig = origRaw != null && !Number.isNaN(Number(origRaw)) ? Number(origRaw) : baseVal;
        let next = useRatio ? orig * ratio : orig + delta;
        next = Number(Math.min(f.pHi, Math.max(f.pLo, next)).toFixed(3));
        if (f.stuck && i > startIdx && copy[i - 1][f.key] != null && Math.abs(next - copy[i - 1][f.key]) < 1e-4) {
          const jitter = ((i % 3) + 1) * 0.02;
          next = Number((next + jitter <= f.pHi ? next + jitter : next - jitter).toFixed(3));
        }
        copy[i][f.key] = next;
      }
      copy[lastIdx][f.key] = targetVal;
    });
    return copy;
  }

  function resetPredictPanel(message, cls, badge) {
    C.drawAqiGauge($('predictGaugeHost'), { aqi: null, compact: true, unitLabel: 'Predicted CPCB AQI' });
    setPill($('predCatBadge'), badge || 'Not scored', cls || '');
    setPill($('predQualityBadge'), 'Unavailable', 'warn');
    $('predCurrentAqi').textContent = '–';
    $('predDelta').textContent = '–';
    $('predDelta').className = '';
    $('predClean').textContent = '–';
    $('predTimestamps').textContent = message;
    $('predIssuesHost').textContent = message;
  }

  async function runPredict() {
    const btn = $('runPredictBtn');
    setBusy(btn, true);
    try {
      if (!state.baseObservations.length) await loadPlaygroundObservations(false);
      const observations = buildAdjustedObservations();
      const res = await fetch('/predict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observations }),
      });
      const body = await res.json();
      if (!res.ok) {
        resetPredictPanel(body.detail || 'Train or bootstrap a champion model before scoring.', 'critical', 'HTTP ' + res.status);
        return;
      }

      C.drawAqiGauge($('predictGaugeHost'), { aqi: body.aqi, category: body.category, compact: true, unitLabel: 'Predicted CPCB AQI' });
      $('predCatBadge').className = 'pill cat';
      $('predCatBadge').textContent = '';
      h('i', 'dot-c', $('predCatBadge')).style.background = C.catColor(body.category);
      $('predCatBadge').appendChild(document.createTextNode(body.category));

      $('predCurrentAqi').textContent = C.fmt(body.current_aqi, 0);
      const dEl = $('predDelta');
      if (body.current_aqi != null && body.aqi != null) {
        const diff = body.aqi - body.current_aqi;
        dEl.textContent = (diff >= 0 ? '+' : '−') + Math.abs(diff).toFixed(0);
        dEl.className = diff > 5 ? 'up' : diff < -5 ? 'down' : '';
      } else {
        dEl.textContent = '–';
        dEl.className = '';
      }
      $('predClean').textContent = body.inputs_clean ? 'Clean' : 'Flagged';
      $('predTimestamps').textContent = 'Champion ' + body.champion + ' · ' + C.fmtHumanTs(body.made_at) + ' → ' + C.fmtHumanTs(body.target_time);

      const qStatus = body.data_quality || 'pass';
      setPill($('predQualityBadge'), qStatus === 'fail' ? 'Failed' : qStatus === 'warn' ? 'Warnings' : 'Passed', qStatus === 'fail' ? 'critical' : qStatus === 'warn' ? 'warn' : 'good');
      const issuesHost = $('predIssuesHost');
      issuesHost.textContent = '';
      if (body.issues && body.issues.length) {
        const ul = h('ul', null, issuesHost);
        body.issues.forEach((iss) => h('li', null, ul, iss));
      } else {
        issuesHost.textContent = 'All checks passed.';
      }
    } catch (err) {
      resetPredictPanel(err.message, 'critical', 'Network error');
    } finally {
      setBusy(btn, false);
    }
  }

  function guarded(fn, label) {
    return () => fn().catch((err) => showToast(label + ': ' + err.message, 'critical'));
  }

  $('loadLatestBtn').addEventListener('click', guarded(() => loadPlaygroundObservations(false), 'Could not load history'));
  $('loadSampleBtn').addEventListener('click', guarded(() => loadPlaygroundObservations(true), 'Could not load sample'));
  $('resetPlaygroundBtn').addEventListener('click', () => {
    state.stuckFaultActive = false;
    setActivePreset(null);
    syncControlsFromLastRow(state.baseObservations);
  });
  $('runPredictBtn').addEventListener('click', runPredict);

  // Batch scoring
  function setBatchFigs(scored, mean, range) {
    const host = $('batchSummaryPills');
    host.textContent = '';
    [['Windows scored', scored], ['Mean AQI', mean], ['Range', range]].forEach(([k, v]) => {
      const d = h('div', null, host);
      h('span', 'stat-context', d, k);
      h('strong', null, d, v);
    });
  }

  function renderBatchEmpty() {
    C.drawBatchChart($('batchChartHost'), []);
    renderTable($('batchTableHost'), [{ name: 'Status', get: (r) => r }], [], 'Results for each scored window appear here.');
  }
  renderBatchEmpty();

  function loadCsvFile(file) {
    if (!file) return;
    file.text().then((txt) => {
      $('batchCsvInput').value = txt;
      showToast('Loaded ' + file.name, 'info');
    });
  }

  const dz = $('csvDropzone');
  dz.addEventListener('click', () => $('csvFileInput').click());
  dz.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      $('csvFileInput').click();
    }
  });
  dz.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    dz.classList.add('dragover');
  });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', (ev) => {
    ev.preventDefault();
    dz.classList.remove('dragover');
    loadCsvFile(ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]);
  });
  $('csvFileInput').addEventListener('change', (ev) => loadCsvFile(ev.target.files && ev.target.files[0]));

  $('loadSampleCsvBtn').addEventListener('click', async () => {
    try {
      let res = await fetch('/observations/recent?hours=72');
      let payload = res.ok ? await res.json() : null;
      if (!payload || !payload.observations || payload.observations.length < 24) {
        res = await fetch('/observations/recent?hours=72&sample=true');
        payload = res.ok ? await res.json() : null;
      }
      if (!payload || !payload.observations || !payload.observations.length) {
        showToast('Could not load sample observations', 'critical');
        return;
      }
      const cols = ['timestamp', 'pm25', 'pm10', 'no2', 'so2', 'co', 'o3', 'temperature', 'humidity', 'wind_speed', 'precipitation', 'fc_temperature', 'fc_humidity', 'fc_wind_speed', 'fc_precipitation'];
      $('batchCsvInput').value = toCsv(cols, payload.observations.map((r) => cols.map((c) => r[c])));
      showToast('Loaded ' + payload.observations.length + ' hourly rows', 'info');
    } catch (err) {
      showToast('Could not load sample: ' + err.message, 'critical');
    }
  });

  $('runBatchBtn').addEventListener('click', async () => {
    const csvText = $('batchCsvInput').value.trim();
    if (!csvText) {
      showToast('Paste CSV rows or load the 72h sample first', 'warn');
      $('batchCsvInput').focus();
      return;
    }
    const btn = $('runBatchBtn');
    setBusy(btn, true);
    try {
      const res = await fetch('/predict/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csv: csvText }),
      });
      const body = await res.json();
      if (!res.ok) {
        setPill($('batchQualityPill'), 'HTTP ' + res.status, 'critical');
        $('batchIssuesText').textContent = body.detail || 'Batch scoring failed.';
        return;
      }
      state.batchResult = body;
      $('downloadBatchCsvBtn').disabled = !(body.predictions && body.predictions.length);
      const q = body.data_quality || 'pass';
      setPill($('batchQualityPill'), q === 'fail' ? 'Quality failed' : q === 'warn' ? 'Quality warnings' : 'Quality passed', q === 'fail' ? 'critical' : q === 'warn' ? 'warn' : 'good');
      const sum = body.summary || {};
      setBatchFigs(String(body.count), C.fmt(sum.mean_aqi, 0), C.fmt(sum.min_aqi, 0) + '–' + C.fmt(sum.max_aqi, 0));

      const ccHost = $('batchCatCounts');
      ccHost.textContent = '';
      Object.entries(sum.category_counts || {}).forEach(([cat, n]) => {
        if (n) ccHost.appendChild(makeCatPill(cat, ' · ' + n));
      });
      $('batchIssuesText').textContent = body.issues && body.issues.length ? 'Quality notes: ' + body.issues.join('; ') : '';

      C.drawBatchChart($('batchChartHost'), body.predictions || []);
      renderTable(
        $('batchTableHost'),
        [
          { name: 'Issued', get: (r) => twoLine(C.fmtHumanTs(r.made_at), 'target ' + C.fmtHumanTs(r.target_time)) },
          { name: 'Predicted', num: true, get: (r) => C.fmt(r.aqi, 0) },
          { name: 'Current', num: true, get: (r) => C.fmt(r.current_aqi, 0) },
          { name: 'Category', get: (r) => makeCatPill(r.category) },
          { name: 'Inputs', get: (r) => (r.inputs_clean ? 'Clean' : pill('Flagged', 'warn')) },
        ],
        (body.predictions || []).slice().reverse(),
        'No predictions returned.'
      );
      showToast('Scored ' + body.count + ' windows with ' + body.champion, 'good');
    } catch (err) {
      setPill($('batchQualityPill'), 'Error', 'critical');
      $('batchIssuesText').textContent = err.message;
    } finally {
      setBusy(btn, false);
    }
  });

  $('downloadBatchCsvBtn').addEventListener('click', () => {
    const preds = state.batchResult && state.batchResult.predictions ? state.batchResult.predictions : [];
    if (!preds.length) return;
    const csv = toCsv(
      ['made_at', 'target_time', 'aqi', 'current_aqi', 'category', 'inputs_clean'],
      preds.map((p) => [p.made_at, p.target_time, p.aqi, p.current_aqi, p.category, p.inputs_clean])
    );
    downloadBlob('aqi_batch_predictions.csv', csv, 'text/csv;charset=utf-8');
    showToast('Downloaded ' + preds.length + ' predictions', 'good');
  });

  /* ---------- View 4: Registry & audit ---------- */
  const STAGE_CLS = { champion: 'info', shadow: 'warn', rolled_back: 'critical', archived: '' };
  const STAGE_LABEL = { champion: 'Champion', shadow: 'Shadow', rolled_back: 'Rolled back', archived: 'Archived' };

  function populateEventKinds(events) {
    const sel = $('eventKindFilter');
    const prev = sel.value;
    const kinds = Array.from(new Set((events || []).map((e) => e.kind))).sort();
    sel.textContent = '';
    h('option', null, sel, 'All kinds').value = '';
    kinds.forEach((k) => {
      h('option', null, sel, k.replace(/_/g, ' ')).value = k;
    });
    if (kinds.includes(prev)) sel.value = prev;
  }

  function filteredEventsList() {
    const kind = $('eventKindFilter').value;
    const sev = $('eventSevFilter').value;
    const q = $('eventSearch').value.trim().toLowerCase();
    return state.events
      .filter((e) => {
        if (kind && e.kind !== kind) return false;
        if (sev && (e.severity || 'info') !== sev) return false;
        if (q && !(String(e.message || '').toLowerCase().includes(q) || String(e.kind || '').toLowerCase().includes(q))) return false;
        return true;
      })
      .slice()
      .reverse();
  }

  function renderFilteredEvents() {
    const rows = filteredEventsList();
    $('eventsSub').textContent =
      'Showing ' + rows.length + ' of ' + state.events.length + ' events · promotions, rejections, rollbacks, safe mode, holds and quality alerts.';
    renderTable(
      $('eventsTableHost'),
      [
        { name: 'When', get: (r) => twoLine(C.fmtHumanTs(r.ts), String(r.kind || '').replace(/_/g, ' ')) },
        { name: 'Severity', get: (r) => pill(r.severity || 'info', r.severity === 'error' ? 'critical' : r.severity === 'warning' ? 'warn' : '') },
        { name: 'Message', wrap: true, get: (r) => r.message },
      ],
      rows,
      state.events.length ? 'No events match these filters.' : 'No audit events recorded yet.'
    );
  }

  $('eventKindFilter').addEventListener('change', renderFilteredEvents);
  $('eventSevFilter').addEventListener('change', renderFilteredEvents);
  $('eventSearch').addEventListener('input', renderFilteredEvents);

  $('exportEventsJsonBtn').addEventListener('click', () => {
    const rows = filteredEventsList();
    downloadBlob('aqi_events.json', JSON.stringify(rows, null, 2), 'application/json;charset=utf-8');
    showToast('Exported ' + rows.length + ' events as JSON', 'good');
  });

  $('exportEventsCsvBtn').addEventListener('click', () => {
    const rows = filteredEventsList();
    const csv = toCsv(['ts', 'severity', 'kind', 'message'], rows.map((r) => [r.ts, r.severity || 'info', r.kind, r.message]));
    downloadBlob('aqi_events.csv', csv, 'text/csv;charset=utf-8');
    showToast('Exported ' + rows.length + ' events as CSV', 'good');
  });

  function renderRegistry(D, A) {
    const versions = (A && A.version_metrics && A.version_metrics.length ? A.version_metrics : D.versions) || [];
    const linHost = $('versionLineageCards');
    linHost.textContent = '';
    if (!versions.length) {
      h('div', 'empty-msg', linHost, 'No models registered yet. Bootstrapping registers the first champion.');
    } else {
      versions
        .slice()
        .reverse()
        .slice(0, 8)
        .forEach((v) => {
          const card = h('div', 'lineage-card' + (v.stage === 'champion' ? ' is-champion' : ''), linHost);
          const top = h('div', 'lineage-top', card);
          h('span', 'lineage-ver', top, v.version);
          top.appendChild(pill(STAGE_LABEL[v.stage] || v.stage, STAGE_CLS[v.stage] || ''));
          h('div', 'stat-context', card, [v.kind, v.strategy, v.trigger].filter(Boolean).join(' · ') || '–');
          h('div', 'stat-context', card, v.trained_at ? 'Trained ' + C.fmtHumanTs(v.trained_at).replace(/ \d{2}:00$/, '') + ' ' + String(v.trained_at).slice(0, 4) : '');
          const figs = h('div', 'lineage-figs', card);
          const f1 = h('div', null, figs);
          h('b', null, f1, C.fmt(v.val_mae));
          f1.appendChild(document.createTextNode('val MAE'));
          const f2 = h('div', null, figs);
          h('b', null, f2, C.fmt(v.ref_rel_mae, 2));
          f2.appendChild(document.createTextNode('vs persistence'));
        });
    }

    renderTable(
      $('versionsTableHost'),
      [
        { name: 'Version', get: (r) => twoLine(r.version, r.kind || '') },
        { name: 'Stage', get: (r) => pill(STAGE_LABEL[r.stage] || r.stage, STAGE_CLS[r.stage] || '') },
        { name: 'Strategy', get: (r) => twoLine(r.strategy || '–', r.trigger || '') },
        { name: 'Rows', num: true, get: (r) => (r.n_train_rows != null ? Number(r.n_train_rows).toLocaleString('en') : '–') },
        { name: 'Val MAE', num: true, get: (r) => C.fmt(r.val_mae) },
        { name: 'Rel MAE', num: true, get: (r) => C.fmt(r.ref_rel_mae, 2) },
        { name: 'Trained', get: (r) => String(r.trained_at || '').slice(0, 10) },
      ],
      versions.slice().reverse(),
      'No models registered yet.',
      { rowClass: (r) => (r.stage === 'champion' ? 'is-current' : null) }
    );

    renderTable(
      $('retrainTableHost'),
      [
        { name: 'Date', get: (r) => twoLine(r.date, r.trigger || '') },
        { name: 'Outcome', get: (r) => pill(r.outcome === 'promoted' ? 'Promoted ' + (r.version || '') : r.outcome, r.outcome === 'promoted' ? 'good' : 'warn') },
        { name: 'Champion', num: true, get: (r) => C.fmt(r.champion_mae) },
        { name: 'Challenger', wrap: true, get: (r) => twoLine(C.fmt(r.best_mae), r.best || '') },
        { name: 'P(better)', num: true, get: (r) => C.fmt(r.prob, 2) },
      ],
      (D.retrain_log || []).slice().reverse(),
      'No retraining attempts yet. They start when drift or error crosses a threshold.'
    );
  }

  /* ---------- View 5: Architecture & config ---------- */
  function renderPipelineView(A, cfg) {
    const stages = (A && A.pipeline_stages) || [
      { id: 'ingest', name: 'Ingest', status: 'empty', metric: '–', detail: 'No data ingested yet.' },
      { id: 'quality', name: 'Quality gate', status: 'empty', metric: '–', detail: 'Waiting for observations.' },
      { id: 'forecast', name: 'Forecast', status: 'empty', metric: '–', detail: 'No forecast run yet.' },
      { id: 'monitor', name: 'Monitor', status: 'empty', metric: '–', detail: 'No cycle snapshot yet.' },
      { id: 'decide', name: 'Decide', status: 'empty', metric: '–', detail: 'No policy decision yet.' },
      { id: 'retrain', name: 'Retrain & gate', status: 'empty', metric: '–', detail: 'No retraining attempts yet.' },
      { id: 'registry', name: 'Registry', status: 'empty', metric: '–', detail: 'No models registered yet.' },
    ];
    if (!state.selectedStageId && stages.length) state.selectedStageId = stages[0].id;

    const STATUS_CLS = { ok: 'good', warn: 'warn', alert: 'critical' };
    const STATUS_LABEL = { ok: 'OK', warn: 'Warning', alert: 'Alert', empty: 'Idle', idle: 'Idle' };
    const flowHost = $('pipelineFlowHost');
    flowHost.textContent = '';
    stages.forEach((st, idx) => {
      const selected = st.id === state.selectedStageId;
      const btn = h('button', 'stage-node' + (selected ? ' selected' : ''), flowHost);
      btn.type = 'button';
      btn.setAttribute('aria-pressed', String(selected));
      h('div', 'stage-idx', btn, String(idx + 1).padStart(2, '0'));
      h('div', 'stage-name', btn, st.name);
      btn.appendChild(pill(STATUS_LABEL[st.status] || st.status, STATUS_CLS[st.status] || ''));
      h('div', 'stage-metric', btn, st.metric || '–');
      btn.addEventListener('click', () => {
        state.selectedStageId = st.id;
        renderPipelineView(A, cfg);
        const again = flowHost.querySelectorAll('.stage-node')[idx];
        if (again) again.focus();
      });
    });

    const activeStage = stages.find((s) => s.id === state.selectedStageId) || stages[0];
    const det = $('stageDetailHost');
    det.textContent = '';
    if (activeStage) {
      h('div', 'eyebrow', det, 'Stage ' + (stages.indexOf(activeStage) + 1) + ' · ' + (STATUS_LABEL[activeStage.status] || activeStage.status));
      h('strong', null, det, activeStage.name + (activeStage.metric ? ' · ' + activeStage.metric : ''));
      h('p', null, det, activeStage.detail || '');
    }

    const telHost = $('telemetryStrip');
    telHost.textContent = '';
    if (cfg) {
      const mb = cfg.db_size_bytes != null ? (cfg.db_size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '0 MB';
      [
        ['SQLite store', cfg.db_exists ? mb : 'Not created', cfg.run_dir || 'runs/default'],
        ['Model registry', String(cfg.version_count ?? 0) + ' versions', 'Champion ' + (cfg.champion || 'none')],
        ['Cycles', String(cfg.cycle_count ?? 0), String(cfg.event_count ?? 0) + ' audit events'],
        ['Runtime', 'Python ' + (cfg.python_version || '–'), cfg.report_exists ? 'Report ready at /report' : 'Report not built yet'],
      ].forEach(([label, val, sub]) => {
        const tile = h('div', 'tile', telHost);
        h('div', 'label', tile, label);
        h('div', 'value', tile, val);
        h('div', 'delta', tile, sub);
      });
    } else {
      h('div', 'empty-msg', telHost, 'Runtime details are unavailable.');
    }

    const cfgHost = $('configInspectorHost');
    cfgHost.textContent = '';
    if (cfg) {
      const m = cfg.model || {};
      const mon = cfg.monitor || {};
      const rt = cfg.retrain || {};
      const sections = [
        ['Station & source', { location: cfg.location, source: cfg.source && cfg.source.type, simulation_seed: cfg.simulation && cfg.simulation.seed, injected_events: cfg.simulation && cfg.simulation.event_count }],
        ['Data & horizon', cfg.data || {}],
        ['Model', { candidates: (m.candidates || []).join(', '), target_mode: m.target_mode, cv_folds: m.cv_folds, cv_test_days: m.cv_test_days, train_stride_hours: m.train_stride_hours }],
        ['Monitor thresholds', { drift_warn: mon.drift_warn_score, drift_alert: mon.drift_alert_score, perf_warn: mon.perf_warn_ratio, perf_critical: mon.perf_critical_ratio, cusum_alarm: mon.cusum_alarm, safe_enter: mon.safe_enter_ratio }],
        ['Retrain policy', { strategies: (rt.strategies || []).join(', '), cooldown_days: rt.cooldown_days, min_improvement: rt.min_improvement, confidence: rt.confidence, probation_days: rt.probation_days, max_age_days: rt.max_age_days }],
      ];
      sections.forEach(([title, obj]) => {
        const card = h('div', 'kv-card', cfgHost);
        h('h3', null, card, title);
        Object.entries(obj).forEach(([k, v]) => {
          const row = h('div', 'kv-row', card);
          h('span', null, row, k.replace(/_/g, ' '));
          h('strong', null, row, v == null || v === '' ? '–' : String(v));
        });
      });
    }

    const cliHost = $('cliRefHost');
    cliHost.textContent = '';
    [
      ['Run the full demo simulation', 'aqi-pipeline --run-dir runs/demo simulate'],
      ['Serve this console and the API', 'aqi-pipeline --run-dir runs/demo serve --port 8000'],
      ['Print status and build the report', 'aqi-pipeline --run-dir runs/demo status && aqi-pipeline --run-dir runs/demo report'],
      ['Docker demo profile', 'docker compose --profile demo up --build'],
    ].forEach(([title, cmd]) => {
      const card = h('div', 'kv-card', cliHost);
      h('h3', null, card, title);
      const line = h('div', 'code-line', card);
      h('code', null, line, cmd);
      const cp = h('button', 'btn ghost', line, 'Copy');
      cp.type = 'button';
      cp.setAttribute('aria-label', 'Copy command: ' + cmd);
      cp.addEventListener('click', () => {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard
            .writeText(cmd)
            .then(() => showToast('Copied to clipboard', 'good'))
            .catch(() => showToast('Clipboard unavailable', 'warn'));
        } else {
          showToast(cmd, 'info');
        }
      });
    });
  }

  /* ---------- Data fetching (race-free) ---------- */
  async function loadAllData() {
    const reqId = ++state.reqSeq;
    if (state.abortCtrl) {
      try {
        state.abortCtrl.abort();
      } catch (_) {}
    }
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    state.abortCtrl = ctrl;
    const opts = ctrl ? { signal: ctrl.signal } : undefined;
    $('refreshBtn').setAttribute('aria-busy', 'true');

    try {
      const [dashRes, evRes, anRes, cfgRes] = await Promise.all([
        fetch('/dashboard/data?hours=' + encodeURIComponent(state.hours), opts),
        fetch('/events?limit=500', opts).catch(() => null),
        fetch('/api/analytics', opts).catch(() => null),
        fetch('/api/config', opts).catch(() => null),
      ]);
      if (reqId !== state.reqSeq) return;
      if (!dashRes.ok) throw new Error('HTTP ' + dashRes.status);

      const D = await dashRes.json();
      const evs = evRes && evRes.ok ? await evRes.json() : D.events || [];
      const an = anRes && anRes.ok ? await anRes.json() : null;
      const cfg = cfgRes && cfgRes.ok ? await cfgRes.json() : null;
      if (reqId !== state.reqSeq) return;

      state.data = D;
      state.events = evs;
      state.analytics = an;
      state.config = cfg;

      renderShell(D);
      renderOverview(D);
      renderDiagnostics(D, an);
      renderRegistry(D, an);
      populateEventKinds(evs);
      renderFilteredEvents();
      renderPipelineView(an, cfg);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (reqId !== state.reqSeq) return;
      $('emptyStateBanner').hidden = false;
      $('emptyStateTitle').textContent = 'Could not reach the pipeline API';
      $('emptyStateText').textContent = 'Loading dashboard data failed (' + err.message + '). Check that aqi-pipeline serve is running, then refresh.';
      setPill($('servingPill'), 'Offline', 'critical');
      $('pageSummary').textContent = 'The console could not load data from the API.';
    } finally {
      if (reqId === state.reqSeq) $('refreshBtn').removeAttribute('aria-busy');
    }
  }

  $('horizonSelector')
    .querySelectorAll('button[data-hours]')
    .forEach((btn) => {
      btn.addEventListener('click', () => {
        state.hours = Number(btn.getAttribute('data-hours')) || 72;
        $('horizonSelector')
          .querySelectorAll('button')
          .forEach((b) => {
            b.classList.remove('active');
            b.setAttribute('aria-pressed', 'false');
          });
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');
        loadAllData();
      });
    });

  $('refreshBtn').addEventListener('click', () => {
    loadAllData();
    showToast('Refreshed', 'info');
  });

  function updateCountdownLabel() {
    if (!state.autoTimer) return;
    const left = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
    $('autoRefreshLabel').textContent = 'Auto-refresh · ' + left + 's';
  }

  $('autoRefreshBtn').addEventListener('click', () => {
    if (state.autoTimer) {
      clearInterval(state.autoTimer);
      clearInterval(state.countdownTimer);
      state.autoTimer = null;
      state.countdownTimer = null;
      $('autoRefreshLabel').textContent = 'Auto-refresh off';
      $('autoRefreshDot').classList.remove('on');
      $('autoRefreshBtn').setAttribute('aria-pressed', 'false');
    } else {
      state.nextRefreshAt = Date.now() + 30000;
      state.autoTimer = setInterval(() => {
        state.nextRefreshAt = Date.now() + 30000;
        loadAllData();
      }, 30000);
      state.countdownTimer = setInterval(updateCountdownLabel, 1000);
      updateCountdownLabel();
      $('autoRefreshDot').classList.add('on');
      $('autoRefreshBtn').setAttribute('aria-pressed', 'true');
    }
  });

  loadAllData().then(() => loadPlaygroundObservations(false).catch(() => {}));
})();
