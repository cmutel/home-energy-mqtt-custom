"""Replay night plan policies day by day, to choose how far to trust the PV forecast.

The question (night.py): how much should the storage heaters charge from the
grid overnight, given tomorrow's forecast? Too little and the house is cold
on a grey day; too much and the cores are full when the sun comes, so the
PV is exported for ~0.09 CHF/kWh instead of saving ~0.25.

Model, per day D with a forecast made the evening before:

- Heat needed: kwh_per_degree × max(0, limit - D's measured mean temperature).
  The same rule the plan uses, but with the real temperature.
- PV the heaters could have taken on D: minute by minute, the heaters that
  fit into the recorded export minus the margin (in priority order, with
  their measured watts). This ignores switching delays, so it's a little
  optimistic; and the recorded export already has the heating done by hand,
  so in that respect it's pessimistic.
- The night charge (before D) covers the need first; PV then fills what's
  left, up to what it could take. Need still left is "short" (a colder
  house); night charge beyond the need is "excess" (a warmer house, and
  money spent for nothing).
- Cost: night charge at the low-tariff import price, plus PV used for
  heating at the export price (revenue lost). Short kWh are reported, and
  also priced as if they were bought at the high-tariff import price, so
  policies that keep the house cold don't look cheap.

Policies: no night charging (PV only); the plan with pv_credit from 0
(ignore the PV forecast) to 1; and perfect foresight (night charge = need -
PV the heaters could take, both known exactly).

Only days with a stored day forecast (`dorfsteig-forecast backfill`) and
grid data are replayed: from 2025-10, as no older weather forecasts are
archived.

Results, 2025-10 to 2026-10 (176 days, 112 of them below 15 °C, 1700 kWh
of heat needed with 1.7 kWh/°C): on heating days the heaters could have
taken only 23% of the forecast PV surplus (almost nothing from November to
February), because the house uses more than the warm-day base load. So
crediting the forecast in full leaves the house short; pv_credit 0.2 matches
what arrived. Costs hardly differ: with short kWh priced at the high tariff,
pv_credit 0 costs CHF 436, 0.2 CHF 425, perfect foresight CHF 404. The
choice is mostly comfort (short kWh) against grid use.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dorfsteig_control.replay import TZ, daily_mean_temperature, load_grid_days
from dorfsteig_core.tariff import prices_for

PLAN_TIME = time(20, 0)
CREDITS = (0.0, 0.1, 0.2, 0.3, 0.5, 1.0)


@dataclass(frozen=True)
class Day:
    day: date
    temperature: float          # measured daily mean, °C
    forecast_temperature: float
    forecast_surplus_kwh: float
    usable_pv_kwh: float        # PV the heaters could have taken
    night_price: float          # CHF/kWh, low-tariff import
    day_price: float            # CHF/kWh, high-tariff import
    export_price: float         # CHF/kWh, high-tariff export


@dataclass
class NightResult:
    policy: str
    days: int = 0
    heat_days: int = 0          # days that needed heat
    need_kwh: float = 0.0
    night_kwh: float = 0.0
    pv_kwh: float = 0.0
    short_kwh: float = 0.0
    excess_kwh: float = 0.0
    cost_chf: float = 0.0       # night import + lost export
    short_chf: float = 0.0      # short kWh at the high-tariff import price
    months: dict[str, list[float]] = field(default_factory=dict)   # month → [night, pv, short] kWh


def usable_pv(minutes_g: list[float], heaters: dict[str, float], margin_w: float) -> float:
    """kWh the heaters could take: each minute, the heaters (in order) that fit the export minus margin."""
    wh = 0.0
    for g in minutes_g:
        budget = g - margin_w
        taken = 0.0
        for watts in heaters.values():
            if watts <= budget:
                taken += watts
                budget -= watts
        wh += taken / 60
    return wh / 1000


def forecast_as_of_evening(conn: sqlite3.Connection) -> dict[date, tuple[float, float]]:
    """Day → (temperature, surplus kWh) from the newest day forecast made by PLAN_TIME the evening before."""
    newest: dict[date, tuple[float, float, float]] = {}
    for issued, day_text, temperature, surplus in conn.execute(
            "SELECT issued, day, temperature, surplus_kwh FROM day_forecast"):
        day = date.fromisoformat(day_text)
        plan_made = datetime.combine(day - timedelta(days=1), PLAN_TIME, TZ).timestamp()
        if issued > plan_made:
            continue
        if day not in newest or issued > newest[day][0]:
            newest[day] = (issued, temperature, surplus)
    return {day: (temperature, surplus) for day, (_, temperature, surplus) in newest.items()}


def load_days(conn: sqlite3.Connection, start: date, end: date, heaters: dict[str, float],
              margin_w: float) -> list[Day]:
    forecasts = forecast_as_of_evening(conn)
    temperatures = daily_mean_temperature(conn)
    days = []
    for run in load_grid_days(conn, start, end):
        by_day: dict[date, list[float]] = {}
        for minute in run:
            by_day.setdefault(minute.local.date(), []).append(minute.g0)
        for day, grid in by_day.items():
            if day not in forecasts or day not in temperatures:
                continue
            prices = prices_for(day) or prices_for(date(2026, 1, 1))
            forecast_temperature, surplus = forecasts[day]
            days.append(Day(day, temperatures[day], forecast_temperature, surplus or 0.0,
                            usable_pv(grid, heaters, margin_w),
                            prices.import_price(False), prices.import_price(True), prices.export_price(True)))
    return days


def replay_night(name: str, days: list[Day], limit: float, kwh_per_degree: float, min_kwh: float,
                 credit: float | None, perfect: bool = False) -> NightResult:
    """`credit` None = no night charging at all."""
    result = NightResult(name)
    for d in days:
        need = kwh_per_degree * max(0.0, limit - d.temperature)
        if perfect:
            night = max(0.0, need - d.usable_pv_kwh)
        elif credit is None:
            night = 0.0
        else:
            forecast_need = kwh_per_degree * max(0.0, limit - d.forecast_temperature)
            night = max(0.0, forecast_need - credit * d.forecast_surplus_kwh)
        if night < min_kwh:
            night = 0.0

        # PV only charges heaters on days below the limit (fit mode's gate), into what's left of the need
        pv = min(d.usable_pv_kwh, max(0.0, need - night)) if d.temperature < limit else 0.0
        short = max(0.0, need - night - pv)
        excess = max(0.0, night - need)

        result.days += 1
        result.heat_days += need > 0
        result.need_kwh += need
        result.night_kwh += night
        result.pv_kwh += pv
        result.short_kwh += short
        result.excess_kwh += excess
        result.cost_chf += night * d.night_price + pv * d.export_price
        result.short_chf += short * d.day_price
        month = result.months.setdefault(d.day.strftime("%Y-%m"), [0.0, 0.0, 0.0])
        month[0] += night
        month[1] += pv
        month[2] += short
    return result


def run_all(days: list[Day], limit: float, kwh_per_degree: float, min_kwh: float) -> list[NightResult]:
    results = [replay_night("no night charging", days, limit, kwh_per_degree, min_kwh, credit=None)]
    for credit in CREDITS:
        results.append(replay_night(f"plan, pv_credit {credit}", days, limit, kwh_per_degree, min_kwh,
                                    credit=credit))
    results.append(replay_night("perfect foresight", days, limit, kwh_per_degree, min_kwh,
                                credit=None, perfect=True))
    return results


def night_report(results: list[NightResult], days: list[Day]) -> str:
    first = min(d.day for d in days)
    last = max(d.day for d in days)
    base = results[0]
    lines = [
        f"{base.days} days replayed ({first} to {last}), {base.heat_days} needing heat, "
        f"{base.need_kwh:.0f} kWh of heat needed.",
        "",
        f"{'policy':24s} {'night kWh':>9s} {'PV kWh':>7s} {'short kWh':>9s} {'excess kWh':>10s} "
        f"{'cost CHF':>8s} {'+ short CHF':>11s}",
    ]
    for r in results:
        lines.append(f"{r.policy:24s} {r.night_kwh:9.0f} {r.pv_kwh:7.0f} {r.short_kwh:9.0f} "
                     f"{r.excess_kwh:10.0f} {r.cost_chf:8.2f} {r.cost_chf + r.short_chf:11.2f}")
    lines += ["", "Per month, night / PV / short kWh:"]
    lines.append("  month    " + "".join(f"{r.policy[-12:]:>17s}" for r in results))
    for month in base.months:
        cells = "".join(f"{r.months[month][0]:6.0f}{r.months[month][1]:5.0f}{r.months[month][2]:5.0f} "
                        for r in results)
        lines.append(f"  {month}  {cells}")
    return "\n".join(lines)
