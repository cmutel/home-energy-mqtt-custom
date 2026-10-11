"""Domain objects for air quality sensors (Luftdaten-style).

Each sensor publishes three MQTT messages per reading, assembled here
into a single AirQualityReading. Topics follow the pattern:

    haus/{room}/feinstaub   → pm25, pm10
    haus/{room}/meteo       → temperature, humidity
    haus/{room}/netzwerk    → signal

Known sensors:
    room='balkon'  sensor_id='6089660'
    room='küchen'  sensor_id='6102434'

Timestamps in the payload are local time (Europe/Zurich).

Example (balkon, clear day):
    room        = 'balkon'
    sensor_id   = '6089660'
    pm25        = 0.1    # µg/m³
    pm10        = 0.1    # µg/m³
    temperature = 10.5   # °C
    humidity    = 99.9   # %
    signal      = -43    # dBm

Example (küchen):
    room        = 'küchen'
    sensor_id   = '6102434'
    pm25        = 3.08   # µg/m³
    pm10        = 3.45   # µg/m³
    temperature = 18.9   # °C
    humidity    = 61.3   # %
    signal      = -73    # dBm
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AirQualityReading:
    """Combined air quality snapshot assembled from three MQTT messages."""
    timestamp: int    # Unix epoch seconds (derived from payload local time)
    room: str
    sensor_id: str
    pm25: float       # µg/m³ — PM2.5 particulates
    pm10: float       # µg/m³ — PM10 particulates
    temperature: float  # °C
    humidity: float     # %
    signal: int         # dBm — WiFi signal strength
