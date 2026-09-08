# Capacity Planner and Forecasting

Standalone Excel-driven capacity planning and forecasting prototype.

## Included

- Excel telemetry upload (`.xlsx` / `.xls`)
- Synthetic 3-year telemetry generator for demos
- Daily host-level normalization
- Holt-Winters forecasting with linear-regression fallback
- 28-day forecast backtesting with MAPE
- CPU, memory and disk capacity thresholds
- Projected breach dates
- Traffic-growth what-if scenarios
- Capacity risk table and recommended actions
- Enterprise dashboard

## Start

```bash
cd excel_capacity_planner
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000` and use **Download Mock 3-Year Excel** for demo data.
