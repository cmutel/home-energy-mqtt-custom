"""Reading the forecasts written by `dorfsteig-forecast` (tables day_forecast, pv_forecast, forecast_models).

The controller doesn't import dorfsteig_forecast; it only reads its tables.
Every function returns None when there's no forecast (service not running
yet, or not enough history to fit the models).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

PV_MODEL = "ratio"   # the PV model the controller uses (pv_forecast.model)


@dataclass(frozen=True)
class DayForecast:
    day: date
    issued: float
    temperature: float | None   # °C, daily mean
    pv_kwh: float
    base_kwh: float             # usual house load without heating
    surplus_kwh: float          # PV beyond that load


def day_forecast(conn: sqlite3.Connection, day: date, now: datetime) -> DayForecast | None:
    """The newest forecast for the local `day` made by `now`."""
    try:
        row = conn.execute(
            "SELECT issued, temperature, pv_kwh, base_kwh, surplus_kwh FROM day_forecast "
            "WHERE day = ? AND issued <= ? ORDER BY issued DESC LIMIT 1",
            (day.isoformat(), now.timestamp())).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    issued, temperature, pv_kwh, base_kwh, surplus_kwh = row
    return DayForecast(day, issued, temperature, pv_kwh, base_kwh, surplus_kwh)


def pv_remaining_kwh(conn: sqlite3.Connection, now: datetime, until: datetime) -> float | None:
    """Forecast PV from now until `until`, the newest forecast for each hour (the current hour pro rata)."""
    try:
        hour_start = int(now.timestamp()) // 3600 * 3600
        rows = conn.execute(
            "SELECT f.ts, f.pv_wh FROM pv_forecast f WHERE f.model = ? AND f.ts >= ? AND f.ts < ? "
            "AND f.issued = (SELECT MAX(issued) FROM pv_forecast "
            "                WHERE model = f.model AND ts = f.ts AND issued <= ?)",
            (PV_MODEL, hour_start, until.timestamp(), now.timestamp())).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    total = 0.0
    for ts, wh in rows:
        if ts == hour_start:
            wh *= 1 - (now.timestamp() - hour_start) / 3600   # only the rest of this hour
        total += wh
    return total / 1000


def fitted_heating(conn: sqlite3.Connection) -> dict | None:
    """The heating demand fitted to the history: kwh_per_degree, below (°C), days, ..."""
    try:
        row = conn.execute("SELECT params FROM forecast_models WHERE name = 'heating'").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else json.loads(row[0])
