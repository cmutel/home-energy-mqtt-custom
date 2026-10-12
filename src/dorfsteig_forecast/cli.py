"""Command line entry point: `dorfsteig-forecast <command>`."""

import argparse
from datetime import date, datetime, timedelta

from dorfsteig_core.db import init_db
from dorfsteig_core.logs import setup_logging
from dorfsteig_core.tariff import TZ
from dorfsteig_forecast import daily


def print_models(conn) -> None:
    pv, _ = daily.load_model(conn, "pv")
    base, _ = daily.load_model(conn, "base_load")
    heating, _ = daily.load_model(conn, "heating")
    if pv:
        first = datetime.fromtimestamp(pv["first"], TZ).date()
        last = datetime.fromtimestamp(pv["last"], TZ).date()
        print(f"PV: fitted on {pv['hours']} hours ({first} to {last}), "
              f"daily error on those {pv['daily_mae_kwh']:.1f} kWh")
    if base:
        print(f"Base load: {sum(base['wh_per_hour']) / 1000:.1f} kWh/day, "
              f"from {base['days']} days at or above {base['from_temperature']:.0f} °C")
        print("  by hour (kWh): " + " ".join(f"{wh / 1000:.1f}" for wh in base["wh_per_hour"]))
    if heating:
        print(f"Heating: {heating['kwh_per_degree']:.2f} kWh/day per °C below {heating['below']:.0f} °C, "
              f"base {heating['base_kwh']:.1f} kWh/day, error {heating['daily_mae_kwh']:.1f} kWh/day, "
              f"{heating['days']} days")


def main() -> None:
    parser = argparse.ArgumentParser(prog="dorfsteig-forecast")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Keep the PV and day forecasts up to date (service)")
    sub.add_parser("fit", help="Refit the models now and print them")
    bf = sub.add_parser("backfill", help="Day forecasts for past days, as known the evening before")
    bf.add_argument("--from", dest="start", default="2025-10-02", help="ISO date; default 2025-10-02")
    args = parser.parse_args()
    setup_logging()

    match args.command:
        case "run":
            daily.run()
        case "fit":
            conn = init_db()
            daily.refit(conn, datetime.now(TZ))
            print_models(conn)
        case "backfill":
            conn = init_db()
            end = date.today() - timedelta(days=1)
            written = daily.backfill(conn, date.fromisoformat(args.start), end)
            print(f"{written} days written")
            print_models(conn)


if __name__ == "__main__":
    main()
