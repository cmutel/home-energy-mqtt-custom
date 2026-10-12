"""Controller configuration: schedule rules and PV surplus parameters.

Stored as JSON in SQLite (see store.py). Phase 2 adds a web UI that edits
this same model; until then use `dorfsteig-control import-legacy-config`.
"""

from datetime import datetime, time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dorfsteig_control.heaters import BY_ID, BY_LEGACY_NAME


class _Model(BaseModel):
    # Validate edits made through the web UI as they happen
    model_config = ConfigDict(validate_assignment=True)


class _Rule(_Model):
    heater: str
    enabled: bool = True
    note: str = ""

    @field_validator("heater")
    @classmethod
    def _known_heater(cls, v: str) -> str:
        if v not in BY_ID:
            raise ValueError(f"Unknown heater {v!r}")
        return v


class AlwaysOn(_Rule):
    kind: Literal["always_on"] = "always_on"


class DailyWindow(_Rule):
    """On every day between two times. A window may cross midnight (22:00–02:00)."""

    kind: Literal["daily"] = "daily"
    start: time
    stop: time


class OneOff(_Rule):
    """On between two specific moments, e.g. a weekend or a two-hour boost."""

    kind: Literal["once"] = "once"
    start: datetime
    stop: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "OneOff":
        if self.stop <= self.start:
            raise ValueError("stop must be after start")
        return self


Rule = Annotated[AlwaysOn | DailyWindow | OneOff, Field(discriminator="kind")]

# Same order as the legacy controller. Heaters not listed here are never
# switched on PV surplus; they only follow schedule rules.
LEGACY_SOLAR_PRIORITY = [
    "corridor_1st_floor",
    "upstairs_guest",
    "living_room",
    "playroom_north",
    "kids_bedroom",
    "playroom_south",
]


class SolarParams(_Model):
    """Thresholds for switching heaters on PV surplus.

    Grid power is positive when exporting. The defaults match the legacy
    controller: add one heater above 1500 W export, remove one below 200 W import.
    """

    enabled: bool = True
    priority: list[str] = Field(default_factory=lambda: list(LEGACY_SOLAR_PRIORITY))
    export_on_w: float = 1500
    import_off_w: float = -200
    min_switch_interval_s: float = 120   # minimum time between switches of one heater
    stale_after_s: float = 120           # shed solar heaters if no grid reading this long

    @field_validator("priority")
    @classmethod
    def _known_heaters(cls, v: list[str]) -> list[str]:
        unknown = [h for h in v if h not in BY_ID]
        if unknown:
            raise ValueError(f"Unknown heaters {unknown}")
        if len(set(v)) != len(v):
            raise ValueError("Duplicate heaters in priority list")
        return v


WaterMode = Literal["always_on", "shadow", "pv"]


class WaterParams(_Model):
    """Water heaters on PV surplus (see water.py).

    always_on: schedule rules only (normally an always-on rule per tank).
    shadow: as always_on, but the PV logic runs alongside and logs what it would do.
    pv: PV surplus first, with a daily backup and a safety rule so a tank is
        never short. Always-on rules for these tanks are set aside; other
        rules (boosts, daily windows) still switch them on.
    """

    mode: WaterMode = "always_on"
    heaters: list[str] = Field(default_factory=lambda: ["downstairs_water", "upstairs_water"])
    backup_at: time = time(16, 0)        # heat until full if not full since full_since
    full_since: time = time(10, 0)
    safety_h: dict[str, float] = Field(  # heat until full if not seen full for this long
        default_factory=lambda: {"downstairs_water": 36, "upstairs_water": 24})
    on_margin_w: float = 100             # export beyond the heater's power needed to switch on
    off_import_w: float = 200            # import tolerated before switching a tank off
    min_on_s: float = 600
    min_off_s: float = 600
    settle_s: float = 60                 # wait after any switch for the boards and the meter
    full_after_s: float = 600            # relay on without draw this long = tank full

    @field_validator("heaters")
    @classmethod
    def _water_heaters(cls, v: list[str]) -> list[str]:
        if any(h not in BY_ID or not BY_ID[h].water for h in v):
            raise ValueError("Only water heaters can be listed")
        return v


class NotifySettings(_Model):
    """Pushover messages. Alerts (stale data, boards offline, wattage drift) are always sent."""

    daily_summary_at: time | None = time(21, 0)
    midday_status_at: time | None = None
    morning_report_at: time | None = time(6, 0)   # yesterday's energy and costs per circuit
    board_offline_after_s: float = 300
    # Air quality / climate sensors (rooms in the air_quality table) to watch; they publish every few minutes
    watched_sensors: list[str] = Field(default_factory=lambda: ["balkon", "küchen"])
    sensor_offline_after_s: float = 30 * 60
    alert_repeat_s: float = 4 * 3600   # don't repeat the same alert more often than this


class ControlConfig(_Model):
    rules: list[Rule] = Field(default_factory=list)
    solar: SolarParams = Field(default_factory=SolarParams)
    water: WaterParams = Field(default_factory=WaterParams)
    notify: NotifySettings = Field(default_factory=NotifySettings)


def default_config() -> ControlConfig:
    """Water heaters always on; everything else by PV surplus."""
    return ControlConfig(rules=[AlwaysOn(heater="upstairs_water"), AlwaysOn(heater="downstairs_water")])


def _legacy_value(value: Any) -> Any:
    """Decode the legacy DateTimeEncoder format: {"__type__": "time", "dt": "07:00:00"}."""
    if isinstance(value, dict) and "__type__" in value:
        if value["__type__"] == "datetime":
            return datetime.fromisoformat(value["dt"])
        if value["__type__"] == "time":
            return time.fromisoformat(value["dt"])
    return value


def from_legacy(data: dict[str, dict], solar_enabled: bool = True) -> ControlConfig:
    """Convert the legacy config.json written by write_config.py."""
    rules: list[Rule] = []
    for legacy_name, entry in data.items():
        heater = BY_LEGACY_NAME[legacy_name].id
        if entry.get("always_on"):
            rules.append(AlwaysOn(heater=heater))
        elif "start" in entry and "stop" in entry:
            start, stop = _legacy_value(entry["start"]), _legacy_value(entry["stop"])
            if isinstance(start, datetime):
                rules.append(OneOff(heater=heater, start=start, stop=stop))
            else:
                rules.append(DailyWindow(heater=heater, start=start, stop=stop))
    return ControlConfig(rules=rules, solar=SolarParams(enabled=solar_enabled))
