"""Water heaters on PV surplus, with backup heating.

The relay only allows heating; each tank's own thermostat decides. A tank
that is full draws nothing, so keeping its relay on costs nothing.

In "pv" mode, per tick, for each tank in `water.heaters` order:

1. Backup: if the tank hasn't been seen full since `full_since` on the day
   of the latest `backup_at` (e.g. since 10:00, checked from 16:00), or not
   for `safety_h`, the relay is on until it's full.
2. Stale grid data: relays on. For water heaters on is the safe state, as
   for the boards' own fallback.
3. PV, on the smoothed grid power (positive = export):
   - export above heater power + `on_margin_w`: switch on the first tank
     that's off (and has been off for `min_off_s`);
   - import above `off_import_w`: switch off the last tank switched on for
     PV (once it's been on for `min_on_s`), unless room heaters are still on
     PV: they're shed first.
   One switch per tick, and none within `settle_s` of the last one, so the
   boards (polling every 15 s) and the meter can catch up.

"Full" = relay on for `full_after_s` without the heater drawing: measured
by IoTaWatt downstairs, estimated upstairs (house_minutes). Replayed on
2026-04 to 2026-10 data (see replay.py for the current numbers).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from dorfsteig_control.config import WaterParams
from dorfsteig_core.iotawatt import WATER_HEATER_W

HEATER_W = WATER_HEATER_W   # each tank, across two phases
DRAW_THRESHOLD_W = 100

# Where each tank's draw comes from, as (table, SQL expression for its power in W).
# Downstairs the CT is on one of the heater's two legs, so it's twice the channel.
DRAW_SOURCES = {
    "downstairs_water": ("iotawatt_readings", "water_heater * 2"),
    "upstairs_water": ("house_minutes", "upstairs_water"),
}


@dataclass(frozen=True)
class WaterDecision:
    heater: str
    on: bool
    why: str          # "water pv", "water backup", "water safety", "stale data", "import"


def backup_due(params: WaterParams, heater: str, now: datetime, last_full: float) -> str | None:
    """Why the tank must heat now regardless of PV, or None."""
    if now.timestamp() - last_full > params.safety_h.get(heater, 24) * 3600:
        return "water safety"
    backup = datetime.combine(now.date(), params.backup_at, now.tzinfo)
    if backup > now:
        backup -= timedelta(days=1)
    since = datetime.combine(backup.date(), params.full_since, now.tzinfo)
    if last_full < since.timestamp():
        return "water backup"
    return None


def decide_water(
    params: WaterParams,
    now: datetime,
    relays: Mapping[str, tuple[bool, float, str | None]],   # heater → (on, since, reason)
    grid_avg: float | None,
    stale: bool,
    last_full: Mapping[str, float],
    last_switch: float,
    room_heaters_on_pv: bool,
    managed: set[str],
) -> list[WaterDecision]:
    """Relay changes for the water heaters in `managed` (those not held by an override or rule)."""
    ts = now.timestamp()
    decisions: list[WaterDecision] = []
    tanks = [h for h in params.heaters if h in managed]

    pv_candidates = []
    for heater in tanks:
        on, since, reason = relays[heater]
        why = backup_due(params, heater, now, last_full.get(heater, 0.0))
        if why is None and stale:
            why = "stale data"
        if why is not None:
            if not on or reason != why:
                decisions.append(WaterDecision(heater, True, why))
            continue
        if on and reason in ("water backup", "water safety", "stale data"):
            # Backup done (tank full) or data back: from here on, PV decides
            decisions.append(WaterDecision(heater, True, "water pv"))
        pv_candidates.append(heater)

    if stale or grid_avg is None or ts - last_switch < params.settle_s:
        return decisions

    if grid_avg >= HEATER_W + params.on_margin_w:
        for heater in pv_candidates:
            on, since, _ = relays[heater]
            if not on and ts - since >= params.min_off_s:
                decisions.append(WaterDecision(heater, True, "water pv"))
                break
    elif grid_avg < -params.off_import_w and not room_heaters_on_pv:
        for heater in reversed(pv_candidates):
            on, since, reason = relays[heater]
            if on and reason == "water pv" and ts - since >= params.min_on_s:
                decisions.append(WaterDecision(heater, False, "import"))
                break
    return decisions


def detect_full(conn: sqlite3.Connection, heater: str, on_since: float, full_after_s: float) -> float | None:
    """Latest time the tank was seen full: relay on for full_after_s without drawing.

    Uses the newest minutes available (IoTaWatt and house_minutes arrive a
    few minutes late). Returns None if that can't be seen yet.
    """
    table, power = DRAW_SOURCES[heater]
    try:
        rows = conn.execute(
            f"SELECT ts, {power} FROM {table} WHERE ts >= ? AND {power} IS NOT NULL ORDER BY ts",
            (on_since + 60,),   # skip the minute the relay switched in
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    end = rows[-1][0] + 60
    window = [w for ts, w in rows if ts >= end - full_after_s]
    if end - on_since < full_after_s or len(window) < 0.8 * full_after_s / 60:
        return None
    if all(w < DRAW_THRESHOLD_W for w in window):
        return float(end)
    return None
