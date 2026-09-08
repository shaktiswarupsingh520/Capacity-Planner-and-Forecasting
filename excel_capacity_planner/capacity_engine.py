from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    HAS_STATSMODELS = True
except Exception:
    HAS_STATSMODELS = False

THRESHOLDS = {'cpu_pct': 80.0, 'memory_pct': 85.0, 'disk_pct': 80.0}
LABELS = {'cpu_pct': 'CPU', 'memory_pct': 'Memory', 'disk_pct': 'Disk'}


def _clean_series(s):
    return pd.to_numeric(s, errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()


def _linear_forecast(y, steps):
    x=np.arange(len(y),dtype=float)
    if len(y)<2: return np.repeat(y[-1] if len(y) else np.nan, steps)
    slope,intercept=np.polyfit(x,y,1)
    return intercept+slope*np.arange(len(y),len(y)+steps)


def forecast_series(series, steps):
    y=_clean_series(series)
    if len(y)<14:
        pred=_linear_forecast(y.to_numpy(),steps); model='Linear Regression'
    elif HAS_STATSMODELS:
        try:
            seasonal=7 if len(y)>=28 else None
            fit=ExponentialSmoothing(y,trend='add',seasonal='add' if seasonal else None,seasonal_periods=seasonal,initialization_method='estimated').fit(optimized=True,use_brute=True)
            pred=fit.forecast(steps).to_numpy(); model='Holt-Winters'+(' + Weekly Seasonality' if seasonal else '')
        except Exception:
            pred=_linear_forecast(y.to_numpy(),steps); model='Linear Regression Fallback'
    else:
        pred=_linear_forecast(y.to_numpy(),steps); model='Linear Regression'
    fitted=_linear_forecast(y.to_numpy(),len(y)) if len(y)>=10 else np.repeat(y.mean() if len(y) else 0,len(y))
    sigma=float(np.nanstd(y.to_numpy()-fitted)) if len(y) else 0.0; band=1.28*max(sigma,0.5)
    pred=np.clip(pred,0,100)
    return {'forecast':pred,'lower':np.clip(pred-band,0,100),'upper':np.clip(pred+band,0,100),'model':model}


def backtest_score(series):
    y=_clean_series(series)
    if len(y)<60: return None
    train,actual=y.iloc[:-28],y.iloc[-28:].to_numpy()
    if HAS_STATSMODELS:
        try:
            fit=ExponentialSmoothing(train,trend='add',seasonal='add',seasonal_periods=7,initialization_method='estimated').fit(optimized=True,use_brute=True)
            pred=fit.forecast(28).to_numpy()
        except Exception: pred=_linear_forecast(train.to_numpy(),28)
    else: pred=_linear_forecast(train.to_numpy(),28)
    return float(np.mean(np.abs((actual-pred)/np.maximum(np.abs(actual),1.0)))*100)


def _risk(value,threshold):
    if value>=threshold: return 'At Risk'
    if value>=threshold*0.85: return 'Watch'
    return 'Normal'


def _simulate(value,growth_pct,metric):
    elasticity={'cpu_pct':0.90,'memory_pct':0.60,'disk_pct':0.45}[metric]
    scale=max(0.01,1+growth_pct/100.0)
    return float(np.clip(value*(scale**elasticity),0,100))


def analyze_capacity(daily,horizon_months,growth_pct):
    horizon_days=int(round(horizon_months*30.4375)); hosts=[]; enterprise={}; series_payload={}; risk_rows=[]
    for host,hdf in daily.groupby('host'):
        hdf=hdf.sort_values('timestamp'); host_row={'host':host,'metrics':{}}
        for metric,threshold in THRESHOLDS.items():
            s=hdf.set_index('timestamp')[metric].dropna().resample('1D').mean().dropna()
            if len(s)<10: continue
            fc=forecast_series(s,horizon_days); score=backtest_score(s); current=float(s.iloc[-1]); avg=float(s.mean()); peak=float(s.max()); forecast_peak=float(np.max(fc['forecast']))
            breach_idx=np.where(fc['forecast']>=threshold)[0]
            breach_date=(s.index[-1]+pd.Timedelta(days=int(breach_idx[0])+1)).strftime('%Y-%m-%d') if len(breach_idx) else None
            simulated=_simulate(forecast_peak,growth_pct,metric); risk=_risk(forecast_peak,threshold)
            row={'metric':LABELS[metric],'current':round(current,2),'average':round(avg,2),'peak':round(peak,2),'forecast_peak':round(forecast_peak,2),'threshold':threshold,'risk':risk,'breach_date':breach_date,'simulated_peak':round(simulated,2),'simulated_risk':_risk(simulated,threshold),'model':fc['model'],'backtest_mape':round(score,2) if score is not None else None}
            host_row['metrics'][metric]=row; risk_rows.append({'host':host,**row}); series_payload.setdefault(host,{})
            hist_tail=s.tail(min(len(s),180)); stride=max(1,horizon_days//120); future_idx=pd.date_range(s.index[-1]+pd.Timedelta(days=1),periods=horizon_days,freq='1D')[::stride]
            pred=fc['forecast'][::stride][:len(future_idx)]; low=fc['lower'][::stride][:len(future_idx)]; high=fc['upper'][::stride][:len(future_idx)]
            series_payload[host][metric]={'history':[{'date':i.strftime('%Y-%m-%d'),'value':round(float(v),2)} for i,v in hist_tail.items()],'forecast':[{'date':i.strftime('%Y-%m-%d'),'value':round(float(v),2),'lower':round(float(l),2),'upper':round(float(u),2)} for i,v,l,u in zip(future_idx,pred,low,high)]}
        hosts.append(host_row)
    for metric in THRESHOLDS:
        vals=[r['forecast_peak'] for r in risk_rows if r['metric']==LABELS[metric]]
        enterprise[metric]={'label':LABELS[metric],'threshold':THRESHOLDS[metric],'hosts_at_risk':sum(r['risk']=='At Risk' for r in risk_rows if r['metric']==LABELS[metric]),'hosts_watch':sum(r['risk']=='Watch' for r in risk_rows if r['metric']==LABELS[metric]),'forecast_peak':round(max(vals),2) if vals else None}
    risk_rows.sort(key=lambda r:(r['risk']!='At Risk',-r['forecast_peak']))
    scenarios=[]
    for scenario in [-20,0,10,20,50,100]:
        per_metric={}
        for metric,threshold in THRESHOLDS.items():
            rs=[r for r in risk_rows if r['metric']==LABELS[metric]]; vals=[_simulate(r['forecast_peak'],scenario,metric) for r in rs]
            per_metric[metric]={'label':LABELS[metric],'peak':round(max(vals),2) if vals else None,'threshold':threshold,'hosts_at_risk':sum(v>=threshold for v in vals)}
        scenarios.append({'growth_pct':scenario,'metrics':per_metric})
    recommendations=[]
    for r in risk_rows[:12]:
        if r['risk']=='At Risk': action=f"Plan {r['metric']} capacity before {r['breach_date']}" if r['breach_date'] else f"Review {r['metric']} capacity and headroom"
        elif r['risk']=='Watch': action=f"Monitor {r['metric']} trend and validate growth assumptions"
        else: continue
        recommendations.append({'host':r['host'],'metric':r['metric'],'risk':r['risk'],'action':action})
    return {'generated':pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M UTC'),'horizon_months':horizon_months,'growth_pct':growth_pct,'enterprise':enterprise,'hosts':hosts,'risks':risk_rows[:50],'recommendations':recommendations,'scenarios':scenarios,'series':series_payload}
