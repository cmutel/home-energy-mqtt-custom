"""Listen to air quality sensor MQTT topics and write readings to SQLite.

Sensors publish every ~5 minutes. Each reading is written immediately
on arrival — no averaging, as the sensors already publish at low frequency.
"""

import logging

from dorfsteig_core.db import init_db
from dorfsteig_core.domain.climate import AirQualityReading
from dorfsteig_core.mqtt import connect_with_retry, new_client
from dorfsteig_core.settings import settings
from dorfsteig_ingest.air_quality_consumer import AirQualityConsumer

log = logging.getLogger(__name__)

INSERT_SQL = """
    INSERT OR REPLACE INTO air_quality
        (ts, room, sensor_id, pm25, pm10, temperature, humidity, signal)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def run() -> None:
    conn = init_db()
    log.info("Writing to %s", settings.db_path)

    def on_reading(r: AirQualityReading) -> None:
        conn.execute(INSERT_SQL, (
            r.timestamp, r.room, r.sensor_id,
            r.pm25, r.pm10, r.temperature, r.humidity, r.signal,
        ))
        conn.commit()
        log.info("%-10s  temp=%.1f°C  hum=%.1f%%  pm2.5=%.2f  pm10=%.2f  signal=%ddBm",
                 r.room, r.temperature, r.humidity, r.pm25, r.pm10, r.signal)

    consumer = AirQualityConsumer(on_reading=on_reading)
    client = new_client()
    client.on_connect = consumer.on_connect
    client.on_message = consumer.on_message
    connect_with_retry(client)
    client.loop_forever()
