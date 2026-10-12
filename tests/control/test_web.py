from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dorfsteig_control.config import DailyWindow
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_control.web import create_app
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier


@pytest.fixture
def setup(tmp_path: Path):
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    controller.tick(datetime.now())
    return controller, TestClient(create_app(controller), follow_redirects=False)


@pytest.mark.parametrize("path", ["/heating/", "/heating/fragments/status", "/heating/schedules",
                                  "/heating/solar", "/heating/notifications", "/heating/wattage",
                                  "/heating/fragments/test", "/heating/history"])
def test_pages_render(setup, path):
    _, client = setup
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert "Dorfsteig" in response.text or path.startswith("/heating/fragments")


def test_boost_and_stop(setup):
    controller, client = setup
    r = client.post("/heating/boost", data={"heater": "living_room", "hours": "2"})
    assert r.status_code == 303
    assert controller.states["living_room"].on
    assert client.get("/heating/fragments/status").text.count("stop boost") == 1
    client.post("/heating/boost/stop", data={"heater": "living_room"})
    assert not controller.states["living_room"].on


def test_schedule_add_toggle_delete(setup):
    controller, client = setup
    client.post("/heating/schedules/add", data={"heater": "kids_bedroom", "kind": "daily",
                                                "start_time": "22:00", "stop_time": "02:00"})
    rule = controller.config.rules[-1]
    assert isinstance(rule, DailyWindow) and rule.start.hour == 22
    n = len(controller.config.rules)
    client.post(f"/heating/schedules/{n - 1}/toggle")
    assert not controller.config.rules[-1].enabled
    client.post(f"/heating/schedules/{n - 1}/delete")
    assert len(controller.config.rules) == n - 1
    # Survives a restart
    assert len(controller.store.load_config().rules) == n - 1


def test_invalid_rule_shows_error(setup):
    controller, client = setup
    n = len(controller.config.rules)
    r = client.post("/heating/schedules/add", data={
        "heater": "kids_bedroom", "kind": "once",
        "start_dt": "2026-01-02T10:00", "stop_dt": "2026-01-01T10:00"})
    assert "error=" in r.headers["location"]
    assert len(controller.config.rules) == n


def test_solar_settings_and_priority(setup):
    controller, client = setup
    client.post("/heating/solar", data={"export_on_w": "1200", "import_off_w": "-100",
                                        "min_switch_interval_s": "180", "stale_after_s": "120"})
    assert not controller.config.solar.enabled
    assert controller.config.solar.export_on_w == 1200
    client.post("/heating/solar/priority", data={"heater": "upstairs_guest", "action": "up"})
    assert controller.config.solar.priority[0] == "upstairs_guest"
    client.post("/heating/solar/priority", data={"heater": "upstairs_beta", "action": "add"})
    assert controller.config.solar.priority[-1] == "upstairs_beta"


def test_notification_settings(setup):
    controller, client = setup
    client.post("/heating/notifications", data={"daily_summary_at": "20:30", "midday_status_at": "",
                                                "board_offline_min": "10", "alert_repeat_h": "2",
                                                "watched_sensors": "balkon, küchen",
                                                "sensor_offline_min": "45"})
    n = controller.config.notify
    assert n.daily_summary_at.hour == 20 and n.midday_status_at is None
    assert n.board_offline_after_s == 600 and n.alert_repeat_s == 7200
    assert n.watched_sensors == ["balkon", "küchen"] and n.sensor_offline_after_s == 2700


def test_locked_with_legacy_config(tmp_path: Path):
    legacy = tmp_path / "config.json"
    legacy.write_text('{"upstairs water": {"always_on": true}}')
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), legacy_config=legacy,
                            notifier=Notifier(enabled=False))
    client = TestClient(create_app(controller), follow_redirects=False)
    r = client.post("/heating/boost", data={"heater": "living_room", "hours": "1"})
    assert "error=" in r.headers["location"]


def test_test_all_skips_water_heaters(setup, monkeypatch):
    controller, client = setup
    started = []
    monkeypatch.setattr(controller, "start_tests", started.extend)
    r = client.post("/heating/wattage/test", data={"heater": "all"})
    assert r.status_code == 303
    assert len(started) == 10 and not any("water" in h for h in started)
    client.post("/heating/wattage/test", data={"heater": "upstairs_water"})
    assert started[-1] == "upstairs_water"   # still testable on its own
