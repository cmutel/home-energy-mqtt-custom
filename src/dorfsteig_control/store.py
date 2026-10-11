"""Controller tables in the shared SQLite database.

- control_config: every saved config version; the latest row is current
- heater_state:   current state per heater, so restarts keep timing guards
- control_events: every switch, with the reason and grid power at the time
- heater_power:   wattage measurements (see wattage.py)
"""

import sqlite3
import time

from dorfsteig_control import wattage
from dorfsteig_control.config import ControlConfig, default_config
from dorfsteig_control.engine import Event, HeaterState, States, initial_states

SCHEMA = """
CREATE TABLE IF NOT EXISTS control_config (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at   INTEGER NOT NULL,
    source     TEXT NOT NULL,      -- e.g. "legacy import", "web"
    config     TEXT NOT NULL       -- ControlConfig as JSON
);
CREATE TABLE IF NOT EXISTS heater_state (
    heater     TEXT PRIMARY KEY,
    is_on      INTEGER NOT NULL,
    since      REAL NOT NULL,
    reason     TEXT
);
CREATE TABLE IF NOT EXISTS control_events (
    ts         REAL NOT NULL,
    heater     TEXT NOT NULL,
    is_on      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    grid_power REAL,               -- W, positive = export
    dry_run    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS control_events_ts ON control_events (ts);
"""


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)
        wattage.init_tables(conn)

    # ---- config

    def load_config(self) -> ControlConfig:
        row = self.conn.execute("SELECT config FROM control_config ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return default_config()
        return ControlConfig.model_validate_json(row[0])

    def save_config(self, config: ControlConfig, source: str) -> None:
        self.conn.execute(
            "INSERT INTO control_config (saved_at, source, config) VALUES (?, ?, ?)",
            (int(time.time()), source, config.model_dump_json()),
        )
        self.conn.commit()

    # ---- state

    def load_states(self) -> States:
        states = initial_states()
        for heater, is_on, since, reason in self.conn.execute(
            "SELECT heater, is_on, since, reason FROM heater_state"
        ):
            if heater in states:  # ignore heaters that no longer exist
                states[heater] = HeaterState(on=bool(is_on), since=since, reason=reason)
        return states

    def save_states(self, states: States) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO heater_state (heater, is_on, since, reason) VALUES (?, ?, ?, ?)",
            [(h, int(s.on), s.since, s.reason) for h, s in states.items()],
        )
        self.conn.commit()

    # ---- events

    def log_events(self, events: list[Event], dry_run: bool = False) -> None:
        self.conn.executemany(
            "INSERT INTO control_events (ts, heater, is_on, reason, grid_power, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(e.ts, e.heater, int(e.on), e.reason, e.grid_power, int(dry_run)) for e in events],
        )
        self.conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, heater, is_on, reason, grid_power, dry_run FROM control_events "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ("ts", "heater", "on", "reason", "grid_power", "dry_run")
        return [dict(zip(keys, row)) for row in rows]
