"""The physical heater circuits and which ESP32 relay switches each one.

This is wiring, not configuration: it only changes when relays are rewired.
See firmware/esp32/wiring.md.
"""

from dataclasses import dataclass
from typing import Literal

Board = Literal["upstairs", "downstairs"]
Colour = Literal["red", "yellow", "green", "blue"]

# Key order matches the legacy controller's output, so the JSON is identical.
COLOURS: tuple[Colour, ...] = ("red", "yellow", "green", "blue")


@dataclass(frozen=True)
class Heater:
    id: str
    name: str           # human-readable
    legacy_name: str    # key used in the legacy config.json
    board: Board
    bus: str            # I2C bus on the board ("1" or "2")
    colour: Colour
    # Water heaters have their own thermostat: a closed relay only means they
    # *may* draw power, so they're left out of "test all".
    water: bool = False


HEATERS: tuple[Heater, ...] = (
    Heater("upstairs_water", "Upstairs water heater", "upstairs water", "upstairs", "1", "blue",
           water=True),
    Heater("upstairs_guest", "Guest room / Matvei", "upstairs guest/matvei", "upstairs", "1", "red"),
    Heater("upstairs_alpha", "Upstairs alpha (corridor)", "upstairs alpha", "upstairs", "1", "green"),
    Heater("upstairs_beta", "Upstairs beta (Direktheizung)", "upstairs beta", "upstairs", "1", "yellow"),
    Heater("downstairs_water", "Downstairs water heater", "downstairs water", "downstairs", "2", "blue",
           water=True),
    Heater("living_room", "Entrance & living room", "downstairs living room", "downstairs", "1", "red"),
    Heater("corridor_1st_floor", "Corridor 1st floor", "downstairs corridor 1st floor",
           "downstairs", "1", "green"),
    Heater("parents_storage", "Parents / storage", "downstairs parents/storage", "downstairs", "1", "yellow"),
    Heater("direct_zusatz", "Direkt & Zusatz switch", "downstairs direct/zusatz switch",
           "downstairs", "1", "blue"),
    Heater("playroom_south", "Playroom south", "downstairs playroom south", "downstairs", "2", "red"),
    Heater("playroom_north", "Playroom north", "downstairs playroom north", "downstairs", "2", "green"),
    Heater("kids_bedroom", "Kids bedroom", "downstairs kids bedroom", "downstairs", "2", "yellow"),
)

BY_ID: dict[str, Heater] = {h.id: h for h in HEATERS}
BY_LEGACY_NAME: dict[str, Heater] = {h.legacy_name: h for h in HEATERS}
