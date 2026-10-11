import asyncio
import time
from datetime import datetime
from pathlib import Path

from dorfsteig_control.config import ControlConfig, NotifySettings
from dorfsteig_control.engine import Event
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_control.summary import day_summary, format_summary, heater_intervals
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier


class FakeNotifier(Notifier):
    def __init__(self):
        super().__init__(enabled=False)
        self.messages: list[str] = []

    async def send(self, title, message, priority=0):
        self.messages.append(title)
        return True


def ts(h, m=0, day=8):
    return datetime(2026, 1, day, h, m).timestamp()


def test_heater_intervals_across_midnight(tmp_path: Path):
    store = Store(init_db(tmp_path / "t.sqlite"))
    store.log_events([
        Event(ts(23, day=7), "kids_bedroom", True, "schedule", None),   # on since yesterday
        Event(ts(2), "kids_bedroom", False, "schedule ended", None),
        Event(ts(12), "living_room", True, "export", 2000),
        Event(ts(13, 30), "living_room", False, "import", -300),
        Event(ts(15), "living_room", True, "export", 2000),            # still on at 16:00
    ])
    totals = heater_intervals(store.conn, ts(0), ts(16), dry_run=False)
    assert totals["kids_bedroom"]["scheduled"] == 2 * 3600
    assert totals["living_room"]["solar"] == 2.5 * 3600

    summary = day_summary(store.conn, datetime(2026, 1, 8, 16))
    assert summary.heaters["living_room"].kwh == 5.0      # 2.5 h × 2000 W default
    assert summary.solar_heating_kwh == 5.0
    assert "Kids bedroom: 2.0 h" in format_summary(summary)


def make_controller(tmp_path: Path) -> tuple[Controller, FakeNotifier]:
    notifier = FakeNotifier()
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), notifier=notifier)
    controller.config = ControlConfig(notify=NotifySettings(daily_summary_at=None))
    controller.store.save_config(controller.config, "test")
    return controller, notifier


def test_alerts_raise_once_and_clear(tmp_path: Path):
    controller, notifier = make_controller(tmp_path)
    controller.grid_ts = time.time() - 600
    controller.started = time.time() - 600
    asyncio.run(controller.monitor())
    asyncio.run(controller.monitor())
    assert sorted(notifier.messages) == [
        "Downstairs board offline", "No grid power data", "Upstairs board offline"]

    notifier.messages.clear()
    controller.on_grid_power(100)
    controller.board_polls["upstairs"] = {"ts": time.time()}
    asyncio.run(controller.monitor())
    assert sorted(notifier.messages) == ["Grid power data is back", "Upstairs board back online"]


def test_daily_summary_sent_once(tmp_path: Path):
    controller, notifier = make_controller(tmp_path)
    controller.config = ControlConfig(notify=NotifySettings(daily_summary_at="21:00"))
    controller.store.save_config(controller.config, "test")
    controller.board_polls = {b: {"ts": time.time()} for b in ("upstairs", "downstairs")}
    controller.on_grid_power(0)
    asyncio.run(controller.monitor(datetime.now().replace(hour=20, minute=59)))
    assert notifier.messages == []
    asyncio.run(controller.monitor(datetime.now().replace(hour=21, minute=1)))
    asyncio.run(controller.monitor(datetime.now().replace(hour=21, minute=2)))
    assert [m for m in notifier.messages if m.startswith("Summary")] and len(notifier.messages) == 1
