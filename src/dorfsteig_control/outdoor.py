"""Mean outdoor temperature of a day, for the room heater decisions.

Hour by hour: the MeteoSwiss station where it has measured, otherwise the
latest forecast issued so far (ICON-CH2, then ICON-CH1, then Open-Meteo's
best_match; see dorfsteig_core.weather). Both tables are filled by
`dorfsteig-ingest weather`.

Used for the zone budgets (budget.py) of the current budget day and of
tomorrow (the night plan, see night.py), which is then forecast only.
"""

import sqlite3
from datetime import date, datetime, time, timedelta

from dorfsteig_core.weather import forecast_hours

MIN_HOURS = 18   # hours of the day that need a value


def day_mean(conn: sqlite3.Connection, day: date, now: datetime) -> tuple[float | None, str]:
    """(mean °C, where it came from) for the local `day`, as known at `now`; (None, why) if unknown."""
    start = datetime.combine(day, time()).timestamp()
    end = datetime.combine(day + timedelta(days=1), time()).timestamp()
    try:
        observed = dict(conn.execute(
            "SELECT ts, temperature FROM weather WHERE source = 'meteoswiss' AND ts >= ? AND ts < ? "
            "AND temperature IS NOT NULL", (start, end)))
        forecast = forecast_hours(conn, start, end, as_of=now.timestamp())
    except sqlite3.OperationalError:
        return None, "no weather tables"

    values = []
    used = set()
    for hour in range(int(start), int(end), 3600):
        if hour in observed:
            values.append(observed[hour])
            used.add("station")
        elif hour in forecast:
            values.append(forecast[hour].temperature)
            used.add(forecast[hour].model)
    if len(values) < MIN_HOURS:
        return None, f"only {len(values)} hours of weather data"
    return sum(values) / len(values), " + ".join(sorted(used))


def today_mean(conn: sqlite3.Connection, now: datetime) -> tuple[float | None, str]:
    return day_mean(conn, now.date(), now)
