"""Reading the weather tables: the forecast as it was known at a given time.

`weather_forecast` keeps every forecast fetched (see dorfsteig_ingest.weather),
so for any hour there are many forecasts, issued at different times and by
different models. The forecaster, the controller and the replays all need
the same thing: for each hour, the newest forecast that had been issued by
some moment `as_of`, from the best model that has one.
"""

import sqlite3
from dataclasses import dataclass

# Best first. ICON-CH2 forecasts the local temperature best (see PLAN.md, 3a);
# ICON-CH1 only reaches ~33 h ahead; best_match is Open-Meteo's global default.
FORECAST_MODELS = ("icon_ch2", "icon_ch1", "best_match")


@dataclass(frozen=True)
class ForecastHour:
    ts: int                    # start of the hour, Unix time
    model: str
    temperature: float | None  # °C
    radiation: float | None    # W/m², global horizontal, mean over the hour


def forecast_hours(conn: sqlite3.Connection, start: float, end: float,
                   as_of: float) -> dict[int, ForecastHour]:
    """For each hour in [start, end): the newest forecast issued at or before `as_of`.

    Models are tried in FORECAST_MODELS order, hour by hour. Hours that no
    model covers are missing from the result.
    """
    result: dict[int, ForecastHour] = {}
    for model in FORECAST_MODELS:
        rows = conn.execute(
            """
            SELECT f.ts, f.temperature, f.radiation
            FROM weather_forecast f
            WHERE f.model = ? AND f.ts >= ? AND f.ts < ?
              AND f.issued = (SELECT MAX(issued) FROM weather_forecast
                              WHERE model = f.model AND ts = f.ts AND issued <= ?)
            """,
            (model, start, end, as_of),
        )
        for ts, temperature, radiation in rows:
            if ts not in result and temperature is not None:
                result[ts] = ForecastHour(ts, model, temperature, radiation)
    return result
