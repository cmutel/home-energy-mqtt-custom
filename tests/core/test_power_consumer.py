from types import SimpleNamespace

from dorfsteig_core.domain.power import GridReading, InverterReading
from dorfsteig_core.power_consumer import PowerConsumer


def message(topic: str, value: float) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=str(value).encode())


def make_consumer():
    inverter: list[InverterReading] = []
    grid: list[GridReading] = []
    return PowerConsumer(on_inverter=inverter.append, on_grid=grid.append), inverter, grid


def test_grid_reading_emitted_once_all_fields_arrive():
    consumer, _, grid = make_consumer()
    consumer.on_message(None, None, message("mbmd/solaredge1-1-1/Power", -1442.0))
    consumer.on_message(None, None, message("mbmd/solaredge1-1-1/Import", 56165.1))
    assert grid == []
    consumer.on_message(None, None, message("mbmd/solaredge1-1-1/Export", 31377.8))
    assert len(grid) == 1
    assert grid[0].power == -1442.0
    assert grid[0].import_total == 56165.1


def test_inverter_and_grid_prefixes_do_not_mix():
    consumer, inverter, grid = make_consumer()
    # "mbmd/solaredge1-1/" is a prefix of "mbmd/solaredge1-1-1/" as a string,
    # so grid topics must not be picked up as inverter fields.
    for suffix, value in [("Power", 500.0), ("Import", 1.0), ("Export", 2.0)]:
        consumer.on_message(None, None, message(f"mbmd/solaredge1-1-1/{suffix}", value))
    assert len(grid) == 1
    assert inverter == []


def test_inverter_reading():
    consumer, inverter, _ = make_consumer()
    fields = {"DCPower": 407.1, "DCVoltage": 747.5, "DCCurrent": 0.545,
              "Power": 401.0, "Export": 61953.4}
    for suffix, value in fields.items():
        consumer.on_message(None, None, message(f"mbmd/solaredge1-1/{suffix}", value))
    assert len(inverter) == 1
    assert inverter[0].power_ac == 401.0


def test_non_numeric_payload_ignored():
    consumer, inverter, grid = make_consumer()
    consumer.on_message(None, None, SimpleNamespace(topic="mbmd/solaredge1-1-1/Power",
                                                    payload=b"nan?"))
    assert inverter == [] and grid == []
