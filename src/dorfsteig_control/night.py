"""Night plan: charging the storage heaters from the grid before cold days.

In autumn and spring the storage heaters charge from PV only (fit mode, see
engine.py). On colder days that isn't enough; until now the house was then
heated by switching heaters on by hand. The night plan does this instead.
Once a day, at `night.charge_from` (20:00, when the low tariff starts), it
works out how much heat tomorrow needs and how much of that PV will bring:

    need     = the zones' budgets for tomorrow's mean temperature (budget.py)
    from PV  = pv_credit × tomorrow's forecast PV surplus, shared by the zones
               in proportion to their budgets
    grid     = for each zone that may charge at night (Zone.night):
               its budget - its share of the PV; nothing at all if the
               zones together need less than min_kwh

- Tomorrow's mean temperature: forecast (outdoor.day_mean).
- PV surplus: the forecast PV beyond the usual house load, from the
  `day_forecast` table written by `dorfsteig-forecast`. Without it nothing
  is credited for PV, so the plan charges more, not less.
- pv_credit < 1 because the surplus is optimistic in winter: the house uses
  more than on the warm days the base load comes from, and not all of it
  fits whole heaters. Over 2025-26, the heaters could have taken 23% of the
  forecast surplus on heating days, hence the default 0.2 (see
  replay_night.py, `dorfsteig-control replay --night`).

Only heaters that draw power charge (the latest wattage test didn't find
them switched off by hand). They all start at charge_from (or when the plan
was made, if later), each for its zone's grid kWh / its watts, and never past
charge_until. What a heater charges counts towards its zone's budget for
tomorrow, so on a sunny day PV then only tops up.

With night.mode "off" no plan is made: the house is heated from PV only, and
gets colder on grey days.

Why charge for tomorrow, and why early in the night: a storage heater gives
off its heat over the following day. Charged early, its core is emptier by
midday and takes more PV, the same reason the water heaters' backup isn't at
night (PLAN.md, 3b).

The plan is made once per night and stored in `night_plan`, so it survives
restarts and shows on the status and Solar pages.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dorfsteig_control import forecasts, outdoor
from dorfsteig_control.config import ControlConfig, NightParams
from dorfsteig_control.heaters import BY_ID


@dataclass
class NightPlan:
    night: date                  # the evening the charging starts
    made_at: float
    mode: str                    # night.mode when it was made ("shadow" or "on")
    temperature: float | None    # tomorrow's mean, °C
    need_kwh: float
    surplus_kwh: float | None    # tomorrow's forecast PV surplus; None without a forecast
    grid_kwh: float
    heaters: list[str]
    start: float                 # Unix time; all heaters start charging then
    end: float                   # when the last one stops
    note: str                    # how the numbers came about, for people
    ends: dict[str, float] = field(default_factory=dict)   # heater → when it stops; `end` if missing

    def windows(self) -> dict[str, tuple[float, float]]:
        """Heater → (on from, off at), for engine.decide."""
        return {heater: (self.start, self.ends.get(heater, self.end)) for heater in self.heaters}


def night_of(now: datetime, params: NightParams) -> date | None:
    """The evening whose charging window `now` falls in; None during the day."""
    if now.time() >= params.charge_from:
        return now.date()
    if now.time() < params.charge_until:
        return now.date() - timedelta(days=1)
    return None


def make_plan(conn: sqlite3.Connection, config: ControlConfig, now: datetime,
              watts: Mapping[str, float], no_load: set[str]) -> NightPlan:
    """The plan for the night that `now` falls in (call only within the charging window)."""
    params = config.night
    night = night_of(now, params)
    if night is None:
        raise ValueError("Not within the charging window")
    tomorrow = night + timedelta(days=1)
    start = max(datetime.combine(night, params.charge_from).timestamp(), now.timestamp())
    latest_end = datetime.combine(tomorrow, params.charge_until).timestamp()

    # Start from "no charging" and fill in as far as the inputs allow
    plan = NightPlan(night=night, made_at=now.timestamp(), mode=params.mode, temperature=None,
                     need_kwh=0.0, surplus_kwh=None, grid_kwh=0.0, heaters=[], start=start, end=start,
                     note="")

    temperature, source = outdoor.day_mean(conn, tomorrow, now)
    if temperature is None:
        plan.note = f"No temperature forecast for tomorrow ({source}): no charging"
        return plan
    plan.temperature = temperature
    solar = config.solar
    budgets = {h: solar.zones[h].budget_kwh(temperature) for h in solar.priority}
    plan.need_kwh = sum(budgets.values())
    note = f"Tomorrow {temperature:.1f} °C ({source})"
    if plan.need_kwh == 0:
        plan.note = f"{note}: no zone needs heat"
        return plan
    note += f": zones need {plan.need_kwh:.1f} kWh"

    forecast = forecasts.day_forecast(conn, tomorrow, now)
    if forecast is None:
        from_pv = 0.0
        note += ", no PV forecast"
    else:
        plan.surplus_kwh = forecast.surplus_kwh
        from_pv = min(plan.need_kwh, params.pv_credit * forecast.surplus_kwh)
        note += f", PV surplus {forecast.surplus_kwh:.1f} kWh × {params.pv_credit:.1f} = {from_pv:.1f} kWh"

    # Each zone that may charge at night: its budget minus its share of the PV
    heaters = [h for h in solar.priority if solar.zones[h].night and h not in no_load and budgets[h] > 0]
    if not heaters:
        plan.note = f"{note}; no zone that needs heat may charge at night"
        return plan
    grid = {h: budgets[h] * (1 - from_pv / plan.need_kwh) for h in heaters}
    if sum(grid.values()) < params.min_kwh:
        plan.note = f"{note}; grid {sum(grid.values()):.1f} kWh: not worth charging"
        return plan

    capped = []
    for heater in heaters:
        w = watts.get(heater, 2000.0)
        end = start + grid[heater] * 1000 / w * 3600
        if end > latest_end:
            end = latest_end
            grid[heater] = (end - start) / 3600 * w / 1000
            capped.append(heater)
        plan.ends[heater] = end
    plan.heaters = heaters
    plan.end = max(plan.ends.values())
    plan.grid_kwh = sum(grid.values())
    hours = ", ".join(f"{BY_ID[h].name} {(plan.ends[h] - start) / 3600:.1f} h" for h in heaters)
    plan.note = f"{note}; grid {plan.grid_kwh:.1f} kWh: {hours}"
    if capped:
        plan.note += f" (all there's time for until {params.charge_until:%H:%M})"
    return plan
