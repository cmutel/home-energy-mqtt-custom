from datetime import datetime, time

from dorfsteig_control.config import AlwaysOn, DailyWindow, OneOff
from dorfsteig_control.schedule import in_daily_window, rule_active


def test_daily_window():
    assert in_daily_window(time(7), time(9), time(7))
    assert in_daily_window(time(7), time(9), time(8, 59))
    assert not in_daily_window(time(7), time(9), time(9))
    assert not in_daily_window(time(7), time(9), time(6, 59))


def test_daily_window_across_midnight():
    # The legacy controller never matched windows like this
    assert in_daily_window(time(22), time(2), time(23))
    assert in_daily_window(time(22), time(2), time(1, 30))
    assert not in_daily_window(time(22), time(2), time(2))
    assert not in_daily_window(time(22), time(2), time(12))


def test_rules():
    now = datetime(2026, 1, 8, 1, 0)
    assert rule_active(AlwaysOn(heater="upstairs_water"), now)
    assert not rule_active(AlwaysOn(heater="upstairs_water", enabled=False), now)
    assert rule_active(DailyWindow(heater="kids_bedroom", start=time(0, 15), stop=time(3, 30)), now)
    assert rule_active(
        OneOff(heater="kids_bedroom", start=datetime(2026, 1, 7), stop=datetime(2026, 1, 9)), now
    )
    assert not rule_active(
        OneOff(heater="kids_bedroom", start=datetime(2026, 1, 9), stop=datetime(2026, 1, 10)), now
    )
