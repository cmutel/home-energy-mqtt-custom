"""Reading the forecasts written by `dorfsteig-forecast` (tables day_forecast, pv_forecast, forecast_models).

The controller doesn't import dorfsteig_forecast; it only reads its tables.
Every function returns None when there's no forecast (service not running
yet, or not enough history to fit the models).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime


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
    """Forecast PV from now until `until`, from the newest PV forecast (the current hour pro rata)."""
    try:
        issued = conn.execute("SELECT MAX(issued) FROM pv_forecast WHERE issued <= ?",
                              (now.timestamp(),)).fetchone()[0]
        if issued is None:
            return None
        hour_start = int(now.timestamp()) // 3600 * 3600
        rows = conn.execute("SELECT ts, pv_wh FROM pv_forecast WHERE issued = ? AND ts >= ? AND ts < ?",
                            (issued, hour_start, until.timestamp())).fetchall()
    except sqlite3.OperationalError:
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
