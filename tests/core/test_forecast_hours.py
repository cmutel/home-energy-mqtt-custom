from pathlib import Path

from dorfsteig_core.db import init_db
from dorfsteig_core.weather import forecast_hours

HOUR = 1_800_000_000 // 3600 * 3600


def test_newest_forecast_known_at_the_time_and_best_model(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    rows = [
        (HOUR - 7200, HOUR, "icon_ch2", 5.0, 100),
        (HOUR - 3600, HOUR, "icon_ch2", 6.0, 200),        # newer
        (HOUR + 600, HOUR, "icon_ch2", 99.0, 999),        # issued after as_of: not known yet
        (HOUR - 3600, HOUR, "best_match", 1.0, 1),        # worse model
        (HOUR - 3600, HOUR + 3600, "best_match", 7.0, 300),   # only best_match has this hour
    ]
    conn.executemany("INSERT INTO weather_forecast VALUES (?, ?, ?, ?, ?, 0)", rows)
    hours = forecast_hours(conn, HOUR, HOUR + 3 * 3600, as_of=HOUR)
    assert (hours[HOUR].model, hours[HOUR].temperature, hours[HOUR].radiation) == ("icon_ch2", 6.0, 200)
    assert hours[HOUR + 3600].model == "best_match"
    assert HOUR + 7200 not in hours
