from datetime import datetime, timedelta
from pathlib import Path

from dorfsteig_control.config import AlwaysOn, ControlConfig, OneOff, WaterParams
from dorfsteig_control.engine import GridSample, HeaterState, decide, initial_states
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_control.water import backup_due, detect_full
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier

DOWN, UP = "downstairs_water", "upstairs_water"
NOON = datetime(2026, 10, 13, 12, 0)


def config(**water) -> ControlConfig:
    return ControlConfig(rules=[AlwaysOn(heater=DOWN), AlwaysOn(heater=UP)],
                         water=WaterParams(mode="pv", **water))


def full_at(when: datetime) -> dict[str, float]:
    return {DOWN: when.timestamp(), UP: when.timestamp()}


def states_with(**on: str) -> dict:
    """Water heaters on for a long time, with the given reasons; others off."""
    states = initial_states()
    for heater, reason in on.items():
        states[heater] = HeaterState(on=True, since=0, reason=reason)
    return states


def run(states, cfg, now, export, full, overrides=None):
    grid = GridSample(power=export, age_s=5, average=export)
    return decide(states, cfg, now, grid, overrides=overrides, water_full=full)


def test_backup_due():
    p = WaterParams()
    assert backup_due(p, DOWN, NOON, (NOON - timedelta(hours=1)).timestamp()) is None
    # 16:00, not full since 10:00 → backup; full at 11:00 → none
    four = NOON.replace(hour=16)
    assert backup_due(p, DOWN, four, NOON.replace(hour=9).timestamp()) == "water backup"
    assert backup_due(p, DOWN, four, NOON.replace(hour=11).timestamp()) is None
    # Before 16:00 the previous day's backup still applies until the tank is full
    assert backup_due(p, DOWN, NOON.replace(hour=3), (NOON - timedelta(days=1, hours=3)).timestamp()) \
        == "water backup"
    # Safety: upstairs after 24 h, downstairs after 36 h
    assert backup_due(p, UP, NOON, (NOON - timedelta(hours=25)).timestamp()) == "water safety"
    assert backup_due(p, DOWN, NOON, (NOON - timedelta(hours=25)).timestamp()) is None


def test_switching_to_pv_mode_keeps_tanks_on_until_import():
    states = states_with(downstairs_water="schedule", upstairs_water="schedule")
    new, events = run(states, config(), NOON, export=500, full=full_at(NOON))
    assert events == [] and new[DOWN].on and new[DOWN].reason == "water pv"
    # Importing: the last tank switched on for PV goes off first
    new, events = run(new, config(), NOON, export=-500, full=full_at(NOON))
    assert [(e.heater, e.on) for e in events] == [(UP, False)]


def test_surplus_switches_tanks_on_in_order_with_settle_time():
    states = initial_states()
    long_ago = NOON - timedelta(hours=2)
    for h in (DOWN, UP):
        states[h] = HeaterState(on=False, since=long_ago.timestamp())
    new, events = run(states, config(), NOON, export=2000, full=full_at(NOON))
    assert [(e.heater, e.on, e.reason) for e in events] == [(DOWN, True, "water pv")]
    # 30 s later: still settling, nothing else switches
    _, events = run(new, config(), NOON + timedelta(seconds=30), export=2000, full=full_at(NOON))
    assert events == []
    # After settling, if the export is still there (tank full), the next tank goes on
    new, events = run(new, config(), NOON + timedelta(seconds=90), export=2000, full=full_at(NOON))
    assert [(e.heater, e.on) for e in events] == [(UP, True)]
    # Room heaters only after that switch has settled too
    _, events = run(new, config(), NOON + timedelta(seconds=180), export=2000, full=full_at(NOON))
    assert [e.heater for e in events] == ["corridor_1st_floor"]


def test_min_on_time_and_room_heaters_shed_first():
    states = states_with(downstairs_water="water pv", living_room="solar")
    states[DOWN] = HeaterState(on=True, since=NOON.timestamp() - 60, reason="water pv")
    cfg = config()
    cfg.solar.priority = ["living_room"]
    # Importing, the tank was just switched on and a room heater is on PV: the room heater goes
    new, events = run(states, cfg, NOON, export=-800, full=full_at(NOON))
    assert [(e.heater, e.on) for e in events] == [("living_room", False)]
    assert new[DOWN].on


def test_room_heaters_wait_for_water_heaters():
    states = initial_states()
    states[DOWN] = HeaterState(on=False, since=NOON.timestamp())   # just switched off: can't go on yet
    cfg = config()
    _, events = run(states, cfg, NOON, export=3000, full=full_at(NOON))
    assert all(e.heater not in cfg.solar.priority for e in events)


def test_backup_and_safety_and_stale_data_switch_on():
    states = initial_states()
    four = NOON.replace(hour=16)
    new, events = run(states, config(), four, export=-500, full=full_at(NOON.replace(hour=8)))
    assert {(e.heater, e.reason) for e in events} == {(DOWN, "water backup"), (UP, "water backup")}
    # Once full again, PV takes over: no event, the relay stays on until import
    new, events = run(new, config(), four + timedelta(minutes=30), export=0, full=full_at(four))
    assert events == [] and new[DOWN].reason == "water pv"
    # No grid data: tanks on
    stale = GridSample(power=None, age_s=600)
    _, events = decide(initial_states(), config(), NOON, stale, water_full=full_at(NOON))
    assert {e.reason for e in events if e.heater in (DOWN, UP)} == {"stale data"}


def test_boost_rule_still_applies_in_pv_mode():
    cfg = config()
    cfg.rules.append(OneOff(heater=UP, start=NOON - timedelta(hours=1), stop=NOON + timedelta(hours=1),
                            note="boost"))
    new, _ = run(initial_states(), cfg, NOON, export=-1000, full=full_at(NOON))
    assert new[UP].on and new[UP].reason == "schedule"


def test_always_on_mode_unchanged():
    cfg = ControlConfig(rules=[AlwaysOn(heater=DOWN)])
    new, events = run(initial_states(), cfg, NOON, export=-1000, full={})
    assert [(e.heater, e.reason) for e in events] == [(DOWN, "schedule")]


def test_detect_full(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    on_since = 1_000_000.0
    rows = [(on_since + 60 * i, 1490.0 if i < 20 else 0.0) for i in range(35)]
    conn.executemany("INSERT INTO iotawatt_readings (ts, water_heater) VALUES (?, ?)", rows)
    assert detect_full(conn, DOWN, on_since, 600) == on_since + 60 * 35
    # Still heating in the last 10 minutes: not full
    conn.execute("UPDATE iotawatt_readings SET water_heater = 1490 WHERE ts = ?", (on_since + 60 * 33,))
    assert detect_full(conn, DOWN, on_since, 600) is None
    # Relay only just switched on: can't tell yet
    assert detect_full(conn, DOWN, on_since + 60 * 30, 600) is None


def test_shadow_mode_logs_without_switching(tmp_path: Path):
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    cfg = config()
    cfg.water.mode = "shadow"
    controller.save_config(cfg)
    controller.water_full = full_at(NOON)
    controller.on_grid_power(-1000)
    controller.grid_ts = NOON.timestamp()
    controller.tick(NOON)
    controller.grid_ts = (NOON + timedelta(minutes=11)).timestamp()
    controller.tick(NOON + timedelta(minutes=11))
    assert controller.states[DOWN].on and controller.states[UP].on          # real relays: always on
    events = controller.store.recent_events(10)
    assert any(e["heater"] == UP and not e["on"] and e["dry_run"] for e in events)
    assert not any(e["heater"] == UP and not e["on"] and not e["dry_run"] for e in events)
