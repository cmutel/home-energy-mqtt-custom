"""Today's mean outdoor temperature, for the room heater temperature limit.

Hour by hour: the MeteoSwiss station where it has measured, otherwise the
latest forecast issued so far (ICON-CH2, then Open-Meteo best_match). Both
tables are filled by `dorfsteig-ingest weather`.
"""

import sqlite3
from datetime import datetime, time, timedelta

FORECAST_MODELS = ("icon_ch2", "icon_ch1", "best_match")
MIN_HOURS = 18   # hours of the day that need a value


def today_mean(conn: sqlite3.Connection, now: datetime) -> tuple[float | None, str]:
    """(mean °C, where it came from) for the local day of `now`; (None, why) if unknown."""
    start = datetime.combine(now.date(), time()).timestamp()
    end = datetime.combine(now.date() + timedelta(days=1), time()).timestamp()
    try:
        observed = dict(conn.execute(
            "SELECT ts, temperature FROM weather WHERE source = 'meteoswiss' AND ts >= ? AND ts < ? "
            "AND temperature IS NOT NULL", (start, end)))
        forecasts = {
            model: dict(conn.execute(
                "SELECT f.ts, f.temperature FROM weather_forecast f "
                "WHERE f.model = ? AND f.ts >= ? AND f.ts < ? "
                "AND f.issued = (SELECT MAX(issued) FROM weather_forecast "
                "                WHERE model = f.model AND ts = f.ts AND issued <= ?)",
                (model, start, end, now.timestamp())))
            for model in FORECAST_MODELS
        }
    except sqlite3.OperationalError:
        return None, "no weather tables"
    values, used = [], set()
    for hour in range(int(start), int(end), 3600):
        if hour in observed:
            values.append(observed[hour])
            used.add("station")
            continue
        for model in FORECAST_MODELS:
            if forecasts[model].get(hour) is not None:
                values.append(forecasts[model][hour])
                used.add(model)
                break
    if len(values) < MIN_HOURS:
        return None, f"only {len(values)} hours of weather data"
    return sum(values) / len(values), " + ".join(sorted(used))
