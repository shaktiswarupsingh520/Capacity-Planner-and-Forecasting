# Excel Telemetry Capacity Planner

A standalone Python/Flask prototype for the CIO-requested capacity planning flow: upload 2–3 years of CPU, memory and disk telemetry from Excel, forecast future utilization, run traffic-growth what-if simulation, identify projected capacity breaches, and surface recommended actions.

## Data format

Required columns: `timestamp`, `host`, `cpu_pct`, `memory_pct`, `disk_pct`.

Common aliases such as `date`, `hostname`, `cpu`, `memory`, and `disk` are also accepted.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. Use **Download Mock 3-Year Excel** in the UI to generate a synthetic workbook.

## Forecasting

- Normalize uploaded Excel telemetry and aggregate to daily host-level series.
- Use Holt-Winters with weekly seasonality when enough history exists.
- Fall back to linear regression if needed.
- Perform a 28-day holdout backtest and expose MAPE.
- Compare forecast peaks with CPU 80%, memory 85% and disk 80% thresholds.
- Calculate the first projected threshold crossing date.
- Run traffic-growth what-if scenarios using metric-specific directional elasticities.

## What-if model

`simulated = forecast_peak × (1 + growth_pct/100) ^ elasticity`

Current prototype elasticities: CPU 0.90, memory 0.60, disk 0.45. Calibrate these against real workload/resource relationships before production use.

## Architecture

`Excel → Validation → Daily Normalization → Forecasting → Capacity Risk → What-if Simulation → Recommendations → Dashboard`

The analytics layer is independent of Dynatrace, so a future Dynatrace/API adapter can feed the same canonical telemetry model.
