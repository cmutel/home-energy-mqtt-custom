import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from dorfsteig_control.config import AlwaysOn, ControlConfig
from dorfsteig_control.engine import HeaterState
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_control.web import create_app
from dorfsteig_core.db import init_db


def make_store(tmp_path: Path) -> Store:
    return Store(init_db(tmp_path / "test.sqlite"))


def test_config_and_state_persist(tmp_path):
    store = make_store(tmp_path)
    config = ControlConfig(rules=[AlwaysOn(heater="living_room")])
    store.save_config(config, source="test")
    assert store.load_config() == config

    states = store.load_states()
    states["living_room"] = HeaterState(on=True, since=123.0, reason="schedule")
    store.save_states(states)
    assert make_store(tmp_path).load_states()["living_room"] == states["living_room"]


def test_tick_switches_logs_and_persists(tmp_path):
    store = make_store(tmp_path)
    controller = Controller(store)
    controller.tick(datetime.now())
    assert store.load_states()["upstairs_water"].on
    assert {e["heater"] for e in store.recent_events()} == {"upstairs_water", "downstairs_water"}


def test_dry_run_does_not_persist_state(tmp_path):
    store = make_store(tmp_path)
    controller = Controller(store, dry_run=True)
    controller.tick(datetime.now())
    assert controller.states["upstairs_water"].on
    assert not store.load_states()["upstairs_water"].on
    assert all(e["dry_run"] for e in store.recent_events())


def test_compare_with_legacy(tmp_path):
    (tmp_path / "upstairs.json").write_text('{"red": 0, "yellow": 0, "green": 0, "blue": 1}')
    (tmp_path / "downstairs.json").write_text('{"1": {"red": 1, "yellow": 0, "green": 0, "blue": 0}, '
                                              '"2": {"red": 0, "yellow": 0, "green": 0, "blue": 1}}')
    controller = Controller(make_store(tmp_path), dry_run=True, compare_dir=tmp_path)
    controller.tick(datetime.now())
    assert controller.legacy_mismatch is not None
    assert list(controller.legacy_mismatch) == ["downstairs"]


def test_board_endpoints(tmp_path):
    controller = Controller(make_store(tmp_path))
    client = TestClient(create_app(controller))

    # Before the first tick the boards get 503 and fall back to their defaults
    assert client.get("/upstairs.json").status_code == 503

    controller.tick(datetime.now())
    assert client.get("/upstairs.json").json() == {"red": 0, "yellow": 0, "green": 0, "blue": 1}
    assert client.get("/downstairs.json").json()["2"]["blue"] == 1
    assert set(controller.board_polls) == {"upstairs", "downstairs"}

    status = client.get("/heating/api/status").json()
    assert status["mode"] == "live"
    assert len(status["heaters"]) == 12

    # Control loop stalls → 503 again
    controller.last_tick = time.time() - 600
    assert client.get("/downstairs.json").status_code == 503
    assert client.get("/healthz").status_code == 503
