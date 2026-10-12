from datetime import date, datetime, timedelta
from pathlib import Path

from dorfsteig_core.db import init_db
from dorfsteig_core.tariff import TZ
from dorfsteig_forecast import daily

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=TZ)
PV = {"factors": [[10.0] * 24] * 52, "hours": 1, "first": 0, "last": 0, "daily_mae_kwh": 0}
BASE = {"wh_per_hour": [500.0] * 24, "days": 1, "from_temperature": 15}


def setup(tmp_path: Path):
    """A forecast issued at 10:00: 100 W/m² from 09:00 to 15:00, 8 °C, for three days."""
    conn = init_db(tmp_path / "t.sqlite")
    issued = int(NOW.replace(minute=0).timestamp())
    midnight = datetime.combine(NOW.date(), datetime.min.time(), TZ)
    for h in range(72):
        t = midnight + timedelta(hours=h)
        radiation = 100.0 if 9 <= t.hour < 15 else 0.0
        conn.execute("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 8, ?, 0)",
                     (issued, int(t.timestamp()), radiation))
    now = NOW.timestamp()
    daily.save_model(conn, "pv", PV, now)
    daily.save_model(conn, "base_load", BASE, now)
    return conn


def test_day_forecast_sums_pv_and_surplus(tmp_path: Path):
    conn = setup(tmp_path)
    day = daily.forecast_day(conn, NOW.date(), NOW.timestamp(), PV, BASE)
    # 6 hours × 1000 Wh of PV; surplus 6 × (1000 - 500); base 24 × 500
    assert (day.pv_kwh, day.surplus_kwh, day.base_kwh, day.temperature) == (6, 3, 12, 8)


def test_update_writes_once_per_weather_forecast(tmp_path: Path):
    conn = setup(tmp_path)
    assert daily.update(conn, NOW)
    days = conn.execute("SELECT day, pv_kwh FROM day_forecast ORDER BY day").fetchall()
    assert days == [(NOW.date().isoformat(), 6), ((NOW.date() + timedelta(days=1)).isoformat(), 6),
                    ((NOW.date() + timedelta(days=2)).isoformat(), 6)]
    assert conn.execute("SELECT COUNT(*) FROM pv_forecast").fetchone()[0] == 72
    assert not daily.update(conn, NOW + timedelta(minutes=10))     # same weather forecast: nothing to do


def test_too_few_forecast_hours(tmp_path: Path):
    conn = setup(tmp_path)
    assert daily.forecast_day(conn, date(2026, 11, 6), NOW.timestamp(), PV, BASE) is None


def test_backfill_hours_uses_the_forecast_made_24h_ahead(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    noon = int(datetime(2026, 11, 3, 12, tzinfo=TZ).timestamp())
    conn.execute("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 8, 100, 0)", (noon - 86400, noon))
    conn.execute("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 8, 999, 0)", (noon - 3600, noon))
    daily.backfill_hours(conn, date(2026, 11, 3), PV)
    assert conn.execute("SELECT model, ts, issued, pv_wh FROM pv_forecast").fetchall() == [
        ("ratio", noon, noon - 86400, 1000.0)]
