"""Health of everything the controller and dashboards depend on, as traffic lights.

Each source has the age of its newest data and two thresholds: green below
the first, yellow below the second, red above (or never seen).
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dorfsteig_control.service import Controller


@dataclass(frozen=True)
class Source:
    name: str
    detail: str
    age_s: float | None
    yellow_s: float
    red_s: float

    @property
    def level(self) -> str:
        if self.age_s is None or self.age_s >= self.red_s:
            return "red"
        return "yellow" if self.age_s >= self.yellow_s else "green"


def _latest(conn: sqlite3.Connection, sql: str, *args) -> float | None:
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        return None


def data_sources(controller: "Controller") -> list[Source]:
    now = time.time()
    conn = controller.store.conn
    notify = controller.config.notify

    def age(ts: float | None) -> float | None:
        return None if ts is None else now - ts

    polls = controller.board_polls
    grid_ts = controller.grid_ts if controller.grid_power is not None else None
    sources = [
        Source(f"{board.title()} board", "ESP32 relay board polling",
               age(polls.get(board, {}).get("ts")), 60, notify.board_offline_after_s)
        for board in ("upstairs", "downstairs")
    ]
    sources += [
        Source("Grid meter", "MBMD via MQTT, live", age(grid_ts), 30, controller.config.solar.stale_after_s),
        Source("Inverter", "MBMD via MQTT, live", age(controller.pv_ts), 60, 600),
        Source("Power history", "dorfsteig-ingest@power, minutes",
               age(_latest(conn, "SELECT MAX(ts) + 60 FROM grid_readings")), 180, 600),
        Source("IoTaWatt", "dorfsteig-ingest@iotawatt, every 5 min",
               age(_latest(conn, "SELECT MAX(ts) + 60 FROM iotawatt_readings")), 12 * 60, 30 * 60),
        Source("Energy tables", "dorfsteig-ingest@energy, every 5 min",
               age(_latest(conn, "SELECT MAX(ts) + 60 FROM house_minutes")), 15 * 60, 45 * 60),
        Source("MeteoSwiss station", "dorfsteig-ingest@weather, hourly",
               age(_latest(conn, "SELECT MAX(ts) + 3600 FROM weather WHERE source = 'meteoswiss'")),
               3 * 3600, 6 * 3600),
        Source("Weather forecast", "dorfsteig-ingest@weather, hourly",
               age(_latest(conn, "SELECT MAX(issued) FROM weather_forecast")), 2 * 3600, 6 * 3600),
    ]
    sources += [
        Source(f"Sensor {room}", "air quality / temperature",
               age(_latest(conn, "SELECT MAX(ts) FROM air_quality WHERE room = ?", room)),
               10 * 60, notify.sensor_offline_after_s)
        for room in notify.watched_sensors
    ]
    return sources
