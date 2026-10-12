from datetime import date, datetime

import pytest

from dorfsteig_control import replay
from dorfsteig_control.replay import HEATER_W, Minute, Policy, Tank

START = int(datetime(2026, 6, 1, tzinfo=replay.TZ).timestamp())


def day(surplus: float, sunny_hours: range, use_at: int = 20, use_wh: float = 3000) -> list[Minute]:
    """One day: `surplus` W of export during sunny hours, and hot water used at `use_at`."""
    minutes = []
    for i in range(1440):
        ts = START + i * 60
        local = datetime.fromtimestamp(ts, replay.TZ)
        demand = use_wh * 60 / 30 if local.hour == use_at and local.minute < 30 else 0.0
        minutes.append(Minute(ts=ts, local=local, g0=surplus if local.hour in sunny_hours else -300.0,
                              demand={"downstairs": demand, "upstairs": 0.0},
                              import_price=0.25, export_price=0.1))
    return minutes


def test_tank_reheats_after_hysteresis():
    tank = Tank("t", last_full=0)
    assert tank.step(replay.HYSTERESIS_WH / 2 * 60, 60) == 0        # small use: thermostat waits
    assert tank.step(replay.HYSTERESIS_WH * 60, 120) == HEATER_W
    tank.relay = False
    assert tank.step(0, 180) == 0                                   # relay off: no heating


def test_pv_policy_moves_heating_into_sunny_hours():
    # Two days: water used in the evening, reheated next day from PV
    # 4 kW export: enough for a 2950 W heater plus margin
    runs = [day(4000, range(10, 15)) + [
        Minute(m.ts + 86400, datetime.fromtimestamp(m.ts + 86400, replay.TZ), m.g0, m.demand, 0.25, 0.1)
        for m in day(4000, range(10, 15))]]
    always = replay.replay(Policy("always", pv=False, backup_at=None), runs, {})
    pv = replay.replay(Policy("pv", backup_at=16, full_since_hour=10), runs, {})
    assert pv.heater_kwh == pytest.approx(always.heater_kwh, abs=0.1)   # same hot water
    assert pv.heater_pv_kwh > always.heater_pv_kwh
    assert pv.net_chf < always.net_chf
    assert pv.cold_minutes == 0


def test_backup_heats_when_no_sun():
    runs = [day(-300, range(0)) + [
        Minute(m.ts + 86400, datetime.fromtimestamp(m.ts + 86400, replay.TZ), m.g0, m.demand, 0.25, 0.1)
        for m in day(-300, range(0))]]
    result = replay.replay(Policy("pv", backup_at=16, full_since_hour=10), runs, {})
    assert result.max_deficit_kwh < 4 and result.cold_minutes == 0


def test_load_forecast_uses_previous_day_issue(tmp_path):
    from dorfsteig_core.db import init_db
    conn = init_db(tmp_path / "t.sqlite")
    noon = int(datetime(2026, 6, 2, 12, tzinfo=replay.TZ).timestamp())
    conn.executemany("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 20, ?, 0)",
                     [(noon - 86400, noon, 500.0), (noon - 3600, noon, 100.0)])   # the late issue is ignored
    expected = 500 * replay.PV_PER_RADIATION / 1000
    assert replay.load_forecast(conn) == {date(2026, 6, 2): pytest.approx(expected)}


def test_rooms_fill_surplus_by_priority_and_respect_capacity():
    run = day(3500, range(10, 16))
    temps = {run[0].local.date(): 5.0}
    heaters = {"a": 2000.0, "b": 1500.0, "c": 1000.0}
    policy = replay.RoomPolicy("t", temp_limit=15, cold=-5, max_hours=2)
    r = replay.replay_rooms(policy, [run], heaters, temps)
    # 3500 W surplus fits a (2000) + b (1500) but not c; each core takes 2 h × 50% = 1 h of power,
    # and once a and b are full, c gets the surplus
    assert r.heat_kwh == pytest.approx(2.0 + 1.5 + 1.0, rel=0.05)
    assert r.import_kwh < 0.1
    # Too warm: nothing
    warm = replay.replay_rooms(policy, [run], heaters, {run[0].local.date(): 16.0})
    assert warm.heat_kwh == 0
