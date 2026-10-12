"""IoTaWatt channels and the live query, shared by the ingest and the controller.

The ingest (dorfsteig_ingest.iotawatt) stores one-minute averages of the
CHANNELS every 5 minutes. The controller's live view (`/heating/api/live`)
asks for the newest 5-second averages of every series the IoTaWatt has, with
current, power factor etc., only while someone has the real-time page open.

Relative times ("s-20s" = 20 seconds ago) avoid the IoTaWatt's timezone
setting (see dorfsteig_ingest.iotawatt).
"""

import httpx

from dorfsteig_core.settings import settings

# Column in iotawatt_readings → IoTaWatt output name
CHANNELS = {
    "input_0": "Input_0.volts",
    "mains_a": "Mains_A.watts",
    "mains_b": "Mains_B.watts",
    "oven": "Oven.watts",
    "zone_a": "Zone_A.watts",
    "zone_b": "Zone_B.watts",
    "zone_c": "Zone_C.watts",
    "zone_d": "Zone_D.watts",
    "zone_e": "Zone_E.watts",
    "zone_f": "Zone_F.watts",
    "clothes_washer": "Clothes_Washer.watts",
    "clothes_dryer": "Clothes_Dryer.watts",
    "water_heater": "Water_Heater.watts",
    "dishwasher": "Dishwasher.watts",
    "stovetop": "Stovetop.watts",
}
COLUMNS = list(CHANNELS)

# The downstairs panel. The house has three phases, but the mains CTs only
# cover two of them, so the downstairs panel is put together from:
# - the two mains CTs;
# - channels on the third phase, which has no mains CT;
# - the other leg of two-phase (400 V) loads, whose CT is on one leg only. Their
#   second leg is on the third phase and draws the same, so their real power is
#   twice the channel. Measured from switching steps, 2026-04 to 2026-10: the
#   whole house rises by 1.97× the water heater channel (888 switch-ons) and 1.98×
#   the oven's (118); the stovetop was tested by hand.
# Whatever the house uses beyond this counts as upstairs.
MAINS_CHANNELS = ("mains_a", "mains_b")
OFF_MAINS_CHANNELS = ("zone_c", "clothes_washer")
TWO_PHASE_CHANNELS = ("water_heater", "oven", "stovetop")

# Both water heaters (same model): 2 × 1479 W downstairs channel, 2931 W steps upstairs
WATER_HEATER_W = 2950.0


def real_power(column: str, value: float | None) -> float | None:
    """A channel's value as the circuit's real power (two-phase loads count twice)."""
    if value is None:
        return None
    return 2 * value if column in TWO_PHASE_CHANNELS else value


def downstairs_sql() -> str:
    """SQL expression for the downstairs panel's power from an iotawatt_readings row."""
    extra = [f"IFNULL({c}, 0)" for c in OFF_MAINS_CHANNELS + TWO_PHASE_CHANNELS]
    return " + ".join(["mains_a", "mains_b", *extra])
SELECT = "[time.utc.unix," + ",".join(CHANNELS.values()) + "]"


# What the live view asks for, per series unit: IoTaWatt computes these from each input
LIVE_QUANTITIES = {
    "Watts": ("watts", "amps", "va", "var", "pf"),
    "Volts": ("volts", "hz"),
}


async def series(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Every series the IoTaWatt has configured: (name, unit), e.g. ("Mains_A", "Watts")."""
    response = await client.get(f"http://{settings.iotawatt_host}/query", params={"show": "series"},
                                timeout=5)
    response.raise_for_status()
    return [(s["name"], s["unit"]) for s in response.json()["series"]]


Values = dict[str, dict[str, float | None]]   # series name → {quantity: value}


async def latest_all(client: httpx.AsyncClient,
                     all_series: list[tuple[str, str]]) -> tuple[int, Values] | None:
    """The newest 5-second average of every quantity of every series.

    Returns (Unix time, {series name: {quantity: value}}), e.g.
    {"Mains_A": {"watts": 1562.9, "amps": 7.6, "va": 1766.9, "var": 824.3, "pf": 0.9}};
    None if the IoTaWatt has nothing for the last 20 seconds.
    """
    wanted = [(name, quantity) for name, unit in all_series
              for quantity in LIVE_QUANTITIES.get(unit, (unit.lower(),))]
    select = "[time.utc.unix," + ",".join(f"{name}.{quantity}" for name, quantity in wanted) + "]"
    response = await client.get(
        f"http://{settings.iotawatt_host}/query",
        params={"select": select, "begin": "s-20s", "end": "s", "group": "5s", "format": "json"},
        timeout=5,
    )
    response.raise_for_status()
    for row in reversed(response.json()):
        if any(value is not None for value in row[1:]):
            values: Values = {}
            for (name, quantity), value in zip(wanted, row[1:]):
                values.setdefault(name, {})[quantity] = value
            return row[0], values
    return None
