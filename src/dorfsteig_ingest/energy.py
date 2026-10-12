"""Derive whole-house, upstairs and hourly energy/cost tables.

The house has two breaker panels. IoTaWatt measures the downstairs one;
MBMD measures the whole house at the grid connection, so:

    house      = PV - grid          (grid is positive when exporting)
    downstairs = mains A + B + zone C + clothes washer
                 + water heater + oven + stovetop (their second leg)
    upstairs   = house - downstairs

The supply is three-phase but the downstairs mains CTs only cover two
phases. Zone C and the washer are on the third phase with their own CTs, so
they're added. The water heater, oven and stovetop are two-phase loads with
a CT on one leg; their other leg is on the third phase and draws the same,
so it's added once more (see dorfsteig_core.iotawatt). Anything else on the
third phase without a CT can't be seen and ends up in upstairs.

The upstairs water heater (2950 W, same model as the downstairs one) isn't
measured. It switches by its own thermostat, so it's found from steps of
about ±2950 W in the upstairs signal: on after a step up, off after a step
down or when upstairs power drops below what the heater alone draws. Runs
shorter than 6 minutes are dropped and runs are cut after 4 hours.
(Until 2026-10-12 it looked for 1498 W steps, and mostly found the
downstairs heater's unmeasured second leg instead.)

Results go to house_minutes and energy_hourly (schema in dorfsteig_core.db).
Both can be recomputed at any time from the raw tables.
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone

from dorfsteig_core.db import DERIVED_TABLES, init_db
from dorfsteig_core.iotawatt import WATER_HEATER_W, downstairs_sql
from dorfsteig_core.tariff import TZ, is_high_tariff, prices_for

log = logging.getLogger(__name__)

UPSTAIRS_WATER_W = WATER_HEATER_W
DOWNSTAIRS = downstairs_sql()
STEP_TOL_W = 300.0
MIN_RUN = 6          # minutes
MAX_RUN = 240        # minutes
PV_FILL = 5          # minutes: use the nearest PV reading this close if one is missing
LOOKBACK = 6 * 3600  # seconds of history to settle the water heater state
MIN_MINUTES = 15     # per hour, below this hourly values are left empty
MAX_COUNTER_WH = 20_000  # a counter jump above this in one hour is a glitch
COUNTER_SLACK = 300      # seconds: counter readings must be this close to the hour boundaries
UPDATE_INTERVAL = 300
CHUNK = 30 * 86400


# ---- per minute

def detect_water_heater(upstairs: list[float | None], watts: float = UPSTAIRS_WATER_W,
                        tol: float = STEP_TOL_W) -> list[bool]:
    """Minutes in which the water heater draws power, from steps in `upstairs`."""
    n = len(upstairs)
    on = [False] * n
    state = False
    for t in range(1, n - 1):
        before, now, after = upstairs[t - 1], upstairs[t], upstairs[t + 1]
        if before is None or now is None or after is None:
            state = False
            continue
        # Minute averages smear a step over the minute it happens in, so
        # compare the minutes either side of it
        step = after - before
        if not state:
            if abs(step - watts) < tol and after >= watts - tol:
                state = True
                on[t] = now - before > watts / 2
                continue
        elif abs(step + watts) < tol or now < watts - tol:
            state = False
            on[t] = before - now < watts / 2
            continue
        on[t] = state
    return clean_runs(on)


def clean_runs(on: list[bool], min_run: int = MIN_RUN, max_run: int = MAX_RUN) -> list[bool]:
    out = list(on)
    start = None
    for t, value in enumerate([*on, False]):
        if value and start is None:
            start = t
        elif not value and start is not None:
            if t - start < min_run:
                out[start:t] = [False] * (t - start)
            elif t - start > max_run:
                out[start + max_run:t] = [False] * (t - start - max_run)
            start = None
    return out


def fill_pv(pv: list[float | None], has_grid: list[bool]) -> list[float | None]:
    """Fill missing PV from the nearest reading within PV_FILL minutes, else 0 W.

    MBMD sometimes skips inverter readings, and the inverter sleeps at night.
    Only minutes with a grid reading are filled.
    """
    out = list(pv)
    for t, value in enumerate(pv):
        if value is not None or not has_grid[t]:
            continue
        for d in range(1, PV_FILL + 1):
            near = [v for v in (pv[t - d] if t >= d else None, pv[t + d] if t + d < len(pv) else None)
                    if v is not None]
            if near:
                out[t] = near[0]
                break
        else:
            out[t] = 0.0
    return out


def compute_minutes(conn: sqlite3.Connection, start: int, end: int) -> int:
    """Recompute house_minutes for [start, end). Returns rows written."""
    first = start - LOOKBACK
    stamps = list(range(first, end, 60))
    index = {ts: i for i, ts in enumerate(stamps)}
    n = len(stamps)
    grid: list[float | None] = [None] * n
    pv: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    for ts, p in conn.execute("SELECT ts, power FROM grid_readings WHERE ts >= ? AND ts < ?", (first, end)):
        if ts in index:
            grid[index[ts]] = p
    for ts, p in conn.execute("SELECT ts, power_ac FROM inverter_readings WHERE ts >= ? AND ts < ?",
                              (first, end)):
        if ts in index:
            pv[index[ts]] = p
    iotawatt = f"SELECT ts, {DOWNSTAIRS} FROM iotawatt_readings WHERE ts >= ? AND ts < ?"
    for ts, d in conn.execute(iotawatt, (first, end)):
        if ts in index and d is not None:
            down[index[ts]] = d

    pv = fill_pv(pv, [g is not None for g in grid])
    house = [None if g is None or p is None else p - g for g, p in zip(grid, pv)]
    upstairs = [None if h is None or d is None else h - d for h, d in zip(house, down)]
    water = [UPSTAIRS_WATER_W * on for on in detect_water_heater(upstairs)]

    rows = [
        (ts, pv[i], house[i], down[i], upstairs[i],
         None if upstairs[i] is None else water[i],
         None if upstairs[i] is None else upstairs[i] - water[i])
        for i, ts in enumerate(stamps)
        if ts >= start and (house[i] is not None or down[i] is not None)
    ]
    conn.execute("DELETE FROM house_minutes WHERE ts >= ? AND ts < ?", (start, end))
    conn.executemany("INSERT INTO house_minutes VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


# ---- per hour

def _mean_wh(values: list[float]) -> float | None:
    """Average power over the available minutes → Wh for the whole hour."""
    return sum(values) / len(values) if len(values) >= MIN_MINUTES else None


def _counter_wh(prev: float | None, last: float | None) -> float | None:
    if prev is None or last is None:
        return None
    wh = (last - prev) * 1000
    return wh if 0 <= wh <= MAX_COUNTER_WH else None


GRID_POWER = "SELECT power FROM grid_readings WHERE ts >= ? AND ts < ?"
COUNTERS = ("SELECT import_total, export_total FROM grid_readings "
            "WHERE ts >= ? AND ts < ? AND import_total IS NOT NULL ORDER BY ts DESC LIMIT 1")


def compute_hours(conn: sqlite3.Connection, start: int, end: int) -> int:
    """Recompute energy_hourly for whole hours in [start, end). Returns rows written."""
    start, end = start // 3600 * 3600, end // 3600 * 3600
    rows = []
    for hour in range(start, end, 3600):
        nxt = hour + 3600
        grid = conn.execute(GRID_POWER, (hour, nxt)).fetchall()
        # Counter readings close to both hour boundaries, so the difference covers this hour only
        prev = conn.execute(COUNTERS, (hour - COUNTER_SLACK, hour)).fetchone()
        last = conn.execute(COUNTERS, (nxt - COUNTER_SLACK, nxt)).fetchone()
        minutes = conn.execute("SELECT pv, upstairs, upstairs_water, upstairs_other FROM house_minutes "
                               "WHERE ts >= ? AND ts < ?", (hour, nxt)).fetchall()
        iota = conn.execute(f"SELECT {DOWNSTAIRS}, water_heater FROM iotawatt_readings "
                            "WHERE ts >= ? AND ts < ? AND mains_a IS NOT NULL", (hour, nxt)).fetchall()
        if not grid and not iota:
            continue

        powers = [p for (p,) in grid if p is not None]
        import_wh = _counter_wh(prev and prev[0], last and last[0])
        export_wh = _counter_wh(prev and prev[1], last and last[1])
        if import_wh is None:
            import_wh = _mean_wh([max(-p, 0) for p in powers])
        if export_wh is None:
            export_wh = _mean_wh([max(p, 0) for p in powers])

        # Energy balance with the exact import/export counters
        pv_wh = _mean_wh([pv for pv, _, _, _ in minutes if pv is not None])
        house_wh = None
        if pv_wh is not None and import_wh is not None and export_wh is not None:
            house_wh = pv_wh + import_wh - export_wh

        downstairs_wh = _mean_wh([d for d, _ in iota])
        downstairs_water_wh = _mean_wh([2 * (w or 0) for _, w in iota])   # two-phase: twice the channel
        upstairs_wh = upstairs_water_wh = upstairs_other_wh = None
        if house_wh is not None and downstairs_wh is not None:
            upstairs_wh = house_wh - downstairs_wh
            upstairs_water_wh = _mean_wh([w for _, u, w, _ in minutes if u is not None])
            if upstairs_water_wh is not None:
                upstairs_other_wh = upstairs_wh - upstairs_water_wh

        local = datetime.fromtimestamp(hour, timezone.utc).astimezone(TZ)
        high = is_high_tariff(local)
        prices = prices_for(local.date())
        import_price = export_price = import_chf = export_chf = None
        if prices is not None:
            import_price, export_price = prices.import_price(high), prices.export_price(high)
            import_chf = None if import_wh is None else import_wh / 1000 * import_price
            export_chf = None if export_wh is None else export_wh / 1000 * export_price

        rows.append((hour, pv_wh, import_wh, export_wh, house_wh, downstairs_wh, downstairs_water_wh,
                     upstairs_wh, upstairs_water_wh, upstairs_other_wh, int(high), import_price, export_price,
                     import_chf, export_chf,
                     len(powers), len(iota)))

    conn.execute("DELETE FROM energy_hourly WHERE ts >= ? AND ts < ?", (start, end))
    conn.executemany(f"INSERT INTO energy_hourly VALUES ({', '.join('?' * 17)})", rows)
    conn.commit()
    return len(rows)


# ---- entry points

def update(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Recompute the last few hours (IoTaWatt data arrives up to ~10 minutes late)."""
    now = int(now or time.time())
    end = now // 60 * 60 - 120
    last = conn.execute("SELECT MAX(ts) FROM energy_hourly").fetchone()[0]
    start = (last - 3 * 3600) if last else end - 2 * 86400
    compute_minutes(conn, start, end)
    compute_hours(conn, start, end)


def rebuild(conn: sqlite3.Connection) -> None:
    """Drop and recompute the derived tables, picking up schema changes."""
    for name, ddl in DERIVED_TABLES.items():
        conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute(ddl)
    conn.commit()
    first = conn.execute(
        "SELECT MIN(ts) FROM (SELECT MIN(ts) ts FROM grid_readings UNION ALL "
        "SELECT MIN(ts) FROM iotawatt_readings WHERE mains_a IS NOT NULL)"
    ).fetchone()[0]
    if first is None:
        log.info("No data")
        return
    end = int(time.time()) // 60 * 60 - 120
    for chunk in range(first // 3600 * 3600, end, CHUNK):
        stop = min(chunk + CHUNK, end)
        m = compute_minutes(conn, chunk, stop)
        h = compute_hours(conn, chunk, stop)
        log.info("%s: %d minutes, %d hours", datetime.fromtimestamp(chunk, TZ).strftime("%Y-%m-%d"), m, h)


def run() -> None:
    conn = init_db()
    log.info("Updating energy tables every %d s", UPDATE_INTERVAL)
    while True:
        try:
            update(conn)
        except Exception:
            log.exception("Energy update failed")
        time.sleep(UPDATE_INTERVAL)
