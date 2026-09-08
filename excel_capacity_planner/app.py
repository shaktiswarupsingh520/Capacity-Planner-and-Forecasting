from __future__ import annotations

import io
import os
import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
from capacity_engine import analyze_capacity

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
REQUIRED_COLUMNS = ['timestamp', 'host', 'cpu_pct', 'memory_pct', 'disk_pct']


def load_excel(file_storage) -> pd.DataFrame:
    df = pd.read_excel(file_storage)
    df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]
    aliases = {'datetime':'timestamp','date':'timestamp','time':'timestamp','hostname':'host','host_name':'host',
               'cpu':'cpu_pct','cpu_%':'cpu_pct','cpu_utilization':'cpu_pct','memory':'memory_pct','memory_%':'memory_pct','mem_pct':'memory_pct',
               'disk':'disk_pct','disk_%':'disk_pct','disk_utilization':'disk_pct'}
    df = df.rename(columns={c: aliases.get(c,c) for c in df.columns})
    missing=[c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing: raise ValueError('Missing required columns: '+', '.join(missing))
    df['timestamp']=pd.to_datetime(df['timestamp'],errors='coerce'); df['host']=df['host'].astype(str).str.strip()
    for c in ['cpu_pct','memory_pct','disk_pct']:
        df[c]=pd.to_numeric(df[c],errors='coerce').clip(lower=0,upper=100)
    return df.dropna(subset=['timestamp','host']).sort_values(['host','timestamp']).reset_index(drop=True)


def make_summary(df):
    return {'rows':int(len(df)),'hosts':int(df['host'].nunique()),'from':df['timestamp'].min().strftime('%Y-%m-%d'),'to':df['timestamp'].max().strftime('%Y-%m-%d'),
            'missing':{c:int(df[c].isna().sum()) for c in ['cpu_pct','memory_pct','disk_pct']},'duplicates':int(df.duplicated(subset=['timestamp','host']).sum())}


def build_mock_excel():
    rng=np.random.default_rng(42); dates=pd.date_range('2023-09-01','2026-08-31',freq='D')
    hosts=['PAYMENTS-APP-01','PAYMENTS-APP-02','CORE-BANKING-01','CORE-BANKING-02','MOBILE-API-01','MOBILE-API-02','REPORTING-DB-01','REPORTING-DB-02']
    rows=[]
    for hi,host in enumerate(hosts):
        t=np.arange(len(dates)); weekly=np.sin(2*np.pi*t/7); monthly=np.sin(2*np.pi*t/30.4)
        for i,d in enumerate(dates):
            cpu=45+hi*2.2+0.018*t[i]+4*weekly[i]+1.8*monthly[i]+rng.normal(0,2)
            mem=55+hi*1.4+0.012*t[i]+2.8*weekly[i]+1.2*monthly[i]+rng.normal(0,1.6)
            disk=48+hi*1.8+0.022*t[i]+1.4*monthly[i]+rng.normal(0,1.2)
            if hi in (0,4): cpu+=7*np.sin(2*np.pi*i/365.25)
            if hi in (2,6): disk+=4*np.sin(2*np.pi*i/365.25)
            rows.append([d,host,cpu,mem,disk])
    df=pd.DataFrame(rows,columns=REQUIRED_COLUMNS); out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        df.to_excel(writer,index=False,sheet_name='Telemetry')
        pd.DataFrame({'Field':REQUIRED_COLUMNS,'Description':['Telemetry timestamp','Host identifier','CPU utilization %','Memory utilization %','Disk utilization %']}).to_excel(writer,index=False,sheet_name='Data Dictionary')
    out.seek(0); return out


def analysis_from_request(df,horizon_months,growth_pct):
    daily=(df.set_index('timestamp').groupby('host')[['cpu_pct','memory_pct','disk_pct']].resample('1D').mean().reset_index())
    return analyze_capacity(daily,horizon_months=horizon_months,growth_pct=growth_pct)

@app.route('/')
def index(): return render_template('index.html')

@app.post('/api/analyze')
def api_analyze():
    if 'file' not in request.files: return jsonify({'error':'Please upload an Excel (.xlsx/.xls) file.'}),400
    f=request.files['file']
    if not f.filename.lower().endswith(('.xlsx','.xls')): return jsonify({'error':'Only Excel files (.xlsx/.xls) are supported.'}),400
    try:
        df=load_excel(f); horizon=max(1,min(int(request.form.get('horizon_months',12)),24)); growth=max(-50,min(float(request.form.get('growth_pct',20)),200))
        return jsonify({'summary':make_summary(df),**analysis_from_request(df,horizon,growth)})
    except Exception as exc: return jsonify({'error':str(exc)}),400

@app.get('/sample-data')
def sample_data():
    return send_file(build_mock_excel(),as_attachment=True,download_name='mock_telemetry_3_years.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=False)
