"""Dishwasher heater watch: alerts when the heating element starts failing.

In November 2026 the heating element's thermal fuse opened. The IoTaWatt
`dishwasher` channel showed it coming: half-heated runs on 19 Sep and 9 Oct
(the second heating phase never came on), then the heater flickering inside
its heating blocks on 31 Oct and 1 Nov, then no heating at all. A normal
program heats twice at a flat ~1.9 kW; the element draws 1.9 kW or nothing,
so a one-minute average between the two inside a heating block means it
switched off for seconds at a time.

Three checks, each run once on every finished full program:

1. Heater missing: the run used less than `MISSING_WH` (one or both heating
   phases didn't run). This was the earliest sign of the 2026 failure, so a
   single run alerts.
2. Flicker: the run has `FLICKER_ALERT_MIN` or more flicker minutes (see
   `flicker_minutes`). Single partial minutes are normal (the thermostat
   holding temperature), so one isn't enough.
3. Drift: the median energy of the last `DRIFT_RUNS` normal runs is more than
   `DRIFT_SHARE` above or below the seasonal normal, e.g. a temperature
   sensor reading too cold so the controller keeps heating. A median ignores a
   single big load (a holiday meal), but follows a lasting change.

The seasonal normal is the median energy of healthy runs at the same time of
year in other years (within `SEASON_DAYS` of the calendar date, at least
`OTHER_YEAR_DAYS` away), because the inlet water is colder in winter: runs
use about 1.25 kWh in August and 1.6 kWh in January.

Back-tested on all 569 full programs from August 2023 to November 2026:
- heater missing: 22 Sep, 3 Nov and 8 Nov 2023, 3 to 8 Feb 2024 (four runs), 22 Apr
  2024, 19 Sep and 9 Oct 2026, and every run from 1 Nov 2026 to the repair;
- flicker: 31 Oct and 1 Nov 2026, and two unusually long midday programs (30
  Nov and 27 Dec 2023, over 2 kWh);
- drift: 27 Apr to 3 May 2024, when runs heated for 60 to 70 instead of 53
  minutes (+17%). Otherwise the 7-run median stayed within -9% to +14%.
The seasonal normal needs a previous year at the same time of year, so the
drift check is silent where there is none (winter 2023-24).
"""

import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# Run detection, as in the November 2026 analysis
ACTIVE_W = 5                  # the dishwasher is running when it draws more than this
RUN_GAP_S = 20 * 60           # a run ends after this long seen idle
FULL_PROGRAM_MIN = 100        # shorter runs are rinse cycles or aborted programs; ignored

# Heater power levels (one-minute averages)
HEATER_ON_W = 800             # a "heater minute": the element was on for most of the minute
FULL_POWER_W = 1700           # the element ran for the whole minute (it draws ~1.9 kW)
PARTIAL_W = 300               # above the pump alone (~60 W): the element ran for part of the minute
FLICKER_W = 1000              # a flicker minute: the element was on most of the minute, but not all

# Alert thresholds
MISSING_WH = 950              # heater missing: normal runs use 1086 to 2233 Wh, half-heated ones 693 to 848
FLICKER_ALERT_MIN = 2         # flicker minutes in one run
DRIFT_RUNS = 7                # recent normal runs whose median is compared with the seasonal normal
DRIFT_MIN_RUNS = 5            # ... at least this many, within DRIFT_WINDOW_DAYS
DRIFT_WINDOW_DAYS = 21
DRIFT_SHARE = 0.15            # drift: median more than this far above or below the seasonal normal

# Seasonal normal
SEASON_DAYS = 21              # same time of year: within this many calendar days
OTHER_YEAR_DAYS = 200         # runs at least this far apart are from another year
MIN_SEASON_RUNS = 10          # fewer than this: no seasonal normal

CHECK_WITHIN_S = 6 * 3600     # only check runs that ended this recently (e.g. after a restart)


@dataclass(frozen=True)
class Run:
    start: float              # Unix timestamp of the first minute
    watts: tuple[float, ...]  # one-minute averages, 0 for minutes without a reading

    @property
    def minutes(self) -> int:
        return len(self.watts)

    @property
    def end(self) -> float:
        return self.start + 60 * (self.minutes - 1)

    @property
    def wh(self) -> float:
        return sum(self.watts) / 60

    @property
    def heater_minutes(self) -> int:
        return sum(1 for w in self.watts if w > HEATER_ON_W)

    @property
    def healthy(self) -> bool:
        """Both heating phases ran."""
        return self.wh >= MISSING_WH

    @property
    def flicker(self) -> int:
        return flicker_minutes(self.watts)

    @property
    def label(self) -> str:
        return datetime.fromtimestamp(self.start).strftime("%a %d %b %H:%M")


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    message: str
    repeat_s: float = 24 * 3600   # at most one message a day while every run has the fault


ALERT_KEYS = {"dishwasher-heater", "dishwasher-flicker", "dishwasher-drift"}


def flicker_minutes(watts: Sequence[float]) -> int:
    """Minutes where the element cut out briefly in the middle of a heating block.

    A heating block is a stretch of minutes above PARTIAL_W. A flicker minute
    is one at FLICKER_W to FULL_POWER_W (the element off for a few seconds)
    that comes after a full-power minute in the same block and is followed,
    later in the block, by two full-power minutes in a row: the heater was
    meant to be on, but dropped out. On 31 Oct and 1 Nov 2026 these minutes
    averaged 1470 to 1640 W.

    Not counted:
    - partial minutes at the start or end of a block: the element switching
      on or off part-way through a minute;
    - the thermostat's on/off cycling while it holds the rinse temperature,
      which never returns to two full minutes in a row;
    - minutes below FLICKER_W between two full ones: two of the program's
      short heating blocks a minute apart (1191, 304, 783 W on 20 Oct 2023
      counts only the 1191 W minute, so it stays below FLICKER_ALERT_MIN).
    """
    count = 0
    block: list[float] = []
    for w in [*watts, 0.0]:          # the trailing 0 closes the last block
        if w > PARTIAL_W:
            block.append(w)
            continue
        count += _block_flicker(block)
        block = []
    return count


def _block_flicker(block: list[float]) -> int:
    full = [w >= FULL_POWER_W for w in block]
    # Index of the last minute that starts two full-power minutes in a row
    last_pair = max((i for i in range(len(block) - 1) if full[i] and full[i + 1]), default=-1)
    first_full = full.index(True) if True in full else len(block)
    return sum(1 for i in range(first_full + 1, last_pair) if FLICKER_W <= block[i] < FULL_POWER_W)


def find_runs(conn: sqlite3.Connection, since: float = 0) -> list[Run]:
    """Full programs (FULL_PROGRAM_MIN or longer) starting after `since`, oldest first.

    The last run may still be in progress; `finished` tells.
    """
    rows = conn.execute(
        "SELECT ts, dishwasher FROM iotawatt_readings WHERE ts >= ? AND dishwasher > ? ORDER BY ts",
        (int(since), ACTIVE_W),
    ).fetchall()

    groups: list[list[tuple[int, float]]] = []
    for ts, w in rows:
        if groups and not _quiet_between(conn, groups[-1][-1][0], ts):
            groups[-1].append((ts, w))
        else:
            groups.append([(ts, w)])

    runs = []
    for group in groups:
        start, end = group[0][0], group[-1][0]
        if (end - start) // 60 + 1 < FULL_PROGRAM_MIN:
            continue
        by_ts = dict(group)
        # Minutes without a reading above ACTIVE_W count as 0 W
        watts = tuple(by_ts.get(t, 0.0) for t in range(start, end + 1, 60))
        runs.append(Run(start=start, watts=watts))
    return runs


def _quiet_between(conn: sqlite3.Connection, a: int, b: int) -> bool:
    """Whether the dishwasher was seen idle for RUN_GAP_S between two active minutes.

    Minutes without any IoTaWatt reading don't count as idle: on the night of
    9 Oct 2026 an hour of readings is missing in the middle of a program.
    """
    if b - a <= RUN_GAP_S:
        return False
    readings = conn.execute(
        "SELECT COUNT(*) FROM iotawatt_readings WHERE ts > ? AND ts < ?", (a, b)).fetchone()[0]
    return readings * 60 >= RUN_GAP_S


def finished(conn: sqlite3.Connection, run: Run) -> bool:
    """True once the IoTaWatt has reported RUN_GAP_S of readings after the run's last minute."""
    newest = conn.execute("SELECT MAX(ts) FROM iotawatt_readings").fetchone()[0]
    return newest is not None and newest - run.end >= RUN_GAP_S


def _calendar_distance(a: float, b: float) -> int:
    """Days between two dates' positions in the year, ignoring the year (0 to 182)."""
    days = abs(datetime.fromtimestamp(a).timetuple().tm_yday - datetime.fromtimestamp(b).timetuple().tm_yday)
    return min(days, 365 - days)


def seasonal_normal(runs: list[Run], at: float) -> float | None:
    """Median Wh of healthy runs at the same time of year as `at`, in other years."""
    same_season = [
        r.wh for r in runs
        if r.healthy and abs(r.start - at) >= OTHER_YEAR_DAYS * 86400
        and _calendar_distance(r.start, at) <= SEASON_DAYS
    ]
    if len(same_season) < MIN_SEASON_RUNS:
        return None
    return statistics.median(same_season)


def recent_median(runs: list[Run], run: Run) -> float | None:
    """Median Wh of the last DRIFT_RUNS healthy runs up to and including `run`."""
    recent = [r for r in runs if r.start <= run.start and run.start - r.start <= DRIFT_WINDOW_DAYS * 86400
              and r.healthy][-DRIFT_RUNS:]
    if len(recent) < DRIFT_MIN_RUNS:
        return None
    return statistics.median(r.wh for r in recent)


def check_run(runs: list[Run], run: Run) -> list[Alert]:
    """The alerts for one finished run, given all runs (for the seasonal normal)."""
    alerts = []
    normal = seasonal_normal(runs, run.start)

    if not run.healthy:
        usual = "" if normal is None else f", against a normal {normal / 1000:.2f} kWh for the season"
        alerts.append(Alert(
            "dishwasher-heater", "Dishwasher heater missing",
            f"The run on {run.label} used {run.wh / 1000:.2f} kWh{usual}, with {run.heater_minutes} "
            "heater minutes. One or both heating phases didn't run: the failure in November 2026 "
            "started like this.",
        ))

    if run.flicker >= FLICKER_ALERT_MIN:
        alerts.append(Alert(
            "dishwasher-flicker", "Dishwasher heater flickering",
            f"The heater cut out briefly in {run.flicker} minutes of its heating blocks on {run.label}. "
            "Before the November 2026 failure it flickered like this for two runs.",
        ))

    median = recent_median(runs, run)
    if normal is not None and median is not None and abs(median / normal - 1) > DRIFT_SHARE:
        direction = "more" if median > normal else "less"
        alerts.append(Alert(
            "dishwasher-drift", f"Dishwasher using {direction} energy",
            f"The last {DRIFT_RUNS} normal runs used a median {median / 1000:.2f} kWh, "
            f"{abs(median / normal - 1):.0%} {direction} than the {normal / 1000:.2f} kWh normal "
            "for the season. If it's more, the temperature sensor may read too cold.",
            repeat_s=7 * 24 * 3600,
        ))
    return alerts


def check_latest(
    conn: sqlite3.Connection, now: float, last_checked: float | None
) -> tuple[float | None, list[Alert]]:
    """Check the newest finished run if it hasn't been checked yet.

    Returns (start of the newest finished run, its alerts). Pass the returned
    start back as `last_checked` next time.
    """
    newest = conn.execute(
        "SELECT ts FROM iotawatt_readings WHERE dishwasher > ? ORDER BY ts DESC LIMIT 1", (ACTIVE_W,)
    ).fetchone()
    if newest is None or now - newest[0] > CHECK_WITHIN_S:
        return last_checked, []

    # Runs of the last few hours: is the newest one finished and new?
    recent = find_runs(conn, since=now - CHECK_WITHIN_S - FULL_PROGRAM_MIN * 60 * 3)
    recent = [r for r in recent if finished(conn, r)]
    if not recent or recent[-1].start == last_checked:
        return last_checked, []
    run = recent[-1]
    return run.start, check_run(find_runs(conn), run)
