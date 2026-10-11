"""Daily numbers for the Pushover summary and the status page.

Heater on-time comes from control_events; PV and grid energy come from the
one-minute tables written by `dorfsteig-ingest power` (if it's running).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time

from dorfsteig_control.engine import States
from dorfsteig_control.heaters import BY_ID
from dorfsteig_control.wattage import estimated_watts


@dataclass
class HeaterDay:
    solar_s: float = 0.0
    scheduled_s: float = 0.0
    other_s: float = 0.0
    watts: float = 0.0

    @property
    def hours(self) -> float:
        return (self.solar_s + self.scheduled_s + self.other_s) / 3600

    @property
    def kwh(self) -> float:
        return self.hours * self.watts / 1000

    @property
    def solar_kwh(self) -> float:
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


def _bucket(reason: str) -> str:
    if reason == "export":
        return "solar"
    if reason == "schedule":
        return "scheduled"
    return "other"


def heater_intervals(conn: sqlite3.Connection, start: float, end: float, dry_run: bool) -> dict[str, dict]:
    """Seconds each heater was on between start and end, split by why it came on."""
    totals: dict[str, dict[str, float]] = {}
    heaters = [r[0] for r in conn.execute(
        "SELECT DISTINCT heater FROM control_events WHERE dry_run = ? AND ts < ?", (int(dry_run), end)
    )]
    for heater in heaters:
        buckets = {"solar": 0.0, "scheduled": 0.0, "other": 0.0}
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
            elif not is_on and on_since is not None:
                buckets[bucket] += ts - on_since
                on_since = None
        if on_since is not None:
            buckets[bucket] += end - on_since
        totals[heater] = buckets
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
    for heater, buckets in heater_intervals(conn, start, end, dry_run).items():
        if heater not in BY_ID or sum(buckets.values()) == 0:
            continue
        summary.heaters[heater] = HeaterDay(
            solar_s=buckets["solar"], scheduled_s=buckets["scheduled"], other_s=buckets["other"],
            watts=estimated_watts(conn, heater)[0],
        )
    summary.pv_kwh, summary.import_kwh, summary.export_kwh = energy(conn, start, end)
    return summary


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
