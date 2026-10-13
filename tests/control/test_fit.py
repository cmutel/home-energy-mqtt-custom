from datetime import datetime, timedelta
from pathlib import Path

from dorfsteig_control.budget import Budget
from dorfsteig_control.config import ControlConfig, SolarParams
from dorfsteig_control.engine import GridSample, HeaterState, decide, initial_states
from dorfsteig_control.outdoor import today_mean
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier

NOON = datetime(2026, 10, 13, 12, 0)
WATTS = {"upstairs_guest": 1830, "corridor_1st_floor": 1660, "living_room": 1535}


def config(**solar) -> ControlConfig:
    return ControlConfig(solar=SolarParams(mode="fit", priority=list(WATTS), **solar))


def budgets(budget: float = 5.0, **delivered: float) -> dict[str, Budget]:
    """Every zone `budget` kWh today; kWh delivered so far as given, else nothing."""
    return {h: Budget(budget, delivered.get(h, 0.0)) for h in WATTS}


def idle() -> dict:
    states = initial_states()
    for h in WATTS:
        states[h] = HeaterState(on=False, since=(NOON - timedelta(hours=1)).timestamp())
    return states


def on_for(states, heater: str, minutes: float) -> None:
    states[heater] = HeaterState(on=True, since=NOON.timestamp() - minutes * 60, reason="solar")


def run(states, export, now=NOON, zone_budgets=None, skip=None, cfg=None):
    grid = GridSample(power=export, age_s=5, average=export)
    if zone_budgets is None:
        zone_budgets = budgets()
    return decide(states, cfg or config(), now, grid, budgets=zone_budgets, watts=WATTS, room_skip=skip or {})


def on_events(events):
    return [(e.heater, e.on, e.reason) for e in events]


def test_ties_go_by_priority_and_a_smaller_heater_may_fit():
    _, events = run(idle(), export=2000)
    assert on_events(events) == [("upstairs_guest", True, "export")]
    # 1800 W doesn't fit the guest room (1830 + 100 margin) but fits the corridor
    _, events = run(idle(), export=1800)
    assert on_events(events) == [("corridor_1st_floor", True, "export")]
    _, events = run(idle(), export=1500)
    assert events == []


def test_zone_furthest_behind_its_budget_goes_first():
    # Guest room 3 of 5 kWh, corridor 1 of 2 kWh, living room 1 of 5 kWh: the living room is furthest behind
    zone_budgets = {"upstairs_guest": Budget(5, 3), "corridor_1st_floor": Budget(2, 1),
                    "living_room": Budget(5, 1)}
    _, events = run(idle(), export=4000, zone_budgets=zone_budgets)
    assert on_events(events) == [("living_room", True, "export")]


def test_one_switch_on_per_settle_time():
    new, _ = run(idle(), export=6000)
    _, events = run(new, export=4000, now=NOON + timedelta(seconds=30))
    assert events == []
    _, events = run(new, export=4000, now=NOON + timedelta(seconds=61))
    assert on_events(events) == [("corridor_1st_floor", True, "export")]


def test_zone_stops_at_its_budget():
    states = idle()
    on_for(states, "living_room", 1)
    zone_budgets = budgets(living_room=5.0, upstairs_guest=5.0)   # both have had their 5 kWh
    new, events = run(states, export=3000, zone_budgets=zone_budgets)
    assert on_events(events) == [("living_room", False, "budget reached")]
    # The guest room is done too, so the corridor gets the surplus
    _, events = run(new, export=3000, now=NOON + timedelta(minutes=2), zone_budgets=zone_budgets)
    assert on_events(events) == [("corridor_1st_floor", True, "export")]
    # All zones have had their budget: nothing goes on, unless max_share allows more (1.2: up to 6 kWh)
    all_done = budgets(**{h: 5.0 for h in WATTS})
    assert run(idle(), export=2000, zone_budgets=all_done)[1] == []
    _, events = run(idle(), export=2000, zone_budgets=all_done, cfg=config(max_share=1.2))
    assert on_events(events) == [("upstairs_guest", True, "export")]


def test_no_budget_or_unknown_temperature_switches_off():
    states = idle()
    on_for(states, "living_room", 1)
    _, events = run(states, export=3000, zone_budgets=budgets(budget=0.0))   # a mild day
    assert on_events(events) == [("living_room", False, "no budget")]
    grid = GridSample(power=3000, age_s=5, average=3000)
    _, events = decide(states, config(), NOON, grid, budgets=None, watts=WATTS)
    assert on_events(events) == [("living_room", False, "no temperature")]


def test_shedding_the_zone_furthest_ahead():
    states = idle()
    on_for(states, "upstairs_guest", 60)
    on_for(states, "living_room", 60)
    on_for(states, "corridor_1st_floor", 1)
    zone_budgets = budgets(upstairs_guest=1.0, living_room=2.0, corridor_1st_floor=4.0)
    # Moderate import: the corridor is furthest ahead but only just on; the living room is next
    _, events = run(states, export=-400, zone_budgets=zone_budgets)
    assert on_events(events) == [("living_room", False, "import")]
    # Large import: the zone furthest ahead goes at once
    _, events = run(states, export=-1500, zone_budgets=zone_budgets)
    assert on_events(events) == [("corridor_1st_floor", False, "import")]


def test_heater_makes_way_for_a_zone_further_behind():
    states = idle()
    on_for(states, "upstairs_guest", 31)
    zone_budgets = budgets(upstairs_guest=4.0, corridor_1st_floor=1.0, living_room=1.0)
    # 500 W export: nothing more fits, but the corridor fits in place of the guest room
    _, events = run(states, export=500, zone_budgets=zone_budgets)
    assert on_events(events) == [("upstairs_guest", False, "rotate"), ("corridor_1st_floor", True, "rotate")]
    # Not if the guest room has only been on a short while, or the others aren't far enough behind
    on_for(states, "upstairs_guest", 10)
    assert run(states, export=500, zone_budgets=zone_budgets)[1] == []
    on_for(states, "upstairs_guest", 31)
    close = budgets(upstairs_guest=2.0, corridor_1st_floor=1.5, living_room=1.5)
    assert run(states, export=500, zone_budgets=close)[1] == []
    # Nor if the zone behind doesn't fit in the power freed: corridor 1660 W for living room 1535 W
    states = idle()
    on_for(states, "living_room", 31)
    zone_budgets = budgets(living_room=4.0)
    assert run(states, export=0, zone_budgets=zone_budgets)[1] == []


def test_skipped_heater_is_switched_off_and_not_chosen():
    states = idle()
    states["upstairs_guest"] = HeaterState(on=True, since=NOON.timestamp() - 120, reason="solar")
    skip = {"upstairs_guest": NOON.timestamp() + 3600}
    new, events = run(states, export=2500, skip=skip)
    assert ("upstairs_guest", False, "no load") in on_events(events)
    _, events = run(new, export=2500, now=NOON + timedelta(minutes=2), skip=skip)
    assert on_events(events) == [("corridor_1st_floor", True, "export")]


def test_legacy_mode_unchanged():
    cfg = ControlConfig(solar=SolarParams(priority=list(WATTS), export_on_w=1000))
    _, events = run(idle(), export=1200, cfg=cfg, zone_budgets={})
    assert on_events(events) == [("upstairs_guest", True, "export")]


def test_today_mean_prefers_station_then_forecast(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    day = datetime(2026, 10, 13)
    hours = [int((day + timedelta(hours=h)).timestamp()) for h in range(24)]
    conn.executemany("INSERT INTO weather VALUES (?, 'meteoswiss', 4.0, 0, NULL, 0)",
                     [(t,) for t in hours[:12]])
    issued = hours[0] - 3600
    conn.executemany("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 10.0, 0, 0)",
                     [(issued, t) for t in hours])
    conn.executemany("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 99.0, 0, 0)",   # issued later
                     [(hours[23] + 3600, t) for t in hours])
    mean, source = today_mean(conn, day + timedelta(hours=12))
    assert mean == 7.0 and source == "icon_ch2 + station"
    assert today_mean(init_db(tmp_path / "empty.sqlite"), day)[0] is None


def test_room_shadow_logs_without_switching(tmp_path: Path):
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    cfg = config(export_on_w=99_999)    # so the legacy logic, still live in shadow mode, switches nothing
    cfg.solar.mode = "shadow"
    controller.store.save_config(cfg, source="test")   # Controller.save_config would tick at the real time
    controller.refresh_inputs = lambda now: None          # no weather tables in the test
    controller.outdoor_mean, controller.watts = 8.0, dict(WATTS)
    for minutes in (0, 2):
        now = NOON + timedelta(minutes=minutes)
        controller.grid_power, controller.grid_ts = 4000.0, now.timestamp()
        controller.tick(now)
    assert not any(controller.states[h].on for h in WATTS)                   # nothing switched for real
    shadow = [e for e in controller.store.recent_events(10) if e["dry_run"]]
    assert [e["heater"] for e in shadow] == ["corridor_1st_floor", "upstairs_guest"]   # newest first


def test_check_room_loads_skips_heater_that_draws_nothing(tmp_path: Path):
    from dorfsteig_control import wattage
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    controller.config.solar.mode = "fit"
    t = 1_000_000.0
    for s in range(-60, 100, 2):
        # Corridor switched on at t: the grid drops by 1660 W. Guest room switched on at t + 1000: no change
        controller.history.add(t + s, 3000.0 - (1660 if s > 20 else 0))
        controller.history.add(t + 1000 + s, 2500.0)
    controller.load_checks = [wattage.PendingSwitch(t, "corridor_1st_floor", True),
                              wattage.PendingSwitch(t + 1000, "upstairs_guest", True)]
    controller.check_room_loads(t + 1200)
    assert list(controller.room_skip) == ["upstairs_guest"]
    assert controller.room_skip["upstairs_guest"] == t + 1000 + controller.config.solar.no_load_skip_s
