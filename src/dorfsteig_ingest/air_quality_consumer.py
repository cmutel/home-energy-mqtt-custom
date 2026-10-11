"""MQTT consumer that assembles per-sensor topic bursts into AirQualityReading objects.

Each sensor publishes three messages per reading cycle (~5 minutes):
    haus/{room}/feinstaub  →  pm25, pm10
    haus/{room}/meteo      →  temperature, humidity
    haus/{room}/netzwerk   →  signal

All three carry the same payload timestamp. A reading is emitted as soon as
all three message types have arrived for a given room.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime

import paho.mqtt.client as mqtt

from dorfsteig_core.domain.climate import AirQualityReading

log = logging.getLogger(__name__)

_REQUIRED = {"feinstaub", "meteo", "netzwerk"}


def _parse_ts(time_str: str) -> int:
    """Parse payload local-time string to Unix epoch seconds."""
    return int(datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").timestamp())


class AirQualityConsumer:
    """Accumulates haus/{room}/* messages and emits complete AirQualityReading snapshots."""

    def __init__(self, on_reading: Callable[[AirQualityReading], None]) -> None:
        self._on_reading = on_reading
        # buf[room] = {message_type: parsed_fields_dict}
        self._buf: dict[str, dict[str, dict]] = {}

    def on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            client.subscribe("haus/#")
            log.info("Subscribed to haus/#")
        else:
            log.error("MQTT connection failed: reason code %s", reason_code)

    def on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        # Topic structure: haus/{room}/{type}
        parts = message.topic.split("/")
        if len(parts) != 3 or parts[2] not in _REQUIRED:
            return

        _, room, msg_type = parts

        try:
            payload = json.loads(message.payload)
        except json.JSONDecodeError:
            log.warning("Bad JSON on %s", message.topic)
            return

        self._buf.setdefault(room, {})[msg_type] = payload

        if self._buf[room].keys() >= _REQUIRED:
            self._emit(room)

    def _emit(self, room: str) -> None:
        buf = self._buf.pop(room)
        try:
            feinstaub = buf["feinstaub"]
            meteo     = buf["meteo"]
            netzwerk  = buf["netzwerk"]

            reading = AirQualityReading(
                timestamp   = _parse_ts(feinstaub["time"]),
                room        = room,
                sensor_id   = feinstaub["tags"]["sensor"],
                pm25        = float(feinstaub["fields"]["2.5"]),
                pm10        = float(feinstaub["fields"]["10"]),
                temperature = float(meteo["fields"]["temperature"]),
                humidity    = float(meteo["fields"]["humidity"]),
                signal      = int(netzwerk["fields"]["signal"]),
            )
            self._on_reading(reading)
        except (KeyError, ValueError) as e:
            log.warning("Failed to assemble reading for room=%s: %s", room, e)
