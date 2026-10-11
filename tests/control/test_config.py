from datetime import datetime, time

import pytest
from pydantic import ValidationError

from dorfsteig_control.config import AlwaysOn, ControlConfig, DailyWindow, OneOff, SolarParams, from_legacy


def test_from_legacy():
    data = {
        "upstairs water": {"always_on": True},
        "downstairs kids bedroom": {
            "always_on": False,
            "start": {"__type__": "time", "dt": "01:00:00"},
            "stop": {"__type__": "time", "dt": "02:30:00"},
        },
        "downstairs playroom north": {
            "always_on": False,
            "start": {"__type__": "datetime", "dt": "2026-11-21T12:00:00"},
            "stop": {"__type__": "datetime", "dt": "2026-11-21T13:30:00"},
        },
        "downstairs living room": {"always_on": False},
    }
    config = from_legacy(data, solar_enabled=False)
    assert config.rules == [
        AlwaysOn(heater="upstairs_water"),
        DailyWindow(heater="kids_bedroom", start=time(1), stop=time(2, 30)),
        OneOff(heater="playroom_north",
               start=datetime(2026, 11, 21, 12), stop=datetime(2026, 11, 21, 13, 30)),
    ]
    assert not config.solar.enabled


def test_json_round_trip():
    config = from_legacy({"downstairs kids bedroom": {
        "start": {"__type__": "time", "dt": "22:00:00"}, "stop": {"__type__": "time", "dt": "02:00:00"}}})
    assert ControlConfig.model_validate_json(config.model_dump_json()) == config


def test_validation():
    with pytest.raises(ValidationError):
        AlwaysOn(heater="sauna")
    with pytest.raises(ValidationError):
        OneOff(heater="kids_bedroom", start=datetime(2026, 1, 2), stop=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        SolarParams(priority=["living_room", "living_room"])
