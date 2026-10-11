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

    log_level: str = "INFO"


settings = Settings()
