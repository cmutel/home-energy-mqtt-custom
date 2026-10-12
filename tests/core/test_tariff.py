from datetime import date, datetime

from dorfsteig_core.tariff import easter, is_high_tariff, prices_for


def test_easter():
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)


def test_high_tariff_times():
    monday = datetime(2026, 10, 12)
    assert not is_high_tariff(monday.replace(hour=6, minute=59))
    assert is_high_tariff(monday.replace(hour=7))
    assert is_high_tariff(monday.replace(hour=19, minute=59))
    assert not is_high_tariff(monday.replace(hour=20))
    saturday = datetime(2026, 10, 17)
    assert is_high_tariff(saturday.replace(hour=12, minute=30))
    assert not is_high_tariff(saturday.replace(hour=13))
    assert not is_high_tariff(datetime(2026, 10, 18, 12))   # Sunday


def test_holidays_are_low_tariff():
    assert not is_high_tariff(datetime(2026, 4, 3, 12))     # Good Friday
    assert not is_high_tariff(datetime(2026, 5, 14, 12))    # Ascension
    assert not is_high_tariff(datetime(2026, 12, 25, 12))


def test_aware_datetimes_are_converted():
    # 05:30 UTC is 07:30 in Zurich in summer
    assert is_high_tariff(datetime.fromisoformat("2026-07-01T05:30:00+00:00"))
    assert not is_high_tariff(datetime.fromisoformat("2026-07-01T04:30:00+00:00"))


def test_prices():
    assert prices_for(date(2026, 6, 1)).import_price(True) == 0.2727
    assert prices_for(date(2026, 6, 1)).export_price(False) == 0.0923
    assert prices_for(date(2027, 6, 1)).import_price(False) == 0.2573
    assert prices_for(date(2025, 6, 1)) is None
