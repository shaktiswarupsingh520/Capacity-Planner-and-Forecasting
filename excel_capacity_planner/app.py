from __future__ import annotations

import io
import os
import re
import uuid
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

# In-memory source registry. Excel files and Dynatrace tokens are kept only for the
# lifetime of this Python process; they are not written to the repository.
SOURCES: dict[str, dict] = {}

METRICS = {
    "host_cpu_usage": ("CPU Utilization", "Infrastructure", "%", "cpu"),
    "host_mem_usage": ("Memory Utilization", "Infrastructure", "%", "memory"),
    "host_disk_used_pct": ("Disk Utilization", "Infrastructure", "%", "disk"),
    "network_traffic": ("Network Traffic In", "Infrastructure", "BytePerSecond", "network"),
    "service_request_count": ("Total Service Request Count", "Performance", "Count", "requests"),
    "service_response_time": ("Avg Service Response Time", "Performance", "ms", "response"),
}

MOCK_APPS = [
    "CBDC Mobile",
    "CBDC Internet Banking",
    "RUPI Switch",
    "Payment Gateway",
    "Customer Authentication",
    "Transaction Processing",
]


def _clean_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Dynatrace Tenant URL is required.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _parse_date(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _safe_sheet(reader, names):
    for name in names:
        if name in reader.sheet_names:
            return pd.read_excel(reader, sheet_name=name)
    return None


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")
        for c in out.columns
    ]
    return out


def _rename_first(df: pd.DataFrame, target: str, aliases: list[str]) -> None:
    if target in df.columns:
        return
    for alias in aliases:
        if alias in df.columns:
            df.rename(columns={alias: target}, inplace=True)
            return


def _prepare_excel(file_storage):
    """Read the documented workbook format and return normalized application-level data."""
    raw = file_storage.read()
    if not raw:
        raise ValueError("The uploaded Excel file is empty.")

    reader = pd.ExcelFile(io.BytesIO(raw))
    telemetry = _safe_sheet(reader, ["Application Telemetry", "Telemetry", "Host Telemetry"])
    performance = _safe_sheet(reader, ["Performance Metrics", "Application Performance"])
    problems = _safe_sheet(reader, ["Problems", "Problem Records"])

    if telemetry is None and performance is None:
        telemetry = pd.read_excel(reader, sheet_name=reader.sheet_names[0])
    if telemetry is None:
        telemetry = pd.DataFrame()
    if performance is None:
        performance = pd.DataFrame()
    if problems is None:
        problems = pd.DataFrame()

    telemetry = _normalise_columns(telemetry)
    performance = _normalise_columns(performance)
    problems = _normalise_columns(problems)

    for df in (telemetry, performance, problems):
        if not df.empty:
            _rename_first(df, "timestamp", ["time", "date", "datetime"])
            _rename_first(df, "application", ["app", "application_name", "service", "service_name", "management_zone"])

    _rename_first(telemetry, "cpu_pct", ["cpu", "cpu_usage", "cpu_utilization", "cpu_percent"])
    _rename_first(telemetry, "memory_pct", ["memory", "memory_usage", "memory_utilization", "memory_percent", "mem_pct"])
    _rename_first(telemetry, "disk_pct", ["disk", "disk_usage", "disk_utilization", "disk_percent", "disk_used_pct"])
    _rename_first(telemetry, "network_bps", ["network", "network_traffic", "network_in", "network_in_bps"])
    _rename_first(performance, "request_count", ["requests", "requests_count", "service_request_count", "requestcount"])
    _rename_first(performance, "response_time_ms", ["response_time", "response_ms", "avg_response_time", "service_response_time"])

    if telemetry.empty and performance.empty:
        raise ValueError(
            "No usable data was found. Use the Download Mock Excel Data template and keep "
            "the Application Telemetry and Performance Metrics sheet names."
        )

    def require(df, cols, label):
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{label} is missing required columns: {', '.join(missing)}.")

    if not telemetry.empty:
        require(telemetry, ["timestamp", "application"], "Application Telemetry")
        for c in ["cpu_pct", "memory_pct", "disk_pct", "network_bps"]:
            if c not in telemetry.columns:
                telemetry[c] = np.nan
        telemetry["timestamp"] = pd.to_datetime(telemetry["timestamp"], errors="coerce", utc=True)
        telemetry["application"] = telemetry["application"].astype(str).str.strip()
        for c in ["cpu_pct", "memory_pct", "disk_pct", "network_bps"]:
            telemetry[c] = pd.to_numeric(telemetry[c], errors="coerce")
        telemetry = telemetry.dropna(subset=["timestamp", "application"])
    else:
        telemetry = pd.DataFrame(columns=["timestamp", "application", "cpu_pct", "memory_pct", "disk_pct", "network_bps"])

    if not performance.empty:
        require(performance, ["timestamp", "application"], "Performance Metrics")
        if "request_count" not in performance.columns:
            performance["request_count"] = np.nan
        if "response_time_ms" not in performance.columns:
            performance["response_time_ms"] = np.nan
        performance["timestamp"] = pd.to_datetime(performance["timestamp"], errors="coerce", utc=True)
        performance["application"] = performance["application"].astype(str).str.strip()
        performance["request_count"] = pd.to_numeric(performance["request_count"], errors="coerce")
        performance["response_time_ms"] = pd.to_numeric(performance["response_time_ms"], errors="coerce")
        performance = performance.dropna(subset=["timestamp", "application"])
    else:
        performance = pd.DataFrame(columns=["timestamp", "application", "request_count", "response_time_ms"])

    telemetry = telemetry.groupby(["timestamp", "application"], as_index=False).mean(numeric_only=True)
    performance = performance.groupby(["timestamp", "application"], as_index=False).sum(numeric_only=True)

    keys = pd.concat(
        [telemetry[["timestamp", "application"]], performance[["timestamp", "application"]]],
        ignore_index=True,
    ).drop_duplicates()
    data = keys.merge(telemetry, on=["timestamp", "application"], how="left")
    data = data.merge(performance, on=["timestamp", "application"], how="left")
    data["request_count"] = data["request_count"].fillna(0)
    data["response_time_ms"] = data["response_time_ms"].fillna(
        data.groupby("application")["response_time_ms"].transform("median")
    )
    data["response_time_ms"] = data["response_time_ms"].fillna(0)
    data = data.sort_values(["timestamp", "application"]).reset_index(drop=True)

    if data.empty:
        raise ValueError("The workbook contains no valid timestamp/application rows.")

    if problems.empty:
        problems = pd.DataFrame(columns=["title", "severity", "status", "startTime", "duration_min", "application"])
    else:
        _rename_first(problems, "title", ["problem_title", "name"])
        _rename_first(problems, "severity", ["problem_severity"])
        _rename_first(problems, "status", ["problem_status"])
        _rename_first(problems, "start_time", ["starttime", "start"])
        _rename_first(problems, "duration_min", ["duration", "duration_minutes"])
        if "start_time" in problems.columns:
            problems["startTime"] = pd.to_datetime(problems["start_time"], errors="coerce", utc=True)
        elif "timestamp" in problems.columns:
            problems["startTime"] = pd.to_datetime(problems["timestamp"], errors="coerce", utc=True)
        else:
            problems["startTime"] = pd.NaT
        for c, default in [("title", "Application problem"), ("severity", "PERFORMANCE"), ("status", "CLOSED"), ("duration_min", 0)]:
            if c not in problems.columns:
                problems[c] = default
        problems["duration_min"] = pd.to_numeric(problems["duration_min"], errors="coerce").fillna(0)
        if "application" not in problems.columns:
            problems["application"] = ""
        problems["application"] = problems["application"].astype(str).replace("nan", "")
        problems = problems[["title", "severity", "status", "startTime", "duration_min", "application"]]

    applications = sorted([x for x in data["application"].dropna().unique() if x])
    management_zone = applications[0] if len(applications) == 1 else "Uploaded Excel Dataset"

    return {
        "kind": "excel",
        "management_zone": management_zone,
        "data": data,
        "problems": problems,
        "applications": applications,
    }


def mock_data():
    rng = np.random.default_rng(1418)
    d = pd.date_range("2023-09-01", datetime.now(timezone.utc).date(), freq="D", tz="UTC")
    t = np.arange(len(d))
    week = np.sin(2 * np.pi * t / 7)
    month = np.sin(2 * np.pi * t / 30.4)
    rows = []
    for ai, application in enumerate(MOCK_APPS):
        trend = 1 + ai * 0.025
        cpu = 35 + 0.018 * t + 4 * week + ai * 1.5 + rng.normal(0, 1.2, len(d))
        memory = 48 + 0.012 * t + 3 * week + ai * 1.1 + rng.normal(0, 1.4, len(d))
        disk = 28 + 0.02 * t + 2 * month + ai * 0.9 + rng.normal(0, 1.0, len(d))
        network = (0.8e6 + 1.5e5 * week + 1200 * t) * trend + rng.normal(0, 6e4, len(d))
        requests = (1.1e6 + 2500 * t + 2.5e5 * week) * trend + rng.normal(0, 8e4, len(d))
        response = 35 + ai * 2 + 0.01 * t + 4 * (1 + week) + rng.normal(0, 1.5, len(d))
        if ai == 3:
            memory += 7 + 0.015 * t
        if ai == 5:
            response += 9 + 0.012 * t
        for j, dt in enumerate(d):
            rows.append({
                "timestamp": dt,
                "application": application,
                "cpu_pct": float(np.clip(cpu[j], 0, 100)),
                "memory_pct": float(np.clip(memory[j], 0, 100)),
                "disk_pct": float(np.clip(disk[j], 0, 100)),
                "network_bps": float(max(0, network[j])),
                "request_count": float(max(0, requests[j])),
                "response_time_ms": float(max(1, response[j])),
            })
    data = pd.DataFrame(rows)
    problems = mock_problems(data["timestamp"].min(), data["timestamp"].max(), data["application"].unique())
    return {"kind": "mock", "management_zone": "CBDCE_RUPISwitch_1418", "data": data, "problems": problems, "applications": MOCK_APPS}


def mock_problems(start, end, applications):
    rng = np.random.default_rng(88)
    dates = pd.date_range(start, end, freq="7D", tz="UTC")
    titles = ["Low disk space", "Multiple infrastructure problems", "Failure rate increase", "Response time degradation", "SRE Availability Degradation", "Multiple service problems"]
    severities = ["RESOURCE_CONTENTION", "AVAILABILITY", "AVAILABILITY", "PERFORMANCE", "CUSTOM_ALERT", "ERROR"]
    rows = []
    for i, dt in enumerate(dates):
        n = 1 + int(i % 4 == 0) + int(i % 6 == 0)
        for j in range(n):
            st = dt + pd.Timedelta(hours=int(rng.integers(0, 20)))
            rows.append({"title": titles[(i + j) % len(titles)], "severity": severities[(i + j) % len(severities)], "status": "CLOSED", "startTime": st, "duration_min": int(rng.integers(20, 500)), "application": applications[(i + j) % len(applications)]})
    return pd.DataFrame(rows)


def _dynatrace_get(cfg, path, params=None, timeout=30):
    headers = {"Authorization": f"Api-Token {cfg['token']}", "Accept": "application/json"}
    response = requests.get(f"{cfg['tenant']}{path}", headers=headers, params=params, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Dynatrace API returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def connect_dynatrace(tenant, token):
    tenant = _clean_url(tenant)
    if not token.strip():
        raise ValueError("Dynatrace Access Token is required.")
    cfg = {"tenant": tenant, "token": token.strip()}
    _dynatrace_get(cfg, "/api/v2/metrics/query", params={"metricSelector": "builtin:service.requestCount:sum", "from": "now-5m", "resolution": "Inf", "entitySelector": 'type("SERVICE")'}, timeout=20)
    source_id = f"dt-{uuid.uuid4().hex[:10]}"
    SOURCES[source_id] = {"kind": "dynatrace", "config": cfg}
    return source_id


def _metric_result_to_series(payload, application_prefix="Application"):
    rows = []
    for result in payload.get("result", []):
        metric_id = result.get("metricId", "")
        for series in result.get("data", []):
            dims = series.get("dimensions", [])
            dim_map = series.get("dimensionMap", {})
            application = dim_map.get("dt.entity.service") or (dims[0] if dims else application_prefix)
            for ts, value in zip(series.get("timestamps", []), series.get("values", [])):
                if value is not None:
                    rows.append({"timestamp": pd.to_datetime(ts, unit="ms", utc=True), "application": application, "metric": metric_id, "value": float(value)})
    return pd.DataFrame(rows)


def _live_data(cfg, management_zone, start, end):
    selector = 'type("SERVICE")'
    if management_zone:
        selector += f',mzName("{management_zone}")'
    from_value = pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_value = (pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    service_selectors = {"service_request_count": 'builtin:service.requestCount:sum:splitBy("dt.entity.service")', "service_response_time": 'builtin:service.response.time:avg:splitBy("dt.entity.service")'}
    service_frames = {}
    for key, metric_selector in service_selectors.items():
        payload = _dynatrace_get(cfg, "/api/v2/metrics/query", params={"metricSelector": metric_selector, "entitySelector": selector, "from": from_value, "to": to_value, "resolution": "1h"})
        service_frames[key] = _metric_result_to_series(payload)
    infra_selectors = {"host_cpu_usage": "builtin:host.cpu.usage:avg", "host_mem_usage": "builtin:host.mem.usage:avg", "host_disk_used_pct": "builtin:host.disk.usedPct:avg"}
    infra = {}
    for key, metric_selector in infra_selectors.items():
        payload = _dynatrace_get(cfg, "/api/v2/metrics/query", params={"metricSelector": metric_selector, "mzSelector": f'mzName("{management_zone}")' if management_zone else None, "from": from_value, "to": to_value, "resolution": "1h"})
        frame = _metric_result_to_series(payload, management_zone or "Management Zone")
        if not frame.empty:
            frame = frame.groupby("timestamp", as_index=False)["value"].mean()
        infra[key] = frame
    req = service_frames["service_request_count"].rename(columns={"value": "request_count"})[["timestamp", "application", "request_count"]]
    resp = service_frames["service_response_time"].rename(columns={"value": "response_time_ms"})[["timestamp", "application", "response_time_ms"]]
    if not resp.empty and resp["response_time_ms"].median() > 10000:
        resp["response_time_ms"] = resp["response_time_ms"] / 1000.0
    perf = req.merge(resp, on=["timestamp", "application"], how="outer")
    perf["timestamp"] = perf["timestamp"].dt.floor("D")
    perf = perf.groupby(["timestamp", "application"], as_index=False).agg(request_count=("request_count", "sum"), response_time_ms=("response_time_ms", "mean"))
    for key, frame in infra.items():
        if frame.empty:
            perf[key] = np.nan
            continue
        frame["timestamp"] = frame["timestamp"].dt.floor("D")
        frame = frame.groupby("timestamp", as_index=False)["value"].mean().rename(columns={"value": key})
        perf = perf.merge(frame, on="timestamp", how="left")
    perf["network_bps"] = np.nan
    problems = pd.DataFrame(columns=["title", "severity", "status", "startTime", "duration_min", "application"])
    if perf.empty:
        raise RuntimeError("No application/service telemetry was returned for the selected management zone and dates.")
    return {"kind": "dynatrace", "management_zone": management_zone or "Dynatrace Management Zone", "data": perf, "problems": problems, "applications": sorted(perf["application"].dropna().unique().tolist())}


def select_window(dataset, start, end):
    start_ts = _parse_date(start)
    end_ts = _parse_date(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    data = dataset["data"]
    return data[(data["timestamp"] >= start_ts) & (data["timestamp"] <= end_ts)].copy()


def forecast(df, days):
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "forecast", "lower", "upper"])
    s = df.set_index("timestamp")["value"].resample("D").mean().interpolate().dropna()
    if len(s) < 21:
        vals = np.repeat(s.iloc[-1], days)
    else:
        try:
            vals = ExponentialSmoothing(s, trend="add", seasonal="add", seasonal_periods=7, initialization_method="estimated").fit(optimized=True).forecast(days).to_numpy()
        except Exception:
            vals = np.polyval(np.polyfit(np.arange(len(s)), s.to_numpy(), 1), np.arange(len(s), len(s) + days))
    resid = float(np.nanstd(np.diff(s.to_numpy()))) if len(s) > 2 else 0.1
    idx = pd.date_range(s.index[-1] + pd.Timedelta(days=1), periods=days, freq="D", tz="UTC")
    vals = np.asarray(vals, dtype=float)
    return pd.DataFrame({"timestamp": idx, "forecast": vals, "lower": vals - 1.96 * resid, "upper": vals + 1.96 * resid})


def trend(df):
    if df.empty:
        return {"mean": None, "start": None, "end": None, "change": None, "slope": None, "direction": "insufficient_data"}
    x = np.arange(len(df)); y = df["value"].to_numpy()
    slope = float(np.polyfit(x, y, 1)[0]) if len(df) > 1 else 0
    a, b = float(y[0]), float(y[-1])
    change = (b - a) / abs(a) * 100 if a else 0
    direction = "increasing" if slope > abs(y.mean()) * 0.0005 else ("decreasing" if slope < -abs(y.mean()) * 0.0005 else "flat")
    return {"mean": float(y.mean()), "start": a, "end": b, "change": float(change), "slope": slope, "direction": direction}


def to_points(df, col, scale=1):
    return [{"t": pd.Timestamp(t).isoformat(), "v": float(v) * scale} for t, v in zip(df.timestamp, df[col]) if pd.notna(v)]


def application_table(data, days):
    rows = []
    if data.empty:
        return pd.DataFrame(columns=["application", "requests_avg", "response_avg", "request_forecast", "response_forecast", "status"])
    for application, group in data.groupby("application"):
        req = group["request_count"].dropna(); resp = group["response_time_ms"].dropna()
        req_avg = float(req.mean()) if len(req) else 0; resp_avg = float(resp.mean()) if len(resp) else 0
        req_slope = np.polyfit(np.arange(len(req)), req.to_numpy(), 1)[0] if len(req) > 2 else 0
        resp_slope = np.polyfit(np.arange(len(resp)), resp.to_numpy(), 1)[0] if len(resp) > 2 else 0
        req_fc = max(0, req.iloc[-1] + req_slope * days) if len(req) else 0
        resp_fc = max(0, resp.iloc[-1] + resp_slope * days) if len(resp) else 0
        risk = "At Risk" if resp_fc >= 500 else ("Watch" if resp_fc >= 250 else "Normal")
        rows.append({"application": str(application), "requests_avg": req_avg, "response_avg": resp_avg, "request_forecast": float(req_fc), "response_forecast": float(resp_fc), "status": risk})
    return pd.DataFrame(rows).sort_values("requests_avg", ascending=False)


def build_analysis(dataset, start, end, months, growth):
    data = select_window(dataset, start, end)
    days = max(1, int(months) * 30)
    metrics = {}; trends = {}; forecasts = {}
    metric_columns = {"host_cpu_usage": "cpu_pct", "host_mem_usage": "memory_pct", "host_disk_used_pct": "disk_pct", "network_traffic": "network_bps", "service_request_count": "request_count", "service_response_time": "response_time_ms"}
    for key, (label, category, unit, _) in METRICS.items():
        column = metric_columns[key]
        if column not in data.columns:
            df = pd.DataFrame(columns=["timestamp", "value"])
        else:
            df = data[["timestamp", column]].rename(columns={column: "value"}).dropna()
        df = df.groupby("timestamp", as_index=False)["value"].mean() if not df.empty else df
        f = forecast(df, days); trends[key] = trend(df); forecasts[key] = f
        metrics[key] = {"label": label, "category": category, "unit": unit, "cap": 100 if key in ("host_cpu_usage", "host_mem_usage", "host_disk_used_pct") else None, "historical": to_points(df, "value"), "forecast": to_points(f, "forecast"), "lower": to_points(f, "lower"), "upper": to_points(f, "upper")}
    base = float(data["request_count"].mean()) if not data.empty and "request_count" in data else None
    app_table = application_table(data, days); sf = 1 + float(growth) / 100
    for key, metric in metrics.items():
        elasticity = 1.15 if key == "service_response_time" else 1
        metric["simulated_forecast"] = [{"t": p["t"], "v": min(metric["cap"], p["v"] * sf**elasticity) if metric["cap"] else p["v"] * sf**elasticity} for p in metric["forecast"]]
    scenarios = []
    cpu_peak = float(metrics["host_cpu_usage"]["forecast"][-1]["v"]) if metrics["host_cpu_usage"]["forecast"] else 0
    mem_peak = float(metrics["host_mem_usage"]["forecast"][-1]["v"]) if metrics["host_mem_usage"]["forecast"] else 0
    disk_peak = float(metrics["host_disk_used_pct"]["forecast"][-1]["v"]) if metrics["host_disk_used_pct"]["forecast"] else 0
    for scenario_growth in [-20, 0, 10, 25, 50]:
        factor = 1 + scenario_growth / 100
        scenarios.append({"growth": scenario_growth, "cpu": min(100, cpu_peak * factor), "memory": min(100, mem_peak * factor), "disk": min(100, disk_peak * factor)})
    recommendations = [{"application": row.application, "risk": row.status, "action": "Review request growth and response-time headroom before the next planning cycle."} for row in app_table.itertuples() if row.status != "Normal"]
    return {"management_zone": dataset["management_zone"], "summary": {"from": str(pd.Timestamp(start).date()), "to": str(pd.Timestamp(end).date()), "applications": int(data["application"].nunique()) if not data.empty else 0, "rows": int(len(data)), "problems": int(len(dataset["problems"])), "baseline_requests": base, "forecast_days": days, "data_source": dataset["kind"]}, "metrics": metrics, "application_table": app_table.to_dict("records"), "trends": trends, "forecasts": forecasts, "problems": dataset["problems"], "scenarios": scenarios, "recommendations": recommendations, "growth": float(growth)}


def get_analysis(source_id, management_zone, start, end, months, growth):
    source = SOURCES.get(source_id)
    if not source:
        raise ValueError("Please select a data source first.")
    if source["kind"] in ("excel", "mock"):
        dataset = source["dataset"]
    elif source["kind"] == "dynatrace":
        dataset = _live_data(source["config"], management_zone, start, end)
    else:
        raise ValueError("Unsupported data source.")
    return build_analysis(dataset, start, end, months, growth)


def build_mock_workbook():
    dataset = mock_data(); data = dataset["data"].copy(); out = io.BytesIO()
    telemetry = data[["timestamp", "application", "cpu_pct", "memory_pct", "disk_pct", "network_bps"]].copy()
    performance = data[["timestamp", "application", "request_count", "response_time_ms"]].copy()
    problems = dataset["problems"].copy()
    telemetry["timestamp"] = telemetry["timestamp"].dt.tz_localize(None)
    performance["timestamp"] = performance["timestamp"].dt.tz_localize(None)
    if "startTime" in problems.columns:
        problems["startTime"] = pd.to_datetime(problems["startTime"], utc=True).dt.tz_localize(None)
    data_dictionary = pd.DataFrame([
        ["Application Telemetry", "timestamp", "ISO/date-time", "Observation timestamp"],
        ["Application Telemetry", "application", "Text", "Application/service name; this is the business/application dimension used by the UI"],
        ["Application Telemetry", "cpu_pct", "Number", "Application-associated CPU utilization percentage"],
        ["Application Telemetry", "memory_pct", "Number", "Application-associated memory utilization percentage"],
        ["Application Telemetry", "disk_pct", "Number", "Application-associated disk utilization percentage"],
        ["Application Telemetry", "network_bps", "Number", "Network traffic in bytes per second"],
        ["Performance Metrics", "timestamp", "ISO/date-time", "Observation timestamp"],
        ["Performance Metrics", "application", "Text", "Application/service name"],
        ["Performance Metrics", "request_count", "Number", "Total requests for the application in the interval"],
        ["Performance Metrics", "response_time_ms", "Number", "Average response time in milliseconds"],
        ["Problems", "application", "Text", "Application related to the problem"],
        ["Problems", "title", "Text", "Problem title"],
        ["Problems", "severity", "Text", "Problem severity/category"],
        ["Problems", "status", "Text", "Problem status"],
        ["Problems", "startTime", "ISO/date-time", "Problem start time"],
        ["Problems", "duration_min", "Number", "Problem duration in minutes"],
    ], columns=["Sheet", "Column", "Type", "Description"])
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        telemetry.to_excel(writer, index=False, sheet_name="Application Telemetry")
        performance.to_excel(writer, index=False, sheet_name="Performance Metrics")
        problems.to_excel(writer, index=False, sheet_name="Problems")
        data_dictionary.to_excel(writer, index=False, sheet_name="Data Dictionary")
    out.seek(0); return out


@app.route("/")
def index():
    d = datetime.now(timezone.utc).date()
    return render_template("index.html", default_from=(d - timedelta(days=90)).isoformat(), default_to=d.isoformat(), company_name="ApMoSys", report_title="Capacity & Performance Report")


@app.post("/api/mock-source")
def api_mock_source():
    source_id = "mock-" + uuid.uuid4().hex[:10]
    SOURCES[source_id] = {"kind": "mock", "dataset": mock_data()}
    return jsonify({"source_id": source_id})


@app.post("/api/connect-dynatrace")
def api_connect_dynatrace():
    try:
        payload = request.get_json() or {}
        source_id = connect_dynatrace(payload.get("tenant_url", ""), payload.get("access_token", ""))
        return jsonify({"source_id": source_id, "message": "Dynatrace connection validated."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/upload-excel")
def api_upload_excel():
    try:
        uploaded = request.files.get("excel_file")
        if uploaded is None or not uploaded.filename:
            raise ValueError("Please choose an Excel file.")
        if not uploaded.filename.lower().endswith((".xlsx", ".xls")):
            raise ValueError("Only .xlsx and .xls files are supported.")
        dataset = _prepare_excel(uploaded)
        source_id = f"excel-{uuid.uuid4().hex[:10]}"
        SOURCES[source_id] = {"kind": "excel", "dataset": dataset}
        dates = dataset["data"]["timestamp"].dropna()
        return jsonify({"source_id": source_id, "applications": dataset["applications"], "from": dates.min().date().isoformat(), "to": dates.max().date().isoformat(), "management_zone": dataset["management_zone"], "rows": len(dataset["data"])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/management-zones")
def zones():
    q = (request.args.get("q") or "").strip().lower(); source_id = request.args.get("source_id", ""); source = SOURCES.get(source_id)
    if source and source["kind"] == "dynatrace":
        try:
            payload = _dynatrace_get(source["config"], "/api/config/v1/managementZones", params={"pageSize": 100})
            names = [{"id": z.get("id", ""), "name": z.get("name", "")} for z in payload.get("values", [])]
        except Exception:
            names = []
    elif source and source["kind"] in ("excel", "mock"):
        names = [{"id": a, "name": a} for a in source["dataset"]["applications"]]
    else:
        names = [{"id": "mock-cbdce", "name": "CBDCE_RUPISwitch_1418"}]
    return jsonify({"zones": [z for z in names if not q or q in z["name"].lower()]})


@app.post("/api/timeseries")
def timeseries():
    try:
        p = request.get_json() or {}
        a = get_analysis(p["source_id"], p.get("management_zone", ""), p["hist_from"], p["hist_to"], int(p.get("forecast_months", 3)), float(p.get("growth_pct", 0)))
        return jsonify({"baseline_requests": a["summary"]["baseline_requests"], "metrics": a["metrics"], "summary": a["summary"], "applications": a["application_table"]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/sample-data")
def sample():
    return send_file(build_mock_workbook(), as_attachment=True, download_name="Capacity_Planner_Mock_Data.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/generate")
def generate():
    try:
        p = request.get_json() or {}
        a = get_analysis(p["source_id"], p.get("management_zone", ""), p["hist_from"], p["hist_to"], int(p.get("forecast_months", 3)), float(p.get("growth_pct", 0)))
        job = uuid.uuid4().hex[:8]
        path = os.path.join(OUT, f"Capacity_Performance_Report_{job}.pdf")
        build_pdf_report(path, p.get("company_name") or "ApMoSys", p.get("report_title") or "Capacity & Performance Report", a)
        return jsonify({"files": [os.path.basename(path)]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/download/<path:name>")
def download(name):
    path = os.path.join(OUT, os.path.basename(name))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
