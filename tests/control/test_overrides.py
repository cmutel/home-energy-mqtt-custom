from datetime import datetime, timedelta

from dorfsteig_control.config import AlwaysOn, ControlConfig
from dorfsteig_control.engine import GridSample, decide, initial_states

T0 = datetime(2026, 1, 8, 12, 0)
FRESH = GridSample(power=0, age_s=1)


def test_override_forces_heater_and_ends_cleanly():
    states, events = decide(initial_states(), ControlConfig(), T0, FRESH, overrides={"living_room": True})
    assert states["living_room"].on and states["living_room"].reason == "override"
    # Import doesn't remove an overridden heater
    states, events = decide(states, ControlConfig(), T0 + timedelta(minutes=10),
                            GridSample(power=-3000, age_s=1), overrides={"living_room": True})
    assert events == []
    # Override removed, no schedule → off
    states, events = decide(states, ControlConfig(), T0 + timedelta(minutes=11), FRESH)
    assert not states["living_room"].on
    assert events[0].reason == "override ended"


def test_override_off_beats_schedule_and_schedule_resumes():
    config = ControlConfig(rules=[AlwaysOn(heater="upstairs_water")])
    states, _ = decide(initial_states(), config, T0, FRESH)
    states, _ = decide(states, config, T0, FRESH, overrides={"upstairs_water": False})
    assert not states["upstairs_water"].on
    states, events = decide(states, config, T0 + timedelta(minutes=3), FRESH)
    assert states["upstairs_water"].on and events[0].reason == "schedule"


def test_hold_solar_pauses_switching():
    _, events = decide(initial_states(), ControlConfig(), T0, GridSample(power=5000, age_s=1),
                       hold_solar=True)
    assert events == []
