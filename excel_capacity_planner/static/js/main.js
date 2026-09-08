(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const source = $('dataSource');
  const dtConfig = $('dynatraceConfig');
  const excelConfig = $('excelConfig');
  const tenantUrl = $('tenantUrl');
  const accessToken = $('accessToken');
  const connectBtn = $('connectBtn');
  const connectionStatus = $('connectionStatus');
  const excelFile = $('excelFile');
  const uploadBtn = $('uploadBtn');
  const uploadStatus = $('uploadStatus');
  const zoneSearch = $('zoneSearch');
  const zoneResults = $('zoneResults');
  const selectedZone = $('selectedZone');
  const zoneName = $('selectedZoneName');
  const clearZone = $('clearZone');
  const sourceHint = $('sourceHint');
  const from = $('histFrom');
  const to = $('histTo');
  const months = $('forecastMonths');
  const slider = $('growthSlider');
  const growth = $('growthValue');
  const kpis = $('kpiGrid');
  const status = $('simStatus');
  const appTable = $('applicationTable').querySelector('tbody');
  const appCount = $('applicationCount');
  const gen = $('generateBtn');
  const genStatus = $('generateStatus');
  const downloads = $('downloadArea');

  let sourceId = '';
  let currentZone = 'CBDCE_RUPISwitch_1418';
  let bundle = null;
  let charts = {};
  let sourceReady = false;

  const keys = ['host_cpu_usage', 'host_mem_usage', 'service_request_count', 'service_response_time'];
  const labels = {
    host_cpu_usage: 'CPU',
    host_mem_usage: 'Memory',
    service_request_count: 'Requests / day',
    service_response_time: 'Response time'
  };

  function apiJson(url, options) {
    return fetch(url, options).then(async response => {
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || 'Request failed');
      return body;
    });
  }

  function setSourcePanels() {
    const value = source.value;
    dtConfig.hidden = value !== 'live';
    excelConfig.hidden = value !== 'excel';

    if (value === 'mock') {
      sourceHint.textContent = 'Using the original workflow with deterministic mock Excel telemetry.';
      currentZone = 'CBDCE_RUPISwitch_1418';
      zoneName.textContent = currentZone;
      zoneSearch.value = '';
      if (sourceId) {
        sourceReady = true;
        loadMock();
      }
    } else if (value === 'excel') {
      sourceHint.textContent = 'Upload the Excel template to load application-wise telemetry into the same UI.';
      zoneName.textContent = 'Upload an Excel dataset';
      bundle = null;
      status.textContent = 'Upload an Excel dataset to begin.';
      appTable.innerHTML = '';
      kpis.innerHTML = '';
    } else {
      sourceHint.textContent = 'Enter the Dynatrace tenant URL and API access token, then connect.';
      zoneName.textContent = 'Dynatrace — connect to load management zones';
      bundle = null;
      status.textContent = 'Connect to Dynatrace to begin.';
      appTable.innerHTML = '';
      kpis.innerHTML = '';
    }
  }

  function loadMock() {
    if (!sourceId) return;
    apiJson('/api/timeseries', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        source_id: sourceId,
        management_zone: currentZone,
        hist_from: from.value,
        hist_to: to.value,
        forecast_months: +months.value,
        growth_pct: +slider.value
      })
    }).then(data => {
      bundle = data;
      sourceReady = true;
      build();
      apply(+slider.value);
    }).catch(e => status.textContent = e.message);
  }

  function loadData() {
    if (!sourceReady || !sourceId) {
      status.textContent = 'Select and configure a data source first.';
      return;
    }
    status.textContent = 'Loading historical data and forecast...';
    apiJson('/api/timeseries', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        source_id: sourceId,
        management_zone: currentZone,
        hist_from: from.value,
        hist_to: to.value,
        forecast_months: +months.value,
        growth_pct: +slider.value
      })
    }).then(data => {
      bundle = data;
      build();
      apply(+slider.value);
    }).catch(e => {
      status.textContent = e.message;
      sourceReady = false;
    });
  }

  function build() {
    keys.forEach(k => {
      if (charts[k]) charts[k].destroy();
      const p = bundle.metrics[k];
      const c = $('chart-' + k);
      if (!p) return;
      const hist = p.historical || [];
      const fc = p.forecast || [];
      const up = p.upper || [];
      const lo = p.lower || [];
      const labelsX = hist.map(x => new Date(x.t).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}))
        .concat(fc.map(x => new Date(x.t).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})));
      const lead = a => Array(Math.max(0, hist.length - 1)).fill(null)
        .concat(hist.length ? [hist.at(-1).v] : []).concat(a);
      const base = lead(fc.map(x => x.v));

      charts[k] = new Chart(c, {
        type: 'line',
        data: {labels: labelsX, datasets: [
          {label: 'Historical', data: hist.map(x => x.v).concat(fc.map(() => null)), borderColor: '#1f4e79', pointRadius: 0, borderWidth: 2},
          {label: 'Forecast range upper', data: lead(up.map(x => x.v)), borderWidth: 0, pointRadius: 0},
          {label: 'Forecast range lower', data: lead(lo.map(x => x.v)), borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: 'rgba(31,78,121,.15)'},
          {label: 'Forecast (baseline)', data: base, borderColor: '#9aa7b2', borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5},
          {label: 'Forecast (simulated)', data: base.slice(), borderColor: '#c55a11', borderDash: [5, 4], pointRadius: 0, borderWidth: 2}
        ]},
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: {mode: 'index', intersect: false},
          plugins: {
            title: {display: true, text: p.label + ' (' + p.unit + ')', font: {size: 13}},
            legend: {position: 'bottom', labels: {font: {size: 10}, filter: i => !i.text.startsWith('Forecast range')}}
          },
          scales: {
            x: {ticks: {maxTicksLimit: 8, font: {size: 9}}},
            y: {beginAtZero: false, suggestedMax: p.cap || undefined, ticks: {font: {size: 9}}}
          }
        }
      });
    });
  }

  function risk(k, value) {
    if (value == null) return 'No data';
    if (k === 'host_cpu_usage' || k === 'host_mem_usage') {
      if (value >= 95) return 'Critical';
      if (value >= 85) return 'High risk';
      if (value >= 70) return 'Watch';
      return 'Healthy';
    }
    if (k === 'service_response_time') {
      if (value >= 1000) return 'Critical';
      if (value >= 500) return 'High risk';
      if (value >= 250) return 'Watch';
      return 'Healthy';
    }
    return 'Informational';
  }

  function apply(g) {
    if (!bundle) return;
    growth.textContent = (g >= 0 ? '+' : '') + g + '%';
    document.querySelectorAll('.scenario-btn').forEach(b => b.classList.toggle('active', +b.dataset.growth === g));
    const sf = 1 + g / 100;
    let html = '';
    const parts = [];

    keys.forEach(k => {
      const p = bundle.metrics[k];
      const ch = charts[k];
      const elasticity = k === 'service_response_time' ? 1.15 : 1;
      const cap = p.cap;
      const scale = value => cap ? Math.min(cap, value * sf ** elasticity) : value * sf ** elasticity;
      if (ch) {
        ch.data.datasets[4].data = ch.data.datasets[3].data.map(v => v == null ? null : scale(v));
        ch.update('none');
      }
      const values = (p.historical || []).slice(-14).map(x => x.v);
      const current = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
      const projected = current == null ? null : scale(current);
      const r = risk(k, projected);
      html += `<article class="kpi-card risk-${r.toLowerCase().replace(' ', '-')}">
        <div class="kpi-label">${labels[k]}</div>
        <div class="kpi-values"><strong>${projected == null ? 'No data' : projected.toLocaleString(undefined, {maximumFractionDigits: k === 'service_request_count' ? 0 : 1})} ${p.unit}</strong>
        <span>from ${current == null ? 'No data' : current.toLocaleString(undefined, {maximumFractionDigits: k === 'service_request_count' ? 0 : 1})} ${p.unit}</span></div>
        <div class="kpi-meta"><span>${r}</span><span>${current ? ((projected - current) / current * 100).toFixed(1) + '%' : ''}</span></div>
      </article>`;
      parts.push(labels[k] + ' ' + (current == null ? '—' : current.toFixed(1)) + ' → ' + (projected == null ? '—' : projected.toFixed(1)));
    });

    kpis.innerHTML = html;
    status.textContent = parts.join('   |   ');
    renderApplications(g);
  }

  function renderApplications(g) {
    const apps = bundle.applications || [];
    appCount.textContent = apps.length + ' application(s)';
    appTable.innerHTML = '';
    apps.forEach(row => {
      const growthFactor = 1 + g / 100;
      const requestForecast = row.request_forecast * growthFactor;
      const responseForecast = row.response_forecast * growthFactor ** 1.15;
      const statusText = responseForecast >= 500 ? 'At Risk' : (responseForecast >= 250 ? 'Watch' : 'Normal');
      const statusClass = statusText === 'At Risk' ? 'app-risk' : (statusText === 'Watch' ? 'app-watch' : 'app-normal');
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${escapeHtml(row.application)}</td><td>${formatNumber(row.requests_avg)}</td><td>${formatNumber(row.response_avg, 1)} ms</td><td>${formatNumber(requestForecast)}</td><td>${formatNumber(responseForecast, 1)} ms</td><td><span class="app-status ${statusClass}">${statusText}</span></td>`;
      appTable.appendChild(tr);
    });
  }

  function formatNumber(v, decimals = 0) {
    return Number(v || 0).toLocaleString(undefined, {maximumFractionDigits: decimals});
  }

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'}[c]));
  }

  function searchZones() {
    const q = zoneSearch.value.trim();
    if (!q) { zoneResults.innerHTML = ''; return; }
    fetch('/api/management-zones?q=' + encodeURIComponent(q) + '&source_id=' + encodeURIComponent(sourceId))
      .then(r => r.json()).then(data => {
        zoneResults.innerHTML = '';
        const zones = data.zones || [];
        if (!zones.length) {
          const item = document.createElement('div');
          item.className = 'autocomplete-item';
          item.textContent = q;
          item.onclick = () => chooseZone(q);
          zoneResults.appendChild(item);
          return;
        }
        zones.slice(0, 10).forEach(z => {
          const item = document.createElement('div');
          item.className = 'autocomplete-item';
          item.textContent = z.name;
          item.onclick = () => chooseZone(z.name);
          zoneResults.appendChild(item);
        });
      }).catch(() => {});
  }

  function chooseZone(name) {
    currentZone = name;
    zoneName.textContent = name;
    selectedZone.hidden = false;
    zoneSearch.hidden = true;
    zoneResults.innerHTML = '';
    if (sourceReady) loadData();
  }

  source.onchange = () => {
    sourceId = '';
    sourceReady = false;
    setSourcePanels();
    if (source.value === 'mock') {
      apiJson('/api/mock-source', {method: 'POST'}).then(data => {
        sourceId = data.source_id;
        sourceReady = true;
        loadMock();
      }).catch(e => status.textContent = e.message);
    }
  };

  connectBtn.onclick = () => {
    connectionStatus.textContent = 'Validating Dynatrace connection...';
    connectBtn.disabled = true;
    apiJson('/api/connect-dynatrace', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tenant_url: tenantUrl.value, access_token: accessToken.value})
    }).then(data => {
      sourceId = data.source_id;
      sourceReady = true;
      connectionStatus.textContent = 'Connected. Search/select a management zone below.';
      currentZone = '';
      zoneName.textContent = 'Enter/search a management zone';
      zoneSearch.focus();
    }).catch(e => {
      connectionStatus.textContent = e.message;
      sourceReady = false;
    }).finally(() => connectBtn.disabled = false);
  };

  uploadBtn.onclick = () => {
    const file = excelFile.files[0];
    if (!file) { uploadStatus.textContent = 'Choose an Excel file first.'; return; }
    const form = new FormData();
    form.append('excel_file', file);
    uploadStatus.textContent = 'Reading Excel and validating the dataset...';
    uploadBtn.disabled = true;
    apiJson('/api/upload-excel', {method: 'POST', body: form})
      .then(data => {
        sourceId = data.source_id;
        sourceReady = true;
        currentZone = data.management_zone;
        zoneName.textContent = currentZone;
        zoneSearch.value = '';
        from.value = data.from;
        to.value = data.to;
        uploadStatus.textContent = `Loaded ${data.rows.toLocaleString()} rows across ${data.applications.length} application(s).`;
        loadData();
      }).catch(e => {
        uploadStatus.textContent = e.message;
        sourceReady = false;
      }).finally(() => uploadBtn.disabled = false);
  };

  clearZone.onclick = () => { zoneSearch.hidden = false; zoneSearch.focus(); };
  zoneSearch.oninput = searchZones;
  slider.oninput = () => apply(+slider.value);
  document.querySelectorAll('.scenario-btn').forEach(b => b.onclick = () => { slider.value = b.dataset.growth; apply(+b.dataset.growth); });
  [from, to, months].forEach(x => x.onchange = () => { if (sourceReady) loadData(); });

  gen.onclick = () => {
    if (!sourceReady || !sourceId) { genStatus.textContent = 'Configure a data source before generating the report.'; return; }
    gen.disabled = true;
    genStatus.textContent = 'Generating report...';
    apiJson('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, management_zone: currentZone, hist_from: from.value, hist_to: to.value, forecast_months: +months.value, growth_pct: +slider.value, company_name: $('companyName').value, report_title: $('reportTitle').value})
    }).then(data => {
      genStatus.textContent = 'Report ready.';
      data.files.forEach(f => {
        const a = document.createElement('a');
        a.className = 'download-link';
        a.href = '/api/download/' + encodeURIComponent(f);
        a.textContent = '⬇ Download ' + f;
        downloads.prepend(a);
      });
    }).catch(e => genStatus.textContent = e.message).finally(() => gen.disabled = false);
  };

  setSourcePanels();
  apiJson('/api/mock-source', {method: 'POST'}).then(data => {
    sourceId = data.source_id;
    sourceReady = true;
    if (source.value === 'mock') loadMock();
  }).catch(e => status.textContent = e.message);
})();
