"""Import history from the legacy InfluxDB.

MBMD power goes into grid_readings and inverter_readings; the air quality
sensors (`climate` and `feinstaub` measurements, e.g. the balcony sensor's
outdoor temperature) go into air_quality. Their timestamps are also naive
local time (`~/DataNetwork/mirror_luftdaten.py`).

`~/DataNetwork/mbmd_listener.py` writes one snapshot per field at seconds
0-1 of every minute, timestamped with naive local time. InfluxDB treats those
as UTC, so they are converted from Europe/Zurich wall time here. In the
autumn DST overlap both hours land on the first one, and the second is
dropped.

A snapshot at the start of a minute is a poor stand-in for the minute's
average, which is what IoTaWatt and the live `power` ingester store. The mean
of the snapshots at the start and end of the minute is much closer, so that's
what is stored.

Rows are only added, never replaced: live MBMD minute averages win.
"""

import logging
import sqlite3
from datetime import date, datetime, timezone

import httpx

from dorfsteig_core.db import init_db
from dorfsteig_core.settings import settings
from dorfsteig_core.tariff import TZ

log = logging.getLogger(__name__)

FIRST_DAY = date(2024, 11, 1)

QUERY = (
    'SELECT mean("net-power"), mean("pv-generation"), last("total-import"), last("total-export") '
    "FROM electricity WHERE time >= '{begin}' AND time < '{end}' GROUP BY time(1m) fill(none)"
)


def wall_to_utc(label: int) -> int:
    """InfluxDB 'UTC' seconds that really are Europe/Zurich wall time → real Unix time."""
    wall = datetime.fromtimestamp(label, timezone.utc).replace(tzinfo=TZ)
    return int(wall.timestamp())


def fetch_month(client: httpx.Client, month: date) -> list[list]:
    nxt = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    response = client.get(
        f"{settings.influx_url}/query",
        params={"db": settings.influx_db, "epoch": "s", "q": QUERY.format(begin=month, end=nxt)},
        timeout=120,
    )
    response.raise_for_status()
    series = response.json()["results"][0].get("series")
    return series[0]["values"] if series else []


def to_rows(values: list[list]) -> tuple[list[tuple], list[tuple]]:
    """(grid rows, inverter rows) with real timestamps and start/end-of-minute means."""
    by_ts: dict[int, list] = {}
    for label, grid, pv, imp, exp in values:
        by_ts.setdefault(wall_to_utc(label), [grid, pv, imp, exp])

    def avg(ts: int, i: int) -> float | None:
        now = by_ts[ts][i]
        nxt = by_ts.get(ts + 60, [None] * 4)[i]
        if now is None:
            return None
        return now if nxt is None else (now + nxt) / 2

    grid_rows, inverter_rows = [], []
    for ts, (grid, pv, imp, exp) in sorted(by_ts.items()):
        if grid is not None:
            grid_rows.append((ts, avg(ts, 0), imp, exp))
        if pv is not None:
            inverter_rows.append((ts, avg(ts, 1)))
    return grid_rows, inverter_rows


def import_history(start: date = FIRST_DAY, conn: sqlite3.Connection | None = None) -> None:
    """MBMD power history, then the air quality sensors."""
    conn = conn or init_db()
    import_power(start, conn)
    import_climate(start, conn)


def import_power(start: date, conn: sqlite3.Connection) -> None:
    month = start.replace(day=1)
    today = date.today()
    with httpx.Client() as client:
        while month <= today:
            grid_rows, inverter_rows = to_rows(fetch_month(client, month))
            g = conn.executemany(
                "INSERT OR IGNORE INTO grid_readings (ts, power, import_total, export_total) "
                "VALUES (?, ?, ?, ?)", grid_rows,
            ).rowcount
            i = conn.executemany(
                "INSERT OR IGNORE INTO inverter_readings (ts, power_ac) VALUES (?, ?)", inverter_rows
            ).rowcount
            conn.commit()
            log.info("%s: %d grid and %d inverter minutes added", month.strftime("%Y-%m"), g, i)
            month = date(month.year + month.month // 12, month.month % 12 + 1, 1)


CLIMATE_QUERY = ("SELECT temperature, humidity, sensor FROM climate "
                 "WHERE time >= '{begin}' AND time < '{end}' GROUP BY room")
DUST_QUERY = 'SELECT "2.5", "10" FROM feinstaub WHERE time >= \'{begin}\' AND time < \'{end}\' GROUP BY room'


def _query(client: httpx.Client, q: str) -> list[dict]:
    response = client.get(f"{settings.influx_url}/query",
                          params={"db": settings.influx_db, "epoch": "s", "q": q}, timeout=120)
    response.raise_for_status()
    return response.json()["results"][0].get("series", [])


def climate_rows(climate: list[dict], dust: list[dict]) -> list[tuple]:
    """air_quality rows (ts, room, sensor_id, pm25, pm10, temperature, humidity) from both measurements."""
    pm = {(wall_to_utc(t), s["tags"]["room"]): (p25, p10) for s in dust for t, p25, p10 in s["values"]}
    rows = []
    for series in climate:
        room = series["tags"]["room"]
        for t, temp, hum, sensor in series["values"]:
            ts = wall_to_utc(t)
            p25, p10 = pm.get((ts, room), (None, None))
            rows.append((ts, room, sensor, p25, p10, temp, hum))
    return rows


def import_climate(start: date = FIRST_DAY, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or init_db()
    month = start.replace(day=1)
    with httpx.Client() as client:
        while month <= date.today():
            nxt = date(month.year + month.month // 12, month.month % 12 + 1, 1)
            rows = climate_rows(_query(client, CLIMATE_QUERY.format(begin=month, end=nxt)),
                                _query(client, DUST_QUERY.format(begin=month, end=nxt)))
            n = conn.executemany(
                "INSERT OR IGNORE INTO air_quality (ts, room, sensor_id, pm25, pm10, temperature, humidity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
            ).rowcount
            conn.commit()
            log.info("%s: %d air quality readings added", month.strftime("%Y-%m"), n)
            month = nxt
