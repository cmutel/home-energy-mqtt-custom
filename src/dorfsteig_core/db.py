"""SQLite schema and connection helper shared by all dorfsteig services.

Each service opens its own connection. WAL mode lets the ingest writers,
the controller and Datasette use the same file concurrently.
"""

import sqlite3
from pathlib import Path

from dorfsteig_core.settings import settings

# Channels are fixed (see Channel docstring for definitions).
# Wide table: one row per timestamp, one column per channel.
# ts is Unix epoch seconds (INTEGER for efficient range queries).
IOTAWATT_READINGS = """
CREATE TABLE IF NOT EXISTS iotawatt_readings (
    ts             INTEGER NOT NULL PRIMARY KEY,
    input_0        REAL,   -- Volts
    mains_a        REAL,   -- Watts
    mains_b        REAL,   -- Watts
    oven           REAL,   -- Watts
    zone_a         REAL,   -- Watts
    zone_b         REAL,   -- Watts
    zone_c         REAL,   -- Watts
    zone_d         REAL,   -- Watts
    zone_e         REAL,   -- Watts
    zone_f         REAL,   -- Watts
    clothes_washer REAL,   -- Watts
    clothes_dryer  REAL,   -- Watts
    water_heater   REAL,   -- Watts
    dishwasher     REAL,   -- Watts
    stovetop       REAL    -- Watts
);
"""

# One-minute averages from the SolarEdge inverter via MBMD/MQTT.
# Cumulative fields (export_total) store the last value in the minute.
INVERTER_READINGS = """
CREATE TABLE IF NOT EXISTS inverter_readings (
    ts           INTEGER NOT NULL PRIMARY KEY,
    power_dc     REAL,   -- W, average
    voltage_dc   REAL,   -- V, average
    current_dc   REAL,   -- A, average
    power_ac     REAL,   -- W, average
    export_total REAL    -- kWh, last value in minute
);
"""

# One-minute averages from the grid meter via MBMD/MQTT.
# Cumulative fields store the last value in the minute.
GRID_READINGS = """
CREATE TABLE IF NOT EXISTS grid_readings (
    ts           INTEGER NOT NULL PRIMARY KEY,
    power        REAL,   -- W, average (positive = exporting, negative = importing)
    import_total REAL,   -- kWh, last value in minute
    export_total REAL    -- kWh, last value in minute
);
"""

# One row per sensor reading. ts comes from the payload (local time → Unix epoch).
# (ts, room) is the natural primary key since two sensors publish simultaneously.
AIR_QUALITY = """
CREATE TABLE IF NOT EXISTS air_quality (
    ts          INTEGER NOT NULL,
    room        TEXT NOT NULL,
    sensor_id   TEXT NOT NULL,
    pm25        REAL,   -- µg/m³
    pm10        REAL,   -- µg/m³
    temperature REAL,   -- °C
    humidity    REAL,   -- %
    signal      INTEGER, -- dBm
    PRIMARY KEY (ts, room)
);
"""

# Derived by dorfsteig_ingest.energy from the tables above; can be rebuilt.
# Upstairs = whole house (MBMD: PV - grid) - downstairs panel. The downstairs
# mains CTs only cover two of the three phases, so downstairs = mains A + B +
# zone C + clothes washer (on the third phase, with their own CTs) + the
# second leg of the two-phase water heater, oven and stovetop (see
# dorfsteig_core.iotawatt). The upstairs water heater isn't measured; it's
# estimated from ±2950 W steps.
HOUSE_MINUTES = """
CREATE TABLE IF NOT EXISTS house_minutes (
    ts             INTEGER NOT NULL PRIMARY KEY,
    pv             REAL,   -- W, gaps up to 5 min filled, else 0 (inverter asleep)
    house          REAL,   -- W, whole house consumption
    downstairs     REAL,   -- W, IoTaWatt panel, see above
    upstairs       REAL,   -- W, house - downstairs
    upstairs_water REAL,   -- W, estimated: 2950 or 0
    upstairs_other REAL    -- W, upstairs - water heater
);
"""

# One row per hour (ts = start of the hour, UTC epoch). Energies in Wh.
# Import/export come from the meter counters where possible, so they're
# right even when minutes are missing; other values are scaled up from the
# minutes available (see the *_minutes columns).
ENERGY_HOURLY = """
CREATE TABLE IF NOT EXISTS energy_hourly (
    ts                  INTEGER NOT NULL PRIMARY KEY,
    pv_wh               REAL,
    import_wh           REAL,
    export_wh           REAL,
    house_wh            REAL,   -- pv + import - export
    downstairs_wh       REAL,
    downstairs_water_wh REAL,   -- measured by IoTaWatt (twice the channel: two phases)
    upstairs_wh         REAL,   -- house - downstairs
    upstairs_water_wh   REAL,   -- estimated
    upstairs_other_wh   REAL,   -- upstairs - water heater
    high_tariff         INTEGER NOT NULL,
    import_price        REAL,   -- CHF/kWh; NULL if there are no prices for the year
    export_price        REAL,
    import_chf          REAL,
    export_chf          REAL,
    grid_minutes        INTEGER NOT NULL,
    iotawatt_minutes    INTEGER NOT NULL
);
"""

# Hourly weather observations, one row per source and hour (ts = start of the
# hour, UTC epoch): "meteoswiss" = nearest SwissMetNet station (hourly means),
# "era5" = Open-Meteo's ERA5 reanalysis (a few days behind). Radiation is as
# published; against our PV, both line up with the hour starting at ts.
# The balcony sensor stays in air_quality.
WEATHER = """
CREATE TABLE IF NOT EXISTS weather (
    ts           INTEGER NOT NULL,
    source       TEXT NOT NULL,
    temperature  REAL,   -- °C at 2 m
    radiation    REAL,   -- W/m², global horizontal
    cloud_cover  REAL,   -- %
    fetched_at   INTEGER NOT NULL,
    PRIMARY KEY (ts, source)
);
"""

# Every forecast fetched, kept by issue time so forecast skill can be
# measured later. Models: "icon_ch1" / "icon_ch2" (MeteoSwiss, via
# Open-Meteo), "best_match" (Open-Meteo's global default).
WEATHER_FORECAST = """
CREATE TABLE IF NOT EXISTS weather_forecast (
    issued       INTEGER NOT NULL,   -- fetch time, rounded down to the hour
    ts           INTEGER NOT NULL,
    model        TEXT NOT NULL,
    temperature  REAL,
    radiation    REAL,
    cloud_cover  REAL,
    PRIMARY KEY (issued, ts, model)
);
-- For "the newest forecast for this hour" (dorfsteig_core.weather)
CREATE INDEX IF NOT EXISTS weather_forecast_model_ts ON weather_forecast (model, ts, issued);
"""

# Written by dorfsteig_forecast (see its docstrings), read by the controller.
# Models fitted to our history, one row per model ("pv", "base_load",
# "heating"), parameters as JSON; refitted once a day.
FORECAST_MODELS = """
CREATE TABLE IF NOT EXISTS forecast_models (
    name       TEXT NOT NULL PRIMARY KEY,
    fitted_at  INTEGER NOT NULL,
    params     TEXT NOT NULL      -- JSON
);
"""

# PV forecast per hour and model, from the weather forecasts known at
# `issued`. Every forecast is kept, like weather_forecast, so models can be
# compared (the Learning dashboard). The key order suits "the newest forecast
# of this model for this hour".
PV_FORECAST = """
CREATE TABLE IF NOT EXISTS pv_forecast (
    model      TEXT NOT NULL,     -- "ratio" (dorfsteig_forecast/pv.py)
    ts         INTEGER NOT NULL,  -- start of the hour forecast
    issued     INTEGER NOT NULL,  -- when the forecast was made, Unix time
    pv_wh      REAL NOT NULL,
    PRIMARY KEY (model, ts, issued)
);
"""

# Per local day, as forecast at `issued`. surplus_kwh is the PV beyond the
# usual (non-heating) house load: roughly what storage heaters could take.
DAY_FORECAST = """
CREATE TABLE IF NOT EXISTS day_forecast (
    issued       INTEGER NOT NULL,
    day          TEXT NOT NULL,   -- ISO date, local
    temperature  REAL,            -- °C, daily mean
    pv_kwh       REAL,
    base_kwh     REAL,            -- usual house load without heating
    surplus_kwh  REAL,            -- sum over hours of max(0, PV - base load)
    PRIMARY KEY (issued, day)
);
"""

DERIVED_TABLES = {"house_minutes": HOUSE_MINUTES, "energy_hourly": ENERGY_HOURLY}

ALL_TABLES = [IOTAWATT_READINGS, INVERTER_READINGS, GRID_READINGS, AIR_QUALITY, WEATHER, WEATHER_FORECAST,
              FORECAST_MODELS, PV_FORECAST, DAY_FORECAST, *DERIVED_TABLES.values()]


def _migrate(conn: sqlite3.Connection) -> None:
    """Changes to existing tables. pv_forecast got a model column (2026-10-12); it only
    holds a few hours of forecasts from before then, which the forecaster rewrites."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(pv_forecast)")]
    if columns and "model" not in columns:
        conn.execute("DROP TABLE pv_forecast")


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Safe to share across threads: Python's SQLite is built in serialized
    # mode (sqlite3.threadsafety == 3). The controller's web server needs this.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    for ddl in ALL_TABLES:
        conn.executescript(ddl)
    conn.commit()
    return conn
