import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dorfsteig_control.budget import Budget, budget_day, day_start, zone_budgets
from dorfsteig_control.config import ControlConfig, SolarParams, Zone
from dorfsteig_control.engine import Event
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier

EIGHT_PM = time(20, 0)


def test_zone_budget_depends_on_the_temperature():
    upstairs = Zone(heats_below=11.0, kwh_per_degree=0.35)
    assert upstairs.budget_kwh(12.0) == 0.0
    assert round(upstairs.budget_kwh(5.0), 6) == 2.1


def test_budget_share_and_done():
    assert Budget(4.0, 1.0).share == 0.25
    assert not Budget(4.0, 3.9).done(max_share=1.0)
    assert Budget(4.0, 4.0).done(max_share=1.0)
    assert not Budget(4.0, 4.0).done(max_share=1.2)
    assert Budget(0.0, 0.0).done(max_share=1.2)       # no budget: nothing to do


def test_budget_day_runs_from_eight_pm():
    assert budget_day(datetime(2026, 11, 3, 19, 59), EIGHT_PM) == date(2026, 11, 3)
    assert budget_day(datetime(2026, 11, 3, 20, 0), EIGHT_PM) == date(2026, 11, 4)
    assert day_start(date(2026, 11, 4), EIGHT_PM) == datetime(2026, 11, 3, 20, 0)


def test_every_zone_has_settings_and_old_configs_load():
    solar = SolarParams(priority=["living_room", "upstairs_guest"],
                        zones={"living_room": Zone(kwh_per_degree=0.6)})
    assert solar.zones["upstairs_guest"] == Zone()
    assert solar.need_kwh(5.0) == (0.6 + 0.4) * 10
    # Saved before zones existed: temp_limit and night.kwh_per_degree are ignored, zones get the defaults
    old = {"solar": {"priority": ["living_room"], "temp_limit": 15.0}, "night": {"kwh_per_degree": 1.7}}
    config = ControlConfig.model_validate_json(json.dumps(old))
    assert config.solar.zones == {"living_room": Zone()}
    assert zone_budgets(config.solar, None, {}) is None


def test_controller_counts_what_each_zone_got_since_eight_pm(tmp_path: Path):
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=Notifier(enabled=False))
    zones = ["living_room", "upstairs_guest"]
    controller.config = ControlConfig(solar=SolarParams(mode="fit", priority=zones))
    controller.watts = {"living_room": 1500.0, "upstairs_guest": 2000.0}
    controller.outdoor_mean = 5.0
    evening = datetime(2026, 11, 3, 20, 0)

    def switch(when: datetime, heater: str, on: bool, reason: str) -> None:
        controller.store.log_events([Event(when.timestamp(), heater, on, reason, None)])

    switch(evening - timedelta(hours=1), "living_room", True, "export")       # the day before: not counted
    switch(evening, "living_room", False, "import")
    switch(evening + timedelta(hours=1), "upstairs_guest", True, "night plan")
    switch(evening + timedelta(hours=3), "upstairs_guest", False, "night plan done")
    switch(evening + timedelta(hours=16), "living_room", True, "export")      # 12:00, still on

    budgets = controller.zone_budgets(evening + timedelta(hours=17), dry_run=False)
    assert budgets == {"living_room": Budget(4.0, 1.5), "upstairs_guest": Budget(4.0, 4.0)}
