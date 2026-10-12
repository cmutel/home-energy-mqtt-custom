"""Fetch one-minute readings from the IoTaWatt and store them in SQLite.

Two entry points share the same fetch code:

* `poll` runs continuously, fetching anything newer than the last stored row.
* `backfill` walks forward from a start date, skipping complete hours.
  It resumes automatically if interrupted.

Times are sent and received as Unix timestamps (`time.utc.unix`). Date
strings and `time.unix` are interpreted in the IoTaWatt's own timezone
setting, which is a fixed offset without DST (currently -1). Before
2026-10-12 this module used them, which stored everything an hour early and
read the last hour as empty future rows; `shift_timestamps` repairs that.
Rows without any values (e.g. the IoTaWatt was offline) are not stored, so
they are fetched again later.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

from dorfsteig_core.db import init_db
from dorfsteig_core.iotawatt import COLUMNS, SELECT  # shared with the controller's live view
from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

POLL_INTERVAL = 300   # seconds between polls
MAX_RETRIES   = 3
RETRY_BACKOFF = 10    # seconds; multiplied by attempt number

# First day with IoTaWatt data
DEFAULT_START = datetime(2023, 8, 10, tzinfo=timezone.utc)

INSERT_SQL = """
    INSERT OR REPLACE INTO iotawatt_readings (
        ts,
        input_0, mains_a, mains_b, oven,
        zone_a, zone_b, zone_c, zone_d, zone_e, zone_f,
        clothes_washer, clothes_dryer, water_heater, dishwasher, stovetop
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def fetch_range(client: httpx.Client, begin: datetime, end: datetime) -> list[list]:
    response = client.get(
        f"http://{settings.iotawatt_host}/query",
        params={
            "select": SELECT,
            "begin": str(int(begin.timestamp())),
            "end":   str(int(end.timestamp())),
            "group": "1m",
            "format": "json",
            "header": "yes",
            "limit": "none",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"]


def store_rows(conn: sqlite3.Connection, rows: list[list]) -> int:
    """Store rows that have at least one value. Returns the number stored."""
    rows = [r for r in rows if any(v is not None for v in r[1:])]
    conn.executemany(INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def fetch_with_retry(client: httpx.Client, begin: datetime, end: datetime) -> list[list]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_range(client, begin, end)
        except Exception as e:
            if attempt == MAX_RETRIES:
                log.warning("Giving up on %s after %d attempts: %s", begin.isoformat(), MAX_RETRIES, e)
                return []
            wait = RETRY_BACKOFF * attempt
            log.warning("Attempt %d failed (%s), retrying in %ds", attempt, e, wait)
            time.sleep(wait)
    return []


# ---- continuous polling ---------------------------------------------------

def last_stored(conn: sqlite3.Connection) -> datetime:
    row = conn.execute("SELECT MAX(ts) FROM iotawatt_readings").fetchone()
    if row[0] is None:
        # No data yet — start from 24 hours ago
        return datetime.now(tz=timezone.utc) - timedelta(hours=24)
    return datetime.fromtimestamp(row[0], tz=timezone.utc) + timedelta(minutes=1)


def poll_once(conn: sqlite3.Connection, client: httpx.Client) -> int:
    """Fetch all missing data up to now. Returns number of rows written."""
    # Leave a 2-minute buffer so the IoTaWatt has time to record the current minute
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=2)
    begin = last_stored(conn)

    total = 0
    while begin < now:
        end = min(begin + timedelta(hours=1), now)
        n = store_rows(conn, fetch_with_retry(client, begin, end))
        if n:
            total += n
            log.info("Stored %d rows from %s", n, begin.strftime("%Y-%m-%d %H:%M"))
        begin = end

    return total


def poll() -> None:
    log.info("Starting IoTaWatt poller (host=%s, interval=%ds)", settings.iotawatt_host, POLL_INTERVAL)
    conn = init_db()

    with httpx.Client() as client:
        while True:
            try:
                n = poll_once(conn, client)
                if n:
                    log.info("Poll complete — %d new rows", n)
            except Exception:
                log.exception("Unexpected error during poll")

            time.sleep(POLL_INTERVAL)


# ---- backfill ---------------------------------------------------------------

def parse_start(value: str) -> datetime:
    """Parse a start date: ISO date or relative shorthand (2d, 1w)."""
    now = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.fullmatch(r"(\d+)([dw])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit == "d" else timedelta(weeks=n)
        return now - delta
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def hour_row_count(conn: sqlite3.Connection, begin: datetime) -> int:
    """Return number of rows already stored for the hour starting at begin."""
    end_ts = int((begin + timedelta(hours=1)).timestamp())
    begin_ts = int(begin.timestamp())
    row = conn.execute(
        "SELECT COUNT(*) FROM iotawatt_readings WHERE ts >= ? AND ts < ?",
        (begin_ts, end_ts),
    ).fetchone()
    return row[0]


def backfill(start: datetime = DEFAULT_START, max_hours: int | None = None) -> None:
    """Fetch one hour per request, rate-limited to 1 request per second."""
    conn = init_db()
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)

    total_hours = max(1, int((now - start).total_seconds() / 3600))
    if max_hours is not None:
        total_hours = min(total_hours, max_hours)
    log.info("Backfilling from %s UTC, ~%d hours", start.strftime("%Y-%m-%d %H:%M"), total_hours)

    begin = start
    fetched = skipped = completed = 0
    with httpx.Client() as client:
        while begin < now and skipped + completed < total_hours:
            end = min(begin + timedelta(hours=1), now)
            t0 = time.monotonic()

            # Skip hours that already have a full 60 rows
            expected = int((end - begin).total_seconds() / 60)
            if hour_row_count(conn, begin) >= expected:
                skipped += 1
                begin = end
                continue

            try:
                n = store_rows(conn, fetch_range(client, begin, end))
                fetched += n
                completed += 1
                pct = 100 * (skipped + completed) / total_hours
                log.info("[%5.1f%%] %s  %3d rows  %.2fs", pct, begin.strftime("%Y-%m-%d %H:%M"),
                         n, time.monotonic() - t0)
            except Exception as e:
                log.warning("Error at %s: %s — skipping", begin.isoformat(), e)

            begin = end
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))

    log.info("Done. %d rows fetched, %d hours skipped (already complete). Path: %s",
             fetched, skipped, settings.db_path)


# ---- one-off repair -----------------------------------------------------------

def shift_timestamps(conn: sqlite3.Connection, seconds: int) -> tuple[int, int]:
    """Repair rows stored before the switch to Unix timestamps.

    Deletes rows without values and moves all others by `seconds`. Refuses to
    run twice. Returns (deleted, shifted).
    """
    conn.execute("CREATE TABLE IF NOT EXISTS ingest_meta (key TEXT PRIMARY KEY, value TEXT)")
    if conn.execute("SELECT 1 FROM ingest_meta WHERE key = 'iotawatt_shift'").fetchone():
        raise RuntimeError("IoTaWatt timestamps were already shifted")
    values = " AND ".join(f"{c} IS NULL" for c in COLUMNS)
    with conn:
        deleted = conn.execute(f"DELETE FROM iotawatt_readings WHERE {values}").rowcount
        # Via negative values, so the primary key never collides mid-update
        shifted = conn.execute("UPDATE iotawatt_readings SET ts = -(ts + ?)", (seconds,)).rowcount
        conn.execute("UPDATE iotawatt_readings SET ts = -ts")
        conn.execute("INSERT INTO ingest_meta VALUES ('iotawatt_shift', ?)", (str(seconds),))
    return deleted, shifted
