from datetime import datetime, timezone
from pathlib import Path

from dorfsteig_core.db import init_db
from dorfsteig_ingest import influx, weather

STATION_CSV = """station_abbr;reference_timestamp;tre200h0;tre200hn;gre000h0
XYZ;12.10.2026 06:00;3.4;2.9;0
XYZ;12.10.2026 07:00;4.1;3.0;12
XYZ;12.10.2026 08:00;;;
"""


def test_parse_station_csv():
    rows = weather.parse_station(STATION_CSV)
    six = int(datetime(2026, 10, 12, 6, tzinfo=timezone.utc).timestamp())
    assert rows == [(six, 3.4, 0.0), (six + 3600, 4.1, 12.0)]


def test_store_forecast_keeps_each_issue(tmp_path: Path):
    conn = init_db(tmp_path / "t.sqlite")
    hourly = {"time": [3600, 7200]}
    for api_name in weather.MODELS:
        hourly |= {f"temperature_2m_{api_name}": [1.0, 2.0], f"shortwave_radiation_{api_name}": [0, 100],
                   f"cloud_cover_{api_name}": [50, None]}
    hourly["temperature_2m_meteoswiss_icon_ch1"] = [1.0, None]   # CH1 runs out sooner
    assert weather.store_forecast(conn, hourly, issued=0) == 5
    weather.store_forecast(conn, hourly, issued=3600)
    assert conn.execute("SELECT COUNT(DISTINCT issued) FROM weather_forecast").fetchone()[0] == 2


def test_influx_climate_rows_join_dust_and_convert_time():
    label = int(datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp())   # 12:00 local = 11:00 UTC
    climate = [{"tags": {"room": "balkon"}, "values": [[label, 2.5, 80.0, "6089660"]]}]
    dust = [{"tags": {"room": "balkon"}, "values": [[label, 4.0, 9.0]]}]
    assert influx.climate_rows(climate, dust) == [(label - 3600, "balkon", "6089660", 4.0, 9.0, 2.5, 80.0)]
