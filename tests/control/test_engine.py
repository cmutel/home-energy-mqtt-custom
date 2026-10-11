from datetime import datetime, time, timedelta

from dorfsteig_control.config import AlwaysOn, ControlConfig, DailyWindow, SolarParams
from dorfsteig_control.engine import GridSample, HeaterState, decide, initial_states

T0 = datetime(2026, 1, 8, 12, 0)


def grid(power: float | None, age_s: float = 1) -> GridSample:
    return GridSample(power=power, age_s=age_s)


def on(states) -> set[str]:
    return {h for h, s in states.items() if s.on}


def run(config, steps, start=T0, states=None, step_s=20):
    """Apply a sequence of grid readings, one per tick."""
    states = states or initial_states()
    all_events = []
    for i, power in enumerate(steps):
        states, events = decide(states, config, start + timedelta(seconds=step_s * i), grid(power))
        all_events.extend(events)
    return states, all_events


def test_schedule_turns_on_and_off():
    config = ControlConfig(rules=[DailyWindow(heater="kids_bedroom", start=time(1), stop=time(3))])
    states, _ = run(config, [None], start=datetime(2026, 1, 8, 2))
    assert on(states) == {"kids_bedroom"}
    states, events = run(config, [None], start=datetime(2026, 1, 8, 3), states=states)
    assert on(states) == set()
    assert events[0].reason == "schedule ended"


def test_removing_always_on_turns_heater_off():
    # Legacy bug: the heater stayed on forever
    states, _ = run(ControlConfig(rules=[AlwaysOn(heater="living_room")]), [None])
    assert on(states) == {"living_room"}
    states, _ = run(ControlConfig(), [None], states=states)
    assert on(states) == set()


def test_export_adds_one_heater_per_tick_in_priority_order():
    states, events = run(ControlConfig(), [2000, 2000, 2000])
    assert [e.heater for e in events] == ["corridor_1st_floor", "upstairs_guest", "living_room"]
    assert all(states[h].reason == "solar" for h in on(states))


def test_dead_band_does_nothing():
    states, events = run(ControlConfig(), [2000, 1000, 0, -150])
    assert len(events) == 1


def test_import_removes_last_added_after_min_interval():
    states, _ = run(ControlConfig(), [2000, 2000])
    # 20 s later: too soon to switch off
    states, events = run(ControlConfig(), [-500], start=T0 + timedelta(seconds=40), states=states)
    assert events == []
    states, events = run(ControlConfig(), [-500], start=T0 + timedelta(seconds=200), states=states)
    assert [(e.heater, e.on) for e in events] == [("upstairs_guest", False)]


def test_heater_switched_off_recently_is_skipped():
    states = initial_states()
    ts = T0.timestamp()
    states["corridor_1st_floor"] = HeaterState(on=False, since=ts - 30)
    _, events = decide(states, ControlConfig(), T0, grid(2000))
    assert events[0].heater == "upstairs_guest"


def test_import_never_removes_scheduled_heater():
    config = ControlConfig(rules=[AlwaysOn(heater="playroom_south")])
    states, _ = run(config, [None])
    states, events = run(config, [-2000], start=T0 + timedelta(seconds=600), states=states)
    assert events == []
    assert on(states) == {"playroom_south"}


def test_schedule_takes_over_solar_heater():
    states, _ = run(ControlConfig(), [2000])
    config = ControlConfig(rules=[AlwaysOn(heater="corridor_1st_floor")])
    states, events = run(config, [-2000], start=T0 + timedelta(seconds=600), states=states)
    assert events == []
    assert states["corridor_1st_floor"].reason == "schedule"


def test_stale_data_sheds_all_solar_heaters():
    states, _ = run(ControlConfig(), [2000, 2000])
    states, events = decide(states, ControlConfig(), T0 + timedelta(seconds=60), grid(2000, age_s=300))
    assert on(states) == set()
    assert {e.reason for e in events} == {"stale data"}


def test_no_reading_yet_does_nothing():
    _, events = decide(initial_states(), ControlConfig(), T0, grid(None, age_s=10))
    assert events == []


def test_solar_disabled_sheds_and_never_adds():
    states, _ = run(ControlConfig(), [2000])
    config = ControlConfig(solar=SolarParams(enabled=False))
    states, events = run(config, [5000], start=T0 + timedelta(seconds=600), states=states)
    assert on(states) == set()
    assert [e.reason for e in events] == ["solar disabled"]


def test_heater_removed_from_priority_is_switched_off():
    states, _ = run(ControlConfig(), [2000])
    config = ControlConfig(solar=SolarParams(priority=["living_room"]))
    states, events = run(config, [0], start=T0 + timedelta(seconds=600), states=states)
    assert on(states) == set()
    assert events[0].reason == "removed from priority"
