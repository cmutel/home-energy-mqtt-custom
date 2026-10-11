# ESP32 relay boards

MicroPython firmware for the two relay controllers. Each board fetches its
JSON file from the Pi every 15 seconds and sets its relays; if the request
fails it falls back to `DEFAULTS` (water heaters on, everything else off).

| Board | Fetches | I2C buses |
|---|---|---|
| upstairs | `http://192.168.1.192/upstairs.json` | 1 |
| downstairs | `http://192.168.1.192/downstairs.json` | 2 |

Relay colours → heaters are listed in [wiring.md](wiring.md).

The boards currently in the house run this code with the WiFi credentials
inline in `boot.py`. They don't need reflashing. If you do reflash, copy
`wifi_config.example.py` to `wifi_config.py`, fill it in, and upload it to the
board along with `boot.py` and `main.py` (e.g. with `mpremote cp`).
