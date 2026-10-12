from dorfsteig_control.live import mbmd_by_device, per_phase_house


def test_mbmd_topics_grouped_by_device_quantity_and_phase():
    values = {
        "mbmd/solaredge1-1-1/Power": (-300.0, 100.0),
        "mbmd/solaredge1-1-1/Power/L1": (-500.0, 100.0),
        "mbmd/solaredge1-1-1/Power/L2": (100.0, 100.0),
        "mbmd/solaredge1-1-1/Power/L3": (100.0, 98.0),
        "mbmd/solaredge1-1/Power": (3000.0, 99.0),
        "mbmd/solaredge1-1/Current/L1": (4.0, 99.0),
        "mbmd/solaredge1-1/Current/L2": (4.0, 99.0),
        "mbmd/solaredge1-1/Current/L3": (4.0, 99.0),
        "mbmd/status": ("connected", 99.0),
    }
    devices = mbmd_by_device(values, now=101.0)
    assert devices["solaredge1-1-1"]["name"] == "Grid meter"
    power = devices["solaredge1-1-1"]["values"]["Power"]
    assert power == {"total": -300.0, "L1": -500.0, "L2": 100.0, "L3": 100.0}
    assert devices["solaredge1-1-1"]["age_s"] == 1.0
    # PV split evenly by current: 1000 W per phase; house = PV - grid
    phases = per_phase_house(devices)
    assert phases["L1"] == {"grid": -500.0, "pv": 1000.0, "house": 1500.0}
    assert phases["L2"]["house"] == 900.0
