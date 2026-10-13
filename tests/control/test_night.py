from datetime import date, datetime, timedelta
from pathlib import Path

from dorfsteig_control.config import ControlConfig, NightParams, SolarParams, Zone
from dorfsteig_control.engine import GridSample, HeaterState, decide, initial_states
from dorfsteig_control.night import make_plan, night_of
from dorfsteig_control.report import daily_report, format_report
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier

EVENING = datetime(2026, 11, 3, 20, 0)
TOMORROW = date(2026, 11, 4)
WATTS = {"upstairs_guest": 2000.0, "living_room": 2000.0}


def config(**night) -> ControlConfig:
    """Two zones, each 1 kWh per °C below 15 °C: 2 kWh per °C together."""
    zones = {h: Zone(heats_below=15.0, kwh_per_degree=1.0) for h in WATTS}
    return ControlConfig(solar=SolarParams(mode="fit", priority=list(WATTS), zones=zones),
                         night=NightParams(mode="on", pv_credit=0.5, **night))


def database(tmp_path: Path, temperature: float = 5.0, surplus: float | None = 4.0):
    """Tomorrow forecast at `temperature` all day; a day forecast with `surplus` kWh of PV surplus."""
    conn = init_db(tmp_path / "t.sqlite")
    issued = int((EVENING - timedelta(hours=2)).timestamp())
    start = datetime.combine(TOMORROW, datetime.min.time())
    for h in range(24):
        ts = int((start + timedelta(hours=h)).timestamp())
        conn.execute("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', ?, 0, 0)",
                     (issued, ts, temperature))
    if surplus is not None:
        conn.execute("INSERT INTO day_forecast VALUES (?, ?, ?, 10, 30, ?)",
                     (issued, TOMORROW.isoformat(), temperature, surplus))
    conn.commit()
    return conn


def test_night_of():
    params = NightParams()
    assert night_of(datetime(2026, 11, 3, 21, 0), params) == date(2026, 11, 3)
    assert night_of(datetime(2026, 11, 4, 3, 0), params) == date(2026, 11, 3)
    assert night_of(datetime(2026, 11, 4, 12, 0), params) is None


def test_plan_needs_heat_for_tomorrow_minus_pv(tmp_path: Path):
    # 2 kWh/°C × (15 - 5) = 20 kWh; PV 4 kWh × 0.5 = 2 kWh; grid 18 kWh, 9 kWh each = 4.5 h at 2 kW
    plan = make_plan(database(tmp_path), config(), EVENING, WATTS, no_load=set())
    assert plan.need_kwh == 20 and plan.grid_kwh == 18
    assert plan.heaters == list(WATTS)
    assert plan.start == EVENING.timestamp()
    assert plan.end == (EVENING + timedelta(hours=4.5)).timestamp()
    assert plan.windows()["living_room"] == (plan.start, plan.end)
    assert "PV surplus 4.0 kWh × 0.5" in plan.note


def test_plan_charges_each_zone_for_its_own_budget(tmp_path: Path):
    cfg = config()
    cfg.solar.zones["upstairs_guest"] = Zone(heats_below=10.0, kwh_per_degree=1.0)   # gets heat from below
    # At 5 °C: guest room 5 kWh, living room 10 kWh. PV 2 kWh is shared 1:2, so 13 kWh from the grid
    plan = make_plan(database(tmp_path), cfg, EVENING, WATTS, no_load=set())
    assert plan.need_kwh == 15 and round(plan.grid_kwh, 6) == 13
    hours = {h: (end - plan.start) / 3600 for h, (_, end) in plan.windows().items()}
    assert round(hours["upstairs_guest"], 6) == round(5 * 13 / 15 / 2, 6)
    assert round(hours["living_room"], 6) == round(10 * 13 / 15 / 2, 6)

    # A zone that may not charge at night: its budget is left to PV
    cfg.solar.zones["living_room"].night = False
    plan = make_plan(database(tmp_path / "x"), cfg, EVENING, WATTS, no_load=set())
    assert plan.heaters == ["upstairs_guest"] and round(plan.grid_kwh, 6) == round(5 * 13 / 15, 6)


def test_plan_without_heat_need_or_temperature(tmp_path: Path):
    warm = make_plan(database(tmp_path, temperature=16.0), config(), EVENING, WATTS, no_load=set())
    assert warm.grid_kwh == 0 and warm.heaters == [] and "no zone needs heat" in warm.note

    empty = init_db(tmp_path / "empty.sqlite")
    unknown = make_plan(empty, config(), EVENING, WATTS, no_load=set())
    assert unknown.grid_kwh == 0 and unknown.temperature is None


def test_plan_skips_small_amounts_and_heaters_switched_off(tmp_path: Path):
    # Need 2 × (15 - 14) = 2 kWh, minus 0.5 × 4 kWh PV = 0: below min_kwh
    small = make_plan(database(tmp_path, temperature=14.0), config(), EVENING, WATTS, no_load=set())
    assert small.grid_kwh == 0 and "not worth charging" in small.note

    conn = database(tmp_path / "x", temperature=-10.0, surplus=None)   # no PV forecast: nothing credited
    plan = make_plan(conn, config(), EVENING, WATTS, no_load={"living_room"})
    assert plan.need_kwh == 50 and plan.heaters == ["upstairs_guest"]
    # The guest room's 25 kWh = 12.5 h, but the window ends at 07:00: 11 h, 22 kWh
    assert plan.end == datetime(2026, 11, 4, 7, 0).timestamp()
    assert plan.grid_kwh == 22 and "until 07:00" in plan.note


def test_engine_follows_the_window():
    cfg = config()
    window = (EVENING.timestamp(), (EVENING + timedelta(hours=2)).timestamp())
    grid = GridSample(power=-500, age_s=5, average=-500)
    states = initial_states()
    states, events = decide(states, cfg, EVENING, grid, watts=WATTS,
                            night_windows={"living_room": window})
    assert [(e.heater, e.on, e.reason) for e in events] == [("living_room", True, "night plan")]
    # Import doesn't shed it: the PV steps only touch heaters on for PV
    states, events = decide(states, cfg, EVENING + timedelta(hours=1), grid, watts=WATTS,
                            night_windows={"living_room": window})
    assert events == []
    states, events = decide(states, cfg, EVENING + timedelta(hours=2), grid, watts=WATTS,
                            night_windows={"living_room": window})
    assert [(e.heater, e.on, e.reason) for e in events] == [("living_room", False, "night plan done")]


def test_plan_is_held_by_the_night_plan_even_if_on_for_pv():
    states = initial_states()
    states["living_room"] = HeaterState(on=True, since=EVENING.timestamp() - 600, reason="solar")
    window = (EVENING.timestamp(), (EVENING + timedelta(hours=2)).timestamp())
    new, events = decide(states, config(), EVENING, GridSample(power=0, age_s=5, average=0),
                         watts=WATTS, night_windows={"living_room": window})
    assert events == [] and new["living_room"].reason == "night plan"


def controller_at(tmp_path: Path, mode: str) -> Controller:
    controller = Controller(Store(database(tmp_path)), notifier=Notifier(enabled=False))
    cfg = config()
    cfg.night.mode = mode
    controller.store.save_config(cfg, source="test")   # Controller.save_config would tick at the real time
    controller.refresh_inputs = lambda now: None     # keep the test's wattages and temperature
    controller.watts, controller.outdoor_mean = dict(WATTS), 5.0
    return controller


def test_controller_makes_the_plan_once_and_switches(tmp_path: Path):
    controller = controller_at(tmp_path, "on")
    controller.grid_power, controller.grid_ts = -300.0, EVENING.timestamp()
    controller.tick(EVENING)
    assert controller.night_plan is not None and controller.night_plan.grid_kwh == 18
    assert controller.states["living_room"].reason == "night plan"
    made_at = controller.night_plan.made_at
    controller.tick(EVENING + timedelta(minutes=1))
    assert controller.night_plan.made_at == made_at
    assert controller.store.load_night_plan(EVENING.date()).grid_kwh == 18


def test_shadow_plans_and_logs_without_switching(tmp_path: Path):
    controller = controller_at(tmp_path, "shadow")
    controller.grid_power, controller.grid_ts = -300.0, EVENING.timestamp()
    controller.tick(EVENING)
    assert controller.night_plan.mode == "shadow"
    assert not controller.states["living_room"].on
    logged = controller.store.conn.execute(
        "SELECT heater, reason, dry_run FROM control_events WHERE reason = 'night plan'").fetchall()
    assert sorted(logged) == [("living_room", "night plan", 1), ("upstairs_guest", "night plan", 1)]


def test_morning_report_shows_the_night_plan(tmp_path: Path):
    controller = controller_at(tmp_path, "shadow")
    controller.grid_power, controller.grid_ts = -300.0, EVENING.timestamp()
    controller.tick(EVENING)
    conn = controller.store.conn
    start = int(datetime.combine(EVENING.date(), datetime.min.time()).timestamp())
    conn.executemany("INSERT INTO energy_hourly (ts, pv_wh, import_wh, export_wh, house_wh, high_tariff, "
                     "grid_minutes, iotawatt_minutes) VALUES (?, 0, 500, 0, 500, 0, 60, 0)",
                     [(start + h * 3600,) for h in range(24)])
    text = format_report(daily_report(conn, EVENING.date()))
    assert "Night plan (shadow, not switched): 18.0 kWh from the grid." in text
