"""Forecasts for the heating controller, written to SQLite (the controller only reads the tables).

- pv.py: hourly PV from the weather forecast's radiation
- load.py: usual house load per hour, and heating demand against outdoor temperature
- daily.py: the service loop, writing pv_forecast and day_forecast; backfill for replays
- cli.py: `dorfsteig-forecast run | fit | backfill`

Pure Python and SQLite, so it runs on the Pi without the `ml` extra. See
PLAN.md, phase 4, and notebooks/pv_model.py for why the models are this simple.
"""
