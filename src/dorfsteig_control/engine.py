"""The control decision, as a pure function.

`decide` takes the current heater states, the config, the time and the latest
grid power reading, and returns the new states plus the switching events.
No I/O happens here, so it can be unit tested and replayed over history.

Behaviour (phase 1, matching the legacy controller with its bugs fixed):

1. Schedule rules win. A heater that a rule wants on is switched on at once
   (reason "schedule"). When no rule wants it any more, it is switched off,
   including when an always-on rule is removed or disabled.
2. PV surplus, one heater per tick, in `solar.priority` order:
   - export above `export_on_w`: switch on the first heater that is off and
     has been off for at least `min_switch_interval_s`;
   - import beyond `import_off_w`: switch off the last solar-controlled heater
     that has been on for at least `min_switch_interval_s`.
   Heaters held on by a schedule are never switched off by this step.
3. Safety: if PV control is disabled, or no grid reading has arrived for
   `stale_after_s`, all solar-controlled heaters are switched off.

Water heaters in "pv" mode (see water.py): their always-on rules are set
aside, other rules still apply; between steps 1 and 2 the water step switches
them on PV surplus, backup and safety. Room heaters then only get surplus
once every water heater is on, and are shed first.

Overrides (used by the wattage test) force a heater on or off and take it out
of steps 1–2 until the override is removed. `hold_solar` pauses step 2 so a
measurement isn't disturbed by other heaters switching.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from dorfsteig_control.config import AlwaysOn, ControlConfig
from dorfsteig_control.heaters import HEATERS
from dorfsteig_control.schedule import scheduled_heaters
from dorfsteig_control.water import decide_water

Reason = Literal["schedule", "solar", "override", "water pv", "water backup", "water safety", "stale data"]


@dataclass(frozen=True)
class HeaterState:
    on: bool = False
    since: float = 0.0               # Unix time of the last switch; 0 = never switched
    reason: Reason | None = None     # why it is on; None when off


@dataclass(frozen=True)
class GridSample:
    power: float | None   # W, positive = export; None if nothing received yet
    age_s: float          # seconds since the last reading (or since startup)
    average: float | None = None   # W, smoothed over the last minute


@dataclass(frozen=True)
class Event:
    ts: float
    heater: str
    on: bool
    reason: str           # "schedule", "solar", "schedule ended", "import", "stale data", ...
    grid_power: float | None


States = dict[str, HeaterState]


def initial_states() -> States:
    return {h.id: HeaterState() for h in HEATERS}


def decide(
    states: States,
    config: ControlConfig,
    now: datetime,
    grid: GridSample,
    overrides: Mapping[str, bool] | None = None,
    hold_solar: bool = False,
    water_full: Mapping[str, float] | None = None,
) -> tuple[States, list[Event]]:
    overrides = overrides or {}
    water = config.water
    water_pv = water.mode == "pv"
    ts = now.timestamp()
    new = dict(states)
    events: list[Event] = []

    def switch(heater: str, on: bool, reason: Reason | None, why: str) -> None:
        new[heater] = HeaterState(on=on, since=ts, reason=reason if on else None)
        events.append(Event(ts=ts, heater=heater, on=on, reason=why, grid_power=grid.power))

    # 0. Overrides
    for heater, want_on in overrides.items():
        state = states[heater]
        if state.on != want_on:
            switch(heater, want_on, "override", "override")
        elif want_on and state.reason != "override":
            new[heater] = replace(state, reason="override")

    # 1. Schedules
    rules = config.rules
    if water_pv:
        rules = [r for r in rules if not (isinstance(r, AlwaysOn) and r.heater in water.heaters)]
    wanted = scheduled_heaters(rules, now)
    for heater, state in states.items():
        if heater in overrides:
            continue
        if state.on and state.reason == "override" and heater not in wanted:
            switch(heater, False, None, "override ended")
        elif heater in wanted:
            if not state.on:
                switch(heater, True, "schedule", "schedule")
            elif state.reason != "schedule":
                # Already on from PV surplus or an override; the schedule now holds it on
                new[heater] = replace(state, reason="schedule")
        elif state.on and state.reason == "schedule":
            if water_pv and heater in water.heaters:
                new[heater] = replace(state, reason="water pv")   # the water step decides from here
            else:
                switch(heater, False, None, "schedule ended")

    # Water heaters on PV surplus, backup and safety
    solar = config.solar
    water_ready = True
    if water_pv:
        managed = {h for h in water.heaters if h not in overrides and h not in wanted}
        stale = grid.age_s > solar.stale_after_s or grid.power is None
        decisions = decide_water(
            water, now,
            relays={h: (new[h].on, new[h].since, new[h].reason) for h in water.heaters},
            grid_avg=grid.average if grid.average is not None else grid.power,
            stale=stale,
            last_full=water_full or {},
            last_switch=max(new[h].since for h in water.heaters),
            room_heaters_on_pv=any(new[h].on and new[h].reason == "solar" for h in solar.priority),
            managed=managed,
        )
        for d in decisions:
            if d.on and new[d.heater].on:
                new[d.heater] = replace(new[d.heater], reason=d.why)   # still on, for a new reason
            else:
                switch(d.heater, d.on, d.why if d.on else None, d.why)
        # Room heaters only get surplus once every tank is on and the last switch has settled
        water_ready = (all(new[h].on for h in water.heaters)
                       and ts - max(new[h].since for h in water.heaters) >= water.settle_s)

    # 2–3. PV surplus for room heaters
    for heater, state in list(new.items()):
        if state.reason == "solar" and heater not in solar.priority:
            switch(heater, False, None, "removed from priority")
    solar_on = [h for h in solar.priority if new[h].on and new[h].reason == "solar"]

    if not solar.enabled:
        for heater in solar_on:
            switch(heater, False, None, "solar disabled")
    elif grid.age_s > solar.stale_after_s:
        for heater in solar_on:
            switch(heater, False, None, "stale data")
    elif grid.power is None or hold_solar:
        pass
    elif grid.power > solar.export_on_w and water_ready:
        for heater in solar.priority:
            state = new[heater]
            if heater not in overrides and not state.on and ts - state.since >= solar.min_switch_interval_s:
                switch(heater, True, "solar", "export")
                break
    elif grid.power < solar.import_off_w:
        for heater in reversed(solar_on):
            if ts - new[heater].since >= solar.min_switch_interval_s:
                switch(heater, False, None, "import")
                break

    return new, events
