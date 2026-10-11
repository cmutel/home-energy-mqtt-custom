import json
from types import SimpleNamespace

from dorfsteig_ingest.air_quality_consumer import AirQualityConsumer

TIME = "2026-04-12 10:00:00"


def message(room: str, kind: str, fields: dict) -> SimpleNamespace:
    payload = {"time": TIME, "tags": {"sensor": "6089660"}, "fields": fields}
    return SimpleNamespace(topic=f"haus/{room}/{kind}", payload=json.dumps(payload).encode())


def test_reading_assembled_from_three_messages():
    readings = []
    consumer = AirQualityConsumer(on_reading=readings.append)
    consumer.on_message(None, None, message("balkon", "feinstaub", {"2.5": 0.1, "10": 0.2}))
    consumer.on_message(None, None, message("balkon", "meteo",
                                            {"temperature": 10.5, "humidity": 99.9}))
    assert readings == []
    consumer.on_message(None, None, message("balkon", "netzwerk", {"signal": -43}))

    assert len(readings) == 1
    r = readings[0]
    assert (r.room, r.sensor_id, r.pm25, r.temperature, r.signal) == \
        ("balkon", "6089660", 0.1, 10.5, -43)
