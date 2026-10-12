"""Electricity prices: high (HT) and low (NT) tariff for import and export.

High tariff: Monday–Friday 07:00–20:00 and Saturday 07:00–13:00, local time.
Everything else is low tariff, including the public holidays below. Every
period starts on the hour, so a whole hour is either HT or NT.

Prices are in CHF/kWh including VAT, from the utility's price sheets. Years
without prices have no costs (`prices_for` returns None).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")


@dataclass(frozen=True)
class Prices:
    import_ht: float
    import_nt: float
    export_ht: float
    export_nt: float

    def import_price(self, high: bool) -> float:
        return self.import_ht if high else self.import_nt

    def export_price(self, high: bool) -> float:
        return self.export_ht if high else self.export_nt


PRICES: dict[int, Prices] = {
    2026: Prices(import_ht=0.2727, import_nt=0.2511, export_ht=0.0963, export_nt=0.0923),
    2027: Prices(import_ht=0.2573, import_nt=0.2573, export_ht=0.1035, export_nt=0.1035),
}


def prices_for(day: date) -> Prices | None:
    return PRICES.get(day.year)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays(year: int) -> set[date]:
    """Public holidays billed at low tariff (national, plus the common German-Swiss ones)."""
    e = easter(year)
    return {
        date(year, 1, 1), date(year, 1, 2),             # New Year, Berchtoldstag
        e - timedelta(days=2), e + timedelta(days=1),   # Good Friday, Easter Monday
        e + timedelta(days=39), e + timedelta(days=50), # Ascension, Whit Monday
        date(year, 8, 1),                               # National Day
        date(year, 12, 25), date(year, 12, 26),
    }


def is_high_tariff(when: datetime) -> bool:
    """Whether a moment is billed at high tariff. Naive datetimes are local time."""
    local = when.astimezone(TZ) if when.tzinfo else when
    if local.date() in holidays(local.year):
        return False
    weekday, hour = local.weekday(), local.hour
    if weekday < 5:
        return 7 <= hour < 20
    if weekday == 5:
        return 7 <= hour < 13
    return False
