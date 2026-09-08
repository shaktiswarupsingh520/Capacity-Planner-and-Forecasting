from __future__ import annotations
import io, os, uuid
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, abort
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from report_pdf import build_pdf_report

BASE=os.path.dirname(os.path.abspath(__file__))
OUT=os.path.join(BASE,'outputs')
os.makedirs(OUT,exist_ok=True)
app=Flask(__name__,static_folder='static',template_folder='templates')

METRICS={
 'host_cpu_usage':('Host CPU Usage (avg)','Infrastructure','%','cpu'),
 'host_mem_usage':('Host Memory Usage (avg)','Infrastructure','%','memory'),
 'host_disk_used_pct':('Disk Utilization (avg)','Infrastructure','%','disk'),
 'network_traffic':('Network Traffic In (avg)','Infrastructure','BytePerSecond','network'),
 'service_request_count':('Total Service Request Count','Performance','Count','requests'),
 'service_response_time':('Avg Service Response Time','Performance','Second','response'),
}
HOSTS=['AxisBank_Prod_CBDC - 192.168.169.228','AxisBank_Prod_CBDC - 192.168.135.178','AxisBank_Prod_CBDC - 192.168.135.179','AxisBank_Prod_CBDC - 192.168.169.227','AxisBank_Prod_CBDC - 192.168.93.52','AxisBank_Prod_CBDC - 192.168.176.109','AxisBank_Prod_CBDC - 192.168.93.76','AxisBank_Prod_CBDC - 192.168.169.229','AxisBank_Prod_CBDC - 192.168.176.108','AxisBank_Prod_CBDC - 192.168.176.207','AxisBank_Prod_CBDC - 192.168.169.226','AxisBank_Prod_CBDC - 192.168.176.205']

def mock_data():
    rng=np.random.default_rng(1418)
    d=pd.date_range('2023-09-01',datetime.now(timezone.utc).date(),freq='D',tz='UTC'); t=np.arange(len(d)); w=np.sin(2*np.pi*t/7); m=np.sin(2*np.pi*t/30.4)
    base={}
    base['cpu']=4+0.008*t+0.7*w+rng.normal(0,.45,len(d))
    base['memory']=38+0.015*t+2*w+rng.normal(0,1.2,len(d))
    base['disk']=11+0.018*t+1.0*m+rng.normal(0,.8,len(d))
    base['network']=1.2e6+450*t+1.2e5*w+rng.normal(0,8e4,len(d))
    base['requests']=2.1e6+3500*t+7e5*w+rng.normal(0,2.3e5,len(d)); base['requests']=np.clip(base['requests'],0,None)
    base['response']=.035-0.000015*t+0.006*(1+w)+rng.normal(0,.002,len(d)); base['response']=np.clip(base['response'],.004,None)
    rows=[]
    for hi,h in enumerate(HOSTS):
        for j,dt in enumerate(d):
            mult=1+hi*.025; cpu=base['cpu'][j]*mult+rng.normal(0,.5); mem=base['memory'][j]*(.85+hi*.025)+rng.normal(0,1); disk=base['disk'][j]*(.82+hi*.035)+rng.normal(0,.7)
            if hi in (3,9): mem += 8+.02*t[j]
            if hi==9: disk += 4+.01*t[j]
            rows.append([dt,h,max(0,min(100,cpu)),max(0,min(100,mem)),max(0,min(100,disk))])
    host=pd.DataFrame(rows,columns=['timestamp','host','cpu_pct','memory_pct','disk_pct'])
    agg=pd.DataFrame({'timestamp':d,'host_cpu_usage':base['cpu'],'host_mem_usage':base['memory'],'host_disk_used_pct':base['disk'],'network_traffic':base['network'],'service_request_count':base['requests'],'service_response_time':base['response']})
    return agg,host

def mock_problems(start,end):
    rng=np.random.default_rng(88); dates=pd.date_range(start,end,freq='7D',tz='UTC'); titles=['Low disk space','Multiple infrastructure problems','Failure rate increase','Response time degradation','SRE Availability Degradation | Sub Journey: Get Register Bank List | CBDC','Multiple service problems']; sev=['RESOURCE_CONTENTION','AVAILABILITY','AVAILABILITY','PERFORMANCE','CUSTOM_ALERT','ERROR']; rows=[]
    for i,dt in enumerate(dates):
        n=1+int(i%4==0)+int(i%6==0)
        for j in range(n):
            st=dt+pd.Timedelta(hours=int(rng.integers(0,20))); rows.append({'title':titles[(i+j)%len(titles)],'severity':sev[(i+j)%len(sev)],'status':'CLOSED','startTime':st,'duration_min':int(rng.integers(20,500))})
    return pd.DataFrame(rows)

def select_window(start,end):
    agg,host=mock_data(); start=pd.Timestamp(start,tz='UTC'); end=pd.Timestamp(end,tz='UTC')+pd.Timedelta(days=1)-pd.Timedelta(seconds=1); return agg[(agg.timestamp>=start)&(agg.timestamp<=end)].copy(),host[(host.timestamp>=start)&(host.timestamp<=end)].copy()

def forecast(df,days):
    if df.empty:return pd.DataFrame(columns=['timestamp','forecast','lower','upper'])
    s=df.set_index('timestamp')['value'].asfreq('D').interpolate().dropna()
    if len(s)<21: vals=np.repeat(s.iloc[-1],days)
    else:
        try: vals=ExponentialSmoothing(s,trend='add',seasonal='add',seasonal_periods=7,initialization_method='estimated').fit(optimized=True).forecast(days).to_numpy()
        except Exception: vals=np.polyval(np.polyfit(np.arange(len(s)),s.to_numpy(),1),np.arange(len(s),len(s)+days))
    resid=float(np.nanstd(np.diff(s.to_numpy()))) if len(s)>2 else .1; idx=pd.date_range(s.index[-1]+pd.Timedelta(days=1),periods=days,freq='D',tz='UTC'); vals=np.asarray(vals,dtype=float); return pd.DataFrame({'timestamp':idx,'forecast':vals,'lower':vals-1.96*resid,'upper':vals+1.96*resid})

def trend(df):
    if df.empty:return {'mean':None,'start':None,'end':None,'change':None,'slope':None,'direction':'insufficient_data'}
    x=np.arange(len(df)); y=df.value.to_numpy(); slope=float(np.polyfit(x,y,1)[0]) if len(df)>1 else 0; a=float(y[0]); b=float(y[-1]); change=(b-a)/abs(a)*100 if a else 0; direction='increasing' if slope>abs(y.mean())*.0005 else ('decreasing' if slope<-abs(y.mean())*.0005 else 'flat'); return {'mean':float(y.mean()),'start':a,'end':b,'change':float(change),'slope':slope,'direction':direction}

def to_points(df,col,scale=1): return [{'t':pd.Timestamp(t).isoformat(),'v':float(v)*scale} for t,v in zip(df.timestamp,df[col]) if pd.notna(v)]

def bundle(start,end,months):
    agg,host=select_window(start,end); days=max(1,int(months)*30); metrics={}; fc={}
    for key,(label,cat,unit,src) in METRICS.items():
        df=agg[['timestamp',key]].rename(columns={key:'value'}); f=forecast(df,days); scale=1000 if key=='service_response_time' else 1
        metrics[key]={'label':label,'category':cat,'unit':('ms' if key=='service_response_time' else unit),'cap':100 if key in ('host_cpu_usage','host_mem_usage','host_disk_used_pct') else None,'historical':to_points(df,'value',scale),'forecast':to_points(f,'forecast',scale),'lower':to_points(f,'lower',scale),'upper':to_points(f,'upper',scale)}; fc[key]=f
    base=float(agg.service_request_count.mean()) if not agg.empty else None; return agg,host,metrics,fc,base

def host_table(host,days):
    rows=[]
    for h,g in host.groupby('host'):
        r={'host':h}
        for k in ['cpu_pct','memory_pct','disk_pct']:
            v=g[k].dropna(); slope=np.polyfit(np.arange(len(v)),v,1)[0] if len(v)>2 else 0; r[f'{k}_avg']=float(v.mean()); r[f'{k}_forecast']=float(np.clip(v.iloc[-1]+slope*days,0,100))
        mx=max(r['cpu_pct_forecast']/80,r['memory_pct_forecast']/85,r['disk_pct_forecast']/80); r['status']='At Risk' if mx>=1 else ('Watch' if mx>=.85 else 'Normal'); rows.append(r)
    return pd.DataFrame(rows).sort_values('cpu_pct_avg',ascending=False)

def analysis(start,end,months,growth):
    agg,host,metrics,fc,base=bundle(start,end,months); probs=mock_problems(start,end); days=max(1,int(months)*30); trends={}; forecasts={}; corrs={}
    for key,(label,cat,unit,src) in METRICS.items():
        df=agg[['timestamp',key]].rename(columns={key:'value'}); trends[key]=trend(df); forecasts[key]=forecast(df,days); corrs[key]=0.0
    ht=host_table(host,days); sf=1+growth/100
    for k in metrics:
        el=1.15 if k=='service_response_time' else 1; m=metrics[k]; m['simulated_forecast']=[{'t':p['t'],'v':min(m['cap'],p['v']*sf**el) if m['cap'] else p['v']*sf**el} for p in m['forecast']]
    scenarios=[]
    for g in [-20,0,10,25,50]:
        x=1+g/100; scenarios.append({'growth':g,'cpu':min(100,ht.cpu_pct_forecast.max()*x),'memory':min(100,ht.memory_pct_forecast.max()*x),'disk':min(100,ht.disk_pct_forecast.max()*x)})
    rec=[{'host':r.host,'risk':r.status,'action':'Review resource headroom and validate workload trend before the next planning cycle.'} for r in ht.itertuples() if r.status!='Normal']
    return {'management_zone':'CBDCE_RUPISwitch_1418','summary':{'from':str(pd.Timestamp(start).date()),'to':str(pd.Timestamp(end).date()),'hosts':int(host.host.nunique()),'rows':int(len(agg)),'problems':int(len(probs)),'baseline_requests':base},'metrics':metrics,'host_table':ht.to_dict('records'),'trends':trends,'forecasts':forecasts,'problems':probs,'correlations':corrs,'scenarios':scenarios,'recommendations':rec,'growth':growth}

@app.route('/')
def index():
    d=datetime.now(timezone.utc).date(); return render_template('index.html',default_from=(d-timedelta(days=90)).isoformat(),default_to=d.isoformat(),company_name='ApMoSys',report_title='Capacity & Performance Report')
@app.get('/api/management-zones')
def zones():
    q=(request.args.get('q') or '').lower(); names=[{'id':'mock-cbdce','name':'CBDCE_RUPISwitch_1418'}]; return jsonify({'zones':[z for z in names if q in z['name'].lower()]})
@app.post('/api/timeseries')
def timeseries():
    p=request.get_json() or {}; a=analysis(p['hist_from'],p['hist_to'],int(p.get('forecast_months',3)),float(p.get('growth_pct',0))); return jsonify({'baseline_requests':a['summary']['baseline_requests'],'metrics':a['metrics'],'summary':a['summary']})
@app.get('/sample-data')
def sample():
    agg,host=mock_data(); out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as w:
        host.to_excel(w,index=False,sheet_name='Host Telemetry'); agg.to_excel(w,index=False,sheet_name='Performance Metrics')
    out.seek(0); return send_file(out,as_attachment=True,download_name='Capacity_Planner_Mock_Data.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
@app.post('/api/generate')
def generate():
    p=request.get_json() or {}; a=analysis(p['hist_from'],p['hist_to'],int(p.get('forecast_months',3)),float(p.get('growth_pct',0))); job=uuid.uuid4().hex[:8]; path=os.path.join(OUT,f'Capacity_Performance_Report_{job}.pdf'); build_pdf_report(path,p.get('company_name') or 'ApMoSys',p.get('report_title') or 'Capacity & Performance Report',a); return jsonify({'files':[os.path.basename(path)]})
@app.get('/api/download/<path:name>')
def download(name):
    path=os.path.join(OUT,os.path.basename(name));
    if not os.path.isfile(path): abort(404)
    return send_file(path,as_attachment=True)
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=False)
