from datetime import datetime, timezone
from pathlib import Path

import pytest

from dorfsteig_core.db import init_db
from dorfsteig_ingest import energy, influx

W = energy.UPSTAIRS_WATER_W


def test_detects_water_heater_runs():
    base = [200.0] * 20
    # 30 minutes heating, with a partial first and last minute
    upstairs = base + [200 + W * 0.3] + [200 + W] * 30 + [200 + W * 0.6] + base
    on = energy.detect_water_heater(upstairs)
    assert 29 <= sum(on) <= 32
    assert not any(on[:20]) and not any(on[-19:])


def test_ignores_short_spikes_and_other_steps():
    base = [200.0] * 20
    kettle = base + [200 + W] * 3 + base               # too short
    heater = base + [200 + 1900] * 30 + base           # a 1.9 kW heating circuit
    assert not any(energy.detect_water_heater(kettle))
    assert not any(energy.detect_water_heater(heater))


def test_turns_off_when_load_too_low_and_on_gaps():
    upstairs = [200.0] * 5 + [200 + W] * 20 + [None] + [200 + W] * 20
    on = energy.detect_water_heater(upstairs)
    assert sum(on[:25]) >= 19
    assert not any(on[26:])                             # can't tell after a gap without a new step


def test_fill_pv():
    pv = [100.0, None, None, None, None, None, None, None, None, 50.0]
    filled = energy.fill_pv(pv, [True] * 9 + [False])
    assert filled[1] == 100.0 and filled[8] == 50.0
    assert filled[3] == 100.0 and filled[6] == 50.0
    assert energy.fill_pv([None] * 13, [True] * 13)[6] == 0.0


def test_influx_wall_time():
    # InfluxDB label 2026-07-01 12:00 "UTC" is 12:00 in Zurich = 10:00 UTC
    label = int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp())
    assert influx.wall_to_utc(label) == int(datetime(2026, 7, 1, 10, tzinfo=timezone.utc).timestamp())
    winter = int(datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp())
    assert influx.wall_to_utc(winter) == winter - 3600


def test_influx_rows_average_start_and_end_of_minute():
    label = int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp())
    grid, inverter = influx.to_rows([[label, -100, 0, 1.0, 2.0], [label + 60, -300, None, 1.1, 2.0]])
    assert grid[0][1] == -200 and grid[1][1] == -300
    assert inverter == [(label - 7200, 0)]


def make_hour(conn, hour: int, grid_w: float, pv_w: float, down_w: float, imp: float, exp: float):
    """One hour of minutes with constant power and counters advancing to match."""
    for m in range(60):
        ts = hour + m * 60
        frac = (m + 1) / 60
        conn.execute("INSERT INTO grid_readings VALUES (?, ?, ?, ?)",
                     (ts, grid_w, imp + max(-grid_w, 0) / 1000 * frac, exp + max(grid_w, 0) / 1000 * frac))
        conn.execute("INSERT INTO inverter_readings (ts, power_ac) VALUES (?, ?)", (ts, pv_w))
        conn.execute("INSERT INTO iotawatt_readings (ts, mains_a, mains_b, water_heater) VALUES (?, ?, 0, 0)",
                     (ts, down_w))


def test_hourly_energy_and_costs(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    # Monday 2026-10-12 08:00-10:00 local (high tariff), importing 1 kW with 500 W PV
    start = int(datetime(2026, 10, 12, 6, tzinfo=timezone.utc).timestamp())
    make_hour(conn, start, -1000, 500, 600, 100.0, 50.0)
    make_hour(conn, start + 3600, -1000, 500, 600, 101.0, 50.0)
    energy.compute_minutes(conn, start, start + 7200)
    energy.compute_hours(conn, start, start + 7200)
    row = conn.execute("SELECT pv_wh, import_wh, export_wh, house_wh, downstairs_wh, upstairs_wh, "
                       "high_tariff, "
                       "import_chf FROM energy_hourly WHERE ts = ?", (start + 3600,)).fetchone()
    pv, imp, exp, house, down, up, high, chf = row
    assert pv == pytest.approx(500) and imp == pytest.approx(1000) and exp == pytest.approx(0)
    assert house == pytest.approx(1500) and down == pytest.approx(600) and up == pytest.approx(900)
    assert high == 1 and chf == pytest.approx(0.2727)
    # First hour has no counter reading before it, so it falls back to power
    assert conn.execute("SELECT import_wh FROM energy_hourly WHERE ts = ?", (start,)).fetchone()[0] \
        == pytest.approx(1000)
