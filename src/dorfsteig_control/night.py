"""Night plan: charging the storage heaters from the grid before cold days.

In autumn and spring the storage heaters charge from PV only (fit mode, see
engine.py). On colder days that isn't enough; until now the house was then
heated by switching heaters on by hand. The night plan does this instead.
Once a day, at `night.charge_from` (20:00, when the low tariff starts), it
works out how much heat tomorrow needs and how much of that PV will bring:

    need     = kwh_per_degree × max(0, solar.temp_limit - tomorrow's mean temperature)
    from PV  = pv_credit × tomorrow's forecast PV surplus
    grid     = need - from PV, or nothing if that's less than min_kwh

- Tomorrow's mean temperature: forecast (outdoor.day_mean).
- PV surplus: the forecast PV beyond the usual house load, from the
  `day_forecast` table written by `dorfsteig-forecast`. Without it nothing
  is credited for PV, so the plan charges more, not less.
- pv_credit < 1 because the surplus is optimistic in winter: the house uses
  more than on the warm days the base load comes from, and not all of it
  fits whole heaters. Over 2025-26, the heaters could have taken 23% of the
  forecast surplus on heating days, hence the default 0.2 (see
  replay_night.py, `dorfsteig-control replay --night`).

The grid share is split over the room heaters in `solar.priority` that draw
power (the latest wattage test didn't find them switched off by hand). All
of them charge at the same time, for grid kWh / (their total watts) hours
from charge_from (or from when the plan was made, if later), and never past
charge_until.

Why charge for tomorrow, and why early in the night: a storage heater gives
off its heat over the following day. Charged early, its core is emptier by
midday and takes more PV, the same reason the water heaters' backup isn't at
night (PLAN.md, 3b).

The plan is made once per night and stored in `night_plan`, so it survives
restarts and shows on the status and Solar pages.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dorfsteig_control import forecasts, outdoor
from dorfsteig_control.config import ControlConfig, NightParams


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
    start: float                 # Unix time; all heaters charge from start to end
    end: float
    note: str                    # how the numbers came about, for people

    def windows(self) -> dict[str, tuple[float, float]]:
        """Heater → (on from, off at), for engine.decide."""
        return {heater: (self.start, self.end) for heater in self.heaters}


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
    limit = config.solar.temp_limit
    note = f"Tomorrow {temperature:.1f} °C ({source})"
    plan.need_kwh = params.kwh_per_degree * max(0.0, limit - temperature)
    if plan.need_kwh == 0:
        plan.note = f"{note}, not below {limit:.0f} °C: no heating needed"
        return plan
    note += f": needs {plan.need_kwh:.1f} kWh"

    forecast = forecasts.day_forecast(conn, tomorrow, now)
    if forecast is None:
        from_pv = 0.0
        note += ", no PV forecast"
    else:
        plan.surplus_kwh = forecast.surplus_kwh
        from_pv = params.pv_credit * forecast.surplus_kwh
        note += f", PV surplus {forecast.surplus_kwh:.1f} kWh × {params.pv_credit:.1f} = {from_pv:.1f} kWh"

    grid = max(0.0, plan.need_kwh - from_pv)
    if grid < params.min_kwh:
        plan.note = f"{note}; grid {grid:.1f} kWh: not worth charging"
        return plan

    heaters = [h for h in config.solar.priority if h not in no_load]
    total_w = sum(watts.get(h, 2000.0) for h in heaters)
    if total_w == 0:
        plan.note = f"{note}; no heaters available"
        return plan

    hours = grid * 1000 / total_w
    end = start + hours * 3600
    capped = ""
    if end > latest_end:
        end = latest_end
        hours = (end - start) / 3600
        grid = hours * total_w / 1000
        capped = f" (all there's time for until {params.charge_until:%H:%M})"
    plan.heaters = heaters
    plan.end = end
    plan.grid_kwh = grid
    plan.note = f"{note}; grid {grid:.1f} kWh = {hours:.1f} h on {len(heaters)} heaters{capped}"
    return plan
