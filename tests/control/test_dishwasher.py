import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from dorfsteig_control import dishwasher as dw
from dorfsteig_control.service import Controller
from dorfsteig_control.store import Store
from dorfsteig_core.db import init_db
from dorfsteig_core.notify import Notifier

# Real one-minute profiles (W) from the IoTaWatt dishwasher channel.
# 12 Nov 2026, the first run with the new element: 1.47 kWh, no flicker.
NORMAL = [7, 5, 268, 1900, 55, 56, 42, 43, 1616, 1900, 1907, 1909, 1897, 1896, 1899, 1900, 1906, 1908,
          1903, 1900, 1896, 1894, 1821, 58, 63, 32, 1393, 1904, 1903, 861, 63, 716, 1905, 1898, 1658,
          61, 64, 64, 52, 54, 54, 55, 64, 64, 55, 54, 54, 50, 63, 63, 63, 61, 52, 54, 54, 54, 56, 63,
          63, 63, 53, 54, 53, 53, 52, 63, 63, 63, 57, 53, 53, 53, 53, 57, 63, 63, 63, 51, 53, 53, 53,
          53, 63, 63, 63, 55, 54, 53, 53, 50, 62, 63, 63, 63, 50, 47, 11, 3, 29, 52, 53, 53, 53, 33,
          28, 12, 3, 46, 1170, 1912, 1908, 1910, 1909, 1914, 1911, 1906, 1903, 1903, 1904, 1911, 1910,
          1910, 1916, 1916, 1916, 1915, 1914, 1913, 1916, 1760, 67, 67, 67, 68, 70, 15, 4, 4, 4, 5, 3,
          2, 2, 2, 2]
# 1 Nov 2026 morning: the main wash flickers, the final rinse never heats.
FLICKER_MAIN_WASH = [7, 6, 193, 1850, 56, 39, 40, 1535, 1903, 1909, 1911, 1508, 1608, 1469, 1890, 1906,
                     1906, 1901, 1896, 1897, 1897, 400, 752, 1402, 898, 400, 62, 37, 978, 1893, 604, 1592]
# 9 May 2024: the thermostat cycling while it holds the final rinse temperature.
HOLD_PHASE = [52, 1307, 1899, 1893, 1888, 1888, 1893, 1641, 1350, 1624, 1326, 1338, 1297, 1294, 1277,
              1258, 1174, 1048, 1149, 1221, 1054, 955, 1176, 1023, 906, 400, 10]
# 20 Oct 2023: two short heating blocks a minute apart.
ADJACENT_BLOCKS = [19, 1789, 1902, 1905, 1191, 304, 783, 1931, 1921, 1891, 74]


def test_flicker_minutes():
    assert dw.flicker_minutes(NORMAL) == 0
    assert dw.flicker_minutes(FLICKER_MAIN_WASH) == 3     # 1508, 1608, 1469
    assert dw.flicker_minutes(HOLD_PHASE) == 0
    assert dw.flicker_minutes(ADJACENT_BLOCKS) == 1     # 1191 only, below FLICKER_ALERT_MIN


def no_heat(watts: Sequence[float]) -> list[float]:
    """The same program with the element dead: the pump's ~60 W only."""
    return [min(w, 66) for w in watts]


def run_at(when: datetime, watts: Sequence[float], scale: float = 1.0) -> dw.Run:
    return dw.Run(start=when.timestamp(), watts=tuple(w * scale for w in watts))


def history(now: datetime, recent_scale: float, recent_days: int = 10) -> list[dw.Run]:
    """A run a day at this time of year in 2025, and the last `recent_days` days scaled."""
    last_year = [run_at(now - timedelta(days=365 + d), NORMAL) for d in range(-20, 21)]
    recent = [run_at(now - timedelta(days=d), NORMAL, recent_scale) for d in range(recent_days, 0, -1)]
    return last_year + recent


def keys(alerts: list[dw.Alert]) -> set[str]:
    return {a.key for a in alerts}


NOW = datetime(2026, 11, 20, 23, 0)


def test_normal_run_no_alerts():
    runs = history(NOW, 1.0)
    assert dw.seasonal_normal(runs, NOW.timestamp()) == runs[0].wh
    assert dw.check_run(runs, runs[-1]) == []


def test_heater_missing_and_flicker_alert_on_one_run():
    runs = history(NOW, 1.0)
    dead = run_at(NOW, no_heat(NORMAL))
    assert keys(dw.check_run([*runs, dead], dead)) == {"dishwasher-heater"}
    flickering = run_at(NOW, FLICKER_MAIN_WASH + NORMAL[len(FLICKER_MAIN_WASH):])
    assert keys(dw.check_run([*runs, flickering], flickering)) == {"dishwasher-flicker"}


def test_drift_needs_a_lasting_change():
    # One big holiday load: the median of the last runs doesn't move
    runs = history(NOW, 1.0)
    big = run_at(NOW, NORMAL, 1.5)
    assert dw.check_run([*runs, big], big) == []
    # Every run 25% higher for ten days: the sensor may read too cold
    runs = history(NOW, 1.25)
    alerts = dw.check_run(runs, runs[-1])
    assert keys(alerts) == {"dishwasher-drift"}
    assert "more energy" in alerts[0].title
    # 10% higher is within the seasonal variation
    runs = history(NOW, 1.10)
    assert dw.check_run(runs, runs[-1]) == []


def test_no_drift_without_a_previous_year():
    runs = [run_at(NOW - timedelta(days=d), NORMAL, 1.3) for d in range(10, -1, -1)]
    assert dw.seasonal_normal(runs, NOW.timestamp()) is None
    assert dw.check_run(runs, runs[-1]) == []


# ---- reading runs from the database

def insert(conn, start: datetime, watts: Sequence[float]) -> None:
    t0 = int(start.timestamp())
    conn.executemany("INSERT OR REPLACE INTO iotawatt_readings (ts, dishwasher) VALUES (?, ?)",
                     [(t0 + 60 * i, w) for i, w in enumerate(watts)])


def idle(conn, start: datetime, minutes: int) -> None:
    insert(conn, start, [0.0] * minutes)


def test_find_runs_across_missing_readings(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    start = datetime(2026, 10, 9, 22, 0)
    idle(conn, start - timedelta(hours=1), 60)
    insert(conn, start, NORMAL)
    # An hour of readings missing in the middle of the run (as on 9 Oct 2026)
    conn.execute("DELETE FROM iotawatt_readings WHERE ts BETWEEN ? AND ?",
                 (int((start + timedelta(minutes=40)).timestamp()),
                  int((start + timedelta(minutes=100)).timestamp())))
    idle(conn, start + timedelta(minutes=len(NORMAL)), 30)
    runs = dw.find_runs(conn)
    last_active = max(i for i, w in enumerate(NORMAL) if w > dw.ACTIVE_W)
    assert len(runs) == 1 and runs[0].minutes == last_active + 1
    assert dw.finished(conn, runs[0])


def test_controller_alerts_once_per_run(tmp_path: Path):
    class FakeNotifier(Notifier):
        def __init__(self):
            super().__init__(enabled=False)
            self.titles: list[str] = []

        async def send(self, title, message, priority=0):
            self.titles.append(title)
            return True

    notifier = FakeNotifier()
    controller = Controller(Store(init_db(tmp_path / "t.sqlite")), dry_run=True, notifier=notifier)
    conn = controller.store.conn
    start = datetime(2026, 11, 1, 21, 41)
    insert(conn, start, no_heat(NORMAL))
    end = start + timedelta(minutes=len(NORMAL))

    def check(at: datetime) -> None:
        asyncio.run(controller.check_dishwasher(at.timestamp()))

    idle(conn, end, 10)
    check(end + timedelta(minutes=10))      # not finished yet
    assert notifier.titles == []
    idle(conn, end, 25)
    check(end + timedelta(minutes=25))
    check(end + timedelta(minutes=30))      # same run: no second message
    assert notifier.titles == ["Dishwasher heater missing"]

    # The next run is normal, which clears the alert; the one after is dead again
    for day in (1, 2):
        insert(conn, start + timedelta(days=day), NORMAL if day == 1 else no_heat(NORMAL))
        idle(conn, end + timedelta(days=day), 25)
        check(end + timedelta(days=day, minutes=25))
    assert notifier.titles == ["Dishwasher heater missing"] * 2
