import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from dorfsteig_control import wattage
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier


def make_history(points: list[tuple[float, float]]) -> wattage.GridHistory:
    h = wattage.GridHistory()
    for ts, p in points:
        h.add(ts, p)
    return h


def steady(start: float, end: float, power: float) -> list[tuple[float, float]]:
    return [(t, power + (t % 3) * 10) for t in range(int(start), int(end), 2)]


def test_passive_estimate_switch_on():
    t = 1000.0
    history = make_history(steady(t - 60, t + 20, 1500) + steady(t + 20, t + 100, -500))
    watts, _ = wattage.evaluate_passive(wattage.PendingSwitch(t, "living_room", True), history, [])
    assert watts == pytest.approx(2000, abs=20)


def test_passive_estimate_switch_off():
    t = 1000.0
    history = make_history(steady(t - 60, t + 20, -800) + steady(t + 20, t + 100, 1100))
    watts, _ = wattage.evaluate_passive(wattage.PendingSwitch(t, "living_room", False), history, [])
    assert watts == pytest.approx(1900, abs=20)


def test_passive_estimate_skipped_when_another_switch_nearby():
    t = 1000.0
    history = make_history(steady(t - 60, t + 20, 1500) + steady(t + 20, t + 100, -500))
    assert wattage.evaluate_passive(wattage.PendingSwitch(t, "living_room", True), history, [t + 40]) is None


def test_estimates_and_drift(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    wattage.init_tables(conn)
    assert wattage.estimated_watts(conn, "living_room") == (2000.0, "default")
    wattage.record(conn, 1, "living_room", "active", 2100, 20, 2)
    assert wattage.estimated_watts(conn, "living_room") == (2100, "test")
    assert wattage.drift(conn, "living_room") is None
    for i, w in enumerate([1000, 1050, 1100]):
        wattage.record(conn, 10 + i, "living_room", "passive", w, 20, 11 + i)
    assert wattage.estimated_watts(conn, "living_room") == (1050, "passive")
    assert wattage.drift(conn, "living_room") == (2100, 1050)


def test_zone_match(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    rows = [(ts, 0.0) for ts in range(0, 600, 60)]
    conn.executemany("INSERT INTO iotawatt_readings (ts, zone_c) VALUES (?, ?)", rows)
    conn.execute("UPDATE iotawatt_readings SET zone_c = 1950 WHERE ts >= 300")
    assert wattage.zone_match(conn, 240, 480) == ("zone_c", 1950)


def run_fake_test(
    tmp_path: Path, monkeypatch, heater: str, draw: float
) -> tuple[Controller, wattage.TestStatus]:
    """Run an active test against a grid that drops by `draw` W while `heater` is on."""
    for name in ("SETTLE_S", "BASELINE_S", "SWITCH_WAIT_S", "MEASURE_S"):
        monkeypatch.setattr(wattage, name, 0.3)
    monkeypatch.setattr(wattage, "PASSIVE_MIN_SAMPLES", 2)

    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    controller.tick(datetime.now())

    async def feed():
        while True:
            on = controller.states[heater].on
            controller.on_grid_power(1500.0 - (draw if on else 0.0))
            await asyncio.sleep(0.02)

    async def main():
        feeder = asyncio.create_task(feed())
        status = await wattage.run_test(controller, heater)
        feeder.cancel()
        return status

    return controller, asyncio.run(main())


def test_active_test(tmp_path: Path, monkeypatch):
    controller, status = run_fake_test(tmp_path, monkeypatch, "kids_bedroom", 1800)
    assert status.result == pytest.approx(1800)
    assert not controller.states["kids_bedroom"].on   # restored
    assert controller.overrides == {} and not controller.hold_solar
    assert wattage.estimated_watts(controller.store.conn, "kids_bedroom") == (1800, "test")


def test_no_load_is_not_used_as_estimate(tmp_path: Path, monkeypatch):
    # Water heater relay is on by default, but the tank is hot: toggling changes nothing
    controller, status = run_fake_test(tmp_path, monkeypatch, "downstairs_water", 0)
    assert status.result is None
    assert "tank" in status.note
    assert controller.states["downstairs_water"].on   # restored
    conn = controller.store.conn
    assert [m["method"] for m in wattage.measurements(conn)] == ["no load"]
    assert wattage.estimated_watts(conn, "downstairs_water") == (2000.0, "default")
