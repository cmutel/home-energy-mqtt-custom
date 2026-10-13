"""Daily heat budgets for the room (storage) heaters.

Each zone (a room heater in `solar.priority`) gets a budget for the day from
the day's mean outdoor temperature (config.Zone):

    budget (kWh) = kwh_per_degree × max(0, heats_below - mean temperature)

Fit mode (engine.fit_rooms) shares the PV surplus out by these budgets: the
zone with the smallest share of its budget delivered goes first, a zone
stops at `solar.max_share` of its budget, and a heater that has been on a
while makes way for a zone further behind. So when PV is short every zone
gets about the same share of its budget, and when it's plentiful no zone
gets more than it needs.

The budget follows the day: the mean temperature is the station's
measurements so far plus the forecast for the rest of the day
(outdoor.day_mean), refreshed every ten minutes.

A budget day runs from `night.charge_from` (20:00) to 20:00 the next day,
and is named after the calendar day it ends on. So a night plan's charge
(from 20:00) counts towards the day it's for, and that day's PV fills up the
rest. "Delivered" is all the heat a heater got in the budget day, whatever
switched it on (PV, night plan, boost): on-time × its measured wattage
(summary.delivered_kwh). That's an upper bound, as a storage heater stops
drawing when its core is full.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from dorfsteig_control.config import SolarParams


@dataclass(frozen=True)
class Budget:
    budget_kwh: float
    delivered_kwh: float

    @property
    def share(self) -> float:
        """Share of the budget delivered so far; 1 (nothing more needed) without a budget."""
        if self.budget_kwh <= 0:
            return 1.0
        return self.delivered_kwh / self.budget_kwh

    def done(self, max_share: float) -> bool:
        return self.budget_kwh <= 0 or self.share >= max_share


def budget_day(now: datetime, day_from: time) -> date:
    """The budget day `now` is in: from `day_from` the evening before to `day_from` on that day."""
    return now.date() + timedelta(days=1) if now.time() >= day_from else now.date()


def day_start(day: date, day_from: time) -> datetime:
    return datetime.combine(day - timedelta(days=1), day_from)


def zone_budgets(solar: SolarParams, temperature: float | None,
                 delivered: Mapping[str, float]) -> dict[str, Budget] | None:
    """Budget and kWh delivered so far for each zone; None if the temperature is unknown."""
    if temperature is None:
        return None
    return {heater: Budget(solar.zones[heater].budget_kwh(temperature), delivered.get(heater, 0.0))
            for heater in solar.priority}
