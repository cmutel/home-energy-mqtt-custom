from datetime import datetime, time

from dorfsteig_control.config import AlwaysOn, DailyWindow, OneOff, Rule


def in_daily_window(start: time, stop: time, now: time) -> bool:
    """Half-open window [start, stop); handles windows that cross midnight."""
    if start <= stop:
        return start <= now < stop
    return now >= start or now < stop


def rule_active(rule: Rule, now: datetime) -> bool:
    if not rule.enabled:
        return False
    match rule:
        case AlwaysOn():
            return True
        case DailyWindow():
            return in_daily_window(rule.start, rule.stop, now.time())
        case OneOff():
            return rule.start <= now < rule.stop
    return False


def scheduled_heaters(rules: list[Rule], now: datetime) -> set[str]:
    """Heaters that at least one active rule wants on."""
    return {rule.heater for rule in rules if rule_active(rule, now)}
