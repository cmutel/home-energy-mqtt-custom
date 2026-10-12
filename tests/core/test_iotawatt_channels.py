import sqlite3

from dorfsteig_core.iotawatt import downstairs_sql, real_power


def test_two_phase_loads_count_twice():
    assert real_power("oven", 1000.0) == 2000.0
    assert real_power("zone_a", 1000.0) == 1000.0
    assert real_power("water_heater", None) is None


def test_downstairs_adds_third_phase_channels_and_second_legs():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (mains_a, mains_b, zone_c, clothes_washer, water_heater, oven, stovetop)")
    # Water heater on: 1475 W on its measured leg (inside mains A), the same again on the third phase
    conn.execute("INSERT INTO r VALUES (1600, 100, 20, NULL, 1475, 0, 0)")
    assert conn.execute(f"SELECT {downstairs_sql()} FROM r").fetchone()[0] == 1600 + 100 + 20 + 1475
