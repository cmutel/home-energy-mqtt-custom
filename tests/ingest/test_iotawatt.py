from datetime import datetime, timezone

from dorfsteig_ingest.iotawatt import parse_start


def test_parse_start_iso():
    assert parse_start("2023-08-10") == datetime(2023, 8, 10, tzinfo=timezone.utc)


def test_parse_start_relative():
    assert (parse_start("1d") - parse_start("1w")).days == 6
