"""Controller tables in the shared SQLite database.

- control_config: every saved config version; the latest row is current
- heater_state:   current state per heater, so restarts keep timing guards
- control_events: every switch, with the reason and grid power at the time
- heater_power:   wattage measurements (see wattage.py)
- water_tank:     when each water heater was last seen full (see water.py)
- night_plan:     the night plan made each evening (see night.py)
"""

import json
import sqlite3
import time
from datetime import date

from dorfsteig_control import wattage
from dorfsteig_control.config import ControlConfig, default_config
from dorfsteig_control.engine import Event, HeaterState, States, initial_states
from dorfsteig_control.night import NightPlan

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
CREATE TABLE IF NOT EXISTS water_tank (
    heater     TEXT PRIMARY KEY,
    last_full  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS night_plan (
    night        TEXT PRIMARY KEY,  -- ISO date of the evening charging starts
    made_at      REAL NOT NULL,
    mode         TEXT NOT NULL,     -- "shadow" or "on"
    temperature  REAL,              -- tomorrow's forecast mean, °C
    need_kwh     REAL NOT NULL,
    surplus_kwh  REAL,              -- tomorrow's forecast PV surplus
    grid_kwh     REAL NOT NULL,
    heaters      TEXT NOT NULL,     -- comma-separated heater ids
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    note         TEXT NOT NULL,
    ends         TEXT               -- JSON, heater → when it stops; NULL = all at `end`
);
"""


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)
        self._migrate()
        wattage.init_tables(conn)

    def _migrate(self) -> None:
        """night_plan got a stop time per heater (2026-10-13)."""
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(night_plan)")]
        if "ends" not in columns:
            self.conn.execute("ALTER TABLE night_plan ADD COLUMN ends TEXT")
            self.conn.commit()

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

    # ---- water tanks

    def load_water_full(self) -> dict[str, float]:
        return dict(self.conn.execute("SELECT heater, last_full FROM water_tank"))

    def save_water_full(self, heater: str, ts: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO water_tank (heater, last_full) VALUES (?, ?)", (heater, ts))
        self.conn.commit()

    # ---- night plan

    def save_night_plan(self, plan: NightPlan) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO night_plan (night, made_at, mode, temperature, need_kwh, surplus_kwh, "
            "grid_kwh, heaters, start, end, note, ends) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan.night.isoformat(), plan.made_at, plan.mode, plan.temperature, plan.need_kwh,
             plan.surplus_kwh, plan.grid_kwh, ",".join(plan.heaters), plan.start, plan.end, plan.note,
             json.dumps(plan.ends)),
        )
        self.conn.commit()

    def load_night_plan(self, night: date | None = None) -> NightPlan | None:
        """The plan for `night`, or the latest one if None."""
        query = ("SELECT night, made_at, mode, temperature, need_kwh, surplus_kwh, grid_kwh, heaters, "
                 "start, end, note, ends FROM night_plan")
        if night is None:
            row = self.conn.execute(query + " ORDER BY night DESC LIMIT 1").fetchone()
        else:
            row = self.conn.execute(query + " WHERE night = ?", (night.isoformat(),)).fetchone()
        if row is None:
            return None
        night_text, made_at, mode, temperature, need, surplus, grid, heaters, start, end, note, ends = row
        return NightPlan(date.fromisoformat(night_text), made_at, mode, temperature, need, surplus, grid,
                         heaters.split(",") if heaters else [], start, end, note,
                         json.loads(ends) if ends else {})

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
