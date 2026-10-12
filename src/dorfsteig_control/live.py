"""Everything measured, right now, for the real-time dashboard (`/heating/api/live`).

Sources:
- MBMD over MQTT: every topic the controller receives (`mbmd/#`), e.g. the
  grid meter's power, current, voltage and power factor per phase, and the
  inverter's AC and DC values. Topics are mbmd/<device>/<Quantity>[/<phase>].
- IoTaWatt: every series it has configured, with watts, amps, VA, var and
  power factor (volts and Hz for the voltage input), newest 5-second
  average. Fetched on request, at most once per CACHE_S, so several open
  pages don't load the IoTaWatt; the list of series is refreshed hourly.
- The heater relays and each heater's estimated power (wattage.py), so
  switching something can be matched to the numbers.

Derived, as in the energy tables (dorfsteig_ingest.energy): house = PV -
grid, downstairs = mains A + B + the channels on the third phase + the
second leg of the two-phase loads (water heater, oven, stovetop), upstairs =
house - downstairs. Per phase, house = PV on that phase - grid on that phase,
with the inverter's power split by its current per phase (it feeds all three
about equally). Which IoTaWatt mains CT sits on which grid phase isn't
recorded; the page shows both, so it can be read off.

The sources are a few seconds apart (MBMD every few seconds, IoTaWatt 5 s
averages), so watch steady loads, not the moment of switching.
"""

import logging
import time
from typing import TYPE_CHECKING

import httpx

from dorfsteig_control import wattage
from dorfsteig_control.heaters import BY_ID
from dorfsteig_core import iotawatt

if TYPE_CHECKING:
    from dorfsteig_control.engine import HeaterState
    from dorfsteig_control.service import Controller

log = logging.getLogger(__name__)

CACHE_S = 4
SERIES_REFRESH_S = 3600
PHASES = ("L1", "L2", "L3")
DEVICES = {"solaredge1-1-1": "Grid meter", "solaredge1-1": "Inverter"}   # MBMD device → name
GRID = "solaredge1-1-1"
INVERTER = "solaredge1-1"


def mbmd_by_device(values: dict[str, tuple[float | str, float]], now: float) -> dict:
    """{device: {"name", "values": {quantity: {"total" or phase: value}}, "age_s"}} from MBMD topics."""
    devices: dict[str, dict] = {}
    for topic, (value, ts) in values.items():
        parts = topic.split("/")
        if len(parts) < 3:
            continue        # e.g. mbmd/status
        device, quantity = parts[1], parts[2]
        phase = parts[3] if len(parts) > 3 else "total"
        entry = devices.setdefault(device, {"name": DEVICES.get(device, device), "values": {}, "age_s": None})
        entry["values"].setdefault(quantity, {})[phase] = value
        age = now - ts
        entry["age_s"] = age if entry["age_s"] is None else min(entry["age_s"], age)
    return devices


def number(value) -> float | None:
    return value if isinstance(value, (int, float)) else None


def per_phase_house(devices: dict) -> dict[str, dict[str, float | None]]:
    """Grid, PV and house power per phase (W, grid positive = export)."""
    grid = devices.get(GRID, {}).get("values", {})
    inverter = devices.get(INVERTER, {}).get("values", {})
    pv_total = number(inverter.get("Power", {}).get("total"))
    currents = {p: number(inverter.get("Current", {}).get(p)) for p in PHASES}
    known = [c for c in currents.values() if c is not None]
    total_current = sum(known) if len(known) == len(PHASES) else None
    result = {}
    for phase in PHASES:
        grid_power = number(grid.get("Power", {}).get(phase))
        pv = None
        current = currents[phase]
        if pv_total is not None and current is not None and total_current is not None:
            pv = pv_total * current / total_current if total_current > 0 else 0.0
        house = None if grid_power is None or pv is None else pv - grid_power
        result[phase] = {"grid": grid_power, "pv": pv, "house": house}
    return result


def heater_entry(controller: "Controller", heater: str, state: "HeaterState") -> dict:
    watts, source = wattage.estimated_watts(controller.store.conn, heater)
    return {"id": heater, "name": BY_ID[heater].name, "on": state.on, "reason": state.reason,
            "watts": watts, "watts_source": source}


class LiveView:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient()
        self.series: list[tuple[str, str]] = []
        self.series_at = 0.0
        self.reading: tuple[int, iotawatt.Values] | None = None
        self.error: str | None = None
        self.fetched_at = 0.0

    async def refresh(self) -> None:
        """Fetch the newest IoTaWatt reading, unless the last fetch is recent."""
        now = time.time()
        if now - self.fetched_at < CACHE_S:
            return
        self.fetched_at = now
        try:
            if not self.series or now - self.series_at > SERIES_REFRESH_S:
                self.series = await iotawatt.series(self.client)
                self.series_at = now
            self.reading = await iotawatt.latest_all(self.client, self.series)
            self.error = None
        except Exception as e:
            self.error = f"IoTaWatt: {e!r}"
            log.warning("Live IoTaWatt query failed: %r", e)

    async def snapshot(self, controller: "Controller") -> dict:
        await self.refresh()
        now = time.time()
        channels: iotawatt.Values = {}
        iotawatt_ts = None
        if self.reading is not None:
            iotawatt_ts, channels = self.reading

        # Downstairs panel: the IoTaWatt series named like the stored columns (Mains_A → mains_a)
        watts = {name.lower(): values.get("watts") for name, values in channels.items()}
        downstairs = None
        if all(watts.get(c) is not None for c in iotawatt.MAINS_CHANNELS):
            # As dorfsteig_core.iotawatt.downstairs_sql: the two-phase loads' second leg counts once more
            panel = iotawatt.MAINS_CHANNELS + iotawatt.OFF_MAINS_CHANNELS + iotawatt.TWO_PHASE_CHANNELS
            downstairs = sum(watts.get(c) or 0.0 for c in panel)
        house = None
        if controller.grid_power is not None and controller.pv_power is not None:
            house = controller.pv_power - controller.grid_power
        upstairs = None if house is None or downstairs is None else house - downstairs

        devices = mbmd_by_device(controller.mbmd, now)
        return {
            "now": now,
            "totals": {"grid": controller.grid_power, "pv": controller.pv_power, "house": house,
                       "downstairs": downstairs, "upstairs": upstairs},
            "phases": per_phase_house(devices),
            "downstairs_channels": {"mains": list(iotawatt.MAINS_CHANNELS),
                                    "third_phase": list(iotawatt.OFF_MAINS_CHANNELS),
                                    "two_phase": list(iotawatt.TWO_PHASE_CHANNELS)},
            "iotawatt": {"ts": iotawatt_ts, "age_s": None if iotawatt_ts is None else now - iotawatt_ts,
                         "error": self.error, "units": dict(self.series), "series": channels},
            "mbmd": devices,
            "heaters": [heater_entry(controller, heater, state)
                        for heater, state in controller.states.items()],
        }
