"""Listen to MBMD power topics and write one-minute averages to SQLite.

Accumulates InverterReading and GridReading snapshots as they arrive
(~every 5 seconds from MBMD), then at the top of each minute writes
averaged values to the database.

Cumulative fields (export_total, import_total) store the last value
seen in the minute rather than an average.
"""

import logging
import sqlite3
import time

from dorfsteig_core.db import init_db
from dorfsteig_core.domain.power import GridReading, InverterReading
from dorfsteig_core.mqtt import connect_with_retry, new_client
from dorfsteig_core.power_consumer import PowerConsumer
from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

INVERTER_INSERT = """
    INSERT OR REPLACE INTO inverter_readings
        (ts, power_dc, voltage_dc, current_dc, power_ac, export_total)
    VALUES (?, ?, ?, ?, ?, ?)
"""

GRID_INSERT = """
    INSERT OR REPLACE INTO grid_readings
        (ts, power, import_total, export_total)
    VALUES (?, ?, ?, ?)
"""


class MinuteAverager:
    """Buffers domain object snapshots and flushes one-minute averages to DB."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._inverter: list[InverterReading] = []
        self._grid: list[GridReading] = []
        self._current_minute: int = self._minute_bucket(time.time())

    @staticmethod
    def _minute_bucket(ts: float) -> int:
        """Round a Unix timestamp down to the nearest minute."""
        return int(ts) // 60 * 60

    def on_inverter(self, reading: InverterReading) -> None:
        self._roll_minute(reading.timestamp)
        self._inverter.append(reading)

    def on_grid(self, reading: GridReading) -> None:
        self._roll_minute(reading.timestamp)
        self._grid.append(reading)

    def _roll_minute(self, ts: float) -> None:
        bucket = self._minute_bucket(ts)
        if bucket != self._current_minute:
            self.flush()
            self._current_minute = bucket

    def flush(self) -> None:
        ts = self._current_minute

        if self._inverter:
            n = len(self._inverter)
            row = (
                ts,
                sum(r.power_dc    for r in self._inverter) / n,
                sum(r.voltage_dc  for r in self._inverter) / n,
                sum(r.current_dc  for r in self._inverter) / n,
                sum(r.power_ac    for r in self._inverter) / n,
                self._inverter[-1].export_total,  # last value, not average
            )
            self.conn.execute(INVERTER_INSERT, row)
            log.debug("inverter %s power_ac=%.0fW (n=%d)", ts, row[4], n)

        if self._grid:
            n = len(self._grid)
            row = (
                ts,
                sum(r.power        for r in self._grid) / n,
                self._grid[-1].import_total,
                self._grid[-1].export_total,
            )
            self.conn.execute(GRID_INSERT, row)
            log.debug("grid     %s power=%.0fW (n=%d)", ts, row[1], n)

        self.conn.commit()
        self._inverter = []
        self._grid = []


def run() -> None:
    averager = MinuteAverager(init_db())
    consumer = PowerConsumer(
        on_inverter=averager.on_inverter,
        on_grid=averager.on_grid,
    )

    client = new_client()
    client.on_connect = consumer.on_connect
    client.on_message = consumer.on_message
    log.info("Writing to %s", settings.db_path)
    connect_with_retry(client)
    client.loop_forever()
