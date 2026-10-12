"""PV forecast: forecast radiation times a factor per week of the year and hour of the day.

PV output depends on the radiation reaching the panels. The weather
forecast gives global horizontal radiation; how much of that our panels
turn into power depends on the sun's position (hour and season), the
panels' orientation, shading and the inverter. Rather than model those, we
learn one factor per (week of the year, hour of the day) from our history:

    PV Wh in the hour ≈ factor[week][hour] × forecast radiation (W/m²)

Each factor is a least-squares fit through zero, over every hour at that
time of day within ±WINDOW_DAYS of the week's middle, in any year. It's
fitted against the radiation *forecast a day ahead*, not the measured
radiation, so it also absorbs the forecast's bias (ICON-CH2 forecasts a
little more sun than the station measures).

Hours are UTC, because the sun doesn't follow daylight saving time.

Why not machine learning: notebooks/pv_model.py compares this with gradient
boosting (radiation, temperature, cloud cover, sun position). Leaving out a
month at a time, from 2025-10 to 2026-10: this ratio misses daily PV by
5.9 kWh on average, gradient boosting by 5.8, a single factor by 7.1, and
"the monthly mean" by 10.4 (mean PV 31 kWh/day). Most of the remaining error
is in the weather forecast itself, so the simple model is good enough.
"""

import sqlite3
from datetime import UTC, datetime

MODEL = "ratio"            # name in pv_forecast
WINDOW_DAYS = 30
WEEKS = 52
DAY_AHEAD = (12 * 3600, 36 * 3600)   # forecasts issued this long before the hour count as "day-ahead"
FIT_MODEL = "icon_ch2"


def week_and_hour(ts: float) -> tuple[int, int]:
    """(week 0-51, hour 0-23) in UTC. Days 365 and 366 belong to the last week."""
    t = datetime.fromtimestamp(ts, UTC)
    day_of_year = t.timetuple().tm_yday
    week = min((day_of_year - 1) // 7, WEEKS - 1)
    return week, t.hour


def day_ahead_radiation(conn: sqlite3.Connection) -> dict[int, float]:
    """Hour → radiation from the newest FIT_MODEL forecast issued 12-36 h before it."""
    rows = conn.execute(
        "SELECT ts, issued, radiation FROM weather_forecast "
        "WHERE model = ? AND radiation IS NOT NULL AND issued BETWEEN ts - ? AND ts - ?",
        (FIT_MODEL, DAY_AHEAD[1], DAY_AHEAD[0]),
    )
    newest: dict[int, tuple[int, float]] = {}
    for ts, issued, radiation in rows:
        if ts not in newest or issued > newest[ts][0]:
            newest[ts] = (issued, radiation)
    return {ts: radiation for ts, (_, radiation) in newest.items()}


def fit(conn: sqlite3.Connection) -> dict | None:
    """Fit the factors to all hours with both PV and a day-ahead forecast. None if there are none."""
    radiation = day_ahead_radiation(conn)
    pv = dict(conn.execute("SELECT ts, pv_wh FROM energy_hourly WHERE pv_wh IS NOT NULL"))
    hours = [ts for ts in radiation if ts in pv]
    if not hours:
        return None

    # Sums for the least-squares fit, per (day of year, hour): Σ pv × rad and Σ rad²
    pv_times_rad: dict[tuple[int, int], float] = {}
    rad_squared: dict[tuple[int, int], float] = {}
    for ts in hours:
        t = datetime.fromtimestamp(ts, UTC)
        key = (t.timetuple().tm_yday, t.hour)
        pv_times_rad[key] = pv_times_rad.get(key, 0.0) + pv[ts] * radiation[ts]
        rad_squared[key] = rad_squared.get(key, 0.0) + radiation[ts] ** 2

    factors = []
    for week in range(WEEKS):
        middle = week * 7 + 4
        row = []
        for hour in range(24):
            numerator = denominator = 0.0
            for day in range(1, 367):
                distance = abs(day - middle)
                distance = min(distance, 365 - distance)    # the year wraps around
                if distance <= WINDOW_DAYS:
                    numerator += pv_times_rad.get((day, hour), 0.0)
                    denominator += rad_squared.get((day, hour), 0.0)
            # No radiation at this time (night): no PV
            row.append(numerator / denominator if denominator > 0 else 0.0)
        factors.append(row)

    params = {"factors": factors, "hours": len(hours), "first": min(hours), "last": max(hours)}
    params["daily_mae_kwh"] = in_sample_daily_error(params, hours, radiation, pv)
    return params


def predict_wh(params: dict, ts: float, radiation: float) -> float:
    week, hour = week_and_hour(ts)
    return max(0.0, params["factors"][week][hour] * radiation)


def in_sample_daily_error(params: dict, hours: list[int], radiation: dict, pv: dict) -> float:
    """Mean absolute error of daily PV (kWh) on the fitting data: a check, not a skill score."""
    actual: dict[int, float] = {}
    predicted: dict[int, float] = {}
    count: dict[int, int] = {}
    for ts in hours:
        day = ts // 86400    # UTC days are good enough for a check
        actual[day] = actual.get(day, 0.0) + pv[ts]
        predicted[day] = predicted.get(day, 0.0) + predict_wh(params, ts, radiation[ts])
        count[day] = count.get(day, 0) + 1
    full_days = [day for day in actual if count[day] >= 20]
    if not full_days:
        return float("nan")
    return sum(abs(actual[d] - predicted[d]) for d in full_days) / len(full_days) / 1000
