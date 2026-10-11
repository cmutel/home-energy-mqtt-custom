"""MQTT helpers shared by all services."""

import logging
import time

import paho.mqtt.client as mqtt

from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

MAX_BACKOFF = 300  # seconds


def new_client() -> mqtt.Client:
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def connect_with_retry(client: mqtt.Client, host: str | None = None, port: int | None = None) -> None:
    """Connect to the broker, retrying with exponential backoff.

    `client.connect` raises if the broker is down when the service starts;
    once connected, paho's `loop_forever` handles later reconnects itself.
    """
    host = host or settings.mqtt_host
    port = port or settings.mqtt_port
    delay = 5
    while True:
        try:
            client.connect(host, port, keepalive=60)
            log.info("Connected to MQTT broker %s:%s", host, port)
            return
        except OSError as e:
            log.warning("MQTT connect to %s:%s failed (%s), retrying in %ds", host, port, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)
