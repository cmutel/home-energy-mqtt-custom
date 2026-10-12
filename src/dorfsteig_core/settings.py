from pathlib import Path

from platformdirs import user_data_dir
from pydantic_settings import BaseSettings, SettingsConfigDict

_default_db_path = Path(user_data_dir("dorfsteig")) / "dorfsteig.sqlite"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DORFSTEIG_")

    iotawatt_host: str = "iotawatt.local"
    iotawatt_requests_per_second: float = 2.0

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883

    db_path: Path = _default_db_path

    # Legacy InfluxDB with MBMD history (written by ~/DataNetwork/mbmd_listener.py)
    influx_url: str = "http://localhost:8086"
    influx_db: str = "dorfsteig"

    # House location for weather data (Open-Meteo) and the nearest SwissMetNet
    # station (lower-case abbreviation, see MeteoSwiss ogd-smn_meta_stations.csv).
    # Keep them in .env: the repo is public.
    latitude: float | None = None
    longitude: float | None = None
    meteoswiss_station: str | None = None

    # Heating controller web server; nginx proxies the ESP32 URLs to it
    control_host: str = "127.0.0.1"
    control_port: int = 8002

    log_level: str = "INFO"

    # Pushover (https://pushover.net); messages are only logged if unset
    pushover_token: str = ""
    pushover_user: str = ""


settings = Settings()
