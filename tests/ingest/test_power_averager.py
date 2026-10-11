from pathlib import Path

from dorfsteig_core.db import init_db
from dorfsteig_core.domain.power import GridReading
from dorfsteig_ingest.power import MinuteAverager


def test_minute_average_written_on_rollover(tmp_path: Path):
    conn = init_db(tmp_path / "test.sqlite")
    averager = MinuteAverager(conn)
    averager._current_minute = 600

    averager.on_grid(GridReading(timestamp=605, power=-1000.0, import_total=10.0, export_total=5.0))
    averager.on_grid(GridReading(timestamp=630, power=-2000.0, import_total=10.1, export_total=5.0))
    assert conn.execute("SELECT COUNT(*) FROM grid_readings").fetchone()[0] == 0

    # First reading of the next minute flushes the previous one
    averager.on_grid(GridReading(timestamp=661, power=0.0, import_total=10.2, export_total=5.0))
    rows = conn.execute("SELECT ts, power, import_total FROM grid_readings").fetchall()
    assert rows == [(600, -1500.0, 10.1)]
