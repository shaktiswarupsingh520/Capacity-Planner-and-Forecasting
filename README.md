# Capacity Planner and Forecasting

Local test build of the Dynatrace Capacity & Performance workflow, preserving the original application layout and interaction model while allowing the user to choose the telemetry source.

## Data sources

The Management Zone section now supports three modes:

1. **Mock Excel Data** — deterministic demo data for the CBDCE/RUPI Switch use case.
2. **Upload Excel** — upload a user-prepared `.xlsx`/`.xls` dataset and populate the same UI from that data.
3. **Dynatrace (Live)** — enter the Dynatrace Tenant URL and API Access Token, connect, select/search a management zone, and query live service/infrastructure metrics.

Dynatrace credentials are kept in process memory for the current application session and are not written to the repository.

## What is preserved

- Management Zone selection workflow
- Historical date range and forecast horizon controls
- What-If traffic growth slider and presets
- Four-panel historical / baseline forecast / simulated forecast charts
- KPI cards for CPU, memory, request count and response time
- Generate Report workflow
- Reference-style Capacity & Performance PDF
- Application-wise capacity view instead of exposing server/host rows in the UI/report

## Excel upload format

Use **Download Mock Excel Data** in the application to get the ready-to-use template. The workbook contains:

- `Application Telemetry` — `timestamp`, `application`, `cpu_pct`, `memory_pct`, `disk_pct`, `network_bps`
- `Performance Metrics` — `timestamp`, `application`, `request_count`, `response_time_ms`
- `Problems` — optional application-level problem records
- `Data Dictionary` — column definitions and expected data types

The important dimension is **application**. Server/host identifiers are not required for the application-wise view.

## Run locally

```bash
cd excel_capacity_planner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Dynatrace live mode

For the current live connector, use a Dynatrace environment URL and an API access token with permission to read metrics. The connector uses the Metrics API to query service request/response data and management-zone-scoped infrastructure metrics. If the token cannot read management-zone configuration, the user can still type the management zone name manually.

This build is intended for local functional testing and demonstration; it is not a production deployment package.
