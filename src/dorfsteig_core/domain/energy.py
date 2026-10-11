from dataclasses import dataclass
from enum import Enum


class Unit(Enum):
    VOLTS = "Volts"
    WATTS = "Watts"


@dataclass(frozen=True)
class Channel:
    """A named measurement channel on the IoTaWatt, e.g. 'Mains_A' in Watts.

    Known channels:
        Input_0       – Volts
        Mains_A       – Watts
        Mains_B       – Watts
        Oven          – Watts
        Zone_A        – Watts
        Zone_B        – Watts
        Zone_C        – Watts
        Zone_D        – Watts
        Zone_E        – Watts
        Zone_F        – Watts
        Clothes_Washer – Watts
        Clothes_Dryer  – Watts
        Water_Heater   – Watts
        Dishwasher     – Watts
        Stovetop       – Watts
    """
    name: str
    unit: Unit

    @classmethod
    def from_api(cls, data: dict) -> "Channel":
        return cls(name=data["name"], unit=Unit(data["unit"]))


@dataclass(frozen=True)
class EnergySnapshot:
    """All channel readings at a single point in time."""
    timestamp: int  # Unix epoch seconds
    readings: dict[str, float]  # channel name → value in channel's unit
