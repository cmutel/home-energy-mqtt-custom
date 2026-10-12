"""Daily numbers for the Pushover summary and the status page.

Heater on-time comes from control_events; PV and grid energy come from the
one-minute tables written by `dorfsteig-ingest power` (if it's running).

Energy per heater: a closed relay doesn't mean the heater draws power.
- Water heaters (thermostat): the measured draw while the relay was on,
  IoTaWatt downstairs and the estimate in house_minutes upstairs (see
  water.DRAW_SOURCES).
- Storage heaters: no per-heater measurement (they're in IoTaWatt mains or
  on the upstairs panel), so on-time × measured wattage. That's an upper
  bound, as a heater stops when its core is full; heaters whose latest test
  found no load (switched off by hand) count as 0.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time

from dorfsteig_control.engine import States
from dorfsteig_control.heaters import BY_ID
from dorfsteig_control.water import DRAW_SOURCES
from dorfsteig_control.wattage import estimated_watts, no_load


@dataclass
class HeaterDay:
    solar_s: float = 0.0
    scheduled_s: float = 0.0
    other_s: float = 0.0
    watts: float = 0.0
    measured: dict[str, float] | None = None   # bucket → kWh, for heaters with a measured draw

    @property
    def hours(self) -> float:
        return (self.solar_s + self.scheduled_s + self.other_s) / 3600

    @property
    def kwh(self) -> float:
        if self.measured is not None:
            return sum(self.measured.values())
        return self.hours * self.watts / 1000

    @property
    def solar_kwh(self) -> float:
        if self.measured is not None:
            return self.measured.get("solar", 0.0)
        return self.solar_s / 3600 * self.watts / 1000


@dataclass
class DaySummary:
    start: float
    end: float
    heaters: dict[str, HeaterDay] = field(default_factory=dict)
    pv_kwh: float | None = None
    import_kwh: float | None = None
    export_kwh: float | None = None

    @property
    def heating_kwh(self) -> float:
        return sum(h.kwh for h in self.heaters.values())

    @property
    def solar_heating_kwh(self) -> float:
        return sum(h.solar_kwh for h in self.heaters.values())

    def _part(self, water: bool) -> list[HeaterDay]:
        return [d for h, d in self.heaters.items() if BY_ID[h].water == water]

    @property
    def water_kwh(self) -> float:
        return sum(d.kwh for d in self._part(True))

    @property
    def water_solar_kwh(self) -> float:
        return sum(d.solar_kwh for d in self._part(True))

    @property
    def space_kwh(self) -> float:
        return sum(d.kwh for d in self._part(False))

    @property
    def space_solar_kwh(self) -> float:
        return sum(d.solar_kwh for d in self._part(False))


def _bucket(reason: str) -> str:
    if reason in ("export", "water pv"):
        return "solar"
    if reason in ("schedule", "water backup", "water safety", "night plan"):
        return "scheduled"
    return "other"


def heater_intervals(conn: sqlite3.Connection, start: float, end: float, dry_run: bool) -> dict[str, dict]:
    """Seconds each heater was on between start and end, split by why it came on."""
    return {heater: {bucket: sum(e - s for s, e, b in spans if b == bucket)
                     for bucket in ("solar", "scheduled", "other")}
            for heater, spans in on_spans(conn, start, end, dry_run).items()}


def on_spans(conn: sqlite3.Connection, start: float, end: float,
             dry_run: bool) -> dict[str, list[tuple[float, float, str]]]:
    """(start, end, bucket) for each period each heater was on between start and end."""
    result: dict[str, list[tuple[float, float, str]]] = {}
    heaters = [r[0] for r in conn.execute(
        "SELECT DISTINCT heater FROM control_events WHERE dry_run = ? AND ts < ?", (int(dry_run), end)
    )]
    for heater in heaters:
        spans: list[tuple[float, float, str]] = []
        prior = conn.execute(
            "SELECT is_on, reason FROM control_events WHERE heater = ? AND dry_run = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT 1", (heater, int(dry_run), start),
        ).fetchone()
        on_since, bucket = (start, _bucket(prior[1])) if prior and prior[0] else (None, "other")
        for ts, is_on, reason in conn.execute(
            "SELECT ts, is_on, reason FROM control_events WHERE heater = ? AND dry_run = ? "
            "AND ts >= ? AND ts < ? ORDER BY ts", (heater, int(dry_run), start, end),
        ):
            if is_on and on_since is None:
                on_since, bucket = ts, _bucket(reason)
            elif is_on and on_since is not None and _bucket(reason) != bucket:
                spans.append((on_since, ts, bucket))           # still on, for a new reason
                on_since, bucket = ts, _bucket(reason)
            elif not is_on and on_since is not None:
                spans.append((on_since, ts, bucket))
                on_since = None
        if on_since is not None:
            spans.append((on_since, end, bucket))
        result[heater] = spans
    return result


def measured_kwh(conn: sqlite3.Connection, heater: str,
                 spans: list[tuple[float, float, str]]) -> dict[str, float]:
    """Measured draw of a water heater within its on-spans, kWh per bucket."""
    table, column = DRAW_SOURCES[heater]
    totals = {"solar": 0.0, "scheduled": 0.0, "other": 0.0}
    for start, end, bucket in spans:
        try:
            wh = conn.execute(f"SELECT SUM({column}) / 60.0 FROM {table} WHERE ts >= ? AND ts < ?",
                              (start, end)).fetchone()[0]
        except sqlite3.OperationalError:
            wh = None
        totals[bucket] += (wh or 0) / 1000
    return totals


def energy(conn: sqlite3.Connection, start: float, end: float) -> tuple[float | None, ...]:
    """(PV kWh, grid import kWh, grid export kWh), None where there's no data."""
    try:
        pv = conn.execute(
            "SELECT SUM(power_ac) / 60.0 / 1000, COUNT(*) FROM inverter_readings WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        grid = conn.execute(
            "SELECT MAX(import_total) - MIN(import_total), MAX(export_total) - MIN(export_total), COUNT(*) "
            "FROM grid_readings WHERE ts >= ? AND ts < ?", (start, end),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None, None
    return (pv[0] if pv[1] else None,
            grid[0] if grid[2] else None,
            grid[1] if grid[2] else None)


def day_summary(conn: sqlite3.Connection, now: datetime, dry_run: bool = False) -> DaySummary:
    start = datetime.combine(now.date(), time()).timestamp()
    end = now.timestamp()
    summary = DaySummary(start=start, end=end)
    for heater, spans in on_spans(conn, start, end, dry_run).items():
        if heater not in BY_ID or not spans:
            continue
        seconds = {b: sum(e - s for s, e, bb in spans if bb == b) for b in ("solar", "scheduled", "other")}
        day = HeaterDay(solar_s=seconds["solar"], scheduled_s=seconds["scheduled"], other_s=seconds["other"],
                        watts=0.0 if no_load(conn, heater) else estimated_watts(conn, heater)[0])
        if heater in DRAW_SOURCES:
            day.measured = measured_kwh(conn, heater, spans)
        summary.heaters[heater] = day
    summary.pv_kwh, summary.import_kwh, summary.export_kwh = energy(conn, start, end)
    return summary


def heating_power(conn: sqlite3.Connection, start: float, end: float,
                  dry_run: bool = False) -> tuple[dict[int, float], dict[int, float]]:
    """Per-minute heating power, W: (space, water). Same sources as the daily energy.

    Water minutes are only there once IoTaWatt / house_minutes have them (a
    few minutes late); space minutes are on-time × wattage up to now.
    """
    first, last = int(start) // 60 * 60, int(end) // 60 * 60
    space = dict.fromkeys(range(first, last, 60), 0.0)
    water: dict[int, float] = {}
    for heater, spans in on_spans(conn, start, end, dry_run).items():
        if heater not in BY_ID:
            continue
        if heater in DRAW_SOURCES:
            table, column = DRAW_SOURCES[heater]
            for s, e, _ in spans:
                try:
                    rows = conn.execute(f"SELECT ts, {column} FROM {table} WHERE ts >= ? AND ts < ? "
                                        f"AND {column} IS NOT NULL", (s, e)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for ts, w in rows:
                    water[ts] = water.get(ts, 0.0) + w
        elif not no_load(conn, heater):
            watts = estimated_watts(conn, heater)[0]
            for s, e, _ in spans:
                for ts in range(int(s) // 60 * 60, min(int(e), last), 60):
                    if ts in space:
                        space[ts] += watts
    return space, water


def format_summary(s: DaySummary) -> str:
    lines = []
    if s.pv_kwh is not None:
        lines.append(f"PV {s.pv_kwh:.1f} kWh · exported {s.export_kwh or 0:.1f} · "
                     f"imported {s.import_kwh or 0:.1f} kWh")
    else:
        lines.append("No PV/grid data (is dorfsteig-ingest@power running?)")
    lines.append(f"Heating ≈{s.heating_kwh:.1f} kWh, of which ≈{s.solar_heating_kwh:.1f} kWh on PV surplus")
    for heater, day in sorted(s.heaters.items(), key=lambda kv: -kv[1].hours):
        lines.append(f"• {BY_ID[heater].name}: {day.hours:.1f} h (≈{day.kwh:.1f} kWh)")
    return "\n".join(lines)


def format_status(states: States, grid_power: float | None, s: DaySummary) -> str:
    on = [BY_ID[h].name for h, st in states.items() if st.on]
    grid = "unknown" if grid_power is None else (
        f"exporting {grid_power:.0f} W" if grid_power >= 0 else f"importing {-grid_power:.0f} W")
    return f"Grid {grid}. On: {', '.join(on) or 'nothing'}.\nSo far today:\n{format_summary(s)}"
