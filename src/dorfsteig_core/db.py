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
# zone C + clothes washer (on the third phase, with their own CTs). The
# upstairs water heater isn't measured; it's estimated from ±1498 W steps.
HOUSE_MINUTES = """
CREATE TABLE IF NOT EXISTS house_minutes (
    ts             INTEGER NOT NULL PRIMARY KEY,
    pv             REAL,   -- W, gaps up to 5 min filled, else 0 (inverter asleep)
    house          REAL,   -- W, whole house consumption
    downstairs     REAL,   -- W, IoTaWatt mains A + B + zone C + clothes washer
    upstairs       REAL,   -- W, house - downstairs
    upstairs_water REAL,   -- W, estimated: 1498 or 0
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
    downstairs_water_wh REAL,   -- measured by IoTaWatt
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

DERIVED_TABLES = {"house_minutes": HOUSE_MINUTES, "energy_hourly": ENERGY_HOURLY}

ALL_TABLES = [IOTAWATT_READINGS, INVERTER_READINGS, GRID_READINGS, AIR_QUALITY, *DERIVED_TABLES.values()]


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Safe to share across threads: Python's SQLite is built in serialized
    # mode (sqlite3.threadsafety == 3). The controller's web server needs this.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    for ddl in ALL_TABLES:
        conn.execute(ddl)
    conn.commit()
    return conn
