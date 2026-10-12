import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from dorfsteig_control.config import ControlConfig, NotifySettings
from dorfsteig_control.report import daily_report, format_report
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier


def add_hour(conn, ts: int, high: bool):
    """1.5 kWh used (1 kWh downstairs, 0.5 kWh upstairs), 1 kWh PV of which 0.4 kWh exported."""
    price, export_price = (0.2727, 0.0963) if high else (0.2511, 0.0923)
    conn.execute(
        "INSERT INTO energy_hourly (ts, pv_wh, import_wh, export_wh, house_wh, downstairs_wh, "
        "downstairs_water_wh, upstairs_wh, upstairs_water_wh, upstairs_other_wh, high_tariff, "
        "import_price, export_price, import_chf, export_chf, grid_minutes, iotawatt_minutes) "
        "VALUES (?, 1000, 900, 400, 1500, 1000, 600, 500, 0, 300, ?, ?, ?, ?, ?, 60, 60)",
        (ts, int(high), price, export_price, 0.9 * price, 0.4 * export_price),
    )
    # Downstairs: water heater 600 W, zone A 300 W, rest unmeasured; upstairs: washer 200 W
    conn.executemany(
        "INSERT INTO iotawatt_readings (ts, mains_a, mains_b, water_heater, zone_a, clothes_washer) "
        "VALUES (?, 1000, 0, 600, 300, 200)", [(ts + m * 60,) for m in range(60)],
    )


@pytest.fixture
def conn(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    Store(conn)
    return conn


def test_report_costs_all_usage_and_keeps_revenue_separate(conn):
    day = date(2026, 10, 12)   # Monday
    add_hour(conn, int(datetime(2026, 10, 12, 3).timestamp()), high=False)
    add_hour(conn, int(datetime(2026, 10, 12, 10).timestamp()), high=True)
    add_hour(conn, int(datetime(2026, 10, 13, 10).timestamp()), high=True)   # next day: ignored
    r = daily_report(conn, day)
    assert r.hours == 2
    assert r.house_kwh == pytest.approx(3.0) and r.pv_kwh == pytest.approx(2.0)
    assert r.usage_chf == pytest.approx(1.5 * (0.2727 + 0.2511))
    assert r.export_chf == pytest.approx(0.4 * (0.0963 + 0.0923))
    kwh = {name: v[0] for name, v in r.circuits.items()}
    assert kwh["Water heater downstairs"] == pytest.approx(1.2)
    assert kwh["Zone A (kitchen, bath, dining)"] == pytest.approx(0.6)
    assert kwh["Downstairs other"] == pytest.approx(0.2)         # washer isn't on the downstairs mains
    assert kwh["Washer ↑"] == pytest.approx(0.4)
    assert kwh["Upstairs other ↑"] == pytest.approx(0.6)
    assert sum(kwh.values()) == pytest.approx(r.house_kwh)
    assert sum(v[1] for v in r.circuits.values()) == pytest.approx(r.usage_chf)
    text = format_report(r)
    assert "revenue CHF 0.08" in text and "Usage 3.0 kWh: CHF 0.79" in text
    assert len(text) < 1024   # Pushover limit


def test_report_without_data(conn):
    assert "No energy data" in format_report(daily_report(conn, date(2026, 1, 1)))


def test_morning_report_sent_once(conn):
    sent = []

    class Recorder(Notifier):
        async def send(self, title, message, priority=0):
            sent.append(title)

    controller = Controller(Store(conn), notifier=Recorder(enabled=False))
    controller.config = ControlConfig(notify=NotifySettings(daily_summary_at=None, morning_report_at="06:00"))

    def monitor_at(hour: int, minute: int) -> None:
        now = datetime(2026, 10, 13, hour, minute)
        controller.grid_ts = now.timestamp()
        controller.board_polls = {b: {"ts": now.timestamp()} for b in ("upstairs", "downstairs")}
        asyncio.run(controller.monitor(now))

    monitor_at(5, 59)
    monitor_at(6, 0)
    monitor_at(6, 5)
    assert sent == ["Energy Mon 12 Oct"]
