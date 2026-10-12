"""House load without heating, and heating demand against outdoor temperature.

Both are fitted to `energy_hourly` (whole house use, from the MBMD meter
and the inverter) and the MeteoSwiss station's daily mean temperature.

Base load: the mean house use per local hour of the day, over the last year's
days warm enough not to heat (daily mean at least NO_HEATING_FROM °C). It
includes the water heaters, lights, cooking and so on, so PV beyond it is
what's left for the storage heaters.

Heating demand: daily house use = base + k × max(0, NO_HEATING_FROM - daily
mean temperature), a least-squares fit over all days. k is the extra kWh per
day for each °C the day is colder than the limit (heating degree days). It's
rough: the history is heating by hand (and the wood stove, which isn't
measured), so it scatters by ~10 kWh/day. The controller takes its own k
from the config; this fit is shown next to it as a guide (see night.py in
dorfsteig_control).

From 2024-11 to 2026-10: k ≈ 1.7 kWh per °C below 15 °C, base ≈ 31 kWh/day.
"""

import sqlite3
from datetime import datetime, timedelta

from dorfsteig_core.tariff import TZ

NO_HEATING_FROM = 15.0      # °C daily mean; same as the controller's default temperature limit
BASE_LOAD_DAYS = 365
MIN_HOURS = 20              # hours with data for a day to count


def daily_station_temperature(conn: sqlite3.Connection, since: float) -> dict[str, float]:
    """Local ISO date → mean station temperature, for days with at least MIN_HOURS hours."""
    totals: dict[str, list[float]] = {}
    rows = conn.execute("SELECT ts, temperature FROM weather WHERE source = 'meteoswiss' "
                        "AND temperature IS NOT NULL AND ts >= ?", (since,))
    for ts, temperature in rows:
        day = datetime.fromtimestamp(ts, TZ).date().isoformat()
        total = totals.setdefault(day, [0.0, 0])
        total[0] += temperature
        total[1] += 1
    return {day: total / n for day, (total, n) in totals.items() if n >= MIN_HOURS}


def house_hours(conn: sqlite3.Connection, since: float) -> dict[str, dict[int, float]]:
    """Local ISO date → {local hour: house Wh}."""
    days: dict[str, dict[int, float]] = {}
    rows = conn.execute("SELECT ts, house_wh FROM energy_hourly WHERE house_wh IS NOT NULL AND ts >= ?",
                        (since,))
    for ts, wh in rows:
        local = datetime.fromtimestamp(ts, TZ)
        days.setdefault(local.date().isoformat(), {})[local.hour] = wh
    return days


def fit_base_load(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """Mean house Wh per local hour on warm days of the last year. None without such days."""
    since = (now - timedelta(days=BASE_LOAD_DAYS)).timestamp()
    temperatures = daily_station_temperature(conn, since)
    sums = [0.0] * 24
    counts = [0] * 24
    warm_days = 0
    for day, hours in house_hours(conn, since).items():
        warm = temperatures.get(day, -99) >= NO_HEATING_FROM
        if not warm or len(hours) < MIN_HOURS:
            continue
        warm_days += 1
        for hour, wh in hours.items():
            sums[hour] += wh
            counts[hour] += 1
    if warm_days == 0:
        return None
    profile = [sums[h] / counts[h] if counts[h] else 0.0 for h in range(24)]
    return {"wh_per_hour": profile, "days": warm_days, "from_temperature": NO_HEATING_FROM}


def fit_heating(conn: sqlite3.Connection) -> dict | None:
    """Daily house kWh = base + k × degrees below NO_HEATING_FROM; least squares over all days."""
    temperatures = daily_station_temperature(conn, 0)
    xs, ys = [], []
    for day, hours in house_hours(conn, 0).items():
        if len(hours) >= 23 and day in temperatures:
            xs.append(max(0.0, NO_HEATING_FROM - temperatures[day]))
            ys.append(sum(hours.values()) / 1000)
    if len(xs) < 30 or max(xs) == 0:
        return None

    # Straight line y = base + k x, the usual least-squares formulas
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance = sum((x - mean_x) ** 2 for x in xs)
    k = covariance / variance
    base = mean_y - k * mean_x
    mae = sum(abs(base + k * x - y) for x, y in zip(xs, ys)) / n
    return {"kwh_per_degree": k, "base_kwh": base, "daily_mae_kwh": mae, "days": n,
            "below": NO_HEATING_FROM}
