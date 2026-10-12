"""Morning report: yesterday's generation, usage and costs per circuit.

Reads the hourly table written by `dorfsteig-ingest energy` and the IoTaWatt
channels. All usage is costed at the hour's import price (high or low
tariff), whether the power came from the grid or from PV, so circuit costs
add up to the cost of the whole house's consumption. PV export revenue is
reported separately.

Forecasts (if `dorfsteig-forecast` runs): the PV forecast for the day as it
stood when the day began, today's forecast, and the night plan made that
evening (night.py).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dorfsteig_control import forecasts
from dorfsteig_control.summary import DaySummary, day_summary

# IoTaWatt channels, all on the downstairs panel
CIRCUITS = {
    "zone_a": "Zone A (kitchen, bath, dining)",
    "zone_b": "Zone B (hall, living, entrance)",
    "zone_c": "Zone C (bath up, parents, playroom S)",
    "zone_d": "Zone D (upstairs hallway)",
    "zone_e": "Zone E (kids, playroom N, balcony)",
    "zone_f": "Zone F",
    "water_heater": "Water heater downstairs",
    "oven": "Oven",
    "stovetop": "Stovetop",
    "dishwasher": "Dishwasher",
    "clothes_washer": "Washer",
    "clothes_dryer": "Dryer",
}
DOWNSTAIRS_OTHER = "Downstairs other"
UPSTAIRS_WATER = "Water heater upstairs (est.)"
UPSTAIRS_OTHER = "Upstairs other"


@dataclass
class Report:
    day: date
    hours: int = 0                  # hours with grid data
    pv_kwh: float = 0.0
    export_kwh: float = 0.0
    import_kwh: float = 0.0
    house_kwh: float = 0.0
    usage_chf: float | None = None   # all consumption at the import price
    import_chf: float | None = None  # what was actually bought from the grid
    export_chf: float | None = None  # PV export revenue
    circuits: dict[str, list[float]] = field(default_factory=dict)  # name → [kWh, CHF]
    heating: DaySummary | None = None    # relay-controlled heaters (water measured, space at most)
    pv_forecast_kwh: float | None = None  # the day's PV as forecast when it began
    today: forecasts.DayForecast | None = None   # forecast for the day after (the report's "today")
    night_plan: tuple[str, float, str] | None = None   # (mode, grid kWh, note) for the evening of the day


def _add(total: float | None, value: float | None) -> float | None:
    if value is None:
        return total
    return (total or 0.0) + value


def daily_report(conn: sqlite3.Connection, day: date) -> Report:
    start = int(datetime.combine(day, time()).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), time()).timestamp())
    report = Report(day=day)
    try:
        hourly = conn.execute(
            "SELECT ts, pv_wh, export_wh, import_wh, house_wh, downstairs_wh, upstairs_water_wh, "
            "upstairs_other_wh, import_price, import_chf, export_chf "
            "FROM energy_hourly WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()
        cols = ", ".join(f"AVG({c})" for c in CIRCUITS)
        channels = {
            row[0]: row[1:] for row in conn.execute(
                f"SELECT ts / 3600 * 3600 AS hour, {cols} FROM iotawatt_readings "
                "WHERE ts >= ? AND ts < ? AND mains_a IS NOT NULL GROUP BY hour", (start, end),
            )
        }
    except sqlite3.OperationalError:
        return report
    # Up to the end of the day, or now if the day isn't over (heaters still on don't count ahead)
    now = datetime.now()
    report.heating = day_summary(conn, min(datetime.combine(day, time.max), now))

    day_start = datetime.combine(day, time())
    then = forecasts.day_forecast(conn, day, day_start)
    report.pv_forecast_kwh = None if then is None else then.pv_kwh
    report.today = forecasts.day_forecast(conn, day + timedelta(days=1), now)
    try:
        report.night_plan = conn.execute("SELECT mode, grid_kwh, note FROM night_plan WHERE night = ?",
                                         (day.isoformat(),)).fetchone()
    except sqlite3.OperationalError:
        pass   # the controller hasn't created its tables (tests)

    for (ts, pv, exp, imp, house, down, up_water, up_other, price, imp_chf, exp_chf) in hourly:
        if imp is not None:
            report.hours += 1
        report.pv_kwh += (pv or 0) / 1000
        report.export_kwh += (exp or 0) / 1000
        report.import_kwh += (imp or 0) / 1000
        report.house_kwh += (house or 0) / 1000
        report.import_chf = _add(report.import_chf, imp_chf)
        report.export_chf = _add(report.export_chf, exp_chf)
        if price is not None and house is not None:
            report.usage_chf = _add(report.usage_chf, house / 1000 * price)

        hour: dict[str, float] = {}
        measured = channels.get(ts)
        if measured is not None:
            channels_wh = {c: wh or 0 for c, wh in zip(CIRCUITS, measured)}
            hour.update({CIRCUITS[c]: wh for c, wh in channels_wh.items()})
            if down is not None:
                hour[DOWNSTAIRS_OTHER] = down - sum(channels_wh.values())
        if up_water is not None and up_other is not None:
            hour[UPSTAIRS_WATER] = up_water
            hour[UPSTAIRS_OTHER] = up_other
        for name, wh in hour.items():
            kwh_chf = report.circuits.setdefault(name, [0.0, 0.0])
            kwh_chf[0] += wh / 1000
            if price is not None:
                kwh_chf[1] += wh / 1000 * price
    return report


def format_report(r: Report) -> str:
    def chf(v: float | None) -> str:
        return "–" if v is None else f"{v:.2f}"

    if r.hours == 0:
        return "No energy data for this day (is dorfsteig-ingest@energy running?)"
    forecast = "" if r.pv_forecast_kwh is None else f" (forecast {r.pv_forecast_kwh:.0f})"
    lines = [
        f"PV generated {r.pv_kwh:.1f} kWh{forecast}, exported {r.export_kwh:.1f} kWh: "
        f"revenue CHF {chf(r.export_chf)}",
        f"Usage {r.house_kwh:.1f} kWh: CHF {chf(r.usage_chf)} at tariff",
        f"Imported {r.import_kwh:.1f} kWh: CHF {chf(r.import_chf)}",
    ]
    if r.hours < 24:
        lines.append(f"Only {r.hours} of 24 hours have data")
    if r.heating is not None and r.heating.heaters:
        h = r.heating
        lines.append(f"Heating: water {h.water_kwh:.1f} kWh ({h.water_solar_kwh:.1f} from PV), "
                     f"space ≤{h.space_kwh:.1f} kWh ({h.space_solar_kwh:.1f} from PV)")
    if r.night_plan is not None:
        mode, grid_kwh, note = r.night_plan
        shadow = " (shadow, not switched)" if mode == "shadow" else ""
        lines.append(f"Night plan{shadow}: {grid_kwh:.1f} kWh from the grid. {note}")
    if r.today is not None:
        lines.append(f"Today's forecast: PV {r.today.pv_kwh:.0f} kWh, {r.today.temperature:.1f} °C mean")
    circuits = sorted(r.circuits.items(), key=lambda kv: -kv[1][0])
    circuits = [(name, kwh, cost) for name, (kwh, cost) in circuits if abs(kwh) >= 0.05]
    if circuits:
        lines.append("")
        lines.append("Usage by circuit, kWh · CHF")
        lines += [f"{kwh:5.1f} · {cost:4.2f}  {name}" for name, kwh, cost in circuits]
    return "\n".join(lines)
