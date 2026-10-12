"""Measure how much power each heater circuit draws.

Active test: hold PV switching, measure grid power for a baseline, toggle one
heater, wait for the board to pick it up, measure again, restore. The
difference is the heater's power. It takes about three minutes.

Passive estimate: every normal switch is a free measurement, as long as no
other heater switched shortly before or after. These track slow changes, e.g.
when radiators on a circuit are turned off by hand, and trigger a drift alert
when they disagree with the last active test.

A test that sees no load (thermostat satisfied, radiators turned off by hand,
or a water heater with a hot tank) is stored as method "no load" and never
used as an estimate: it says nothing about the heater's power.

Grid power is positive when exporting, so switching a heater on makes it fall.
"""

import asyncio
import logging
import sqlite3
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dorfsteig_control.heaters import BY_ID

if TYPE_CHECKING:
    from dorfsteig_control.service import Controller

log = logging.getLogger(__name__)

DEFAULT_WATTS = 2000.0

# Active test timing (seconds)
SETTLE_S = 30      # let earlier switching settle before the baseline
BASELINE_S = 45
SWITCH_WAIT_S = 45  # boards poll every 15 s; allow for that plus the relay
MEASURE_S = 45

# Passive estimate windows around a switching event (seconds)
PASSIVE_BEFORE = (-35, -5)
PASSIVE_AFTER = (30, 75)
PASSIVE_MIN_SAMPLES = 5
NO_LOAD_W = 100    # below this a toggle shows nothing measurable

SCHEMA = """
CREATE TABLE IF NOT EXISTS heater_power (
    ts           REAL NOT NULL,      -- when the heater was toggled
    heater       TEXT NOT NULL,
    method       TEXT NOT NULL,      -- "active", "passive" or "no load"
    watts        REAL NOT NULL,
    spread       REAL,               -- W, noise in the grid readings
    window_end   REAL,               -- end of the measurement, for matching IoTaWatt zones
    note         TEXT
);
CREATE INDEX IF NOT EXISTS heater_power_heater ON heater_power (heater, ts);
"""

# IoTaWatt channels that can belong to a heater circuit
ZONE_COLUMNS = ["zone_a", "zone_b", "zone_c", "zone_d", "zone_e", "zone_f", "water_heater"]


class GridHistory:
    """Recent grid power readings (MBMD publishes about every 2 s)."""

    def __init__(self, maxlen: int = 2000) -> None:
        self.samples: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def add(self, ts: float, power: float) -> None:
        self.samples.append((ts, power))

    def window(self, start: float, end: float) -> list[float]:
        return [p for ts, p in self.samples if start <= ts < end]


@dataclass
class Measurement:
    median: float
    spread: float   # median absolute deviation, W
    n: int


def measure(values: list[float]) -> Measurement | None:
    if not values:
        return None
    med = statistics.median(values)
    return Measurement(med, statistics.median(abs(v - med) for v in values), len(values))


# ---- storage

def init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def record(conn: sqlite3.Connection, ts: float, heater: str, method: str, watts: float,
           spread: float | None, window_end: float | None, note: str = "") -> None:
    conn.execute(
        "INSERT INTO heater_power (ts, heater, method, watts, spread, window_end, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, heater, method, watts, spread, window_end, note),
    )
    conn.commit()


def latest_active(conn: sqlite3.Connection, heater: str) -> float | None:
    row = conn.execute(
        "SELECT watts FROM heater_power WHERE heater = ? AND method = 'active' ORDER BY ts DESC LIMIT 1",
        (heater,),
    ).fetchone()
    return row[0] if row else None


def recent_passive(conn: sqlite3.Connection, heater: str, n: int = 5) -> list[float]:
    rows = conn.execute(
        "SELECT watts FROM heater_power WHERE heater = ? AND method = 'passive' ORDER BY ts DESC LIMIT ?",
        (heater, n),
    ).fetchall()
    return [r[0] for r in rows]


def estimated_watts(conn: sqlite3.Connection, heater: str) -> tuple[float, str]:
    """Best current estimate and where it came from.

    Recent passive estimates win over an older active test once there are
    enough of them, because they follow changes to the circuit.
    """
    passive = recent_passive(conn, heater)
    if len(passive) >= 3:
        return statistics.median(passive), "passive"
    active = latest_active(conn, heater)
    if active is not None:
        return active, "test"
    return DEFAULT_WATTS, "default"


def no_load(conn: sqlite3.Connection, heater: str) -> bool:
    """Whether the latest measurement of a heater found no load (e.g. switched off by hand)."""
    row = conn.execute("SELECT method FROM heater_power WHERE heater = ? ORDER BY ts DESC LIMIT 1",
                       (heater,)).fetchone()
    return row is not None and row[0] == "no load"


def measurements(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, heater, method, watts, spread, window_end, note FROM heater_power "
        "ORDER BY ts DESC LIMIT ?", (limit,),
    ).fetchall()
    keys = ("ts", "heater", "method", "watts", "spread", "window_end", "note")
    return [dict(zip(keys, r)) for r in rows]


def zone_match(conn: sqlite3.Connection, toggled_at: float, window_end: float) -> tuple[str, float] | None:
    """Which IoTaWatt channel changed most when the heater was toggled.

    IoTaWatt rows are one-minute averages and arrive every 5 minutes, so this
    only works some minutes after the test. Returns (channel, change in W).
    """
    cols = ", ".join(f"AVG({c})" for c in ZONE_COLUMNS)
    query = f"SELECT {cols}, COUNT(*) FROM iotawatt_readings WHERE ts >= ? AND ts < ?"
    try:
        before = conn.execute(query, (toggled_at - 180, toggled_at - 60)).fetchone()
        after = conn.execute(query, (toggled_at + 60, window_end)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not before or not after or not before[-1] or not after[-1]:
        return None
    deltas = {c: (a or 0) - (b or 0) for c, b, a in zip(ZONE_COLUMNS, before[:-1], after[:-1])}
    channel = max(deltas, key=lambda c: abs(deltas[c]))
    return channel, deltas[channel]


# ---- active test

@dataclass
class TestStatus:
    heater: str
    phase: str
    started: float = field(default_factory=time.time)
    result: float | None = None
    note: str = ""
    done: bool = False


async def run_test(controller: "Controller", heater: str) -> TestStatus:
    status = TestStatus(heater=heater, phase="settling")
    controller.test = status
    controller.hold_solar = True
    history = controller.history
    try:
        await asyncio.sleep(SETTLE_S)

        status.phase = "baseline"
        start = time.time()
        await asyncio.sleep(BASELINE_S)
        baseline = measure(history.window(start, time.time()))

        was_on = controller.states[heater].on
        status.phase = "switching off" if was_on else "switching on"
        controller.overrides[heater] = not was_on
        controller.tick()
        toggled_at = time.time()
        await asyncio.sleep(SWITCH_WAIT_S)

        status.phase = "measuring"
        start = time.time()
        await asyncio.sleep(MEASURE_S)
        end = time.time()
        during = measure(history.window(start, end))

        if baseline is None or during is None or min(baseline.n, during.n) < PASSIVE_MIN_SAMPLES:
            status.note = "Not enough grid readings; is MBMD publishing?"
            return status

        # Heater on → more consumption → grid power (export positive) falls
        watts = (baseline.median - during.median) if not was_on else (during.median - baseline.median)
        spread = max(baseline.spread, during.spread)
        method = "active"
        if watts < NO_LOAD_W:
            method = "no load"
            if BY_ID[heater].water:
                status.note = ("No load: the tank is probably already hot. "
                               "Repeat after hot water has been used.")
            else:
                status.note = "No load: thermostat satisfied, or radiators on this circuit turned off?"
        elif spread > 0.25 * watts:
            status.note = "Noisy readings (other appliances?); consider repeating"
        status.result = watts if method == "active" else None
        record(controller.store.conn, toggled_at, heater, method, watts, spread, end, status.note)
        log.info("Wattage test %s: %.0f W (spread %.0f W) %s", heater, watts, spread, status.note)
        return status
    finally:
        controller.overrides.pop(heater, None)
        controller.hold_solar = False
        controller.tick()
        status.phase = "done"
        status.done = True


async def run_tests(controller: "Controller", heaters: list[str]) -> None:
    for heater in heaters:
        await run_test(controller, heater)


# ---- passive estimates

@dataclass
class PendingSwitch:
    ts: float
    heater: str
    on: bool


def evaluate_passive(
    pending: PendingSwitch, history: GridHistory, other_switches: list[float]
) -> tuple[float, float] | None:
    """(watts, spread) for one switching event, or None if it can't be isolated."""
    lo, hi = pending.ts + PASSIVE_BEFORE[0], pending.ts + PASSIVE_AFTER[1]
    if any(lo <= ts <= hi for ts in other_switches):
        return None
    before = measure(history.window(pending.ts + PASSIVE_BEFORE[0], pending.ts + PASSIVE_BEFORE[1]))
    after = measure(history.window(pending.ts + PASSIVE_AFTER[0], pending.ts + PASSIVE_AFTER[1]))
    if before is None or after is None or min(before.n, after.n) < PASSIVE_MIN_SAMPLES:
        return None
    watts = (before.median - after.median) if pending.on else (after.median - before.median)
    spread = max(before.spread, after.spread)
    if watts < NO_LOAD_W or spread > 0.25 * watts:
        return None
    return watts, spread


def drift(conn: sqlite3.Connection, heater: str) -> tuple[float, float] | None:
    """(tested, recent) if recent passive estimates disagree with the last active test."""
    tested = latest_active(conn, heater)
    passive = recent_passive(conn, heater)
    if tested is None or len(passive) < 3:
        return None
    recent = statistics.median(passive)
    if abs(recent - tested) > max(300, 0.25 * tested):
        return tested, recent
    return None
