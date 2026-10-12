from datetime import date

from pytest import approx

from dorfsteig_control.replay_night import Day, replay_night, usable_pv


def test_usable_pv_fits_whole_heaters():
    heaters = {"a": 2000.0, "b": 1000.0}
    # 2500 W export - 100 margin: a fits (2000), b doesn't (400 left); 1200: only b
    assert usable_pv([2500.0] * 60, heaters, 100) == approx(2.0)
    assert usable_pv([1200.0] * 60, heaters, 100) == approx(1.0)
    assert usable_pv([-500.0] * 60, heaters, 100) == 0.0


def day(temperature=5.0, forecast_temperature=5.0, surplus=10.0, usable=4.0) -> Day:
    return Day(date(2026, 1, 10), temperature, forecast_temperature, surplus, usable,
               night_price=0.25, day_price=0.27, export_price=0.09)


def test_policies():
    days = [day()]   # need 2 × (15 - 5) = 20 kWh
    ignore_pv = replay_night("credit 0", days, 15, 2.0, 2.0, credit=0.0)
    assert (ignore_pv.night_kwh, ignore_pv.pv_kwh, ignore_pv.short_kwh) == (20, 0, 0)
    half = replay_night("credit 0.5", days, 15, 2.0, 2.0, credit=0.5)     # 20 - 5 = 15 at night, 4 from PV
    assert (half.night_kwh, half.pv_kwh, half.short_kwh) == (15, 4, 1)
    perfect = replay_night("perfect", days, 15, 2.0, 2.0, credit=None, perfect=True)
    assert (perfect.night_kwh, perfect.pv_kwh, perfect.short_kwh) == (16, 4, 0)
    assert round(perfect.cost_chf, 2) == round(16 * 0.25 + 4 * 0.09, 2)
    none = replay_night("none", days, 15, 2.0, 2.0, credit=None)
    assert (none.night_kwh, none.pv_kwh, none.short_kwh) == (0, 4, 16)


def test_forecast_too_cold_gives_excess():
    result = replay_night("credit 0", [day(temperature=10.0)], 15, 2.0, 2.0, credit=0.0)
    assert (result.need_kwh, result.night_kwh, result.excess_kwh, result.pv_kwh) == (10, 20, 10, 0)
