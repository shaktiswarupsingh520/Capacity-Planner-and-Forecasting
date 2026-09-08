from __future__ import annotations

import io
import os
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request, send_file, abort
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from report_pdf import build_pdf_report

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "outputs")
os.makedirs(OUT, exist_ok=True)
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
SOURCES: dict[str, dict] = {}

METRICS = {
    "host_cpu_usage": ("CPU Utilization", "Infrastructure", "%", "cpu_pct"),
    "host_mem_usage": ("Memory Utilization", "Infrastructure", "%", "memory_pct"),
    "host_disk_used_pct": ("Disk Utilization", "Infrastructure", "%", "disk_pct"),
    "network_traffic": ("Network Traffic In", "Infrastructure", "BytePerSecond", "network_bps"),
    "service_request_count": ("Total Service Request Count", "Performance", "Count", "request_count"),
    "service_response_time": ("Avg Service Response Time", "Performance", "ms", "response_time_ms"),
}
MOCK_APPS = ["CBDC Mobile", "CBDC Internet Banking", "RUPI Switch", "Payment Gateway", "Customer Authentication", "Transaction Processing"]

ZIP_METRIC_FILES = {
    "cpu_pct": ["CPU Usage %"],
    "memory_available_pct": ["Memory available %"],
    "disk_available_pct": ["Disk available %"],
    "network_bps": ["NIC bytes received"],
    "workload": ["New session received"],
    "jvm_threads": ["JVM thread count"],
}


def _clean_url(url):
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Dynatrace Tenant URL is required.")
    return url if re.match(r"^https?://", url, re.I) else "https://" + url


def _safe_sheet(reader, names):
    for name in names:
        if name in reader.sheet_names:
            return pd.read_excel(reader, sheet_name=name)
    return None


def _norm(df):
    out = df.copy()
    out.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_") for c in out.columns]
    return out


def _rename(df, target, aliases):
    if target in df.columns:
        return
    for a in aliases:
        if a in df.columns:
            df.rename(columns={a: target}, inplace=True)
            return


def _prepare_excel(file_storage):
    raw = file_storage.read()
    if not raw:
        raise ValueError("The uploaded Excel file is empty.")
    reader = pd.ExcelFile(io.BytesIO(raw))
    telemetry = _safe_sheet(reader, ["Application Telemetry", "Telemetry", "Host Telemetry"])
    performance = _safe_sheet(reader, ["Performance Metrics", "Application Performance"])
    problems = _safe_sheet(reader, ["Problems", "Problem Records"])
    telemetry = _norm(telemetry if telemetry is not None else pd.DataFrame())
    performance = _norm(performance if performance is not None else pd.DataFrame())
    problems = _norm(problems if problems is not None else pd.DataFrame())
    for df in (telemetry, performance, problems):
        if not df.empty:
            _rename(df, "timestamp", ["time", "date", "datetime"])
            _rename(df, "application", ["app", "application_name", "service", "service_name", "management_zone"])
    _rename(telemetry, "cpu_pct", ["cpu", "cpu_usage", "cpu_utilization", "cpu_percent"])
    _rename(telemetry, "memory_pct", ["memory", "memory_usage", "memory_utilization", "memory_percent", "mem_pct"])
    _rename(telemetry, "disk_pct", ["disk", "disk_usage", "disk_utilization", "disk_percent", "disk_used_pct"])
    _rename(telemetry, "network_bps", ["network", "network_traffic", "network_in", "network_in_bps"])
    _rename(performance, "request_count", ["requests", "requests_count", "service_request_count", "requestcount"])
    _rename(performance, "response_time_ms", ["response_time", "response_ms", "avg_response_time", "service_response_time"])
    if telemetry.empty and performance.empty:
        raise ValueError("No usable data was found. Use the Download Mock Excel Data template and keep the Application Telemetry and Performance Metrics sheet names.")

    def prep(df, label):
        if df.empty:
            return
        if "timestamp" not in df or "application" not in df:
            raise ValueError(f"{label} must contain timestamp and application columns.")
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df["application"] = df["application"].astype(str).str.strip()
        df.dropna(subset=["timestamp", "application"], inplace=True)

    if not telemetry.empty:
        prep(telemetry, "Application Telemetry")
        for c in ["cpu_pct", "memory_pct", "disk_pct", "network_bps"]:
            if c not in telemetry: telemetry[c] = np.nan
            telemetry[c] = pd.to_numeric(telemetry[c], errors="coerce")
        telemetry = telemetry.groupby(["timestamp", "application"], as_index=False).mean(numeric_only=True)
    else:
        telemetry = pd.DataFrame(columns=["timestamp", "application", "cpu_pct", "memory_pct", "disk_pct", "network_bps"])

    if not performance.empty:
        prep(performance, "Performance Metrics")
        for c in ["request_count", "response_time_ms"]:
            if c not in performance: performance[c] = np.nan
            performance[c] = pd.to_numeric(performance[c], errors="coerce")
        performance = performance.groupby(["timestamp", "application"], as_index=False).sum(numeric_only=True)
    else:
        performance = pd.DataFrame(columns=["timestamp", "application", "request_count", "response_time_ms"])

    keys = pd.concat([telemetry[["timestamp", "application"]], performance[["timestamp", "application"]]], ignore_index=True).drop_duplicates()
    data = keys.merge(telemetry, on=["timestamp", "application"], how="left").merge(performance, on=["timestamp", "application"], how="left")
    data["request_count"] = data["request_count"].fillna(0)
    data["response_time_ms"] = data["response_time_ms"].fillna(data.groupby("application")["response_time_ms"].transform("median")).fillna(0)
    data = data.sort_values(["timestamp", "application"]).reset_index(drop=True)
    if data.empty:
        raise ValueError("The workbook contains no valid timestamp/application rows.")

    if problems.empty:
        problems = pd.DataFrame(columns=["title", "severity", "status", "startTime", "duration_min", "application"])
    else:
        _rename(problems, "title", ["problem_title", "name"])
        _rename(problems, "severity", ["problem_severity"])
        _rename(problems, "status", ["problem_status"])
        _rename(problems, "start_time", ["starttime", "start"])
        _rename(problems, "duration_min", ["duration", "duration_minutes"])
        if "start_time" in problems: problems["startTime"] = pd.to_datetime(problems["start_time"], errors="coerce", utc=True)
        elif "timestamp" in problems: problems["startTime"] = pd.to_datetime(problems["timestamp"], errors="coerce", utc=True)
        else: problems["startTime"] = pd.NaT
        for c, default in [("title", "Application problem"), ("severity", "PERFORMANCE"), ("status", "CLOSED"), ("duration_min", 0), ("application", "")]:
            if c not in problems: problems[c] = default
        problems["duration_min"] = pd.to_numeric(problems["duration_min"], errors="coerce").fillna(0)
        problems["application"] = problems["application"].astype(str).replace("nan", "")
        problems = problems[["title", "severity", "status", "startTime", "duration_min", "application"]]
    apps = sorted(data["application"].dropna().astype(str).unique().tolist())
    return {"kind": "excel", "management_zone": "Uploaded Excel Dataset", "data": data, "problems": problems, "applications": apps}


def _parse_value(value):
    if pd.isna(value): return np.nan
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "-"}: return np.nan
    if text.endswith("%"): text = text[:-1]
    match = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(KiB|MiB|GiB|TiB|k|K|m|M|g|G|t|T)?", text)
    if not match: return np.nan
    number = float(match.group(1)); suffix = (match.group(2) or "").lower()
    return number * {"k":1e3,"m":1e6,"g":1e9,"t":1e12,"kib":1024,"mib":1024**2,"gib":1024**3,"tib":1024**4}.get(suffix, 1.0)


def _zip_application_group(entity):
    s = str(entity).lower()
    if "upintapp" in s: return "UPI Internet Application"
    if "upiweb" in s or "upi_web" in s or "upayiw" in s: return "UPI Web"
    if "upi_db" in s or "[db]" in s or "oracle database" in s or "upiprodb" in s: return "UPI Database"
    if "upi_app_f" in s or "upi_app_nf" in s or "upi_app_arr" in s or "upiapp" in s or "upayia" in s or ("upi" in s and "fap" in s): return "UPI Application Platform"
    return "Other Infrastructure"


def _find_zip_member(names, candidates):
    for name in names:
        low = os.path.basename(name).lower()
        for candidate in candidates:
            if candidate.lower() in low: return name
    return None


def _read_zip_metric(zf, member):
    raw = zf.read(member)
    df = pd.read_csv(io.BytesIO(raw)) if member.lower().endswith(".csv") else pd.read_excel(io.BytesIO(raw))
    if df.empty or len(df.columns) < 2: return pd.DataFrame(columns=["timestamp", "application", "value"])
    df["__timestamp"] = pd.to_datetime(df.iloc[:, 0], errors="coerce", dayfirst=True, format="mixed").dt.normalize()
    rows = []
    for entity in df.columns[1:]:
        values = df[entity].map(_parse_value)
        for ts, value in zip(df["__timestamp"], values):
            if pd.notna(ts) and pd.notna(value): rows.append((ts, _zip_application_group(entity), float(value)))
    return pd.DataFrame(rows, columns=["timestamp", "application", "value"])


def _prepare_zip(file_storage):
    raw = file_storage.read()
    if not raw: raise ValueError("The uploaded ZIP file is empty.")
    try: zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile: raise ValueError("The uploaded file is not a valid ZIP archive.")
    names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    if not names: raise ValueError("The ZIP archive does not contain any usable files.")
    members = {key: _find_zip_member(names, candidates) for key, candidates in ZIP_METRIC_FILES.items()}
    if not members["cpu_pct"] and not members["workload"]: raise ValueError("The ZIP does not contain the expected CPU or workload metric files.")
    frames, coverage = {}, {}
    for key, member in members.items():
        if not member: continue
        frame = _read_zip_metric(zf, member)
        if frame.empty: continue
        frames[key] = frame
        coverage[key] = {"file": os.path.basename(member), "rows": int(len(frame)), "entities": int(frame["application"].nunique())}
    if not frames: raise ValueError("No readable metric values were found in the uploaded ZIP.")
    keys = set()
    for frame in frames.values(): keys.update(zip(frame["timestamp"], frame["application"]))
    data = pd.DataFrame(sorted(keys), columns=["timestamp", "application"])

    def aggregate(key, name, reducer="mean", transform=None):
        nonlocal data
        if key not in frames: data[name] = np.nan; return
        f = frames[key].copy()
        if transform: f["value"] = transform(f["value"])
        a = f.groupby(["timestamp", "application"], as_index=False)["value"].sum() if reducer == "sum" else f.groupby(["timestamp", "application"], as_index=False)["value"].mean()
        data = data.merge(a.rename(columns={"value": name}), on=["timestamp", "application"], how="left")

    cpu_transform = lambda s: s * 100 if s.max(skipna=True) <= 1.5 else s
    memory_transform = lambda s: 100 - (s * 100 if s.max(skipna=True) <= 1.5 else s)
    disk_transform = lambda s: 100 - (s * 100 if s.max(skipna=True) <= 1.5 else s)
    aggregate("cpu_pct", "cpu_pct", "mean", cpu_transform)
    aggregate("memory_available_pct", "memory_pct", "mean", memory_transform)
    aggregate("disk_available_pct", "disk_pct", "mean", disk_transform)
    aggregate("network_bps", "network_bps", "mean")
    aggregate("workload", "request_count", "sum")
    aggregate("jvm_threads", "response_time_ms", "mean")
    data = data.sort_values(["timestamp", "application"]).reset_index(drop=True)
    data["request_count"] = data["request_count"].fillna(0)
    if "response_time_ms" not in data: data["response_time_ms"] = np.nan
    applications = sorted([str(name) for name, group in data.groupby("application") if group[["cpu_pct","memory_pct","disk_pct","network_bps","request_count","response_time_ms"]].notna().any().any()])
    if not applications: raise ValueError("The ZIP contained metric files, but no application groups could be derived from the entity names.")
    metric_meta = {
        "host_cpu_usage": {"label":"CPU Utilization","category":"Infrastructure","unit":"%","cap":100},
        "host_mem_usage": {"label":"Memory Utilization","category":"Infrastructure","unit":"%","cap":100},
        "host_disk_used_pct": {"label":"Disk Utilization","category":"Infrastructure","unit":"%","cap":100},
        "network_traffic": {"label":"Network Traffic In","category":"Infrastructure","unit":"BytePerSecond","cap":None},
        "service_request_count": {"label":"New Sessions / day","category":"Workload","unit":"Count","cap":None},
        "service_response_time": {"label":"JVM Active Threads","category":"Performance","unit":"Count","cap":None},
    }
    return {"kind":"zip","management_zone":"Uploaded ZIP Data","data":data,"problems":pd.DataFrame(columns=["title","severity","status","startTime","duration_min","application"]),"applications":applications,"metric_meta":metric_meta,"source_label":"Uploaded ZIP","coverage":coverage,"file_count":len(names),"from":data["timestamp"].min().date().isoformat(),"to":data["timestamp"].max().date().isoformat()}


def mock_data():
    rng=np.random.default_rng(1418); d=pd.date_range("2023-09-01",datetime.now(timezone.utc).date(),freq="D",tz="UTC"); t=np.arange(len(d)); week=np.sin(2*np.pi*t/7); month=np.sin(2*np.pi*t/30.4); rows=[]
    for ai,app_name in enumerate(MOCK_APPS):
        mult=1+ai*.025; cpu=35+.018*t+4*week+ai*1.5+rng.normal(0,1.2,len(d)); mem=48+.012*t+3*week+ai*1.1+rng.normal(0,1.4,len(d)); disk=28+.02*t+2*month+ai*.9+rng.normal(0,1,len(d)); net=(.8e6+1.5e5*week+1200*t)*mult+rng.normal(0,6e4,len(d)); req=(1.1e6+2500*t+2.5e5*week)*mult+rng.normal(0,8e4,len(d)); resp=35+ai*2+.01*t+4*(1+week)+rng.normal(0,1.5,len(d))
        if ai==3: mem+=7+.015*t
        if ai==5: resp+=9+.012*t
        for j,dt in enumerate(d): rows.append({"timestamp":dt,"application":app_name,"cpu_pct":float(np.clip(cpu[j],0,100)),"memory_pct":float(np.clip(mem[j],0,100)),"disk_pct":float(np.clip(disk[j],0,100)),"network_bps":float(max(0,net[j])),"request_count":float(max(0,req[j])),"response_time_ms":float(max(1,resp[j]))})
    data=pd.DataFrame(rows); return {"kind":"mock","management_zone":"CBDCE_RUPISwitch_1418","data":data,"problems":mock_problems(data.timestamp.min(),data.timestamp.max(),data.application.unique()),"applications":MOCK_APPS}


def mock_problems(start,end,applications):
    rng=np.random.default_rng(88); dates=pd.date_range(start,end,freq="7D",tz="UTC"); titles=["Low disk space","Multiple infrastructure problems","Failure rate increase","Response time degradation","SRE Availability Degradation","Multiple service problems"]; sev=["RESOURCE_CONTENTION","AVAILABILITY","AVAILABILITY","PERFORMANCE","CUSTOM_ALERT","ERROR"]; rows=[]
    for i,dt in enumerate(dates):
        for j in range(1+int(i%4==0)+int(i%6==0)): rows.append({"title":titles[(i+j)%6],"severity":sev[(i+j)%6],"status":"CLOSED","startTime":dt+pd.Timedelta(hours=int(rng.integers(0,20))),"duration_min":int(rng.integers(20,500)),"application":applications[(i+j)%len(applications)]})
    return pd.DataFrame(rows)


def _dt_get(cfg,path,params=None,timeout=30):
    r=requests.get(cfg["tenant"]+path,headers={"Authorization":f"Api-Token {cfg['token']}","Accept":"application/json"},params=params,timeout=timeout)
    if r.status_code>=400: raise RuntimeError(f"Dynatrace API returned HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def connect_dynatrace(tenant,token):
    if not token or not token.strip(): raise ValueError("Dynatrace Access Token is required.")
    cfg={"tenant":_clean_url(tenant),"token":token.strip()}; _dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":"builtin:service.requestCount:sum","from":"now-5m","resolution":"Inf","entitySelector":'type("SERVICE")'},20); sid="dt-"+uuid.uuid4().hex[:10]; SOURCES[sid]={"kind":"dynatrace","config":cfg}; return sid


def _series(payload):
    rows=[]
    for result in payload.get("result",[]):
        mid=result.get("metricId","")
        for s in result.get("data",[]):
            dm=s.get("dimensionMap",{}); dims=s.get("dimensions",[]); app_name=dm.get("dt.entity.service") or (dims[0] if dims else "Application")
            for ts,val in zip(s.get("timestamps",[]),s.get("values",[])):
                if val is not None: rows.append({"timestamp":pd.to_datetime(ts,unit="ms",utc=True),"application":app_name,"metric":mid,"value":float(val)})
    return pd.DataFrame(rows)


def _live_data(cfg,management_zone,start,end):
    selector='type("SERVICE")'+(f',mzName("{management_zone}")' if management_zone else ''); fr=pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ"); to=(pd.Timestamp(end)+pd.Timedelta(days=1)-pd.Timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"); frames={}
    for key,ms in {"request_count":'builtin:service.requestCount:sum:splitBy("dt.entity.service")',"response_time_ms":'builtin:service.response.time:avg:splitBy("dt.entity.service")'}.items(): frames[key]=_series(_dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":ms,"entitySelector":selector,"from":fr,"to":to,"resolution":"1h"}))
    req=frames["request_count"].rename(columns={"value":"request_count"})[["timestamp","application","request_count"]]; resp=frames["response_time_ms"].rename(columns={"value":"response_time_ms"})[["timestamp","application","response_time_ms"]]
    if not resp.empty and resp.response_time_ms.median()>10000: resp.response_time_ms/=1000
    perf=req.merge(resp,on=["timestamp","application"],how="outer"); perf.timestamp=perf.timestamp.dt.floor("D"); perf=perf.groupby(["timestamp","application"],as_index=False).agg(request_count=("request_count","sum"),response_time_ms=("response_time_ms","mean"))
    for key,ms in {"cpu_pct":"builtin:host.cpu.usage:avg","memory_pct":"builtin:host.mem.usage:avg","disk_pct":"builtin:host.disk.usedPct:avg"}.items():
        p=_dt_get(cfg,"/api/v2/metrics/query",{"metricSelector":ms,"mzSelector":f'mzName("{management_zone}")' if management_zone else None,"from":fr,"to":to,"resolution":"1h"}); f=_series(p)
        if f.empty: perf[key]=np.nan
        else: f.timestamp=f.timestamp.dt.floor("D"); f=f.groupby("timestamp",as_index=False).value.mean().rename(columns={"value":key}); perf=perf.merge(f,on="timestamp",how="left")
    perf["network_bps"]=np.nan
    if perf.empty: raise RuntimeError("No application/service telemetry was returned for the selected management zone and dates.")
    return {"kind":"dynatrace","management_zone":management_zone or "Dynatrace Management Zone","data":perf,"problems":pd.DataFrame(columns=["title","severity","status","startTime","duration_min","application"]),"applications":sorted(perf.application.dropna().unique().tolist())}


def _window(dataset,start,end,application):
    data=dataset["data"].copy(); data["timestamp"]=pd.to_datetime(data["timestamp"],utc=True); s=pd.Timestamp(start); s=s.tz_localize("UTC") if s.tzinfo is None else s.tz_convert("UTC"); e=pd.Timestamp(end); e=e.tz_localize("UTC") if e.tzinfo is None else e.tz_convert("UTC"); e=e+pd.Timedelta(days=1)-pd.Timedelta(seconds=1)
    if application: data=data[data.application.astype(str)==str(application)]
    data=data[(data.timestamp>=s)&(data.timestamp<=e)].copy(); problems=dataset["problems"].copy()
    if application and not problems.empty and "application" in problems: problems=problems[problems.application.astype(str)==str(application)].copy()
    return data,problems


def _forecast(df,days):
    if df.empty:return pd.DataFrame(columns=["timestamp","forecast","lower","upper"])
    s=df.set_index("timestamp")["value"].resample("D").mean().interpolate().dropna()
    if len(s)<21: vals=np.repeat(s.iloc[-1],days)
    else:
        try: vals=ExponentialSmoothing(s,trend="add",seasonal="add",seasonal_periods=7,initialization_method="estimated").fit(optimized=True).forecast(days).to_numpy()
        except Exception: vals=np.polyval(np.polyfit(np.arange(len(s)),s.to_numpy(),1),np.arange(len(s),len(s)+days))
    resid=float(np.nanstd(np.diff(s.to_numpy()))) if len(s)>2 else .1; idx=pd.date_range(s.index[-1]+pd.Timedelta(days=1),periods=days,freq="D",tz="UTC"); vals=np.asarray(vals,float); return pd.DataFrame({"timestamp":idx,"forecast":vals,"lower":vals-1.96*resid,"upper":vals+1.96*resid})


def _trend(df):
    if df.empty:return {"mean":None,"start":None,"end":None,"change":None,"slope":None,"direction":"insufficient_data"}
    y=df.value.to_numpy(); slope=float(np.polyfit(np.arange(len(y)),y,1)[0]) if len(y)>1 else 0; a=float(y[0]); b=float(y[-1]); ch=(b-a)/abs(a)*100 if a else 0; d="increasing" if slope>abs(y.mean())*.0005 else ("decreasing" if slope<-abs(y.mean())*.0005 else "flat"); return {"mean":float(y.mean()),"start":a,"end":b,"change":float(ch),"slope":slope,"direction":d}


def _points(df,col): return [{"t":pd.Timestamp(t).isoformat(),"v":float(v)} for t,v in zip(df.timestamp,df[col]) if pd.notna(v)]


def _app_table(data,days):
    rows=[]
    for name,g in data.groupby("application"):
        req=g.request_count.dropna(); perf=g.response_time_ms.dropna(); rs=float(np.polyfit(np.arange(len(req)),req.to_numpy(),1)[0]) if len(req)>2 else 0; ps=float(np.polyfit(np.arange(len(perf)),perf.to_numpy(),1)[0]) if len(perf)>2 else 0; rf=max(0,float(req.iloc[-1]+rs*days)) if len(req) else 0; pf=max(0,float(perf.iloc[-1]+ps*days)) if len(perf) else 0; rows.append({"application":str(name),"requests_avg":float(req.mean()) if len(req) else 0,"response_avg":float(perf.mean()) if len(perf) else 0,"request_forecast":rf,"response_forecast":pf})
    return sorted(rows,key=lambda x:x["requests_avg"],reverse=True)


def build_analysis(dataset,start,end,months,growth,application):
    data,problems=_window(dataset,start,end,application)
    if data.empty: raise ValueError(f"No data was found for application '{application}'.")
    days=max(1,int(months)*30); meta=dataset.get("metric_meta",{}); metrics={}; trends={}; forecasts={}
    for key,(default_label,default_cat,default_unit,col) in METRICS.items():
        info=meta.get(key,{}); label=info.get("label",default_label); cat=info.get("category",default_cat); unit=info.get("unit",default_unit); cap=info.get("cap",100 if key in ("host_cpu_usage","host_mem_usage","host_disk_used_pct") else None); df=data[["timestamp",col]].rename(columns={col:"value"}).dropna() if col in data else pd.DataFrame(columns=["timestamp","value"]); df=df.groupby("timestamp",as_index=False).mean() if not df.empty else df; f=_forecast(df,days); trends[key]=_trend(df); forecasts[key]=f; metrics[key]={"label":label,"category":cat,"unit":unit,"cap":cap,"historical":_points(df,"value"),"forecast":_points(f,"forecast"),"lower":_points(f,"lower"),"upper":_points(f,"upper")}
    sf=1+float(growth)/100
    for key,m in metrics.items():
        el=1.15 if key=="service_response_time" and m["unit"]=="ms" else 1; m["simulated_forecast"]=[{"t":p["t"],"v":min(m["cap"],p["v"]*sf**el) if m["cap"] else p["v"]*sf**el} for p in m["forecast"]]
    peaks={k:(float(metrics[k]["forecast"][-1]["v"]) if metrics[k]["forecast"] else 0) for k in ["host_cpu_usage","host_mem_usage","host_disk_used_pct"]}; scenarios=[{"growth":g,"cpu":min(100,peaks["host_cpu_usage"]*(1+g/100)),"memory":min(100,peaks["host_mem_usage"]*(1+g/100)),"disk":min(100,peaks["host_disk_used_pct"]*(1+g/100))} for g in [-20,0,10,25,50]]; apps=_app_table(data,days); perf_label=meta.get("service_response_time",{}).get("label","Avg Service Response Time"); workload_label=meta.get("service_request_count",{}).get("label","Total Service Request Count")
    for r in apps:
        threshold=500 if perf_label.lower().find("response")>=0 else 100; r["status"]="At Risk" if r["response_forecast"]>=threshold*2 else ("Watch" if r["response_forecast"]>=threshold else "Normal")
    return {"management_zone":dataset["management_zone"],"application":application,"summary":{"from":str(pd.Timestamp(start).date()),"to":str(pd.Timestamp(end).date()),"applications":1,"rows":int(len(data)),"problems":int(len(problems)),"baseline_requests":float(data.request_count.mean()) if "request_count" in data else None,"forecast_days":days,"data_source":dataset["kind"],"data_source_label":dataset.get("source_label",dataset["kind"].title()),"workload_label":workload_label,"performance_label":perf_label},"metrics":metrics,"application_table":apps,"trends":trends,"forecasts":forecasts,"problems":problems,"scenarios":scenarios,"recommendations":[{"application":r["application"],"risk":r["status"],"action":"Review workload growth and capacity headroom before the next planning cycle."} for r in apps if r["status"]!="Normal"],"growth":float(growth)}


def get_analysis(source_id,zone,start,end,months,growth,application):
    src=SOURCES.get(source_id)
    if not src: raise ValueError("Please select a data source first.")
    if not application: raise ValueError("Please select an application before loading the report.")
    if src["kind"] in ("excel","mock","zip"): dataset=src["dataset"]
    elif src["kind"]=="dynatrace": dataset=_live_data(src["config"],zone,start,end)
    else: raise ValueError("Unsupported data source.")
    if application not in dataset["applications"]: raise ValueError(f"Application '{application}' was not found in the selected data source.")
    return build_analysis(dataset,start,end,months,growth,application)


def build_mock_workbook():
    d=mock_data(); out=io.BytesIO(); data=d["data"]; telemetry=data[["timestamp","application","cpu_pct","memory_pct","disk_pct","network_bps"]].copy(); perf=data[["timestamp","application","request_count","response_time_ms"]].copy(); probs=d["problems"].copy(); telemetry.timestamp=telemetry.timestamp.dt.tz_localize(None); perf.timestamp=perf.timestamp.dt.tz_localize(None); probs.startTime=pd.to_datetime(probs.startTime,utc=True).dt.tz_localize(None); dictionary=pd.DataFrame([["Application Telemetry","timestamp","ISO/date-time","Observation timestamp"],["Application Telemetry","application","Text","Application/service name"],["Application Telemetry","cpu_pct","Number","Application-associated CPU utilization percentage"],["Application Telemetry","memory_pct","Number","Application-associated memory utilization percentage"],["Application Telemetry","disk_pct","Number","Application-associated disk utilization percentage"],["Application Telemetry","network_bps","Number","Network traffic in bytes per second"],["Performance Metrics","timestamp","ISO/date-time","Observation timestamp"],["Performance Metrics","application","Text","Application/service name"],["Performance Metrics","request_count","Number","Total requests for the application in the interval"],["Performance Metrics","response_time_ms","Number","Average response time in milliseconds"],["Problems","application","Text","Application related to the problem"],["Problems","title","Text","Problem title"],["Problems","severity","Text","Problem severity/category"],["Problems","status","Text","Problem status"],["Problems","startTime","ISO/date-time","Problem start time"],["Problems","duration_min","Number","Problem duration in minutes"]],columns=["Sheet","Column","Type","Description"])
    with pd.ExcelWriter(out,engine="openpyxl") as w: telemetry.to_excel(w,index=False,sheet_name="Application Telemetry"); perf.to_excel(w,index=False,sheet_name="Performance Metrics"); probs.to_excel(w,index=False,sheet_name="Problems"); dictionary.to_excel(w,index=False,sheet_name="Data Dictionary")
    out.seek(0); return out

@app.route("/")
def index():
    d=datetime.now(timezone.utc).date(); return render_template("index.html",default_from=(d-timedelta(days=90)).isoformat(),default_to=d.isoformat(),company_name="ApMoSys",report_title="Capacity & Performance Report")

@app.post("/api/mock-source")
def api_mock_source():
    sid="mock-"+uuid.uuid4().hex[:10]; SOURCES[sid]={"kind":"mock","dataset":mock_data()}; return jsonify({"source_id":sid,"applications":SOURCES[sid]["dataset"]["applications"]})

@app.post("/api/connect-dynatrace")
def api_connect_dynatrace():
    try:
        p=request.get_json() or {}; sid=connect_dynatrace(p.get("tenant_url",""),p.get("access_token","")); return jsonify({"source_id":sid,"message":"Dynatrace connection validated."})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.post("/api/upload-excel")
def api_upload_excel():
    try:
        f=request.files.get("excel_file")
        if f is None or not f.filename: raise ValueError("Please choose an Excel file first.")
        if not f.filename.lower().endswith((".xlsx",".xls")): raise ValueError("Only .xlsx and .xls files are supported.")
        ds=_prepare_excel(f); sid="excel-"+uuid.uuid4().hex[:10]; SOURCES[sid]={"kind":"excel","dataset":ds}; dates=ds["data"].timestamp.dropna(); return jsonify({"source_id":sid,"applications":ds["applications"],"from":dates.min().date().isoformat(),"to":dates.max().date().isoformat(),"management_zone":ds["management_zone"],"rows":len(ds["data"])})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.post("/api/upload-zip")
def api_upload_zip():
    try:
        f=request.files.get("zip_file")
        if f is None or not f.filename: raise ValueError("Please choose a ZIP file first.")
        if not f.filename.lower().endswith(".zip"): raise ValueError("Only .zip files are supported.")
        ds=_prepare_zip(f); sid="zip-"+uuid.uuid4().hex[:10]; SOURCES[sid]={"kind":"zip","dataset":ds}; return jsonify({"source_id":sid,"applications":ds["applications"],"from":ds["from"],"to":ds["to"],"management_zone":ds["management_zone"],"rows":len(ds["data"]),"files":ds["file_count"]})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.get("/api/management-zones")
def zones():
    q=(request.args.get("q") or "").strip().lower(); sid=request.args.get("source_id",""); src=SOURCES.get(sid); names=[]
    if src and src["kind"]=="dynatrace":
        try:
            p=_dt_get(src["config"],"/api/config/v1/managementZones",{"pageSize":100}); names=[{"id":z.get("id",""),"name":z.get("name","")} for z in p.get("values",[])]
        except Exception: names=[]
    return jsonify({"zones":[z for z in names if not q or q in z["name"].lower()]})

@app.get("/api/applications")
def applications():
    sid=request.args.get("source_id",""); zone=request.args.get("management_zone",""); src=SOURCES.get(sid)
    if not src:return jsonify({"error":"Please configure a data source first."}),400
    try:
        if src["kind"] in ("mock","excel","zip"): apps=src["dataset"]["applications"]
        else: apps=_live_data(src["config"],zone,request.args.get("hist_from"),request.args.get("hist_to"))["applications"]
        return jsonify({"applications":[{"id":a,"name":a} for a in apps]})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.post("/api/timeseries")
def timeseries():
    try:
        p=request.get_json() or {}; a=get_analysis(p["source_id"],p.get("management_zone",""),p["hist_from"],p["hist_to"],int(p.get("forecast_months",3)),float(p.get("growth_pct",0)),p.get("application","")); return jsonify({"baseline_requests":a["summary"]["baseline_requests"],"metrics":a["metrics"],"summary":a["summary"],"applications":a["application_table"]})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.get("/sample-data")
def sample(): return send_file(build_mock_workbook(),as_attachment=True,download_name="Capacity_Planner_Mock_Data.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.post("/api/generate")
def generate():
    try:
        p=request.get_json() or {}; a=get_analysis(p["source_id"],p.get("management_zone",""),p["hist_from"],p["hist_to"],int(p.get("forecast_months",3)),float(p.get("growth_pct",0)),p.get("application","")); job=uuid.uuid4().hex[:8]; path=os.path.join(OUT,f"Capacity_Performance_Report_{job}.pdf"); build_pdf_report(path,p.get("company_name") or "ApMoSys",p.get("report_title") or "Capacity & Performance Report",a); return jsonify({"files":[os.path.basename(path)]})
    except Exception as e:return jsonify({"error":str(e)}),400

@app.get("/api/download/<path:name>")
def download(name):
    path=os.path.join(OUT,os.path.basename(name));
    if not os.path.isfile(path): abort(404)
    return send_file(path,as_attachment=True)

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
