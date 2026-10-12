"""The forecaster: keeps `pv_forecast` and `day_forecast` up to date.

Every UPDATE_S it checks for a newer weather forecast (fetched hourly by
`dorfsteig-ingest weather`). When there is one, it

1. refits the models (pv.py, load.py) if they're more than a day old, and
   stores them in `forecast_models`;
2. writes the PV forecast for every hour from today to the day after
   tomorrow to `pv_forecast`;
3. writes a summary per day to `day_forecast`: mean temperature, PV, usual
   house load, and the PV surplus beyond that load.

The controller reads these tables (it doesn't import this package); without
them it still runs, just without a PV forecast.

`backfill` fills in the past from the archived weather forecasts:
- `day_forecast` as it would have been at 20:00 the evening before, when the
  controller makes its night plan (for `dorfsteig-control replay --night`);
- `pv_forecast` for every hour, made 24 h ahead (for the Learning dashboard).
The models are fitted on the same history, so backfilled forecasts look a
little better than real ones would have been.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock

from dorfsteig_core.db import init_db
from dorfsteig_core.tariff import TZ
from dorfsteig_core.weather import forecast_hours
from dorfsteig_forecast import load, pv

log = logging.getLogger(__name__)

UPDATE_S = 600
REFIT_S = 24 * 3600
DAYS_AHEAD = 3              # today, tomorrow, the day after
MIN_HOURS = 18              # forecast hours needed for a day summary
PLAN_TIME = clock(20, 0)    # backfill: forecast as known at this time the evening before


# ---- models

def save_model(conn: sqlite3.Connection, name: str, params: dict, now: float) -> None:
    conn.execute("INSERT OR REPLACE INTO forecast_models (name, fitted_at, params) VALUES (?, ?, ?)",
                 (name, int(now), json.dumps(params)))
    conn.commit()


def load_model(conn: sqlite3.Connection, name: str) -> tuple[dict | None, float]:
    """(params, fitted_at); (None, 0) if never fitted."""
    row = conn.execute("SELECT params, fitted_at FROM forecast_models WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None, 0.0
    return json.loads(row[0]), row[1]


def refit(conn: sqlite3.Connection, now: datetime) -> None:
    """Fit all models to the history and store the ones that could be fitted."""
    fitted = {
        "pv": pv.fit(conn),
        "base_load": load.fit_base_load(conn, now),
        "heating": load.fit_heating(conn),
    }
    for name, params in fitted.items():
        if params is None:
            log.warning("Not enough data to fit the %s model", name)
        else:
            save_model(conn, name, params, now.timestamp())
    log.info("Models refitted")


# ---- forecasts

@dataclass
class DayForecast:
    day: date
    temperature: float
    pv_kwh: float
    base_kwh: float
    surplus_kwh: float
    hours: dict[int, float]   # hour start → PV Wh


def forecast_day(conn: sqlite3.Connection, day: date, as_of: float,
                 pv_params: dict, base_params: dict | None) -> DayForecast | None:
    """The local `day` as forecast at `as_of`; None if fewer than MIN_HOURS hours are forecast."""
    start = datetime.combine(day, clock(), TZ)
    end = datetime.combine(day + timedelta(days=1), clock(), TZ)
    weather = forecast_hours(conn, start.timestamp(), end.timestamp(), as_of)
    if len(weather) < MIN_HOURS:
        return None

    temperatures = []
    pv_by_hour: dict[int, float] = {}
    base_total = surplus_total = 0.0
    for ts, hour in sorted(weather.items()):
        temperatures.append(hour.temperature)
        pv_wh = pv.predict_wh(pv_params, ts, hour.radiation or 0.0)
        pv_by_hour[ts] = pv_wh
        if base_params is not None:
            base_wh = base_params["wh_per_hour"][datetime.fromtimestamp(ts, TZ).hour]
            base_total += base_wh
            surplus_total += max(0.0, pv_wh - base_wh)

    return DayForecast(
        day=day,
        temperature=sum(temperatures) / len(temperatures),
        pv_kwh=sum(pv_by_hour.values()) / 1000,
        base_kwh=base_total / 1000,
        surplus_kwh=surplus_total / 1000,
        hours=pv_by_hour,
    )


def store_day(conn: sqlite3.Connection, issued: int, forecast: DayForecast, with_hours: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO day_forecast (issued, day, temperature, pv_kwh, base_kwh, surplus_kwh) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (issued, forecast.day.isoformat(), forecast.temperature, forecast.pv_kwh, forecast.base_kwh,
         forecast.surplus_kwh),
    )
    if with_hours:
        conn.executemany("INSERT OR REPLACE INTO pv_forecast (model, ts, issued, pv_wh) VALUES (?, ?, ?, ?)",
                         [(pv.MODEL, ts, issued, wh) for ts, wh in forecast.hours.items()])


def update(conn: sqlite3.Connection, now: datetime) -> bool:
    """Forecast the next days if there's a newer weather forecast. True if something was written."""
    issued = conn.execute("SELECT MAX(issued) FROM weather_forecast WHERE issued <= ?",
                          (now.timestamp(),)).fetchone()[0]
    if issued is None:
        log.warning("No weather forecasts yet")
        return False
    done = conn.execute("SELECT 1 FROM day_forecast WHERE issued = ? LIMIT 1", (issued,)).fetchone()
    if done:
        return False

    _, fitted_at = load_model(conn, "pv")
    if now.timestamp() - fitted_at > REFIT_S:
        refit(conn, now)
    pv_params, _ = load_model(conn, "pv")
    base_params, _ = load_model(conn, "base_load")
    if pv_params is None:
        log.warning("No PV model yet (needs PV history and day-ahead weather forecasts)")
        return False

    today = now.astimezone(TZ).date()
    for offset in range(DAYS_AHEAD):
        forecast = forecast_day(conn, today + timedelta(days=offset), issued, pv_params, base_params)
        if forecast is None:
            continue
        store_day(conn, issued, forecast)
        log.info("%s: %.1f °C, PV %.1f kWh, surplus %.1f kWh", forecast.day, forecast.temperature,
                 forecast.pv_kwh, forecast.surplus_kwh)
    conn.commit()
    return True


def backfill(conn: sqlite3.Connection, start: date, end: date) -> int:
    """Day forecasts for past days (as known at PLAN_TIME the evening before) and hourly
    PV forecasts (made 24 h ahead). Returns days written."""
    refit(conn, datetime.now(TZ))
    pv_params, _ = load_model(conn, "pv")
    base_params, _ = load_model(conn, "base_load")
    if pv_params is None:
        raise RuntimeError("No PV model: needs PV history and day-ahead weather forecasts")
    written = 0
    day = start
    while day <= end:
        as_of = datetime.combine(day - timedelta(days=1), PLAN_TIME, TZ)
        forecast = forecast_day(conn, day, as_of.timestamp(), pv_params, base_params)
        if forecast is not None:
            store_day(conn, int(as_of.timestamp()), forecast, with_hours=False)
            written += 1
        backfill_hours(conn, day, pv_params)
        day += timedelta(days=1)
    conn.commit()
    return written


def backfill_hours(conn: sqlite3.Connection, day: date, pv_params: dict) -> None:
    """PV forecast for each hour of the local `day`, from the weather forecast known 24 h before it."""
    hour = datetime.combine(day, clock(), TZ)
    end = datetime.combine(day + timedelta(days=1), clock(), TZ)
    rows = []
    while hour < end:
        ts = int(hour.timestamp())
        issued = ts - 24 * 3600
        weather = forecast_hours(conn, ts, ts + 3600, as_of=issued)
        if ts in weather:
            rows.append((pv.MODEL, ts, issued, pv.predict_wh(pv_params, ts, weather[ts].radiation or 0.0)))
        hour += timedelta(hours=1)
    conn.executemany("INSERT OR REPLACE INTO pv_forecast (model, ts, issued, pv_wh) VALUES (?, ?, ?, ?)",
                     rows)


def run() -> None:
    conn = init_db()
    log.info("Forecasting every %d s", UPDATE_S)
    while True:
        try:
            update(conn, datetime.now(TZ))
        except Exception:
            log.exception("Forecast update failed")
        time.sleep(UPDATE_S)
