from datetime import datetime, timedelta
from pathlib import Path

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


def idle() -> dict:
    states = initial_states()
    for h in WATTS:
        states[h] = HeaterState(on=False, since=(NOON - timedelta(hours=1)).timestamp())
    return states


def run(states, export, now=NOON, temp=8.0, skip=None, cfg=None):
    grid = GridSample(power=export, age_s=5, average=export)
    return decide(states, cfg or config(), now, grid, outdoor_mean=temp, watts=WATTS, room_skip=skip or {})


def on_events(events):
    return [(e.heater, e.on, e.reason) for e in events]


def test_fits_highest_priority_heater_or_a_smaller_one():
    _, events = run(idle(), export=2000)
    assert on_events(events) == [("upstairs_guest", True, "export")]
    # 1700 W doesn't fit the guest room (1830 + 100 margin) but fits the corridor
    _, events = run(idle(), export=1800)
    assert on_events(events) == [("corridor_1st_floor", True, "export")]
    _, events = run(idle(), export=1500)
    assert events == []


def test_one_switch_on_per_settle_time():
    new, _ = run(idle(), export=6000)
    _, events = run(new, export=4000, now=NOON + timedelta(seconds=30))
    assert events == []
    _, events = run(new, export=4000, now=NOON + timedelta(seconds=61))
    assert on_events(events) == [("corridor_1st_floor", True, "export")]


def test_too_warm_or_unknown_temperature_switches_off():
    states = idle()
    states["living_room"] = HeaterState(on=True, since=NOON.timestamp() - 60, reason="solar")
    _, events = run(states, export=3000, temp=16.0)
    assert on_events(events) == [("living_room", False, "too warm")]
    _, events = run(states, export=3000, temp=None)
    assert on_events(events) == [("living_room", False, "no temperature")]


def test_shedding_min_on_and_fast_shed():
    states = idle()
    states["upstairs_guest"] = HeaterState(on=True, since=NOON.timestamp() - 3600, reason="solar")
    states["living_room"] = HeaterState(on=True, since=NOON.timestamp() - 60, reason="solar")
    # Moderate import: the lowest-priority heater that may go off (on for min_on_s) is shed; the
    # living room was only just switched on, so it's the guest room, to stop importing now
    _, events = run(states, export=-400)
    assert on_events(events) == [("upstairs_guest", False, "import")]
    # Large import: shed the lowest-priority heater at once
    _, events = run(states, export=-1500)
    assert on_events(events) == [("living_room", False, "import")]


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
    _, events = run(idle(), export=1200, cfg=cfg, temp=None)
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
    controller.save_config(cfg)
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
