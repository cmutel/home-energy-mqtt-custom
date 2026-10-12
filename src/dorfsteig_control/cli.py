"""Command line entry point: `dorfsteig-control <command>`."""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from dorfsteig_control import replay, replay_night, wattage
from dorfsteig_control.config import from_legacy
from dorfsteig_control.service import Controller, serve
from dorfsteig_control.store import Store
from dorfsteig_core import tariff
from dorfsteig_core.db import init_db
from dorfsteig_core.logs import setup_logging
from dorfsteig_core.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="dorfsteig-control")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the controller and its web server")
    run.add_argument("--dry-run", action="store_true",
                     help="Decide and log, but don't persist heater state (events are flagged dry_run)")
    run.add_argument("--legacy-config", type=Path, default=None,
                     help="Read the legacy config.json on every tick instead of the database config")
    run.add_argument("--legacy-no-solar", action="store_true",
                     help="With --legacy-config: disable PV switching, like HEATING = False")
    run.add_argument("--compare-legacy", type=Path, default=None, metavar="DIR",
                     help="Compare outputs with the legacy upstairs.json/downstairs.json in DIR")
    run.add_argument("--host", default=settings.control_host)
    run.add_argument("--port", type=int, default=settings.control_port)

    imp = sub.add_parser("import-legacy-config", help="Save a legacy config.json as the current config")
    imp.add_argument("path", type=Path)
    imp.add_argument("--no-solar", action="store_true", help="Disable PV switching, like HEATING = False")

    sub.add_parser("show-config", help="Print the current config as JSON")

    rp = sub.add_parser("replay", help="Compare water heater policies on recorded data (offline)")
    rp.add_argument("--from", dest="start", type=date.fromisoformat, default=None,
                    help="Default 2026-04-01; with --night 2025-10-01 (first archived forecasts)")
    rp.add_argument("--to", dest="end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    rp.add_argument("--prices", type=int, default=None, help="Use one year's prices, e.g. 2027")
    rp.add_argument("--rooms", action="store_true",
                    help="Storage heaters on PV surplus instead (solar priority, measured wattages)")
    rp.add_argument("--night", action="store_true",
                    help="Night plan policies over the days with stored day forecasts")

    args = parser.parse_args()
    setup_logging()
    store = Store(init_db())

    match args.command:
        case "run":
            controller = Controller(
                store,
                dry_run=args.dry_run,
                legacy_config=args.legacy_config,
                legacy_solar_enabled=not args.legacy_no_solar,
                compare_dir=args.compare_legacy,
            )
            try:
                asyncio.run(serve(controller, args.host, args.port))
            except KeyboardInterrupt:
                pass
        case "import-legacy-config":
            config = from_legacy(json.loads(args.path.read_text()), solar_enabled=not args.no_solar)
            store.save_config(config, source=f"legacy import: {args.path}")
            print(config.model_dump_json(indent=2))
        case "show-config":
            print(store.load_config().model_dump_json(indent=2))
        case "replay" if args.night:
            config = store.load_config()
            heaters = {h: watts for h in config.solar.priority
                       if (watts := wattage.estimated_watts(store.conn, h)[0]) > wattage.NO_LOAD_W}
            start = args.start or date(2025, 10, 1)
            days = replay_night.load_days(store.conn, start, args.end, heaters, config.solar.margin_w)
            if not days:
                print("No days with day forecasts: run `dorfsteig-forecast backfill` first")
                return
            results = replay_night.run_all(days, config.solar.temp_limit, config.night.kwh_per_degree,
                                           config.night.min_kwh)
            print(replay_night.night_report(results, days))
        case "replay" if args.rooms:
            # Heaters that drew nothing in their last test (switched off by hand) are left out
            heaters = {h: watts for h in store.load_config().solar.priority
                       if (watts := wattage.estimated_watts(store.conn, h)[0]) > wattage.NO_LOAD_W}
            runs = replay.load_grid_days(store.conn, args.start or date(2026, 4, 1), args.end)
            temps = replay.daily_mean_temperature(store.conn)
            policies = [replay.RoomPolicy(f"limit {t} °C", temp_limit=t) for t in (12, 15, 18)]
            print(replay.room_report([replay.replay_rooms(p, runs, heaters, temps) for p in policies]))
        case "replay":
            prices = tariff.PRICES[args.prices] if args.prices else None
            runs = replay.load_days(store.conn, args.start or date(2026, 4, 1), args.end, prices)
            forecast = replay.load_forecast(store.conn)
            print(replay.report([replay.replay(p, runs, forecast) for p in replay.POLICIES]))


if __name__ == "__main__":
    main()
