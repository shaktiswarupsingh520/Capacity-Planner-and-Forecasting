from __future__ import annotations
import io, os, re, uuid, zipfile
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request, send_file, abort
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from report_pdf import build_pdf_report

BASE=os.path.dirname(os.path.abspath(__file__)); OUT=os.path.join(BASE,"outputs"); os.makedirs(OUT,exist_ok=True)
app=Flask(__name__,static_folder="static",template_folder="templates"); app.config["MAX_CONTENT_LENGTH"]=25*1024*1024
SOURCES={}
MOCK_APPS=["CBDC Mobile","CBDC Internet Banking","RUPI Switch","Payment Gateway","Customer Authentication","Transaction Processing"]
METRICS={
 "host_cpu_usage":("CPU Utilization","Infrastructure","%","cpu_pct"),
 "host_mem_usage":("Memory Utilization","Infrastructure","%","memory_pct"),
 "host_disk_used_pct":("Disk Utilization","Infrastructure","%","disk_pct"),
 "network_traffic":("Network Traffic In","Infrastructure","BytePerSecond","network_bps"),
 "service_request_count":("Workload Proxy","Workload","Rate","workload"),
 "service_response_time":("JVM Threads","JVM","Count","jvm_threads"),
}
ZIP_FILES={
 "cpu_pct":["CPU Usage %"], "memory_available_pct":["Memory available %"], "disk_available_pct":["Disk available %"],
 "network_in":["NIC bytes received"], "network_out":["NIC bytes sent"], "workload":["New session received"],
 "jvm_active":["JVM average number of active threads"], "jvm_count":["JVM thread count"],
 "jvm_heap_used":["JVM heap memory pool used bytes"], "jvm_heap_max":["jvm memory pool max","JVM memory pool max"],
}

def _clean_url(url):
 url=(url or "").strip().rstrip("/")
 if not url: raise ValueError("Dynatrace Tenant URL is required.")
 return url if re.match(r"^https?://",url,re.I) else "https://"+url

def _parse_value(v):
 if pd.isna(v): return np.nan
 s=str(v).strip().replace(",","")
 if not s or s.lower() in {"nan","none","-"}: return np.nan
 if s.startswith("<"): s=s[1:].strip()
 if s.endswith("%"): s=s[:-1]
 m=re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(KiB|MiB|GiB|TiB|kB|MB|GB|TB|k|m|g|t)?",s,re.I)
 if not m:return np.nan
 n=float(m.group(1)); u=(m.group(2) or "").lower()
 return n*{"k":1e3,"m":1e6,"g":1e9,"t":1e12,"kb":1e3,"mb":1e6,"gb":1e9,"tb":1e12,"kib":1024,"mib":1024**2,"gib":1024**3,"tib":1024**4}.get(u,1)

def _group(e):
 s=str(e).lower().replace("-","_")
 if any(x in s for x in ["upintapp","upayintapp","upi_int"]): return "UPI Internet Application"
 if any(x in s for x in ["upayiw","upiweb","upi_web","l2cpw","lmcpw"]): return "UPI Web"
 if any(x in s for x in ["upi_db","upiprodb","upidb"]) or "[db]" in s: return "UPI Database"
 if any(x in s for x in ["upayia","upiapp","upi_app_f","upi_app_nf","upi_app_arr","fap","l2cpa","l1cpa","l6cpa","lmcpaf","lmcpau"]): return "UPI Application Platform"
 if "nwb" in s or "lmcpn" in s:return "UPI Network"
 if "nap" in s:return "UPI NAP"
 return "Other Infrastructure"

def _find(names,candidates):
 for n in names:
  b=os.path.basename(n).lower()
  for c in candidates:
   if c.lower() in b:return n
 return None

def _read_metric(zf,member):
 raw=zf.read(member); df=pd.read_csv(io.BytesIO(raw)) if member.lower().endswith(".csv") else pd.read_excel(io.BytesIO(raw))
 if df.empty or len(df.columns)<2:return pd.DataFrame(columns=["timestamp","entity","application","value"])
 ts=pd.to_datetime(df.iloc[:,0],errors="coerce",dayfirst=True,format="mixed").dt.normalize(); rows=[]
 for entity in df.columns[1:]:
  vals=df[entity].map(_parse_value)
  for t,v in zip(ts,vals):
   if pd.notna(t) and pd.notna(v):rows.append((t,str(entity),_group(entity),float(v)))
 return pd.DataFrame(rows,columns=["timestamp","entity","application","value"])

def _prepare_zip(fs):
 raw=fs.read()
 if not raw:raise ValueError("The uploaded ZIP file is empty.")
 try:zf=zipfile.ZipFile(io.BytesIO(raw))
 except zipfile.BadZipFile:raise ValueError("The uploaded file is not a valid ZIP archive.")
 names=[n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
 found={k:_find(names,v) for k,v in ZIP_FILES.items()}; frames={}
 for k,m in found.items():
  if m:
   f=_read_metric(zf,m)
   if not f.empty:frames[k]=f
 if not frames:raise ValueError("No readable metric values were found in the ZIP.")
 keys=set()
 for f in frames.values():keys.update(zip(f.timestamp,f.application))
 data=pd.DataFrame(sorted(keys),columns=["timestamp","application"])
 def agg(key,out,reducer="mean",transform=None):
  nonlocal data
  if key not in frames:data[out]=np.nan;return
  f=frames[key].copy()
  if transform:f.value=transform(f.value)
  g=f.groupby(["timestamp","application"],as_index=False).value.agg(reducer)
  data=data.merge(g.rename(columns={"value":out}),on=["timestamp","application"],how="left")
 pct=lambda s:s*100 if s.max(skipna=True)<=1.5 else s
 avail=lambda s:100-(s*100 if s.max(skipna=True)<=1.5 else s)
 agg("cpu_pct","cpu_pct","mean",pct)
 agg("memory_available_pct","memory_pct","mean",avail)
 # Disk used is a byte/space metric in this export; only the explicit available-% metric is used for utilization.
 agg("disk_available_pct","disk_pct","mean",avail)
 # These are rates/activity observations per entity and timestamp; aggregate across the selected application group.
 agg("network_in","network_bps","sum")
 agg("workload","workload","sum")
 # JVM active-thread and JVM thread-count exports describe different application tiers. Coalesce them per group.
 if "jvm_active" in frames or "jvm_count" in frames:
  active=frames["jvm_active"].groupby(["timestamp","application"],as_index=False).value.mean().rename(columns={"value":"active"}) if "jvm_active" in frames else pd.DataFrame(columns=["timestamp","application","active"])
  count=frames["jvm_count"].groupby(["timestamp","application"],as_index=False).value.mean().rename(columns={"value":"count"}) if "jvm_count" in frames else pd.DataFrame(columns=["timestamp","application","count"])
  jt=active.merge(count,on=["timestamp","application"],how="outer"); jt["jvm_threads"]=jt["active"].combine_first(jt["count"])
  data=data.merge(jt[["timestamp","application","jvm_threads"]],on=["timestamp","application"],how="left")
 else:data["jvm_threads"]=np.nan
 if "jvm_heap_used" in frames and "jvm_heap_max" in frames:
  u=frames["jvm_heap_used"].groupby(["timestamp","application"],as_index=False).value.mean().rename(columns={"value":"used"}); m=frames["jvm_heap_max"].groupby(["timestamp","application"],as_index=False).value.mean().rename(columns={"value":"max"}); h=u.merge(m,on=["timestamp","application"],how="inner"); h["heap_pct"]=np.where(h["max"]>0,h["used"]/h["max"]*100,np.nan); data=data.merge(h[["timestamp","application","heap_pct"]],on=["timestamp","application"],how="left")
 else:data["heap_pct"]=np.nan
 data=data.sort_values(["timestamp","application"]).reset_index(drop=True)
 apps=[str(n) for n,g in data.groupby("application") if g[["cpu_pct","memory_pct","disk_pct","network_bps","workload","jvm_threads"]].notna().any().any()]
 if not apps:raise ValueError("The ZIP contained metric files, but no application groups could be derived from the entity names.")
 meta={
  "host_cpu_usage":{"label":"CPU Utilization","category":"Infrastructure","unit":"%","cap":100},
  "host_mem_usage":{"label":"Memory Utilization","category":"Infrastructure","unit":"%","cap":100},
  "host_disk_used_pct":{"label":"Disk Utilization","category":"Infrastructure","unit":"%","cap":100},
  "network_traffic":{"label":"Network Traffic In","category":"Infrastructure","unit":"BytePerSecond","cap":None},
  "service_request_count":{"label":"New Session Rate (workload proxy)","category":"Workload","unit":"Rate","cap":None},
  "service_response_time":{"label":"JVM Threads","category":"JVM","unit":"Count","cap":None},
 }
 return {"kind":"zip","management_zone":"Uploaded ZIP Data","data":data,"problems":pd.DataFrame(columns=["title","severity","status","startTime","duration_min","application"]),"applications":sorted(apps),"metric_meta":meta,"source_label":"Uploaded ZIP","file_count":len(names),"from":data.timestamp.min().date().isoformat(),"to":data.timestamp.max().date().isoformat(),"files_found":{k:os.path.basename(v) for k,v in found.items() if v}}

def _robust_series(df):
 s=df.set_index("timestamp")["value"].resample("D").mean().sort_index().replace([np.inf,-np.inf],np.nan).interpolate(limit_direction="both").dropna()
 if len(s)>=30:s=s.clip(s.quantile(.01),s.quantile(.99))
 return s

def _mape(a,p):
 a=np.asarray(a,float);p=np.asarray(p,float);mask=np.isfinite(a)&np.isfinite(p)&(np.abs(a)>1e-9)
 return float(np.mean(np.abs((a[mask]-p[mask])/a[mask]))*100) if mask.sum() else float("inf")

def _forecast(df,days,cap=None):
 if df.empty:return pd.DataFrame(columns=["timestamp","forecast","lower","upper"]),{"model":"No data","confidence":"None","mape":None}
 s=_robust_series(df)
 if len(s)<14:
  val=float(s.iloc[-1]); vals=np.repeat(val,days); err=max(abs(val)*.1,.01);model="Recent median";mape=None;conf="Low"
 else:
  split=max(7,int(len(s)*.2));train=s.iloc[:-split];test=s.iloc[-split:];cands=[]
  med=float(train.tail(min(30,len(train))).median());pred=np.repeat(med,len(test));cands.append(("Recent median",_mape(test,pred),np.repeat(med,days),float(np.nanstd(test.to_numpy()-pred))))
  if len(train)>=30:
   try:
    fit=ExponentialSmoothing(train,trend="add",seasonal="add",seasonal_periods=7,initialization_method="estimated").fit(optimized=True);tp=fit.forecast(len(test));fp=fit.forecast(days);cands.append(("Holt-Winters",_mape(test,tp),fp.to_numpy(),float(np.nanstd(test.to_numpy()-tp.to_numpy()))))
   except Exception:pass
  x=np.arange(len(train));coef=np.polyfit(x,train.to_numpy(),1);tp=np.polyval(coef,np.arange(len(train),len(train)+len(test)));fp=np.polyval(coef,np.arange(len(train),len(train)+days));cands.append(("Trend regression",_mape(test,tp),fp,float(np.nanstd(test.to_numpy()-tp))))
  model,mape,vals,err=min(cands,key=lambda z:z[1] if np.isfinite(z[1]) else 1e99);conf="High" if mape<10 else "Medium" if mape<20 else "Low"
 idx=pd.date_range(s.index[-1]+pd.Timedelta(days=1),periods=days,freq="D",tz="UTC");vals=np.maximum(np.asarray(vals,float),0)
 if cap is not None:vals=np.minimum(vals,cap)
 band=max(float(err),float(np.nanstd(np.diff(s.to_numpy()))*.75) if len(s)>2 else 0,.01);return pd.DataFrame({"timestamp":idx,"forecast":vals,"lower":np.maximum(vals-1.96*band,0),"upper":vals+1.96*band}),{"model":model,"confidence":conf,"mape":None if mape is None else round(float(mape),1)}

def _trend(df):
 if df.empty:return {"mean":None,"start":None,"end":None,"change":None,"slope":None,"direction":"insufficient_data"}
 y=df.value.to_numpy();slope=float(np.polyfit(np.arange(len(y)),y,1)[0]) if len(y)>1 else 0;recent=float(np.mean(y[-30:]));prior=float(np.mean(y[-60:-30])) if len(y)>60 else float(np.mean(y[:max(1,len(y)//2)]));change=(recent-prior)/abs(prior)*100 if abs(prior)>1e-9 else None;direction="insufficient_data" if change is None else "increasing" if change>5 else "decreasing" if change<-5 else "stable";return {"mean":float(np.mean(y)),"start":float(y[0]),"end":float(y[-1]),"change":None if change is None else float(change),"slope":slope,"direction":direction}
def _points(df,c):return [{"t":pd.Timestamp(t).isoformat(),"v":float(v)} for t,v in zip(df.timestamp,df[c]) if pd.notna(v)]
def _window(ds,start,end,app_name):
 d=ds["data"].copy();d.timestamp=pd.to_datetime(d.timestamp,utc=True);s=pd.Timestamp(start);s=s.tz_localize("UTC") if s.tzinfo is None else s.tz_convert("UTC");e=pd.Timestamp(end);e=e.tz_localize("UTC") if e.tzinfo is None else e.tz_convert("UTC");e=e+pd.Timedelta(days=1)-pd.Timedelta(seconds=1);return d[(d.application.astype(str)==str(app_name))&(d.timestamp>=s)&(d.timestamp<=e)].copy(),ds["problems"].copy()
def _regression_sim(data,driver,resource):
 d=data[[driver,resource]].dropna();
 if len(d)<30 or d[driver].nunique()<10:return None
 x=d[driver].to_numpy(float);y=d[resource].to_numpy(float);corr=float(np.corrcoef(x,y)[0,1]) if np.std(x)>0 and np.std(y)>0 else 0
 if corr<.25:return None
 coef=np.polyfit(x,y,1);base=float(np.mean(y[-30:]));drv=float(np.mean(x[-30:]));elasticity=float(coef[0]*drv/base) if base>1e-9 else 0
 return {"correlation":corr,"elasticity":elasticity,"driver":drv,"coef":float(coef[0])}

def build_analysis(ds,start,end,months,growth,app_name):
 data,problems=_window(ds,start,end,app_name)
 if data.empty:raise ValueError(f"No data was found for application '{app_name}'.")
 days=max(1,int(months)*30);meta=ds.get("metric_meta",{});metrics={};trends={};models={}
 for key,(_,_,_,col) in METRICS.items():
  info=meta.get(key,{});label=info.get("label",METRICS[key][0]);cat=info.get("category",METRICS[key][1]);unit=info.get("unit",METRICS[key][2]);cap=info.get("cap",100 if key in ("host_cpu_usage","host_mem_usage","host_disk_used_pct") else None);f=data[["timestamp",col]].rename(columns={col:"value"}).dropna() if col in data else pd.DataFrame();f=f.groupby("timestamp",as_index=False).mean() if not f.empty else f;fc,mi=_forecast(f,days,cap);trends[key]=_trend(f);models[key]=mi;metrics[key]={"label":label,"category":cat,"unit":unit,"cap":cap,"historical":_points(f,"value"),"forecast":_points(fc,"forecast"),"lower":_points(fc,"lower"),"upper":_points(fc,"upper"),"model":mi}
 # Simulation is only driven by a positive, measurable relationship. No blind multiplication by traffic growth.
 sim={};driver="workload"
 for key,(_,_,_,col) in METRICS.items():
  if key=="service_request_count" or col not in data:continue
  base=metrics[key]["forecast"][-1]["v"] if metrics[key]["forecast"] else None
  if key=="host_disk_used_pct":sim[key]={"baseline":base,"simulated":base,"method":"time-trend forecast"};continue
  r=_regression_sim(data,driver,col)
  if r and r["elasticity"]>0:sim[key]={**r,"baseline":base,"simulated":max(0,base*(1+r["elasticity"]*growth/100)) if base is not None else None,"method":"workload/resource regression"}
  else:sim[key]={"baseline":base,"simulated":base,"correlation":None if not r else r["correlation"],"elasticity":0,"method":"baseline forecast; no reliable positive workload relationship"}
 scenarios=[]
 for g in [-20,0,10,25,50,100]:
  vals={}
  for key,out in [("host_cpu_usage","cpu"),("host_mem_usage","memory"),("host_disk_used_pct","disk")]:
   base=metrics[key]["forecast"][-1]["v"] if metrics[key]["forecast"] else None
   if key=="host_disk_used_pct":v=base
   else:
    r=_regression_sim(data,driver,METRICS[key][3]);v=base*(1+r["elasticity"]*g/100) if r and r["elasticity"]>0 and base is not None else base
   vals[out]=None if v is None else min(100,max(0,float(v)))
  scenarios.append({"growth":g,**vals})
 workload=data.workload.dropna();jvm=data.jvm_threads.dropna();wavg=float(workload.mean()) if len(workload) else None;javg=float(jvm.mean()) if len(jvm) else None;wf=metrics["service_request_count"]["forecast"][-1]["v"] if metrics["service_request_count"]["forecast"] else None;jf=metrics["service_response_time"]["forecast"][-1]["v"] if metrics["service_response_time"]["forecast"] else None
 cpu_rel=_regression_sim(data,driver,"cpu_pct");confidence="High" if cpu_rel and cpu_rel["correlation"]>=.6 else "Medium" if cpu_rel and cpu_rel["correlation"]>=.4 else "Low"
 row={"application":app_name,"requests_avg":wavg or 0,"response_avg":javg or 0,"request_forecast":wf or 0,"response_forecast":jf or 0,"status":"Planning Required" if any((x.get("simulated") or 0)>=85 for x in sim.values() if x.get("baseline") is not None) else "Normal"}
 summary={"from":str(pd.Timestamp(start).date()),"to":str(pd.Timestamp(end).date()),"applications":1,"rows":len(data),"problems":len(problems),"baseline_requests":wavg,"forecast_days":days,"data_source":ds["kind"],"data_source_label":ds.get("source_label",ds["kind"].title()),"workload_label":meta.get("service_request_count",{}).get("label","Workload"),"performance_label":meta.get("service_response_time",{}).get("label","Performance"),"model_confidence":confidence,"simulation_method":"Positive workload/resource regression only when supported by the historical data; otherwise baseline forecast is retained","workload_available":bool(len(workload))}
 return {"management_zone":ds["management_zone"],"application":app_name,"summary":summary,"metrics":metrics,"application_table":[row],"trends":trends,"forecasts":{k:metrics[k] for k in metrics},"problems":problems,"scenarios":scenarios,"recommendations":[{"application":app_name,"risk":row["status"],"action":"Use projected resource headroom for planning; validate workload assumptions against request or transaction metrics before committing capacity."}],"growth":float(growth),"simulation":sim,"models":models}

def _safe_sheet(r,names):
 for n in names:
  if n in r.sheet_names:return pd.read_excel(r,sheet_name=n)
 return None
def _norm(df):
 x=df.copy();x.columns=[re.sub(r"[^a-z0-9]+","_",str(c).strip().lower()).strip("_") for c in x.columns];return x
def _rename(df,target,aliases):
 if target in df:return
 for a in aliases:
  if a in df.columns:df.rename(columns={a:target},inplace=True);return
def _prepare_excel(fs):
 raw=fs.read();r=pd.ExcelFile(io.BytesIO(raw));t=_safe_sheet(r,["Application Telemetry","Telemetry","Host Telemetry"]);p=_safe_sheet(r,["Performance Metrics","Application Performance"]);pr=_safe_sheet(r,["Problems","Problem Records"]);t=_norm(t if t is not None else pd.DataFrame());p=_norm(p if p is not None else pd.DataFrame());pr=_norm(pr if pr is not None else pd.DataFrame())
 for d in [t,p,pr]:
  if not d.empty:_rename(d,"timestamp",["time","date","datetime"]);_rename(d,"application",["app","application_name","service","service_name"])
 if t.empty and p.empty:raise ValueError("No usable telemetry/performance data was found.")
 def prep(d):
  if d.empty:return
  if "timestamp" not in d or "application" not in d:raise ValueError("Uploaded Excel must contain timestamp and application columns.")
  d.timestamp=pd.to_datetime(d.timestamp,errors="coerce",utc=True);d.application=d.application.astype(str).str.strip();d.dropna(subset=["timestamp","application"],inplace=True)
 prep(t);prep(p)
 for c,a in [("cpu_pct",["cpu","cpu_usage","cpu_utilization","cpu_percent"]),("memory_pct",["memory","memory_usage","memory_utilization","memory_percent"]),("disk_pct",["disk","disk_usage","disk_utilization","disk_percent"]),("network_bps",["network","network_traffic","network_in","network_in_bps"])]:_rename(t,c,a);t[c]=pd.to_numeric(t.get(c,np.nan),errors="coerce")
 for c,a in [("request_count",["requests","requests_count","service_request_count","requestcount"]),("response_time_ms",["response_time","response_ms","avg_response_time","service_response_time"])]:_rename(p,c,a);p[c]=pd.to_numeric(p.get(c,np.nan),errors="coerce")
 t=t.groupby(["timestamp","application"],as_index=False).mean(numeric_only=True) if not t.empty else pd.DataFrame(columns=["timestamp","application"]);p=p.groupby(["timestamp","application"],as_index=False).sum(numeric_only=True) if not p.empty else pd.DataFrame(columns=["timestamp","application"]);keys=pd.concat([t[["timestamp","application"]],p[["timestamp","application"]]],ignore_index=True).drop_duplicates();d=keys.merge(t,on=["timestamp","application"],how="left").merge(p,on=["timestamp","application"],how="left");return {"kind":"excel","management_zone":"Uploaded Excel Dataset","data":d,"problems":pr,"applications":sorted(d.application.astype(str).unique())}
def mock_data():
 dates=pd.date_range("2023-09-01",datetime.now(timezone.utc).date(),freq="D",tz="UTC");rng=np.random.default_rng(1418);rows=[];t=np.arange(len(dates))
 for i,a in enumerate(MOCK_APPS):
  for j,dt in enumerate(dates):rows.append({"timestamp":dt,"application":a,"cpu_pct":35+.018*t[j]+rng.normal(0,1),"memory_pct":48+.012*t[j]+rng.normal(0,1),"disk_pct":30+.02*t[j],"network_bps":1e6,"request_count":1e6+2500*t[j]+rng.normal(0,5e4),"response_time_ms":35+.01*t[j]+rng.normal(0,1)})
 d=pd.DataFrame(rows);return {"kind":"mock","management_zone":"CBDCE_RUPISwitch_1418","data":d,"problems":pd.DataFrame(columns=["title","severity","status","startTime","duration_min","application"]),"applications":MOCK_APPS}
def _dt_get(cfg,path,params=None,timeout=30):
 r=requests.get(cfg["tenant"]+path,headers={"Authorization":f"Api-Token {cfg['token']}"},params=params,timeout=timeout)
 if r.status_code>=400:raise RuntimeError(f"Dynatrace API returned HTTP {r.status_code}: {r.text[:500]}")
 return r.json()
def connect_dynatrace(tenant,token):
 if not token.strip():raise ValueError("Dynatrace Access Token is required.")
 cfg={"tenant":_clean_url(tenant),"token":token.strip()};_dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":"builtin:service.requestCount:sum","from":"now-5m","resolution":"Inf","entitySelector":'type("SERVICE")'},20);sid="dt-"+uuid.uuid4().hex[:10];SOURCES[sid]={"kind":"dynatrace","config":cfg};return sid
def _series(payload):
 rows=[]
 for result in payload.get("result",[]):
  for s in result.get("data",[]):
   dm=s.get("dimensionMap",{});dims=s.get("dimensions",[]);a=dm.get("dt.entity.service") or (dims[0] if dims else "Application")
   for ts,v in zip(s.get("timestamps",[]),s.get("values",[])):
    if v is not None:rows.append({"timestamp":pd.to_datetime(ts,unit="ms",utc=True),"application":a,"value":float(v)})
 return pd.DataFrame(rows)
def _live_data(cfg,mz,start,end):
 fr=pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ");to=(pd.Timestamp(end)+pd.Timedelta(days=1)-pd.Timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ");sel='type("SERVICE")'+(f',mzName("{mz}")' if mz else "");req=_series(_dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":'builtin:service.requestCount:sum:splitBy("dt.entity.service")',"entitySelector":sel,"from":fr,"to":to,"resolution":"1h"}));resp=_series(_dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":'builtin:service.response.time:avg:splitBy("dt.entity.service")',"entitySelector":sel,"from":fr,"to":to,"resolution":"1h"}));req=req.rename(columns={"value":"request_count"})[["timestamp","application","request_count"]];resp=resp.rename(columns={"value":"response_time_ms"})[["timestamp","application","response_time_ms"]];d=req.merge(resp,on=["timestamp","application"],how="outer");d.timestamp=d.timestamp.dt.floor("D");d=d.groupby(["timestamp","application"],as_index=False).agg(request_count=("request_count","sum"),response_time_ms=("response_time_ms","mean"));d["cpu_pct"]=np.nan;d["memory_pct"]=np.nan;d["disk_pct"]=np.nan;d["network_bps"]=np.nan;d["workload"]=d["request_count"];d["jvm_threads"]=np.nan;return {"kind":"dynatrace","management_zone":mz or "Dynatrace Management Zone","data":d,"problems":pd.DataFrame(columns=["title","severity","status","startTime","duration_min","application"]),"applications":sorted(d.application.dropna().unique())}
def _dataset(sid,zone,start,end):
 src=SOURCES.get(sid)
 if not src:raise ValueError("Please select a data source first.")
 return src["dataset"] if src["kind"] in ("mock","excel","zip") else _live_data(src["config"],zone,start,end)
def _mock_excel():
 d=mock_data()["data"].copy();d.timestamp=d.timestamp.dt.tz_localize(None);o=io.BytesIO()
 with pd.ExcelWriter(o,engine="openpyxl") as w:d.to_excel(w,index=False,sheet_name="Application Telemetry")
 o.seek(0);return o
@app.get("/")
def index():
 d=datetime.now(timezone.utc).date();return render_template("index.html",default_from=(d-timedelta(days=90)).isoformat(),default_to=d.isoformat(),company_name="ApMoSys",report_title="Capacity & Performance Report")
@app.post("/api/mock-source")
def mock_source():
 sid="mock-"+uuid.uuid4().hex[:10];SOURCES[sid]={"kind":"mock","dataset":mock_data()};return jsonify({"source_id":sid,"applications":MOCK_APPS})
@app.post("/api/upload-excel")
def upload_excel():
 try:f=request.files.get("excel_file");ds=_prepare_excel(f);sid="excel-"+uuid.uuid4().hex[:10];SOURCES[sid]={"kind":"excel","dataset":ds};return jsonify({"source_id":sid,"applications":ds["applications"],"from":ds["data"].timestamp.min().date().isoformat(),"to":ds["data"].timestamp.max().date().isoformat(),"management_zone":ds["management_zone"],"rows":len(ds["data"])})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.post("/api/upload-zip")
def upload_zip():
 try:f=request.files.get("zip_file");ds=_prepare_zip(f);sid="zip-"+uuid.uuid4().hex[:10];SOURCES[sid]={"kind":"zip","dataset":ds};return jsonify({"source_id":sid,"applications":ds["applications"],"from":ds["from"],"to":ds["to"],"management_zone":ds["management_zone"],"rows":len(ds["data"]),"files":ds["file_count"]})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.post("/api/connect-dynatrace")
def api_connect():
 try:p=request.get_json() or {};return jsonify({"source_id":connect_dynatrace(p.get("tenant_url",""),p.get("access_token",""))})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.get("/api/management-zones")
def zones():
 src=SOURCES.get(request.args.get("source_id"));q=(request.args.get("q") or "").lower();vals=[]
 if src and src["kind"]=="dynatrace":
  try:vals=[{"id":z.get("id",""),"name":z.get("name","")} for z in _dt_get(src["config"],"/api/config/v1/managementZones",{"pageSize":100}).get("values",[])]
  except:pass
 return jsonify({"zones":[z for z in vals if q in z["name"].lower()]})
@app.get("/api/applications")
def applications():
 src=SOURCES.get(request.args.get("source_id"))
 if not src:return jsonify({"error":"Configure a data source first."}),400
 try:
  apps=src["dataset"]["applications"] if src["kind"] in ("mock","excel","zip") else _live_data(src["config"],request.args.get("management_zone",""),request.args.get("hist_from"),request.args.get("hist_to"))["applications"];return jsonify({"applications":[{"id":a,"name":a} for a in apps]})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.post("/api/timeseries")
def timeseries():
 try:
  p=request.get_json() or {};ds=_dataset(p["source_id"],p.get("management_zone",""),p["hist_from"],p["hist_to"]);a=p.get("application");
  if a not in ds["applications"]:raise ValueError(f"Application '{a}' was not found.")
  x=build_analysis(ds,p["hist_from"],p["hist_to"],int(p.get("forecast_months",3)),float(p.get("growth_pct",0)),a);return jsonify({"baseline_requests":x["summary"]["baseline_requests"],"metrics":x["metrics"],"summary":x["summary"],"applications":x["application_table"]})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.get("/sample-data")
def sample():return send_file(_mock_excel(),as_attachment=True,download_name="Capacity_Planner_Mock_Data.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
@app.post("/api/generate")
def generate():
 try:
  p=request.get_json() or {};ds=_dataset(p["source_id"],p.get("management_zone",""),p["hist_from"],p["hist_to"]);x=build_analysis(ds,p["hist_from"],p["hist_to"],int(p.get("forecast_months",3)),float(p.get("growth_pct",0)),p.get("application"));job=uuid.uuid4().hex[:8];path=os.path.join(OUT,f"Capacity_Performance_Report_{job}.pdf");build_pdf_report(path,p.get("company_name") or "ApMoSys",p.get("report_title") or "Capacity & Performance Report",x);return jsonify({"files":[os.path.basename(path)]})
 except Exception as e:return jsonify({"error":str(e)}),400
@app.get("/api/download/<path:name>")
def download(name):
 path=os.path.join(OUT,os.path.basename(name));
 if not os.path.isfile(path):abort(404)
 return send_file(path,as_attachment=True)
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
