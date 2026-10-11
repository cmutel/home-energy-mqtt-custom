"""Command line entry point: `dorfsteig-control <command>`."""

import argparse
import asyncio
import json
from pathlib import Path

from dorfsteig_control.config import from_legacy
from dorfsteig_control.service import Controller, serve
from dorfsteig_control.store import Store
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


if __name__ == "__main__":
    main()
