# Capacity Planner and Forecasting

Local test build of the Dynatrace Capacity & Performance workflow, preserving the original application layout and interaction model while adding a deterministic Mock Excel data source.

## What is preserved

- Management Zone selection workflow
- Historical date range and forecast horizon controls
- What-If traffic growth slider and presets
- Four-panel historical / baseline forecast / simulated forecast charts
- KPI cards for CPU, memory, request count and response time
- Generate Report workflow
- Reference-style Capacity & Performance PDF with executive summary, metric charts, host resource table, problem analysis, what-if simulation and recommendations

## Mock data

Select **Mock Excel Data — CBDCE_RUPISwitch_1418** in the Management Zone section. The application generates deterministic three-year telemetry for the demo environment and uses the same forecasting / simulation presentation as the original UI.

You can also download the workbook with **Download Mock Excel Data**. It contains `Host Telemetry` and `Performance Metrics` sheets.

## Run locally

```bash
cd excel_capacity_planner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

This build is intended for local functional testing and demonstration; it is not a production deployment package.
