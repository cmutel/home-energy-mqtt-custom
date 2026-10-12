from datetime import datetime, timezone

from dorfsteig_ingest.iotawatt import parse_start


def test_parse_start_iso():
    assert parse_start("2023-08-10") == datetime(2023, 8, 10, tzinfo=timezone.utc)


def test_parse_start_relative():
    assert (parse_start("1d") - parse_start("1w")).days == 6


def test_fetch_uses_unix_times(monkeypatch):
    import httpx

    from dorfsteig_ingest import iotawatt

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    begin = datetime(2026, 10, 12, 5, tzinfo=timezone.utc)
    iotawatt.fetch_range(client, begin, begin.replace(hour=6))
    assert seen["begin"] == "1791781200" and seen["end"] == "1791784800"
    assert seen["select"].startswith("[time.utc.unix,")


def test_empty_rows_not_stored_and_shift(tmp_path):
    import pytest

    from dorfsteig_core.db import init_db
    from dorfsteig_ingest import iotawatt

    conn = init_db(tmp_path / "t.sqlite")
    empty = [None] * len(iotawatt.COLUMNS)
    full = [230.0] + [100.0] * (len(iotawatt.COLUMNS) - 1)
    assert iotawatt.store_rows(conn, [[0, *full], [60, *full], [120, *empty]]) == 2

    conn.execute("INSERT INTO iotawatt_readings (ts) VALUES (3600)")   # legacy empty row
    assert iotawatt.shift_timestamps(conn, 3600) == (1, 2)
    assert [r[0] for r in conn.execute("SELECT ts FROM iotawatt_readings ORDER BY ts")] == [3600, 3660]
    with pytest.raises(RuntimeError):
        iotawatt.shift_timestamps(conn, 3600)
