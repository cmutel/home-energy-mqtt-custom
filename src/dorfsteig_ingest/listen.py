"""Listen to MQTT and record one example payload per topic.

Useful for discovering what sensors publish and what their payloads look like.
"""

import time

import paho.mqtt.client as mqtt

from dorfsteig_core.mqtt import connect_with_retry, new_client
from dorfsteig_core.settings import settings


def run(topic: str = "#", minutes: float = 10) -> None:
    duration = minutes * 60
    examples: dict[str, str] = {}  # topic → first payload seen
    start = time.monotonic()

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            print(f"Connected to {settings.mqtt_host}:{settings.mqtt_port}")
            print(f"Listening on '{topic}' for {minutes:g} minutes...\n")
            client.subscribe(topic)
        else:
            print(f"Connection failed: reason code {reason_code}")
            client.disconnect()

    def on_message(client, userdata, message: mqtt.MQTTMessage) -> None:
        if message.topic not in examples:
            try:
                payload = message.payload.decode("utf-8")
            except UnicodeDecodeError:
                payload = repr(message.payload)
            examples[message.topic] = payload
            print(f"  new  {message.topic}")

        if time.monotonic() - start >= duration:
            client.disconnect()

    client = new_client()
    client.on_connect = on_connect
    client.on_message = on_message
    connect_with_retry(client)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        pass

    print(f"\n{'─' * 60}")
    print(f"Captured {len(examples)} unique topics:\n")
    for t in sorted(examples):
        print(f"  {t}")
        print(f"    {examples[t]}")
        print()
