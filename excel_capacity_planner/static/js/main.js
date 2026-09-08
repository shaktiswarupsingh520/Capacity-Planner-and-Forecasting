(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const source = $('dataSource');
  const dtConfig = $('dynatraceConfig');
  const excelConfig = $('excelConfig');
  const zipConfig = $('zipConfig');
  const tenantUrl = $('tenantUrl');
  const accessToken = $('accessToken');
  const connectBtn = $('connectBtn');
  const connectionStatus = $('connectionStatus');
  const excelFile = $('excelFile');
  const uploadBtn = $('uploadBtn');
  const uploadStatus = $('uploadStatus');
  const zipFile = $('zipFile');
  const zipUploadBtn = $('zipUploadBtn');
  const zipUploadStatus = $('zipUploadStatus');
  const zoneSearch = $('zoneSearch');
  const zoneSearchWrap = $('zoneSearchWrap');
  const zoneResults = $('zoneResults');
  const selectedZone = $('selectedZone');
  const zoneName = $('selectedZoneName');
  const clearZone = $('clearZone');
  const applicationSelect = $('applicationSelect');
  const selectedApplication = $('selectedApplication');
  const selectedApplicationName = $('selectedApplicationName');
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
  let currentApplication = '';
  let bundle = null;
  let charts = {};
  let sourceReady = false;

  const keys = ['host_cpu_usage', 'host_mem_usage', 'service_request_count', 'service_response_time'];

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
    zipConfig.hidden = value !== 'zip';
    zoneSearchWrap.hidden = value !== 'live';
    zoneSearch.hidden = false;
    zoneResults.innerHTML = '';
    selectedZone.hidden = value !== 'live';
    applicationSelect.innerHTML = '<option value="">Select an application...</option>';
    applicationSelect.disabled = true;
    selectedApplication.hidden = true;
    currentApplication = '';
    bundle = null;
    appTable.innerHTML = '';
    kpis.innerHTML = '';
    if (value === 'mock') {
      sourceHint.textContent = 'Mock Excel data loaded at application level. Select one application to view its trend and forecast.';
      status.textContent = 'Select an application to begin.';
    } else if (value === 'excel') {
      sourceHint.textContent = 'Upload the Excel template to load application-wise telemetry. Then select one application.';
      status.textContent = 'Upload an Excel dataset to begin.';
    } else if (value === 'zip') {
      sourceHint.textContent = 'Upload a ZIP export. The application list is derived from the entity names in the uploaded data.';
      status.textContent = 'Upload a ZIP dataset to begin.';
    } else {
      sourceHint.textContent = 'Connect to Dynatrace, select a management zone, then select an application/service.';
      zoneName.textContent = 'Dynatrace — connect to load management zones';
      status.textContent = 'Connect to Dynatrace to begin.';
    }
  }

  function populateApplications(apps) {
    applicationSelect.innerHTML = '<option value="">Select an application...</option>';
    (apps || []).forEach(a => {
      const name = typeof a === 'string' ? a : a.name;
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      applicationSelect.appendChild(opt);
    });
    applicationSelect.disabled = !(apps && apps.length);
    if (apps && apps.length) status.textContent = 'Select an application to load its trend, forecast and capacity report.';
  }

  function loadApplications() {
    if (!sourceId) return;
    const params = new URLSearchParams({source_id: sourceId});
    if (currentZone && source.value === 'live') {
      params.set('management_zone', currentZone);
      params.set('hist_from', from.value);
      params.set('hist_to', to.value);
    }
    apiJson('/api/applications?' + params.toString()).then(data => populateApplications(data.applications)).catch(e => status.textContent = e.message);
  }

  function loadData() {
    if (!sourceReady || !sourceId) { status.textContent = 'Select and configure a data source first.'; return; }
    if (!currentApplication) { status.textContent = 'Select an application first.'; return; }
    status.textContent = 'Loading historical data and forecast for ' + currentApplication + '...';
    apiJson('/api/timeseries', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({source_id: sourceId, management_zone: currentZone, application: currentApplication, hist_from: from.value, hist_to: to.value, forecast_months: +months.value, growth_pct: +slider.value})}).then(data => { bundle = data; build(); apply(+slider.value); }).catch(e => { status.textContent = e.message; });
  }

  function build() {
    keys.forEach(k => {
      if (charts[k]) charts[k].destroy();
      const p = bundle.metrics[k], c = $('chart-' + k);
      if (!p) return;
      const hist = p.historical || [], fc = p.forecast || [], up = p.upper || [], lo = p.lower || [];
      const labelsX = hist.map(x => new Date(x.t).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})).concat(fc.map(x => new Date(x.t).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})));
      const lead = a => Array(Math.max(0, hist.length - 1)).fill(null).concat(hist.length ? [hist.at(-1).v] : []).concat(a);
      const base = lead(fc.map(x => x.v));
      charts[k] = new Chart(c, {type: 'line', data: {labels: labelsX, datasets: [
        {label: 'Historical', data: hist.map(x => x.v).concat(fc.map(() => null)), borderColor: '#1f4e79', pointRadius: 0, borderWidth: 2},
        {label: 'Forecast range upper', data: lead(up.map(x => x.v)), borderWidth: 0, pointRadius: 0},
        {label: 'Forecast range lower', data: lead(lo.map(x => x.v)), borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: 'rgba(31,78,121,.15)'},
        {label: 'Forecast (baseline)', data: base, borderColor: '#9aa7b2', borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5},
        {label: 'Forecast (simulated)', data: base.slice(), borderColor: '#c55a11', borderDash: [5, 4], pointRadius: 0, borderWidth: 2}
      ]}, options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: 'index', intersect: false}, plugins: {title: {display: true, text: p.label + ' (' + p.unit + ')', font: {size: 13}}, legend: {position: 'bottom', labels: {font: {size: 10}, filter: i => !i.text.startsWith('Forecast range')}}}, scales: {x: {ticks: {maxTicksLimit: 8, font: {size: 9}}}, y: {beginAtZero: false, suggestedMax: p.cap || undefined, ticks: {font: {size: 9}}}}}});
    });
  }

  function risk(k, value) {
    if (value == null) return 'No data';
    if (k === 'host_cpu_usage' || k === 'host_mem_usage') return value >= 95 ? 'Critical' : value >= 85 ? 'High risk' : value >= 70 ? 'Watch' : 'Healthy';
    if (k === 'service_response_time' && bundle?.metrics?.service_response_time?.unit === 'ms') return value >= 1000 ? 'Critical' : value >= 500 ? 'High risk' : value >= 250 ? 'Watch' : 'Healthy';
    return 'Informational';
  }

  function fmt(v, unit) {
    if (v == null || !Number.isFinite(Number(v))) return 'No data';
    const decimals = unit === '%' ? 1 : unit === 'Count' ? 0 : 1;
    return Number(v).toLocaleString(undefined, {maximumFractionDigits: decimals});
  }

  function apply(g) {
    if (!bundle) return;
    growth.textContent = (g >= 0 ? '+' : '') + g + '%';
    document.querySelectorAll('.scenario-btn').forEach(b => b.classList.toggle('active', +b.dataset.growth === g));
    const sf = 1 + g / 100; let html = ''; const parts = [];
    keys.forEach(k => {
      const p = bundle.metrics[k], ch = charts[k], elasticity = k === 'service_response_time' && p.unit === 'ms' ? 1.15 : 1, cap = p.cap;
      const scale = value => cap ? Math.min(cap, value * sf ** elasticity) : value * sf ** elasticity;
      if (ch) { ch.data.datasets[4].data = ch.data.datasets[3].data.map(v => v == null ? null : scale(v)); ch.update('none'); }
      const values = (p.historical || []).slice(-14).map(x => x.v); const current = values.length ? values.reduce((a,b) => a+b,0)/values.length : null; const projected = current == null ? null : scale(current); const r = risk(k, projected);
      html += `<article class="kpi-card risk-${r.toLowerCase().replace(' ', '-')}"><div class="kpi-label">${escapeHtml(p.label || k)}</div><div class="kpi-values"><strong>${fmt(projected,p.unit)} ${projected == null ? '' : escapeHtml(p.unit)}</strong><span>from ${fmt(current,p.unit)} ${current == null ? '' : escapeHtml(p.unit)}</span></div><div class="kpi-meta"><span>${r}</span><span>${current ? ((projected-current)/current*100).toFixed(1)+'%' : ''}</span></div></article>`;
      parts.push((p.label || k) + ' ' + (current == null ? '—' : fmt(current,p.unit)) + ' → ' + (projected == null ? '—' : fmt(projected,p.unit)));
    });
    kpis.innerHTML = html; status.textContent = currentApplication + ' — ' + parts.join('   |   '); renderApplications(g);
  }

  function renderApplications(g) {
    const apps = bundle.applications || [];
    const wl = bundle.summary?.workload_label || 'Workload';
    const pl = bundle.summary?.performance_label || 'Performance';
    appCount.textContent = apps.length + ' application(s) — selected only';
    document.querySelector('#applicationTable thead th:nth-child(2)').textContent = 'Avg ' + wl;
    document.querySelector('#applicationTable thead th:nth-child(3)').textContent = 'Avg ' + pl;
    document.querySelector('#applicationTable thead th:nth-child(4)').textContent = 'Forecast ' + wl;
    document.querySelector('#applicationTable thead th:nth-child(5)').textContent = 'Forecast ' + pl;
    appTable.innerHTML = '';
    apps.forEach(row => {
      const requestForecast = row.request_forecast * (1 + g / 100), responseForecast = row.response_forecast * (1 + g / 100) ** (bundle.metrics.service_response_time.unit === 'ms' ? 1.15 : 1);
      const performanceUnit = bundle.metrics.service_response_time.unit || '';
      const statusText = responseForecast >= 500 && performanceUnit === 'ms' ? 'At Risk' : responseForecast >= 250 && performanceUnit === 'ms' ? 'Watch' : 'Normal';
      const statusClass = statusText === 'At Risk' ? 'app-risk' : statusText === 'Watch' ? 'app-watch' : 'app-normal';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${escapeHtml(row.application)}</td><td>${fmt(row.requests_avg,bundle.metrics.service_request_count.unit)}</td><td>${fmt(row.response_avg,performanceUnit)}</td><td>${fmt(requestForecast,bundle.metrics.service_request_count.unit)}</td><td>${fmt(responseForecast,performanceUnit)}</td><td><span class="app-status ${statusClass}">${statusText}</span></td>`;
      appTable.appendChild(tr);
    });
  }

  function escapeHtml(value) { return String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }

  function searchZones() {
    const q = zoneSearch.value.trim(); if (!q) { zoneResults.innerHTML = ''; return; }
    fetch('/api/management-zones?q=' + encodeURIComponent(q) + '&source_id=' + encodeURIComponent(sourceId)).then(r => r.json()).then(data => {
      zoneResults.innerHTML = ''; const zones = data.zones || [];
      if (!zones.length) { const item=document.createElement('div'); item.className='autocomplete-item'; item.textContent=q; item.onclick=()=>chooseZone(q); zoneResults.appendChild(item); return; }
      zones.slice(0,10).forEach(z => { const item=document.createElement('div'); item.className='autocomplete-item'; item.textContent=z.name; item.onclick=()=>chooseZone(z.name); zoneResults.appendChild(item); });
    }).catch(()=>{});
  }

  function chooseZone(name) {
    currentZone=name; zoneName.textContent=name; selectedZone.hidden=false; zoneSearch.hidden=true; zoneResults.innerHTML=''; currentApplication=''; applicationSelect.innerHTML='<option value="">Loading applications...</option>'; applicationSelect.disabled=true; loadApplications();
  }

  source.onchange = () => {
    sourceId=''; sourceReady=false; setSourcePanels();
    if (source.value === 'mock') apiJson('/api/mock-source',{method:'POST'}).then(data=>{sourceId=data.source_id;sourceReady=true;loadApplications();}).catch(e=>status.textContent=e.message);
  };

  connectBtn.onclick = () => {
    connectionStatus.textContent='Validating Dynatrace connection...'; connectBtn.disabled=true;
    apiJson('/api/connect-dynatrace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tenant_url:tenantUrl.value,access_token:accessToken.value})}).then(data=>{sourceId=data.source_id;sourceReady=true;connectionStatus.textContent='Connected. Search/select a management zone below.';currentZone='';zoneName.textContent='Enter/search a management zone';zoneSearch.focus();}).catch(e=>{connectionStatus.textContent=e.message;sourceReady=false;}).finally(()=>connectBtn.disabled=false);
  };

  uploadBtn.onclick = () => {
    const file=excelFile.files[0]; if(!file){uploadStatus.textContent='Choose an Excel file first.';return;}
    const form=new FormData(); form.append('excel_file',file); uploadStatus.textContent='Reading Excel and validating the dataset...'; uploadBtn.disabled=true;
    apiJson('/api/upload-excel',{method:'POST',body:form}).then(data=>{sourceId=data.source_id;sourceReady=true;currentZone=data.management_zone;zoneName.textContent=currentZone;zoneSearch.value='';from.value=data.from;to.value=data.to;uploadStatus.textContent=`Loaded ${data.rows.toLocaleString()} rows across ${data.applications.length} application(s).`;populateApplications(data.applications);}).catch(e=>{uploadStatus.textContent=e.message;sourceReady=false;}).finally(()=>uploadBtn.disabled=false);
  };

  zipUploadBtn.onclick = () => {
    const file=zipFile.files[0]; if(!file){zipUploadStatus.textContent='Choose a ZIP file first.';return;}
    const form=new FormData(); form.append('zip_file',file); zipUploadStatus.textContent='Reading ZIP and building application groups...'; zipUploadBtn.disabled=true;
    apiJson('/api/upload-zip',{method:'POST',body:form}).then(data=>{sourceId=data.source_id;sourceReady=true;currentZone=data.management_zone;from.value=data.from;to.value=data.to;zipUploadStatus.textContent=`Loaded ${data.files} metric files and ${data.rows.toLocaleString()} normalized rows across ${data.applications.length} application group(s).`;populateApplications(data.applications);}).catch(e=>{zipUploadStatus.textContent=e.message;sourceReady=false;}).finally(()=>zipUploadBtn.disabled=false);
  };

  clearZone.onclick=()=>{zoneSearch.hidden=false;selectedZone.hidden=false;currentApplication='';applicationSelect.innerHTML='<option value="">Select an application...</option>';applicationSelect.disabled=true;zoneSearch.focus();};
  zoneSearch.oninput=searchZones;
  applicationSelect.onchange=()=>{currentApplication=applicationSelect.value;selectedApplicationName.textContent=currentApplication||'Select an application';selectedApplication.hidden=!currentApplication;if(currentApplication && $('reportTitle').value==='Capacity & Performance Report') $('reportTitle').value='Capacity & Performance Report — '+currentApplication;if(currentApplication && sourceReady) loadData();};
  slider.oninput=()=>apply(+slider.value);
  document.querySelectorAll('.scenario-btn').forEach(b=>b.onclick=()=>{slider.value=b.dataset.growth;apply(+b.dataset.growth);});
  [from,to,months].forEach(x=>x.onchange=()=>{if(sourceReady && currentApplication) loadData();});

  gen.onclick=()=>{
    if(!sourceReady||!sourceId||!currentApplication){genStatus.textContent='Select a data source and application before generating the report.';return;}
    gen.disabled=true; genStatus.textContent='Generating report...';
    apiJson('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,management_zone:currentZone,application:currentApplication,hist_from:from.value,hist_to:to.value,forecast_months:+months.value,growth_pct:+slider.value,company_name:$('companyName').value,report_title:$('reportTitle').value})}).then(data=>{genStatus.textContent='Report ready for '+currentApplication+'.';data.files.forEach(f=>{const a=document.createElement('a');a.className='download-link';a.href='/api/download/'+encodeURIComponent(f);a.textContent='⬇ Download '+f;downloads.prepend(a);});}).catch(e=>genStatus.textContent=e.message).finally(()=>gen.disabled=false);
  };

  setSourcePanels();
  apiJson('/api/mock-source',{method:'POST'}).then(data=>{sourceId=data.source_id;sourceReady=true;if(source.value==='mock')loadApplications();}).catch(e=>status.textContent=e.message);
})();
