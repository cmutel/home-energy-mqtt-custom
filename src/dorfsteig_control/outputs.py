"""JSON payloads for the ESP32 boards and the legacy status page.

The formats must match what the boards in the house expect:

    upstairs.json    {"red": 0, "yellow": 0, "green": 0, "blue": 1}
    downstairs.json  {"1": {"red": .., ...}, "2": {"red": .., ...}}
"""

import time
from typing import Any

from dorfsteig_control.engine import States
from dorfsteig_control.heaters import COLOURS, HEATERS


def _relays(states: States, board: str, bus: str) -> dict[str, int]:
    relays = {colour: 0 for colour in COLOURS}
    for heater in HEATERS:
        if heater.board == board and heater.bus == bus:
            relays[heater.colour] = int(states[heater.id].on)
    return relays


def upstairs_payload(states: States) -> dict[str, int]:
    return _relays(states, "upstairs", "1")


def downstairs_payload(states: States) -> dict[str, dict[str, int]]:
    return {"1": _relays(states, "downstairs", "1"), "2": _relays(states, "downstairs", "2")}


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def legacy_status(states: States, power: float | None, now: float) -> dict[str, Any]:
    """Same shape as the legacy controller's status.json."""
    return {
        "now": _fmt(now),
        "power": power if power is not None else 0,
        "state": [
            {
                "heater": heater.legacy_name,
                "on at": _fmt(states[heater.id].since),
                "seconds on": "{:.1f}".format(now - states[heater.id].since),
            }
            for heater in HEATERS
            if states[heater.id].on
        ],
    }
