"""Command line entry point: `dorfsteig-ingest <command>`.

Each long-running command is its own systemd service (see deploy/systemd/).
"""

import argparse
from datetime import date

from dorfsteig_core.db import init_db
from dorfsteig_core.logs import setup_logging
from dorfsteig_ingest import climate, energy, influx, iotawatt, listen, power


def main() -> None:
    parser = argparse.ArgumentParser(prog="dorfsteig-ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("power", help="Store one-minute MBMD inverter and grid averages")
    sub.add_parser("climate", help="Store air quality sensor readings")
    sub.add_parser("iotawatt", help="Poll the IoTaWatt every 5 minutes")

    bf = sub.add_parser("backfill-iotawatt", help="Backfill IoTaWatt history (resumable)")
    bf.add_argument("--from", dest="start", default=None,
                    help="ISO date or relative (2d, 1w); default 2023-08-10")
    bf.add_argument("--test", action="store_true", help="Only fetch the first 10 hours")

    en = sub.add_parser("energy", help="Keep house_minutes and energy_hourly up to date (every 5 minutes)")
    en.add_argument("--rebuild", action="store_true", help="Recompute everything once and exit")

    im = sub.add_parser("import-influx", help="One-off: import MBMD history from the legacy InfluxDB")
    im.add_argument("--from", dest="start", default=None, help="ISO date; default 2024-11-01")

    sh = sub.add_parser("shift-iotawatt", help="One-off: repair timestamps stored before 2026-10-12")
    sh.add_argument("hours", type=int, help="Hours to add to every stored IoTaWatt timestamp")

    ls = sub.add_parser("listen", help="Print one example payload per MQTT topic")
    ls.add_argument("topic", nargs="?", default="#")
    ls.add_argument("minutes", nargs="?", type=float, default=10)

    args = parser.parse_args()
    setup_logging()

    match args.command:
        case "power":
            power.run()
        case "climate":
            climate.run()
        case "iotawatt":
            iotawatt.poll()
        case "backfill-iotawatt":
            start = iotawatt.parse_start(args.start) if args.start else iotawatt.DEFAULT_START
            iotawatt.backfill(start, max_hours=10 if args.test else None)
        case "energy":
            if args.rebuild:
                energy.rebuild(init_db())
            else:
                energy.run()
        case "import-influx":
            influx.import_history(date.fromisoformat(args.start) if args.start else influx.FIRST_DAY)
        case "shift-iotawatt":
            deleted, shifted = iotawatt.shift_timestamps(init_db(), args.hours * 3600)
            print(f"Deleted {deleted} empty rows, shifted {shifted} rows by {args.hours} h")
        case "listen":
            listen.run(args.topic, args.minutes)


if __name__ == "__main__":
    main()
