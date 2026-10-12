from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from dorfsteig_core.db import init_db
from dorfsteig_core.tariff import TZ
from dorfsteig_forecast import load, pv

START = datetime(2026, 3, 1, tzinfo=UTC)


def hours(days: int):
    for h in range(days * 24):
        yield int((START + timedelta(hours=h)).timestamp())


def test_pv_factor_per_week_and_hour(tmp_path: Path):
    """PV = 10 × radiation at noon UTC and 5 × at 15:00; no radiation at night."""
    conn = init_db(tmp_path / "t.sqlite")
    for ts in hours(60):
        hour = datetime.fromtimestamp(ts, UTC).hour
        radiation = {12: 400.0, 15: 200.0}.get(hour, 0.0)
        factor = {12: 10.0, 15: 5.0}.get(hour, 0.0)
        conn.execute("INSERT INTO weather_forecast VALUES (?, ?, 'icon_ch2', 10, ?, 0)",
                     (ts - 86400, ts, radiation))
        conn.execute("INSERT INTO energy_hourly (ts, pv_wh, high_tariff, grid_minutes, iotawatt_minutes) "
                     "VALUES (?, ?, 0, 60, 0)", (ts, factor * radiation))
    params = pv.fit(conn)
    noon = int(datetime(2026, 3, 20, 12, tzinfo=UTC).timestamp())
    assert pv.predict_wh(params, noon, 300) == 3000
    assert pv.predict_wh(params, noon + 3 * 3600, 300) == 1500
    assert pv.predict_wh(params, noon - 12 * 3600, 300) == 0     # midnight: never any radiation
    assert params["daily_mae_kwh"] == 0


def test_base_load_and_heating(tmp_path: Path):
    """Days at 20 °C use 24 kWh; days at 5 °C use 24 + 2 × 10 kWh."""
    conn = init_db(tmp_path / "t.sqlite")
    for day in range(60):
        temperature = 20.0 if day % 2 else 5.0
        extra_wh = 0.0 if day % 2 else 2000 * 10 / 24
        midnight = datetime.combine(date(2026, 5, 1) + timedelta(days=day), time(), TZ)   # no DST change
        for h in range(24):
            ts = int((midnight + timedelta(hours=h)).timestamp())
            conn.execute("INSERT INTO weather VALUES (?, 'meteoswiss', ?, 0, NULL, 0)", (ts, temperature))
            conn.execute("INSERT INTO energy_hourly (ts, house_wh, high_tariff, grid_minutes, "
                         "iotawatt_minutes) VALUES (?, ?, 0, 60, 0)", (ts, 1000 + extra_wh))
    base = load.fit_base_load(conn, datetime(2026, 7, 1, tzinfo=TZ))
    assert base["days"] == 30 and base["wh_per_hour"] == [1000.0] * 24
    heating = load.fit_heating(conn)
    assert round(heating["kwh_per_degree"], 6) == 2 and round(heating["base_kwh"], 6) == 24
