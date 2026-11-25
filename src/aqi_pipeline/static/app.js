(function () {
  'use strict';

  const C = window.AqiCharts;
  const $ = (id) => document.getElementById(id);

  const ROUTES = ['overview', 'diagnostics', 'simulator', 'registry', 'pipeline'];

  const PLAYGROUND_FIELDS = [
    { key: 'pm25', label: 'PM2.5 (µg/m³)', min: 0, max: 500, step: 1, def: 65, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'pm10', label: 'PM10 (µg/m³)', min: 0, max: 600, step: 1, def: 120, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'o3', label: 'O3 (µg/m³)', min: 0, max: 350, step: 1, def: 45, pLo: 0, pHi: 800, scale: true, stuck: true },
    { key: 'no2', label: 'NO2 (µg/m³)', min: 0, max: 300, step: 1, def: 38, pLo: 0, pHi: 1000, scale: true, stuck: true },
    { key: 'temperature', label: 'Temperature (°C)', min: -10, max: 50, step: 0.5, def: 24, pLo: -30, pHi: 55, scale: false, stuck: true },
    { key: 'humidity', label: 'Humidity (%)', min: 5, max: 100, step: 1, def: 60, pLo: 0, pHi: 100, scale: false, stuck: true },
    { key: 'wind_speed', label: 'Wind Speed (m/s)', min: 0.2, max: 20, step: 0.1, def: 2.2, pLo: 0, pHi: 60, scale: true, stuck: false },
    { key: 'fc_wind_speed', label: '24h Fc Wind (m/s)', min: 0.2, max: 20, step: 0.1, def: 2.4, pLo: 0, pHi: 60, scale: true, stuck: false },
    { key: 'fc_temperature', label: '24h Fc Temp (°C)', min: -10, max: 50, step: 0.5, def: 24.5, pLo: -30, pHi: 55, scale: false, stuck: false },
  ];

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

  /* ---------- DOM & UI Helpers ---------- */
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

  function makeCatPill(catName) {
    const label = catName || 'Unknown';
    const span = h('span', 'pill');
    const dot = h('i', 'dot-c', span);
    dot.style.background = C.catColor(label);
    span.appendChild(document.createTextNode(label));
    return span;
  }

  function showToast(msg, cls) {
    const host = $('toastHost');
    const t = h('div', 'toast' + (cls ? ' ' + cls : ''), host, msg);
    setTimeout(() => {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 3200);
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

  function renderTable(host, cols, rows, emptyText) {
    host.textContent = '';
    if (!rows || !rows.length) {
      h('div', 'empty-msg', host, emptyText || 'No records yet.');
      return;
    }
    const tbl = h('table', null, host);
    const trHead = h('tr', null, h('thead', null, tbl));
    cols.forEach((c) => {
      const th = h('th', (c.num ? 'num ' : '') + (c.onSort ? 'sortable' : ''), trHead, c.name);
      if (c.onSort) th.addEventListener('click', c.onSort);
    });
    const tbody = h('tbody', null, tbl);
    rows.forEach((r) => {
      const tr = h('tr', null, tbody);
      cols.forEach((c) => {
        const td = h('td', c.num ? 'num' : null, tr);
        const val = c.get(r);
        if (val instanceof Node) td.appendChild(val);
        else td.textContent = val == null ? '–' : String(val);
      });
    });
  }

  /* ---------- Theme Toggle ---------- */
  function effectiveTheme() {
    const saved = localStorage.getItem('aqi-theme');
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = $('themeToggleBtn');
    const isDark = theme === 'dark';
    btn.textContent = isDark ? 'Light mode' : 'Dark mode';
    btn.setAttribute('aria-pressed', String(isDark));
  }

  function toggleTheme() {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    localStorage.setItem('aqi-theme', next);
    applyTheme(next);
  }

  applyTheme(effectiveTheme());
  $('themeToggleBtn').addEventListener('click', toggleTheme);

  /* ---------- Sidebar Drawer & Hash Routing ---------- */
  function closeDrawer() {
    $('sidebar').classList.remove('open');
    $('drawerBackdrop').classList.remove('open');
  }

  $('menuBtn').addEventListener('click', () => {
    $('sidebar').classList.add('open');
    $('drawerBackdrop').classList.add('open');
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
        a.classList.toggle('active', a.getAttribute('data-route') === state.route);
      });
    closeDrawer();
  }

  window.addEventListener('hashchange', () => setActiveRoute(routeFromHash()));
  setActiveRoute(routeFromHash());

  document.addEventListener('keydown', (ev) => {
    const tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key >= '1' && ev.key <= '5') {
      const targetRoute = ROUTES[Number(ev.key) - 1];
      window.location.hash = '#/' + targetRoute;
    } else if (ev.key === 'r' || ev.key === 'R') {
      loadAllData();
      showToast('Refreshing telemetry…', 'info');
    } else if (ev.key === 't' || ev.key === 'T') {
      toggleTheme();
    }
  });

  /* ---------- CPCB Legend ---------- */
  (function buildCpcbLegend() {
    const host = $('cpcbLegend');
    C.CPCB_CATEGORIES.forEach((c) => {
      const pill = h('span', 'pill', host);
      const dot = h('i', 'dot-c', pill);
      dot.style.background = c.color;
      pill.appendChild(document.createTextNode(c.name + ' (' + c.range + ')'));
    });
  })();

  /* ---------- Header, Banners & KPIs ---------- */
  function renderShell(D) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const dec = cyc && cyc.decision ? cyc.decision : null;
    const meta = st.champion_meta || {};

    $('stationLocation').textContent = st.location || D.location || 'Unknown station';
    $('simBadge').hidden = !D.simulated;
    const champTxt = st.champion ? 'Champion: ' + st.champion + (meta.kind ? ' (' + meta.kind + ')' : '') : 'Champion: none';
    setPill($('badgeChampion'), champTxt, st.champion ? 'info' : 'warn');
    $('sideChampion').textContent = champTxt;
    $('sideHorizon').textContent = 'Horizon: ' + (st.horizon_hours || 24) + 'h · CPCB Scale';
    $('predHorizonLbl').textContent = String(st.horizon_hours || 24);

    setPill($('badgeShadow'), st.shadow ? 'Shadow: ' + st.shadow + ' (probation)' : 'Shadow: none', st.shadow ? 'warn' : '');
    setPill($('badgeSafeMode'), st.safe_mode ? 'Safe Mode: ON (Persistence)' : 'Safe Mode: Off (Model)', st.safe_mode ? 'warn' : 'good');

    const action = dec ? dec.action : 'none';
    const decCls = action === 'hold' || action === 'retrain' ? 'warn' : action === 'watch' ? 'info' : 'good';
    setPill($('badgeDecision'), 'Decision: ' + (cyc ? action : '–'), cyc ? decCls : '');
    $('badgeDecision').title = dec ? dec.reason : 'No cycle run yet';

    $('lastObsTime').textContent = st.last_observation || '–';
    $('lastCycleTime').textContent = cyc ? cyc.cycle_time : '–';

    $('emptyStateBanner').hidden = Boolean(st.bootstrapped);
    if (dec && dec.action !== 'none') {
      $('decisionBanner').hidden = false;
      $('decisionBannerTitle').textContent = 'Latest decision (' + dec.action.toUpperCase() + ')';
      $('decisionBannerReason').textContent = dec.reason || '';
      $('decisionBannerTime').textContent = cyc.cycle_time || '';
    } else {
      $('decisionBanner').hidden = true;
    }
  }

  /* ---------- View 1: Overview ---------- */
  function renderOverview(D) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const cur = st.current;

    $('gaugeCatPill').textContent = '';
    if (cur && cur.aqi != null) {
      $('gaugeCatPill').appendChild(makeCatPill(cur.category));
      C.drawAqiGauge($('aqiGaugeHost'), { aqi: cur.aqi, category: cur.category });
      $('gaugeSub').textContent = 'Issued ' + cur.timestamp;
      $('gaugeFooter').textContent = cur.dominant ? 'Dominant pollutant: ' + cur.dominant.toUpperCase() : 'CPCB 24h/8h index';
    } else {
      C.drawAqiGauge($('aqiGaugeHost'), { aqi: null, category: 'Unknown' });
      $('gaugeSub').textContent = st.last_observation ? 'Last obs ' + st.last_observation : 'No observations yet';
      $('gaugeFooter').textContent = st.last_observation ? 'Insufficient valid PM hours for CPCB AQI' : 'Waiting for data';
    }

    // Pollutant & Weather Cards
    const pHost = $('pollutantCards');
    pHost.textContent = '';
    const r = cur && cur.readings ? cur.readings : {};
    if (cur && cur.qc_notes) setPill($('qcLatestPill'), 'QC: ' + cur.qc_notes, 'warn');
    else setPill($('qcLatestPill'), cur ? 'QC: pass' : 'QC: –', cur ? 'good' : '');

    const cardDefs = [
      ['PM2.5', r.pm25, 'µg/m³', 1, 60, 120],
      ['PM10', r.pm10, 'µg/m³', 1, 100, 250],
      ['NO2', r.no2, 'µg/m³', 1, 80, 180],
      ['SO2', r.so2, 'µg/m³', 1, 80, 380],
      ['CO', r.co, 'mg/m³', 2, 2, 10],
      ['O3', r.o3, 'µg/m³', 1, 100, 168],
      ['Temp', r.temperature, '°C', 1, null, null],
      ['Humidity', r.humidity, '%', 0, null, null],
      ['Wind', r.wind_speed, 'm/s', 2, null, null],
      ['Rain', r.precipitation, 'mm', 1, null, null],
    ];
    cardDefs.forEach(([label, val, unit, dec, warnAt, highAt]) => {
      const card = h('div', 'pol-card', pHost);
      if (val != null && warnAt != null) {
        card.style.borderLeftColor = val >= highAt ? 'var(--cat-poor)' : val >= warnAt ? 'var(--cat-mod)' : 'var(--cat-good)';
      } else if (val != null) {
        card.style.borderLeftColor = 'var(--s1)';
      }
      h('div', 'pk', card, label);
      h('div', 'pv', card, val != null ? C.fmt(val, dec) : '–');
      h('div', 'pu', card, unit);
    });

    // KPI Strip
    $('kpiCurrentCat').textContent = '';
    if (cur && cur.aqi != null) {
      $('kpiCurrentVal').textContent = C.fmt(cur.aqi, 1);
      $('kpiCurrentCat').appendChild(makeCatPill(cur.category));
      $('kpiCurrentMeta').textContent = (cur.dominant ? 'Dominant: ' + cur.dominant.toUpperCase() + ' · ' : '') + cur.timestamp;
    } else {
      $('kpiCurrentVal').textContent = '–';
      $('kpiCurrentMeta').textContent = 'Waiting for valid PM readings';
    }

    const fcs = D.forecast && D.forecast.forecasts ? D.forecast.forecasts : [];
    const validFc = fcs.filter((f) => f.aqi != null).map((f) => f.aqi);
    $('kpiFcCat').textContent = '';
    if (validFc.length) {
      const meanFc = validFc.reduce((a, b) => a + b, 0) / validFc.length;
      const peakFc = Math.max.apply(null, validFc);
      const peakItem = fcs.find((f) => f.aqi === peakFc) || fcs[0];
      $('kpiFcVal').textContent = C.fmt(meanFc, 0) + ' / ' + C.fmt(peakFc, 0);
      $('kpiFcCat').appendChild(makeCatPill(peakItem.category));
      const sources = Array.from(new Set(fcs.map((f) => f.source).filter(Boolean))).join(', ') || 'model';
      $('kpiFcMeta').textContent = validFc.length + 'h horizon · served by ' + sources;
    } else {
      $('kpiFcVal').textContent = '–';
      $('kpiFcMeta').textContent = st.bootstrapped ? 'Run a cycle to generate forecasts' : 'No trained model yet';
    }

    const perf = cyc && cyc.performance ? cyc.performance : null;
    if (perf && perf.ratio != null) {
      $('kpiPerfVal').textContent = C.fmt(perf.ratio, 2) + 'x';
      const pCls = perf.level === 'critical' ? 'critical' : perf.level === 'warn' ? 'warn' : 'good';
      setPill($('kpiPerfBadge'), perf.level, pCls);
      $('kpiPerfMeta').textContent = 'MAE ' + C.fmt(perf.mae) + ' vs expected ' + C.fmt(perf.expected_mae) + ' (pers ' + C.fmt(perf.mae_naive) + ')';
    } else {
      $('kpiPerfVal').textContent = '–';
      setPill($('kpiPerfBadge'), perf ? perf.level : '–', '');
      $('kpiPerfMeta').textContent = 'Waiting for matured forecast labels';
    }

    const dr = cyc && cyc.drift ? cyc.drift : null;
    const cu = cyc && cyc.cusum ? cyc.cusum : null;
    if (dr) {
      $('kpiDriftVal').textContent = C.fmt(dr.score, 2);
      const dCls = dr.level === 'alert' ? 'critical' : dr.level === 'warn' ? 'warn' : 'good';
      setPill($('kpiDriftBadge'), dr.level + (cu && cu.alarm ? ' + CUSUM' : ''), cu && cu.alarm ? 'critical' : dCls);
      $('kpiDriftMeta').textContent = (dr.drifted ? dr.drifted.length : 0) + ' drifted · CUSUM ' + C.fmt(cu ? cu.g : 0, 2);
    } else {
      $('kpiDriftVal').textContent = '–';
      setPill($('kpiDriftBadge'), '–', '');
      $('kpiDriftMeta').textContent = 'No monitoring cycle yet';
    }

    // Forecast chart & table
    const servedSources = Array.from(new Set(fcs.map((f) => f.source).filter(Boolean)));
    if (st.safe_mode) {
      setPill($('forecastServedBadge'), 'Served by: persistence (safe mode)', 'warn');
    } else if (servedSources.length) {
      setPill($('forecastServedBadge'), 'Served by: ' + servedSources.join(', '), servedSources.includes('persistence') ? 'warn' : 'good');
    } else {
      setPill($('forecastServedBadge'), st.bootstrapped ? 'Served by: –' : 'No forecast yet', '');
    }

    $('forecastSubtitle').textContent =
      'Trailing ' + state.hours + 'h hourly actual AQI vs served forecast + next ' + (st.horizon_hours || 24) + 'h horizon' +
      (st.last_observation ? ' (issued up to ' + st.last_observation + ').' : '.');

    renderForecastChartOnly();

    $('upcomingCount').textContent = String(fcs.length);
    const catHost = $('upcomingCatSummary');
    catHost.textContent = '';
    const counts = {};
    fcs.forEach((f) => {
      const c = f.category || 'Unknown';
      counts[c] = (counts[c] || 0) + 1;
    });
    Object.keys(counts).forEach((cat) => {
      const p = makeCatPill(cat + ': ' + counts[cat] + 'h');
      p.querySelector('.dot-c').style.background = C.catColor(cat);
      catHost.appendChild(p);
    });

    renderTable(
      $('forecastTableHost'),
      [
        { name: 'Target Time', get: (row) => row.target_time },
        { name: 'Made At', get: (row) => row.made_at },
        { name: 'Forecast AQI', num: true, get: (row) => C.fmt(row.aqi) },
        { name: 'Category', get: (row) => makeCatPill(row.category) },
        { name: 'Model', num: true, get: (row) => C.fmt(row.model_aqi) },
        { name: 'Persistence', num: true, get: (row) => C.fmt(row.persistence_aqi) },
        { name: 'Served By', get: (row) => h('span', 'pill ' + (row.source === 'persistence' ? 'warn' : 'good'), null, row.source || '–') },
        { name: 'Inputs', get: (row) => (row.inputs_clean ? 'Clean' : 'Flagged') },
      ],
      fcs,
      st.bootstrapped ? 'No upcoming served forecast rows found.' : 'Bootstrap and run a cycle to produce upcoming forecasts.'
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
      showToast('No upcoming forecasts to export.', 'warn');
      return;
    }
    const csv = toCsv(
      ['target_time', 'made_at', 'aqi', 'category', 'model_aqi', 'persistence_aqi', 'source', 'inputs_clean'],
      fcs.map((f) => [f.target_time, f.made_at, f.aqi, f.category, f.model_aqi, f.persistence_aqi, f.source, f.inputs_clean])
    );
    downloadBlob('aqi_forecasts.csv', csv, 'text/csv;charset=utf-8');
    showToast('Exported ' + fcs.length + ' forecast rows to CSV.', 'good');
  });

  /* ---------- View 2: Diagnostics ---------- */
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
    };
    const arrow = (k) => (state.driftSort.key === k ? (state.driftSort.dir > 0 ? ' ↑' : ' ↓') : '');
    renderTable(
      $('driftFeatureTable'),
      [
        { name: 'Feature' + arrow('name'), onSort: sortBy('name'), get: (r) => r.name },
        { name: 'PSI' + arrow('psi'), num: true, onSort: sortBy('psi'), get: (r) => C.fmt(r.psi, 3) },
        { name: 'Threshold' + arrow('threshold'), num: true, onSort: sortBy('threshold'), get: (r) => C.fmt(r.threshold, 3) },
        { name: 'Ratio' + arrow('ratio'), num: true, onSort: sortBy('ratio'), get: (r) => C.fmt(r.ratio, 2) },
        { name: 'Novelty' + arrow('novel'), num: true, onSort: sortBy('novel'), get: (r) => (r.novel != null ? (r.novel * 100).toFixed(1) + '%' : '–') },
        { name: 'Weight' + arrow('weight'), num: true, onSort: sortBy('weight'), get: (r) => (r.weight != null ? (r.weight * 100).toFixed(1) + '%' : '–') },
        { name: 'Status' + arrow('drifted'), onSort: sortBy('drifted'), get: (r) => h('span', 'pill ' + (r.drifted ? 'critical' : 'good'), null, r.drifted ? 'drifted' : 'ok') },
      ],
      colRows,
      'No per-feature drift statistics recorded yet.'
    );
  }

  function renderDiagnostics(D, A) {
    const st = D.status || {};
    const cyc = st.latest_cycle;
    const thr = D.thresholds || {};

    if (!cyc) {
      setPill($('healthSummaryPill'), 'No cycles recorded', '');
      setPill($('driftLevelPill'), '–', '');
      $('driftScoreNum').textContent = '–';
      $('driftMeter').style.width = '0%';
      $('driftRefMeta').textContent = 'Thresholds: warn ' + C.fmt(thr.drift_warn, 2) + ' · alert ' + C.fmt(thr.drift_alert, 2);
      $('driftedFeaturesList').textContent = '';
      setPill($('perfLevelPill'), '–', '');
      $('perfRatioNum').textContent = '–';
      $('perfMeter').style.width = '0%';
      $('perfDetailMeta').textContent = 'No scored cycle yet';
      $('perfCovMeta').textContent = '';
      setPill($('cusumAlarmPill'), '–', '');
      $('cusumGNum').textContent = '–';
      $('cusumMeter').style.width = '0%';
      $('cusumMeta').textContent = 'Alarm threshold: ' + C.fmt(thr.cusum_alarm, 1);
      $('qualityMeta').textContent = 'Quality bad fraction: –';
      renderDriftFeatureTable(null);
    } else {
      const dr = cyc.drift || {};
      const pf = cyc.performance || {};
      const cu = cyc.cusum || {};
      const dec = cyc.decision || {};
      const act = dec.action || 'none';
      setPill($('healthSummaryPill'), 'Cycle ' + cyc.cycle_time + ' · ' + act, act === 'hold' || act === 'retrain' ? 'warn' : 'good');

      const dCls = dr.level === 'alert' ? 'critical' : dr.level === 'warn' ? 'warn' : 'good';
      setPill($('driftLevelPill'), dr.level || '–', dCls);
      $('driftScoreNum').textContent = C.fmt(dr.score, 3);
      $('driftMeter').style.width = Math.min(100, (dr.score || 0) * 100) + '%';
      $('driftMeter').style.background = dr.level === 'alert' ? 'var(--critical)' : dr.level === 'warn' ? 'var(--warn)' : 'var(--s1)';
      $('driftRefMeta').textContent = 'Ref: ' + (dr.reference || '–') + ' (' + (dr.reference_rows || 0) + ' rows) · warn ' + C.fmt(thr.drift_warn, 2) + ' / alert ' + C.fmt(thr.drift_alert, 2);

      const dList = $('driftedFeaturesList');
      dList.textContent = '';
      const drifted = dr.drifted || [];
      if (!drifted.length) {
        setPill(h('span', 'pill', dList), 'No features drifted', 'good');
      } else {
        drifted.forEach((col) => {
          const info = dr.columns && dr.columns[col] ? dr.columns[col] : {};
          const lbl = col + ' (PSI ' + C.fmt(info.psi, 2) + (info.novel ? ', nov ' + Math.round(info.novel * 100) + '%' : '') + ')';
          setPill(h('span', 'pill', dList), lbl, 'critical');
        });
      }
      renderDriftFeatureTable(dr);

      const pCls = pf.level === 'critical' ? 'critical' : pf.level === 'warn' ? 'warn' : pf.level === 'ok' ? 'good' : '';
      setPill($('perfLevelPill'), pf.level || 'unknown', pCls);
      $('perfRatioNum').textContent = pf.ratio != null ? C.fmt(pf.ratio, 2) + 'x expected' : '–';
      $('perfMeter').style.width = pf.ratio != null ? Math.min(100, (pf.ratio / 2.0) * 100) + '%' : '0%';
      $('perfMeter').style.background = pf.level === 'critical' ? 'var(--critical)' : pf.level === 'warn' ? 'var(--warn)' : 'var(--s1)';
      $('perfDetailMeta').textContent = pf.mae != null ? 'MAE ' + C.fmt(pf.mae) + ' · Persistence ' + C.fmt(pf.mae_naive) + ' · Rel ' + C.fmt(pf.rel_mae, 2) : 'Insufficient matured hours in window';
      $('perfCovMeta').textContent = 'Coverage: ' + (pf.coverage != null ? (pf.coverage * 100).toFixed(0) + '%' : '–') + (pf.category_accuracy != null ? ' · Cat acc: ' + (pf.category_accuracy * 100).toFixed(0) + '%' : '');

      setPill($('cusumAlarmPill'), cu.alarm ? 'ALARM' : 'OK', cu.alarm ? 'critical' : 'good');
      $('cusumGNum').textContent = C.fmt(cu.g, 2);
      const cMax = thr.cusum_alarm || 4.0;
      $('cusumMeter').style.width = Math.min(100, ((cu.g || 0) / cMax) * 100) + '%';
      $('cusumMeter').style.background = cu.alarm ? 'var(--critical)' : 'var(--s1)';
      $('cusumMeta').textContent = 'Alarm threshold: ' + C.fmt(cMax, 1) + ' · Perf streak: ' + (cyc.perf_streak || 0) + ' · Drift streak: ' + (cyc.drift_streak || 0);
      const q = cyc.quality || {};
      $('qualityMeta').textContent = 'Quality: ' + (q.status || '–') + ' · Core bad frac: ' + (cyc.quality_bad_fraction != null ? (cyc.quality_bad_fraction * 100).toFixed(1) + '%' : '–');
    }

    // Feature importance chart
    const impList = (A && A.feature_importance) || [];
    C.drawHorizontalBars(
      $('importanceChartHost'),
      impList.slice(0, 12).map((it) => ({ label: it.feature, value: it.importance })),
      {
        emptyText: 'Train or bootstrap a champion model to view feature importance.',
        ariaLabel: 'Champion feature importance',
        fmtVal: (v) => (v * 100).toFixed(1) + '%',
      }
    );

    // Monthly MAE chart & legend
    const monthlyRows = (A && A.monthly_mae) || D.monthly_mae || [];
    const mLeg = $('monthlyMaeLegend');
    mLeg.textContent = '';
    [
      ['pipeline', 'This pipeline', 'var(--s1)'],
      ['scheduled', 'Fixed monthly', 'var(--s2)'],
      ['persistence', 'Persistence', 'var(--s3)'],
      ['static', 'Never retrained', 'var(--quiet)'],
    ].forEach(([key, label, color]) => {
      if (!monthlyRows.some((r) => r[key] != null)) return;
      const sp = h('span', null, mLeg);
      const sw = h('i', 'swatch', sp);
      sw.style.background = color;
      sp.appendChild(document.createTextNode(label));
    });
    C.drawGroupedBars($('monthlyMaeChartHost'), monthlyRows);

    // Cycle signals & timeline
    C.drawCycleSignals($('cycleChartHost'), D.cycles || [], thr);
    const revCycles = (D.cycles || []).slice().reverse().slice(0, 60);
    renderTable(
      $('cycleTimelineHost'),
      [
        { name: 'Date', get: (r) => r.date },
        { name: 'Action', get: (r) => h('span', 'pill ' + (r.action === 'retrain' || r.action === 'hold' ? 'warn' : r.action === 'watch' ? 'info' : 'good'), null, r.action) },
        { name: 'Ratio', num: true, get: (r) => C.fmt(r.ratio, 2) },
        { name: 'Drift', num: true, get: (r) => C.fmt(r.drift, 2) },
        { name: 'CUSUM', num: true, get: (r) => C.fmt(r.cusum, 2) },
        { name: 'Champion', get: (r) => r.champion || '–' },
      ],
      revCycles,
      'No cycle decisions logged yet.'
    );
  }

  /* ---------- View 3: What-If & Batch Studio ---------- */
  const controlInputs = {};

  (function buildPlaygroundControls() {
    const host = $('playgroundControls');
    PLAYGROUND_FIELDS.forEach((f) => {
      const box = h('div', 'ctrl-box', host);
      const top = h('div', 'ctrl-top', box);
      h('label', null, top, f.label).setAttribute('for', 'num-' + f.key);
      const num = h('input', null, top);
      num.type = 'number';
      num.id = 'num-' + f.key;
      num.min = f.min;
      num.max = f.max;
      num.step = f.step;
      num.value = f.def;

      const rng = h('input', null, box);
      rng.type = 'range';
      rng.id = 'rng-' + f.key;
      rng.min = f.min;
      rng.max = f.max;
      rng.step = f.step;
      rng.value = f.def;
      rng.setAttribute('aria-label', f.label);

      rng.addEventListener('input', () => {
        state.stuckFaultActive = false;
        num.value = rng.value;
      });
      num.addEventListener('input', () => {
        state.stuckFaultActive = false;
        const v = Number(num.value);
        if (!Number.isNaN(v)) rng.value = Math.min(f.max, Math.max(f.min, v));
      });
      controlInputs[f.key] = { num, rng, def: f.def };
    });
    C.drawAqiGauge($('predictGaugeHost'), { aqi: null, category: 'Not run' });
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
  }

  function applyPreset(name) {
    state.stuckFaultActive = name === 'stuck';
    if (name === 'baseline') {
      syncControlsFromLastRow(state.baseObservations);
      showToast('Reset sliders to station baseline.', 'info');
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
    showToast(name === 'stuck' ? 'Stuck Sensor Fault demo armed (12h flatline on PM2.5).' : 'Applied ' + name + ' preset.', 'info');
  }

  document.querySelectorAll('button[data-preset]').forEach((btn) => {
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
    const label =
      state.obsSource === 'station'
        ? 'Station history: ' + payload.count + 'h (' + payload.start + ' to ' + payload.end + ')'
        : 'Synthetic sample: ' + payload.count + 'h (' + payload.start + ' to ' + payload.end + ')';
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

  async function runPredict() {
    if (!state.baseObservations.length) {
      await loadPlaygroundObservations(false);
    }
    const observations = buildAdjustedObservations();
    const btn = $('runPredictBtn');
    btn.disabled = true;
    btn.textContent = 'Scoring…';
    try {
      const res = await fetch('/predict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observations }),
      });
      const body = await res.json();
      if (!res.ok) {
        C.drawAqiGauge($('predictGaugeHost'), { aqi: null, category: 'HTTP ' + res.status });
        setPill($('predCatBadge'), 'HTTP ' + res.status, 'critical');
        setPill($('predQualityBadge'), 'unavailable', 'warn');
        $('predCurrentAqi').textContent = 'Current AQI: –';
        setPill($('predDelta'), 'Delta: –', '');
        setPill($('predClean'), 'Inputs: –', '');
        $('predTimestamps').textContent = body.detail || 'Prediction failed';
        $('predIssuesHost').textContent = body.detail || 'Train or bootstrap a champion model before running live scoring.';
        return;
      }

      C.drawAqiGauge($('predictGaugeHost'), { aqi: body.aqi, category: body.category });
      $('predAqiVal').textContent = C.fmt(body.aqi, 1);
      $('predCatBadge').className = 'pill';
      $('predCatBadge').textContent = '';
      const dot = h('i', 'dot-c', $('predCatBadge'));
      dot.style.background = C.catColor(body.category);
      $('predCatBadge').appendChild(document.createTextNode(body.category));

      $('predCurrentAqi').textContent = 'Current AQI: ' + C.fmt(body.current_aqi, 1);
      if (body.current_aqi != null && body.aqi != null) {
        const diff = body.aqi - body.current_aqi;
        setPill($('predDelta'), 'Delta: ' + (diff >= 0 ? '+' : '') + diff.toFixed(1), diff > 15 ? 'warn' : diff < -15 ? 'good' : '');
      } else {
        setPill($('predDelta'), 'Delta: –', '');
      }
      setPill($('predClean'), body.inputs_clean ? 'Inputs: Clean' : 'Inputs: Flagged', body.inputs_clean ? 'good' : 'warn');
      $('predTimestamps').textContent = 'Champion ' + body.champion + ' · Made ' + body.made_at + ' -> Target ' + body.target_time;

      const qStatus = body.data_quality || 'pass';
      setPill($('predQualityBadge'), qStatus, qStatus === 'fail' ? 'critical' : qStatus === 'warn' ? 'warn' : 'good');
      const issuesHost = $('predIssuesHost');
      issuesHost.textContent = '';
      if (body.issues && body.issues.length) {
        body.issues.forEach((iss) => h('div', null, issuesHost, '• ' + iss));
      } else {
        issuesHost.textContent = 'All quality checks passed (0 issues).';
      }
    } catch (err) {
      setPill($('predCatBadge'), 'Network error', 'critical');
      $('predIssuesHost').textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run POST /predict';
    }
  }

  $('loadLatestBtn').addEventListener('click', () => loadPlaygroundObservations(false));
  $('loadSampleBtn').addEventListener('click', () => loadPlaygroundObservations(true));
  $('resetPlaygroundBtn').addEventListener('click', () => {
    state.stuckFaultActive = false;
    syncControlsFromLastRow(state.baseObservations);
  });
  $('runPredictBtn').addEventListener('click', runPredict);

  // Batch CSV Studio
  function renderBatchEmpty() {
    C.drawBatchChart($('batchChartHost'), []);
    renderTable($('batchTableHost'), [{ name: 'Status', get: (r) => r }], [], 'Load a sample CSV or drop a .csv file and run batch scoring.');
  }
  renderBatchEmpty();

  const dz = $('csvDropzone');
  dz.addEventListener('click', () => $('csvFileInput').click());
  dz.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    dz.classList.add('dragover');
  });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', (ev) => {
    ev.preventDefault();
    dz.classList.remove('dragover');
    const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (file) {
      file.text().then((txt) => {
        $('batchCsvInput').value = txt;
        showToast('Loaded ' + file.name, 'info');
      });
    }
  });
  $('csvFileInput').addEventListener('change', (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (file) {
      file.text().then((txt) => {
        $('batchCsvInput').value = txt;
        showToast('Loaded ' + file.name, 'info');
      });
    }
  });

  $('loadSampleCsvBtn').addEventListener('click', async () => {
    let res = await fetch('/observations/recent?hours=72');
    let payload = res.ok ? await res.json() : null;
    if (!payload || !payload.observations || payload.observations.length < 24) {
      res = await fetch('/observations/recent?hours=72&sample=true');
      payload = res.ok ? await res.json() : null;
    }
    if (!payload || !payload.observations || !payload.observations.length) {
      showToast('Could not load sample observations.', 'critical');
      return;
    }
    const cols = ['timestamp', 'pm25', 'pm10', 'no2', 'so2', 'co', 'o3', 'temperature', 'humidity', 'wind_speed', 'precipitation', 'fc_temperature', 'fc_humidity', 'fc_wind_speed', 'fc_precipitation'];
    $('batchCsvInput').value = toCsv(
      cols,
      payload.observations.map((r) => cols.map((c) => r[c]))
    );
    showToast('Loaded ' + payload.observations.length + 'h CSV rows.', 'info');
  });

  $('runBatchBtn').addEventListener('click', async () => {
    const csvText = $('batchCsvInput').value.trim();
    if (!csvText) {
      showToast('Paste CSV rows or click "Load Sample 72h CSV" first.', 'warn');
      return;
    }
    const btn = $('runBatchBtn');
    btn.disabled = true;
    btn.textContent = 'Scoring…';
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
      setPill($('batchQualityPill'), q, q === 'fail' ? 'critical' : q === 'warn' ? 'warn' : 'good');
      const sum = body.summary || {};
      const sp = $('batchSummaryPills');
      sp.textContent = '';
      h('span', 'pill info', sp, 'Scored: ' + body.count);
      h('span', 'pill', sp, 'Mean AQI: ' + C.fmt(sum.mean_aqi));
      h('span', 'pill', sp, 'Min/Max: ' + C.fmt(sum.min_aqi) + ' – ' + C.fmt(sum.max_aqi));

      const ccHost = $('batchCatCounts');
      ccHost.textContent = '';
      Object.entries(sum.category_counts || {}).forEach(([cat, n]) => {
        if (!n) return;
        const p = makeCatPill(cat + ': ' + n);
        p.querySelector('.dot-c').style.background = C.catColor(cat);
        ccHost.appendChild(p);
      });
      $('batchIssuesText').textContent = body.issues && body.issues.length ? 'QC notes: ' + body.issues.join('; ') : 'Quality checks passed.';

      C.drawBatchChart($('batchChartHost'), body.predictions || []);
      renderTable(
        $('batchTableHost'),
        [
          { name: 'Made At', get: (r) => r.made_at },
          { name: 'Target Time', get: (r) => r.target_time },
          { name: 'Predicted AQI', num: true, get: (r) => C.fmt(r.aqi) },
          { name: 'Current AQI', num: true, get: (r) => C.fmt(r.current_aqi) },
          { name: 'Category', get: (r) => makeCatPill(r.category) },
          { name: 'Inputs', get: (r) => (r.inputs_clean ? 'Clean' : 'Flagged') },
        ],
        (body.predictions || []).slice().reverse(),
        'No predictions returned.'
      );
      showToast('Batch scored ' + body.count + ' rows with ' + body.champion + '.', 'good');
    } catch (err) {
      setPill($('batchQualityPill'), 'Error', 'critical');
      $('batchIssuesText').textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run Batch Predict';
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
    showToast('Downloaded ' + preds.length + ' batch predictions.', 'good');
  });

  /* ---------- View 4: Registry & Audit ---------- */
  function populateEventKinds(events) {
    const sel = $('eventKindFilter');
    const prev = sel.value;
    const kinds = Array.from(new Set((events || []).map((e) => e.kind))).sort();
    sel.textContent = '';
    h('option', null, sel, 'All kinds').value = '';
    kinds.forEach((k) => {
      h('option', null, sel, k).value = k;
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
    renderTable(
      $('eventsTableHost'),
      [
        { name: 'Time', get: (r) => String(r.ts || '').slice(0, 16) },
        { name: 'Sev', get: (r) => h('span', 'pill ' + (r.severity === 'error' ? 'critical' : r.severity === 'warning' ? 'warn' : ''), null, r.severity || 'info') },
        { name: 'Kind', get: (r) => h('span', 'pill', null, r.kind) },
        { name: 'Message', get: (r) => r.message },
      ],
      filteredEventsList(),
      'No events match the current filter.'
    );
  }

  $('eventKindFilter').addEventListener('change', renderFilteredEvents);
  $('eventSevFilter').addEventListener('change', renderFilteredEvents);
  $('eventSearch').addEventListener('input', renderFilteredEvents);

  $('exportEventsJsonBtn').addEventListener('click', () => {
    const rows = filteredEventsList();
    downloadBlob('aqi_events.json', JSON.stringify(rows, null, 2), 'application/json;charset=utf-8');
    showToast('Exported ' + rows.length + ' events (JSON).', 'good');
  });

  $('exportEventsCsvBtn').addEventListener('click', () => {
    const rows = filteredEventsList();
    const csv = toCsv(
      ['ts', 'severity', 'kind', 'message'],
      rows.map((r) => [r.ts, r.severity || 'info', r.kind, r.message])
    );
    downloadBlob('aqi_events.csv', csv, 'text/csv;charset=utf-8');
    showToast('Exported ' + rows.length + ' events (CSV).', 'good');
  });

  function renderRegistry(D, A) {
    const versions = (A && A.version_metrics && A.version_metrics.length ? A.version_metrics : D.versions) || [];
    const linHost = $('versionLineageCards');
    linHost.textContent = '';
    if (!versions.length) {
      h('div', 'empty-msg', linHost, 'No models registered yet.');
    } else {
      versions
        .slice()
        .reverse()
        .forEach((v) => {
          const card = h('div', 'lineage-card' + (v.stage === 'champion' ? ' is-champion' : v.stage === 'shadow' ? ' is-shadow' : ''), linHost);
          const top = h('div', null, card);
          top.style.display = 'flex';
          top.style.justifyContent = 'space-between';
          top.style.alignItems = 'center';
          h('strong', null, top, v.version);
          const sCls = v.stage === 'champion' ? 'good' : v.stage === 'shadow' ? 'warn' : v.stage === 'rolled_back' ? 'critical' : '';
          h('span', 'pill ' + sCls, top, v.stage);
          h('div', 'muted', card, (v.kind || '–') + ' · ' + (v.strategy || '–') + ' · ' + (v.trigger || '–'));
          h('div', null, card, 'Val MAE: ' + C.fmt(v.val_mae) + ' · Ref rel: ' + C.fmt(v.ref_rel_mae, 2));
        });
    }

    renderTable(
      $('versionsTableHost'),
      [
        { name: 'Ver', get: (r) => r.version },
        { name: 'Stage', get: (r) => h('span', 'pill ' + (r.stage === 'champion' ? 'good' : r.stage === 'shadow' ? 'warn' : r.stage === 'rolled_back' ? 'critical' : ''), null, r.stage) },
        { name: 'Model', get: (r) => r.kind || '–' },
        { name: 'Strategy', get: (r) => r.strategy || '–' },
        { name: 'Trigger', get: (r) => r.trigger || '–' },
        { name: 'Rows', num: true, get: (r) => (r.n_train_rows != null ? String(r.n_train_rows) : '–') },
        { name: 'Val MAE', num: true, get: (r) => C.fmt(r.val_mae) },
        { name: 'Ref Rel', num: true, get: (r) => C.fmt(r.ref_rel_mae, 2) },
        { name: 'Trained', get: (r) => String(r.trained_at || '').slice(0, 10) },
      ],
      versions.slice().reverse(),
      'No models registered yet.'
    );

    renderTable(
      $('retrainTableHost'),
      [
        { name: 'Date', get: (r) => r.date },
        { name: 'Trigger', get: (r) => r.trigger || '–' },
        { name: 'Outcome', get: (r) => h('span', 'pill ' + (r.outcome === 'promoted' ? 'good' : 'warn'), null, r.outcome + (r.version ? ' ' + r.version : '')) },
        { name: 'Champ MAE', num: true, get: (r) => C.fmt(r.champion_mae) },
        { name: 'Best', get: (r) => r.best || '–' },
        { name: 'Chall MAE', num: true, get: (r) => C.fmt(r.best_mae) },
        { name: 'P(better)', num: true, get: (r) => C.fmt(r.prob, 2) },
      ],
      (D.retrain_log || []).slice().reverse(),
      'No retraining attempts recorded yet.'
    );
  }

  /* ---------- View 5: Pipeline Architecture & Config ---------- */
  function renderPipelineView(A, cfg) {
    const stages = (A && A.pipeline_stages) || [
      { id: 'ingest', name: 'Ingest', status: 'empty', metric: '–', detail: 'No data ingested yet.' },
      { id: 'quality', name: 'Quality Gate', status: 'empty', metric: '–', detail: 'Waiting for observations.' },
      { id: 'forecast', name: 'Forecast', status: 'empty', metric: '–', detail: 'No forecast run yet.' },
      { id: 'monitor', name: 'Monitor', status: 'empty', metric: '–', detail: 'No cycle snapshot yet.' },
      { id: 'decide', name: 'Decide', status: 'empty', metric: '–', detail: 'No policy decision yet.' },
      { id: 'retrain', name: 'Retrain & Gate', status: 'empty', metric: '–', detail: 'No retraining attempts yet.' },
      { id: 'registry', name: 'Registry', status: 'empty', metric: '–', detail: 'No models registered yet.' },
    ];
    if (!state.selectedStageId && stages.length) state.selectedStageId = stages[0].id;

    const flowHost = $('pipelineFlowHost');
    flowHost.textContent = '';
    stages.forEach((st, idx) => {
      const btn = h('button', 'stage-node' + (st.id === state.selectedStageId ? ' selected' : ''), flowHost);
      btn.type = 'button';
      h('div', 'stage-idx', btn, 'STAGE ' + (idx + 1));
      h('div', 'stage-name', btn, st.name);
      const cls = st.status === 'ok' ? 'good' : st.status === 'warn' ? 'warn' : st.status === 'alert' ? 'critical' : '';
      h('span', 'pill ' + cls, btn, st.status);
      h('div', 'muted', btn, st.metric || '–');
      btn.addEventListener('click', () => {
        state.selectedStageId = st.id;
        renderPipelineView(A, cfg);
      });
    });

    const activeStage = stages.find((s) => s.id === state.selectedStageId) || stages[0];
    const det = $('stageDetailHost');
    det.textContent = '';
    if (activeStage) {
      h('strong', null, det, activeStage.name + ' (' + activeStage.status + ') — ' + (activeStage.metric || ''));
      h('div', 'sub', det, activeStage.detail || '');
    }

    // Telemetry Strip
    const telHost = $('telemetryStrip');
    telHost.textContent = '';
    if (cfg) {
      const mb = cfg.db_size_bytes != null ? (cfg.db_size_bytes / (1024 * 1024)).toFixed(2) + ' MB' : '0 MB';
      [
        ['SQLite Store', cfg.db_exists ? mb : 'Not created', cfg.run_dir || 'runs/default'],
        ['Model Registry', String(cfg.version_count ?? 0) + ' versions', 'Champion: ' + (cfg.champion || 'none')],
        ['Cycles & Events', String(cfg.cycle_count ?? 0) + ' cycles', String(cfg.event_count ?? 0) + ' audit events'],
        ['Environment', 'Python ' + (cfg.python_version || '–'), cfg.report_exists ? 'Report ready (/report)' : 'Report not built yet'],
      ].forEach(([label, val, sub]) => {
        const tile = h('div', 'tile', telHost);
        h('div', 'label', tile, label);
        h('div', 'value', tile, val);
        h('div', 'delta', tile, sub);
      });
    }

    // Config Inspector
    const cfgHost = $('configInspectorHost');
    cfgHost.textContent = '';
    if (cfg) {
      const sections = [
        ['Station & Source', { location: cfg.location, source_type: cfg.source && cfg.source.type, simulation_seed: cfg.simulation && cfg.simulation.seed, simulation_events: cfg.simulation && cfg.simulation.event_count }],
        ['Data & Horizon', cfg.data || {}],
        ['Model Config', { candidates: (cfg.model && cfg.model.candidates || []).join(', '), target_mode: cfg.model && cfg.model.target_mode, cv_folds: cfg.model && cfg.model.cv_folds, cv_test_days: cfg.model && cfg.model.cv_test_days, train_stride_hours: cfg.model && cfg.model.train_stride_hours }],
        ['Monitor Thresholds', { drift_warn: cfg.monitor && cfg.monitor.drift_warn_score, drift_alert: cfg.monitor && cfg.monitor.drift_alert_score, perf_warn: cfg.monitor && cfg.monitor.perf_warn_ratio, perf_critical: cfg.monitor && cfg.monitor.perf_critical_ratio, cusum_alarm: cfg.monitor && cfg.monitor.cusum_alarm, safe_enter: cfg.monitor && cfg.monitor.safe_enter_ratio }],
        ['Retrain Policy', { strategies: (cfg.retrain && cfg.retrain.strategies || []).join(', '), cooldown_days: cfg.retrain && cfg.retrain.cooldown_days, min_improvement: cfg.retrain && cfg.retrain.min_improvement, confidence: cfg.retrain && cfg.retrain.confidence, probation_days: cfg.retrain && cfg.retrain.probation_days, max_age_days: cfg.retrain && cfg.retrain.max_age_days }],
      ];
      sections.forEach(([title, obj]) => {
        const card = h('div', 'kv-card', cfgHost);
        h('h3', null, card, title);
        Object.entries(obj).forEach(([k, v]) => {
          const row = h('div', 'kv-row', card);
          h('span', null, row, k);
          h('strong', 'mono', row, v == null ? '–' : String(v));
        });
      });
    }

    // Quick CLI & API Reference
    const cliHost = $('cliRefHost');
    cliHost.textContent = '';
    [
      ['Run Full Demo Simulation', 'aqi-pipeline --run-dir runs/demo simulate'],
      ['Serve Live Console & API', 'aqi-pipeline --run-dir runs/demo serve --port 8000'],
      ['Inspect Status & Build Report', 'aqi-pipeline --run-dir runs/demo status && aqi-pipeline --run-dir runs/demo report'],
      ['Docker Compose Demo Profile', 'docker compose --profile demo up --build'],
    ].forEach(([title, cmd]) => {
      const card = h('div', 'kv-card', cliHost);
      h('h3', null, card, title);
      const code = h('div', 'mono', card, cmd);
      code.style.marginBottom = '8px';
      code.style.wordBreak = 'break-word';
      const cp = h('button', 'btn sm', card, 'Copy command');
      cp.type = 'button';
      cp.addEventListener('click', () => {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(cmd).then(() => showToast('Copied: ' + cmd, 'good')).catch(() => {});
        } else {
          showToast(cmd, 'info');
        }
      });
    });
  }

  /* ---------- Data Fetching (Race-Free) ---------- */
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
      $('emptyStateText').textContent = 'Could not load dashboard data (' + err.message + ').';
    }
  }

  $('horizonSelector')
    .querySelectorAll('button[data-hours]')
    .forEach((btn) => {
      btn.addEventListener('click', () => {
        state.hours = Number(btn.getAttribute('data-hours')) || 72;
        $('horizonSelector')
          .querySelectorAll('button')
          .forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        loadAllData();
      });
    });

  $('refreshBtn').addEventListener('click', () => {
    loadAllData();
    showToast('Dashboard refreshed.', 'info');
  });

  function updateCountdownLabel() {
    if (!state.autoTimer) return;
    const left = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
    $('autoRefreshLabel').textContent = 'Auto: On (' + left + 's)';
  }

  $('autoRefreshBtn').addEventListener('click', () => {
    if (state.autoTimer) {
      clearInterval(state.autoTimer);
      clearInterval(state.countdownTimer);
      state.autoTimer = null;
      state.countdownTimer = null;
      $('autoRefreshLabel').textContent = 'Auto: Off';
      $('autoRefreshDot').classList.add('off');
      $('autoRefreshBtn').setAttribute('aria-pressed', 'false');
    } else {
      state.nextRefreshAt = Date.now() + 30000;
      state.autoTimer = setInterval(() => {
        state.nextRefreshAt = Date.now() + 30000;
        loadAllData();
      }, 30000);
      state.countdownTimer = setInterval(updateCountdownLabel, 1000);
      updateCountdownLabel();
      $('autoRefreshDot').classList.remove('off');
      $('autoRefreshBtn').setAttribute('aria-pressed', 'true');
    }
  });

  loadAllData().then(() => loadPlaygroundObservations(false).catch(() => {}));
})();
