import json
from datetime import datetime

from dorfsteig_control.config import default_config
from dorfsteig_control.engine import GridSample, decide, initial_states
from dorfsteig_control.outputs import downstairs_payload, legacy_status, upstairs_payload


def test_payloads_byte_identical_to_legacy():
    # Copied from the Pi on 2026-10-11 (water heaters on, everything else off)
    states, _ = decide(initial_states(), default_config(), datetime(2026, 10, 11, 21),
                       GridSample(power=None, age_s=0))
    assert json.dumps(upstairs_payload(states)) == '{"red": 0, "yellow": 0, "green": 0, "blue": 1}'
    assert json.dumps(downstairs_payload(states)) == (
        '{"1": {"red": 0, "yellow": 0, "green": 0, "blue": 0}, '
        '"2": {"red": 0, "yellow": 0, "green": 0, "blue": 1}}'
    )


def test_legacy_status_lists_heaters_that_are_on():
    states, _ = decide(initial_states(), default_config(), datetime(2026, 10, 11, 21),
                       GridSample(power=None, age_s=0))
    status = legacy_status(states, -357.6, datetime(2026, 10, 11, 21, 0, 10).timestamp())
    assert [s["heater"] for s in status["state"]] == ["upstairs water", "downstairs water"]
    assert status["state"][0]["seconds on"] == "10.0"
    assert status["power"] == -357.6
