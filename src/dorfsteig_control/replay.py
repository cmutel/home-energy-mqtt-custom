"""Replay water heater policies against recorded minutes, to compare them in kWh and CHF.

Model:
- Each tank is an energy store. Its hot water demand is the heater's recorded
  draw while it was "always on" (downstairs measured by IoTaWatt, upstairs
  estimated in house_minutes): that's when the thermostat replaced used heat.
- A policy only decides the relay. With the relay on, the heater draws its
  full power until the tank's deficit is made up, as the thermostat would.
- Grid without water heaters = recorded grid + both recorded draws; the
  simulated draws are subtracted from that, and every minute is priced at
  its high/low tariff.
- Only days with grid and IoTaWatt data for at least 90% of minutes are
  replayed; gaps of up to 15 minutes are bridged with the previous value.
  Tanks start full at the beginning of each run of consecutive days, and
  whatever is still unheated at the end is billed at the import price.

What a policy may know: the smoothed surplus (export, ignoring the water
heaters), its own relay states, and when each tank was last seen full
(relay on, no draw). Not the tank's actual deficit.
"""

import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dorfsteig_core.tariff import Prices, is_high_tariff, prices_for

TZ = ZoneInfo("Europe/Zurich")
TANK_WH = 14_400            # 325 l from ~12 °C to 50 °C
USABLE_WH = 10_000          # deficit beyond this counts as running cold
HYSTERESIS_WH = 300         # thermostat restarts after this much use (median reheating run ~12 min)
HEATER_W = 1498.0
PV_PER_RADIATION = 8.4      # W of PV per W/m² of global radiation (fitted on 2025-2026 data)
SURPLUS_WINDOW = 3          # minutes of smoothing
MIN_COVERAGE = 0.9          # share of minutes a day needs to be replayed
BRIDGE_S = 15 * 60          # longest gap bridged with the previous value


@dataclass
class Minute:
    ts: int
    local: datetime
    g0: float                   # grid without water heaters, W, positive = export
    demand: dict[str, float]    # heater → W that the thermostat would have drawn
    import_price: float
    export_price: float


@dataclass
class Tank:
    name: str
    deficit: float = 0.0        # Wh
    relay: bool = True
    last_full: float = 0.0
    on_since: float = 0.0
    off_since: float = 0.0
    backup: bool = False        # relay held on until full
    heating: bool = False       # thermostat calling for heat
    switches: int = 0
    max_deficit: float = 0.0
    cold_minutes: int = 0

    def set_relay(self, on: bool, ts: float) -> None:
        if on != self.relay:
            self.relay = on
            self.switches += 1
            if on:
                self.on_since = ts
            else:
                self.off_since = ts

    def step(self, demand_w: float, ts: float) -> float:
        """Advance one minute; returns the heater's draw in W."""
        self.deficit += demand_w / 60
        if self.deficit >= HYSTERESIS_WH:
            self.heating = True
        draw = 0.0
        if self.relay and self.heating and self.deficit > 0:
            draw = min(HEATER_W, self.deficit * 60)
            self.deficit -= draw / 60
        if self.deficit <= 0:
            self.heating = False
        if self.relay and not self.heating:
            self.last_full = ts
            self.backup = False
        self.max_deficit = max(self.max_deficit, self.deficit)
        if self.deficit > USABLE_WH:
            self.cold_minutes += 1
        return draw


# ---- policies

@dataclass
class Policy:
    """Water heaters on PV surplus, with backup heating so they never run short.

    backup_at: local hour at which a tank that hasn't been full since
    `full_since_hour` (same day; previous day if that's later than backup_at)
    heats until full. None = no backup apart from the safety rule.
    skip_backup_kwh: skip the backup if the day-ahead PV forecast for the
    coming daylight is at least this much; an afternoon backup at 16:00 then
    catches days that turn out worse.
    """

    name: str
    pv: bool = True
    backup_at: int | None = 16
    full_since_hour: int = 10
    skip_backup_kwh: float | None = None
    safety_h: float = 36
    on_margin: float = 100      # W of surplus beyond the heater's power to switch on
    off_import: float = 200     # W of import tolerated before switching off
    min_on_s: float = 600
    min_off_s: float = 600

    def backup_due(self, tank: Tank, m: Minute, forecast_kwh: dict[date, float]) -> bool:
        if m.ts - tank.last_full > self.safety_h * 3600:
            return True
        if self.backup_at is None or m.local.hour != self.backup_at or m.local.minute != 0:
            return self.skip_backup_kwh is not None and m.local.hour == 16 and m.local.minute == 0 \
                and self._not_full_since(tank, m, 10)
        if self.skip_backup_kwh is not None and forecast_kwh.get(m.local.date(), 0) >= self.skip_backup_kwh:
            return False
        return self._not_full_since(tank, m, self.full_since_hour)

    @staticmethod
    def _not_full_since(tank: Tank, m: Minute, hour: int) -> bool:
        since = datetime.combine(m.local.date(), time(hour), TZ)
        if since > m.local:
            since -= timedelta(days=1)
        return tank.last_full < since.timestamp()

    def decide(self, m: Minute, surplus: float, tanks: list[Tank], forecast_kwh: dict[date, float]) -> None:
        if not self.pv and self.backup_at is None and self.skip_backup_kwh is None:
            for tank in tanks:                  # always on
                tank.set_relay(True, m.ts)
            return
        available = surplus
        for tank in tanks:
            if not tank.backup and self.backup_due(tank, m, forecast_kwh):
                tank.backup = True
            if tank.backup:
                tank.set_relay(True, m.ts)
                available -= HEATER_W if tank.heating else 0
                continue
            if not self.pv:
                tank.set_relay(False, m.ts)
                continue
            # A full tank draws nothing, so its relay stays on while there's surplus
            # (the thermostat can start again any time) and the surplus goes to the next tank
            if tank.relay:
                drawing = tank.heating
                keep = (available >= (HEATER_W if drawing else 0) - self.off_import
                        or m.ts - tank.on_since < self.min_on_s)
                tank.set_relay(keep, m.ts)
                if keep and drawing:
                    available -= HEATER_W
            elif available >= HEATER_W + self.on_margin and m.ts - tank.off_since >= self.min_off_s:
                tank.set_relay(True, m.ts)
                available -= HEATER_W


POLICIES = [
    Policy("always on", pv=False, backup_at=None),
    Policy("PV + backup 14:00", backup_at=14, full_since_hour=8),
    Policy("PV + backup 16:00", backup_at=16, full_since_hour=10),
    Policy("PV + backup 18:00", backup_at=18, full_since_hour=12),
    Policy("PV + backup 02:00 (low tariff)", backup_at=2, full_since_hour=14),
    Policy("PV + backup 02:00, skipped if sunny", backup_at=2, full_since_hour=14, skip_backup_kwh=15),
]


# ---- data

def load_days(conn: sqlite3.Connection, start: date, end: date,
              prices: Prices | None = None) -> list[list[Minute]]:
    """Complete days between start and end (inclusive), grouped into runs of consecutive days."""
    first = int(datetime.combine(start, time(), TZ).timestamp())
    last = int(datetime.combine(end + timedelta(days=1), time(), TZ).timestamp())
    grid = dict(conn.execute("SELECT ts, power FROM grid_readings "
                             "WHERE ts >= ? AND ts < ? AND power IS NOT NULL", (first, last)))
    down = dict(conn.execute("SELECT ts, water_heater FROM iotawatt_readings WHERE ts >= ? AND ts < ? "
                             "AND water_heater IS NOT NULL", (first, last)))
    up = dict(conn.execute("SELECT ts, upstairs_water FROM house_minutes WHERE ts >= ? AND ts < ? "
                           "AND upstairs_water IS NOT NULL", (first, last)))
    runs: list[list[Minute]] = []
    current: list[Minute] = []
    day = start
    while day <= end:
        t0 = int(datetime.combine(day, time(), TZ).timestamp())
        t1 = int(datetime.combine(day + timedelta(days=1), time(), TZ).timestamp())
        stamps = range(t0, t1, 60)
        complete = sum(ts in grid and ts in down and ts in up for ts in stamps) >= MIN_COVERAGE * len(stamps)
        if complete:
            day_prices = prices or prices_for(day)
            if day_prices is None:
                raise ValueError(f"No prices for {day.year}")
            last_seen = {"g": (0.0, 0), "u": (0.0, 0), "d": (0.0, 0)}

            def value(key: str, table: dict, ts: int) -> float:
                if ts in table:
                    last_seen[key] = (table[ts], ts)
                    return table[ts]
                v, seen = last_seen[key]
                return v if ts - seen <= BRIDGE_S else 0.0

            for ts in stamps:
                g, d, u = value("g", grid, ts), value("d", down, ts), value("u", up, ts)
                local = datetime.fromtimestamp(ts, TZ)
                high = is_high_tariff(local)
                current.append(Minute(
                    ts=ts, local=local, g0=g + d + u, demand={"downstairs": d, "upstairs": u},
                    import_price=day_prices.import_price(high), export_price=day_prices.export_price(high),
                ))
        elif current:
            runs.append(current)
            current = []
        day += timedelta(days=1)
    if current:
        runs.append(current)
    return runs


def load_forecast(conn: sqlite3.Connection, model: str = "icon_ch2") -> dict[date, float]:
    """Day-ahead PV forecast per local day, kWh: the forecast issued the day before."""
    totals: dict[date, float] = {}
    for ts, radiation in conn.execute(
        "SELECT f.ts, f.radiation FROM weather_forecast f JOIN ("
        "  SELECT ts, MAX(issued) AS issued FROM weather_forecast "
        "  WHERE model = ? AND issued <= ts - 18 * 3600 GROUP BY ts"
        ") latest ON latest.ts = f.ts AND latest.issued = f.issued WHERE f.model = ?",
        (model, model),
    ):
        day = datetime.fromtimestamp(ts, TZ).date()
        totals[day] = totals.get(day, 0.0) + (radiation or 0) * PV_PER_RADIATION / 1000
    return totals


# ---- replay

@dataclass
class Result:
    policy: str
    days: int = 0
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    import_chf: float = 0.0
    export_chf: float = 0.0
    heater_kwh: float = 0.0
    heater_pv_kwh: float = 0.0
    switches: int = 0
    cold_minutes: int = 0
    max_deficit_kwh: float = 0.0
    # month → [net CHF, heater PV kWh, heater kWh]
    months: dict[str, list[float]] = field(default_factory=dict)

    @property
    def net_chf(self) -> float:
        return self.import_chf - self.export_chf


def replay(policy: Policy, runs: list[list[Minute]], forecast_kwh: dict[date, float]) -> Result:
    result = Result(policy.name)
    for run in runs:
        tanks = [Tank("downstairs", last_full=run[0].ts), Tank("upstairs", last_full=run[0].ts)]
        recent: deque[float] = deque(maxlen=SURPLUS_WINDOW)
        result.days += len(run) // 1440
        for m in run:
            recent.append(m.g0)
            policy.decide(m, sum(recent) / len(recent), tanks, forecast_kwh)
            draw = sum(t.step(m.demand[t.name], m.ts) for t in tanks)
            g = m.g0 - draw
            imp, exp = max(-g, 0) / 60000, max(g, 0) / 60000
            result.import_kwh += imp
            result.export_kwh += exp
            result.import_chf += imp * m.import_price
            result.export_chf += exp * m.export_price
            result.heater_kwh += draw / 60000
            result.heater_pv_kwh += min(draw, max(m.g0, 0)) / 60000
            month = result.months.setdefault(m.local.strftime("%Y-%m"), [0.0, 0.0, 0.0])
            month[0] += imp * m.import_price - exp * m.export_price
            month[1] += min(draw, max(m.g0, 0)) / 60000
            month[2] += draw / 60000
        end = run[-1]
        for t in tanks:
            # Heat still owed at the end of the run, bought at the import price
            owed = max(t.deficit, 0) / 1000
            result.import_kwh += owed
            result.import_chf += owed * end.import_price
            result.heater_kwh += owed
            result.months[end.local.strftime("%Y-%m")][0] += owed * end.import_price
            result.months[end.local.strftime("%Y-%m")][2] += owed
            result.switches += t.switches
            result.cold_minutes += t.cold_minutes
            result.max_deficit_kwh = max(result.max_deficit_kwh, t.max_deficit / 1000)
    return result


def report(results: list[Result]) -> str:
    base = results[0]
    lines = [
        f"{base.days} complete days replayed.",
        "",
        f"{'policy':38s} {'net CHF':>8s} {'saved':>7s} {'heater kWh':>10s} {'from PV':>8s} "
        f"{'switches/d':>10s} {'max deficit':>11s} {'cold min':>8s}",
    ]
    for r in results:
        lines.append(
            f"{r.policy:38s} {r.net_chf:8.2f} {base.net_chf - r.net_chf:7.2f} {r.heater_kwh:10.0f} "
            f"{r.heater_pv_kwh:7.0f} ({100 * r.heater_pv_kwh / max(r.heater_kwh, 1):2.0f}%) "
            f"{r.switches / max(r.days, 1):6.1f} {r.max_deficit_kwh:8.1f} kWh {r.cold_minutes:8d}"
        )
    lines += ["", "Saved per month vs always on (CHF), PV share of water heating:"]
    for month in base.months:
        cells = [f"{base.months[month][0] - r.months[month][0]:6.2f} "
                 f"({100 * r.months[month][1] / max(r.months[month][2], 1):3.0f}%)" for r in results[1:]]
        lines.append(f"  {month}  " + "   ".join(cells))
    return "\n".join(lines)
