"""Domain objects for power measurements from the MBMD Modbus gateway.

Messages arrive on MQTT as individual topic/value pairs and are assembled
into snapshots. Two devices are present:

    mbmd/solaredge1-1/      Solar inverter (DC panels → AC output)
    mbmd/solaredge1-1-1/    Grid meter at the mains connection

Sign convention for grid power: positive = exporting to the grid,
negative = importing from the grid. (Verified against data: at night with no
PV the meter reads about -1400 W, and at midday the old heating controller
held it just below +1500 W.)

House consumption can be derived as:
    consumption_w = inverter.power_ac - grid.power
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InverterReading:
    """Snapshot of the SolarEdge inverter (mbmd/solaredge1-1/*).

    Covers the DC input from the panels and the AC output to the house.

    Example values (midday, ~400W production):
        power_dc     = 407.1 W
        voltage_dc   = 747.5 V
        current_dc   = 0.545 A
        power_ac     = 401.0 W
        export_total = 61953.4 kWh  # cumulative lifetime production
    """
    timestamp: int  # Unix epoch seconds

    # DC side — panels → inverter
    power_dc: float      # W
    voltage_dc: float    # V
    current_dc: float    # A

    # AC side — inverter → house / grid
    power_ac: float      # W

    # Cumulative production
    export_total: float  # kWh


@dataclass(frozen=True)
class GridReading:
    """Snapshot of the grid meter at the mains connection (mbmd/solaredge1-1-1/*).

    Power is positive when solar surplus is being exported,
    negative when the house is drawing from the grid.

    Example values (evening, importing ~66W with ~400W solar):
        power        = -66.1 W
        import_total = 56165.1 kWh  # cumulative lifetime import from grid
        export_total = 31377.8 kWh  # cumulative lifetime export to grid
    """
    timestamp: int  # Unix epoch seconds

    power: float          # W, positive = export, negative = import

    # Cumulative energy at the meter
    import_total: float   # kWh — total drawn from grid
    export_total: float   # kWh — total exported to grid
