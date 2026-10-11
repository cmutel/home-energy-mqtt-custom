"""MQTT consumer that assembles mbmd topic bursts into domain objects.

MBMD publishes all measurements for a device as a rapid burst of individual
topic/value messages. This consumer accumulates them and emits a complete
InverterReading or GridReading as soon as all required fields have arrived.

Usage:
    from dorfsteig_core.mqtt import connect_with_retry, new_client
    from dorfsteig_core.power_consumer import PowerConsumer

    def on_inverter(reading: InverterReading) -> None:
        print(reading)

    def on_grid(reading: GridReading) -> None:
        print(reading)

    consumer = PowerConsumer(on_inverter=on_inverter, on_grid=on_grid)

    client = new_client()
    client.on_connect = consumer.on_connect
    client.on_message = consumer.on_message
    connect_with_retry(client)
    client.loop_forever()
"""

import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from dorfsteig_core.domain.power import GridReading, InverterReading

# MQTT topic prefixes for each device
_INVERTER_PREFIX = "mbmd/solaredge1-1/"
_GRID_PREFIX = "mbmd/solaredge1-1-1/"

# Mapping from topic suffix → InverterReading field name
_INVERTER_FIELDS: dict[str, str] = {
    "DCPower":   "power_dc",
    "DCVoltage": "voltage_dc",
    "DCCurrent": "current_dc",
    "Power":     "power_ac",
    "Export":    "export_total",
}

# Mapping from topic suffix → GridReading field name
_GRID_FIELDS: dict[str, str] = {
    "Power":   "power",
    "Import":  "import_total",
    "Export":  "export_total",
}


class PowerConsumer:
    """Accumulates MBMD MQTT messages and emits complete domain object snapshots."""

    def __init__(
        self,
        on_inverter: Callable[[InverterReading], None],
        on_grid: Callable[[GridReading], None],
    ) -> None:
        self._on_inverter = on_inverter
        self._on_grid = on_grid
        self._inverter_buf: dict[str, float] = {}
        self._grid_buf: dict[str, float] = {}

    def on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            client.subscribe(_INVERTER_PREFIX + "#")
            client.subscribe(_GRID_PREFIX + "#")

    def on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        topic: str = message.topic
        try:
            value = float(message.payload)
        except ValueError:
            return

        if topic.startswith(_INVERTER_PREFIX):
            suffix = topic.removeprefix(_INVERTER_PREFIX)
            if suffix in _INVERTER_FIELDS:
                self._inverter_buf[_INVERTER_FIELDS[suffix]] = value
                self._try_emit_inverter()

        elif topic.startswith(_GRID_PREFIX):
            suffix = topic.removeprefix(_GRID_PREFIX)
            if suffix in _GRID_FIELDS:
                self._grid_buf[_GRID_FIELDS[suffix]] = value
                self._try_emit_grid()

    def _try_emit_inverter(self) -> None:
        if self._inverter_buf.keys() >= set(_INVERTER_FIELDS.values()):
            self._on_inverter(InverterReading(
                timestamp=int(time.time()),
                **self._inverter_buf,
            ))
            self._inverter_buf = {}

    def _try_emit_grid(self) -> None:
        if self._grid_buf.keys() >= set(_GRID_FIELDS.values()):
            self._on_grid(GridReading(
                timestamp=int(time.time()),
                **self._grid_buf,
            ))
            self._grid_buf = {}
