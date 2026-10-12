"""Weather observations and forecasts, from MeteoSwiss and Open-Meteo.

Observations (table `weather`):
- "meteoswiss": hourly means from the nearest SwissMetNet station, MeteoSwiss
  open government data (Source: MeteoSwiss). Files per station: historical
  (by decade), recent (this year up to yesterday), now (today, hourly).
- "era5": Open-Meteo's archive (ERA5 reanalysis), about 5 days behind; a
  gridded fallback and for comparison.

Forecasts (table `weather_forecast`): MeteoSwiss ICON-CH1 (1 km, ~33 h ahead)
and ICON-CH2 (2 km, 5 days), via Open-Meteo since the official GRIB files
are only kept for 24 h, and Open-Meteo's global "best_match", fetched every
hour and all kept.
"""

import csv
import io
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from dorfsteig_core.db import init_db
from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
STATION_URL = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{station}/ogd-smn_{station}_h_{part}.csv"
VARIABLES = ("temperature_2m", "shortwave_radiation", "cloud_cover")
MODELS = {"meteoswiss_icon_ch1": "icon_ch1", "meteoswiss_icon_ch2": "icon_ch2", "best_match": "best_match"}
UPDATE_INTERVAL = 3600

OBSERVATION = """
    INSERT OR REPLACE INTO weather (ts, source, temperature, radiation, cloud_cover, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?)
"""
FORECAST = """
    INSERT OR REPLACE INTO weather_forecast (issued, ts, model, temperature, radiation, cloud_cover)
    VALUES (?, ?, ?, ?, ?, ?)
"""


def _location() -> dict:
    if settings.latitude is None or settings.longitude is None:
        raise RuntimeError("Set DORFSTEIG_LATITUDE and DORFSTEIG_LONGITUDE in .env")
    return {"latitude": settings.latitude, "longitude": settings.longitude}


def open_meteo(client: httpx.Client, url: str, **params) -> dict:
    response = client.get(url, params={**_location(), "hourly": ",".join(VARIABLES), "timezone": "GMT",
                                       "timeformat": "unixtime", **params}, timeout=60)
    response.raise_for_status()
    return response.json()["hourly"]


# ---- MeteoSwiss station

def parse_station(text: str) -> list[tuple]:
    """(ts, temperature, radiation) from a SwissMetNet hourly CSV; ts = start of the hour, UTC."""
    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        ts = datetime.strptime(row["reference_timestamp"], "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)

        def value(key: str) -> float | None:
            v = row.get(key) or ""
            return float(v) if v.strip() else None

        temp, rad = value("tre200h0"), value("gre000h0")
        if temp is not None or rad is not None:
            rows.append((int(ts.timestamp()), temp, rad))
    return rows


def fetch_station(client: httpx.Client, part: str) -> list[tuple]:
    if not settings.meteoswiss_station:
        raise RuntimeError("Set DORFSTEIG_METEOSWISS_STATION in .env")
    station = settings.meteoswiss_station.lower()
    response = client.get(STATION_URL.format(station=station, part=part), timeout=120)
    response.raise_for_status()
    return parse_station(response.content.decode("latin1"))


def store_station(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    now = int(time.time())
    conn.executemany(OBSERVATION, [(ts, "meteoswiss", t, r, None, now) for ts, t, r in rows])
    conn.commit()
    return len(rows)


# ---- Open-Meteo

def store_archive(conn: sqlite3.Connection, hourly: dict) -> int:
    now = int(time.time())
    rows = [(ts, "era5", t, r, c, now)
            for ts, t, r, c in zip(hourly["time"], *(hourly[v] for v in VARIABLES)) if t is not None]
    conn.executemany(OBSERVATION, rows)
    conn.commit()
    return len(rows)


def store_forecast(conn: sqlite3.Connection, hourly: dict, issued: int) -> int:
    rows = []
    for api_name, model in MODELS.items():
        columns = [hourly[f"{v}_{api_name}"] for v in VARIABLES]
        rows += [(issued, ts, model, t, r, c)
                 for ts, t, r, c in zip(hourly["time"], *columns) if t is not None]
    conn.executemany(FORECAST, rows)
    conn.commit()
    return len(rows)


# ---- entry points

def update(conn: sqlite3.Connection, client: httpx.Client) -> None:
    issued = int(time.time()) // 3600 * 3600
    hourly = open_meteo(client, FORECAST_URL, models=",".join(MODELS), forecast_days=3)
    n = store_forecast(conn, hourly, issued)
    m = store_station(conn, fetch_station(client, "now"))
    last = conn.execute("SELECT MAX(ts) FROM weather WHERE source = 'meteoswiss' AND ts < ?",
                        (issued - 86400,)).fetchone()[0]
    if last is None or last < issued - 2 * 86400:
        m += store_station(conn, fetch_station(client, "recent"))   # fill gaps after downtime
    log.info("Weather: %d forecast values, %d station hours", n, m)


def backfill(conn: sqlite3.Connection, start: date) -> None:
    first = datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()
    with httpx.Client() as client:
        for part in ("historical_2020-2029", "recent", "now"):
            rows = [r for r in fetch_station(client, part) if r[0] >= first]
            log.info("MeteoSwiss %s: %d hours", part, store_station(conn, rows))
        end = date.today() - timedelta(days=5)
        while start <= end:
            stop = min(start + timedelta(days=90), end)
            n = store_archive(conn, open_meteo(client, ARCHIVE_URL, start_date=start.isoformat(),
                                               end_date=stop.isoformat()))
            log.info("ERA5 %s to %s: %d hours", start, stop, n)
            start = stop + timedelta(days=1)


def backfill_forecasts(conn: sqlite3.Connection, start: date) -> None:
    """Day-ahead forecasts as they were issued (Open-Meteo previous runs), for replays.

    Stored with issued = ts - 24 h. ICON-CH1 doesn't reach a day ahead, so
    only ICON-CH2 and best_match. Available from 2025-10 for ICON-CH2.
    """
    names = ",".join(f"{v}_previous_day1" for v in VARIABLES)
    with httpx.Client() as client:
        for api_name in ("meteoswiss_icon_ch2", "best_match"):
            hourly = open_meteo(client, PREVIOUS_RUNS_URL, models=api_name, start_date=start.isoformat(),
                                end_date=date.today().isoformat(), hourly=names)
            columns = [hourly[f"{v}_previous_day1"] for v in VARIABLES]
            rows = [((ts - 86400) // 3600 * 3600, ts, MODELS[api_name], t, r, c)
                    for ts, t, r, c in zip(hourly["time"], *columns) if t is not None]
            conn.executemany(FORECAST, rows)
            conn.commit()
            log.info("Day-ahead %s forecasts: %d hours", MODELS[api_name], len(rows))


def run() -> None:
    conn = init_db()
    log.info("Updating weather every %d s", UPDATE_INTERVAL)
    with httpx.Client() as client:
        while True:
            try:
                update(conn, client)
            except Exception:
                log.exception("Weather update failed")
            time.sleep(UPDATE_INTERVAL)
