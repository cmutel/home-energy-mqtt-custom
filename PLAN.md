# Plan

Started 2026-10-11. Replaces the legacy scripts in the old `Dorfsteig HA`
folder (`py_control/controller/control.py`, which runs on the Pi as
`~/DataNetwork/heating_controller.py` under supervisor, plus the hand-edited
`write_config.py`).

## Goals

1. Rewrite the heating controller: easier control, react better to PV
   generation, use forecasts, room to experiment with machine learning.
2. Pushover messages: alerts during the day and a daily summary.
3. Local web pages for status, history and configuration, replacing edits to
   `write_config.py`.

## Stack

uv, Python 3.14, pydantic / pydantic-settings, paho-mqtt (aiomqtt for the
async controller), FastAPI + Jinja2 + HTMX + Pico.css for the web UI, SQLite,
httpx for Pushover and Open-Meteo, pytest, systemd, nginx. pandas /
scikit-learn (and possibly LightGBM) for forecasting, in the `ml` extra.

## Known facts and constraints

- Grid power is positive when exporting (see README).
- The ESP32 boards fetch `http://192.168.1.192/{upstairs,downstairs}.json`
  every 15 s and fall back to their defaults (water heaters on) if the request
  fails. These URLs and the JSON format must not change unless the boards are
  reflashed.
- Heater circuits are roughly 2000 W each, but this is not precise and changes
  when heaters on a circuit are disabled by hand. IoTaWatt measures heating
  zones A–F and the water heater separately.
- Three-phase supply, but the downstairs mains CTs cover only two phases. Zone
  C and the washer are on the third phase with their own CTs; the water
  heaters, oven and stovetop are two-phase loads (the CT sees one leg, they
  draw twice that). Both water heaters are ~2950 W. See 2b and
  `dorfsteig_core/iotawatt.py`.
- Until 2026-10-12 the Pi ran the legacy controller with `HEATING = False`;
  it now runs `dorfsteig-control` (water heaters in shadow mode).
- Bugs in the legacy controller to fix in the port: `always_on` → false never
  turns a heater off; overnight time windows (start > stop) never match;
  stale JSON files keep heaters on if the controller dies; plus the issues in
  `code_review.md`.

## Phases

### 0. Consolidate (done 2026-10-11)

- [x] One repo, one `src/` with `dorfsteig_core`, `dorfsteig_ingest`,
      `dorfsteig_control`, `dorfsteig_forecast`; import-linter contracts.
- [x] Port the home-energy ingest code; MQTT connect retry; one CLI.
- [x] Fix the grid power sign in docs and the house consumption formula in
      the power dashboard.
- [x] ESP32 firmware in the repo, WiFi credentials moved to an ignored file.
- [x] Merged nginx config, systemd units.
- [x] Deploy ingest + Datasette on the Pi and start collecting data.

### 1. Controller port (code done 2026-10-11)

- [x] Pure decision function `engine.decide`; schedule rules (always on,
      daily window incl. overnight, one-off range/boost).
- [x] Same behaviour as the legacy controller, with the bugs above fixed;
      PV heaters are also shed if grid data goes stale.
- [x] State, config versions and event log in SQLite; logging to journald.
- [x] ESP32 JSON served by the app (503 if the loop stalls); last poll per board.
- [x] Dry-run mode with `--compare-legacy`; verified identical outputs live.
- [x] Deploy and cut over; retire supervisor's `control_heating` (autostart off).

### 2. Web UI, wattage test, Pushover (code done 2026-10-11)

- [x] FastAPI + Jinja2 + HTMX + Pico.css UI under /heating/: status (live),
      boost 1/2/4 h, schedules, PV settings and priority, wattage,
      notifications, history.
- [x] `import-legacy-config`; `write_config.py` no longer needed after cutover.
- [x] Active wattage test (toggle one heater, compare grid power; IoTaWatt
      channel matched afterwards) and passive estimates; drift alert.
- [x] Pushover with per-alert rate limiting: stale grid data, board offline,
      controller start, wattage drift; daily summary and optional midday
      status. Replaces `warn_mbmd_down.py`.
- [x] Add Pushover credentials on the Pi.
- [ ] Run "test all heaters" once (playroom north was just rewired by hand).

### 2b. Energy and costs (code done 2026-10-12)

- [x] Upstairs consumption = whole house (MBMD) - downstairs panel (IoTaWatt);
      upstairs water heater estimated from 2950 W steps.
- [x] 2026-10-12: water heater, oven and stovetop are two-phase loads with a CT
      on one leg; the other leg is on the unmeasured third phase, so they draw
      twice their channel (house rises 1.97× the water heater channel over 888
      switch-ons, 1.98× the oven's). Both water heaters are ~2950 W, not 1498 W;
      the old 1498 W upstairs detection mostly found the downstairs heater's
      hidden leg. Since 2026-04: upstairs other 14.4 → 10.3 kWh/day, downstairs
      water 4.0 → 8.1 kWh/day. The upstairs water estimate finds ~81% of the
      energy when tried on the downstairs heater, so it runs ~20% low.
- [x] High/low tariff prices for import and export; hourly energy and cost table.
- [x] InfluxDB history import (MBMD since 2024-11); dashboards: upstairs in
      daily consumption, new energy & costs page.
- [x] Import InfluxDB history and enable `dorfsteig-ingest@energy` on the Pi.

### 3. New PV control (code done 2026-10-12)

Goal: use more of the PV surplus in the house instead of exporting it.
Exported power earns ~0.09 CHF/kWh; power used in the house saves
~0.25 CHF/kWh of import. Scope: autumn and spring, when heating can follow
PV. Real winter (heating needed regardless of PV) comes later, with better
measurements around the house.

Facts:

- Room heaters are storage heaters (Speicherheizung). With the relay on they
  charge at full power until the core is full; their thermostat only runs
  the fan. Each can be disabled by hand (playroom north is, for now), so a
  heater that draws nothing when switched on is skipped. The rooms heated
  by wood aren't on controlled circuits.
- Water heaters: 325 l at ~50 °C, so ~14 kWh of heat each; ~2950 W each, on
  two phases. Downstairs use (measured, 484 days, corrected for the second
  leg): 9 kWh/day on average, 5.1-14.2 for 80% of days, 26.6 at most;
  ~12 kWh/day in Nov-Jan, ~6 in July. Upstairs (estimated, ~20% low):
  ~2.2 kWh/day. 45% of reheating happens 19:00-24:00; smaller peaks at 07:00
  on weekdays and spread over the day at weekends. A full tank covers ~3
  average days, and the evening peak comes after the PV hours.
- Low tariff is 0.2511 vs 0.2727 CHF/kWh high tariff in 2026; the same in 2027.

#### 3a. Outdoor temperature (code done 2026-10-12)

- [x] `weather`: hourly observations from the nearest SwissMetNet station
      (MeteoSwiss open data) and ERA5 (Open-Meteo archive).
- [x] `weather_forecast`: every hourly fetch of MeteoSwiss ICON-CH1/CH2 and
      Open-Meteo best_match, plus day-ahead forecasts backfilled from 2025-10.
- [x] Balcony and kitchen sensor history imported from InfluxDB.
- [x] Deploy: location and station in `.env`, `backfill-weather`,
      `dorfsteig-ingest@weather`.

Findings (2025-10 to 2026-10):

- Balcony vs station: +1.3 °C on average, nearly all at night and in the
  morning (+1.7 to +2.7 °C); afternoons agree. Station vs ERA5: +0.75 °C.
- Station radiation explains our PV better than ERA5 (daily correlation
  0.946 vs 0.917).
- Day-ahead forecasts vs the station: ICON-CH2 is better for temperature
  (daily mean MAE 0.82 vs 1.01 °C), radiation is equal (daily MAE 0.45 vs
  0.43 kWh/m²). ICON-CH1 doesn't reach a day ahead.

#### 3b. Replay harness (offline, before anything changes live)

- Replay any control policy minute by minute over the history since 2024-11:
  simulated grid = actual grid - simulated extra heater power.
- Water heaters: each tank is an energy store (~14 kWh). With "always on",
  measured heater energy ≈ hot water used, so that's the demand. A policy
  delays heating; the replay tracks each tank's deficit and flags any time
  it would have run cold.
- Storage heaters: in autumn/spring they only charge from PV, so the replay
  counts PV kWh put into each core (capped at a daily maximum per heater).
- Score per policy and month: PV kWh used, extra import kWh, CHF (import
  cost, export revenue lost, net), switches per day, tank deficit.
- Baseline: today's behaviour (water heaters always on, no PV room heating).

Water heater results (`dorfsteig-control replay`, 83 complete days
2026-04 to 2026-10; redone 2026-10-12 with the real 2950 W heaters and
twice the measured downstairs use): PV share of water heating 25% → 65%,
saving ~CHF 56 (~0.68/day) with 2026 prices; ~13 relay switches per day for
both tanks. But the largest deficit is now 10.6 kWh of ~10 usable: ~360
minutes "cold" over the 83 days (never with always-on), so the backup rule
needs work before pv mode goes live (e.g. also heat in the evening when the
tank hasn't been full since the backup). Backup time hardly matters
(14:00-18:00 within CHF 1.5); a 02:00 low-tariff backup is the worst option
even with 2026 prices, because a tank refilled at night has less room for PV.
(The first run, with 1498 W heaters, gave 25% → 68%, CHF 42 and never cold.) Skipping the night
backup on sunny forecasts changes nothing, so forecasts aren't needed for
the water heaters. Missing: winter, and most of autumn/spring, as IoTaWatt
history only overlaps the grid history from 2026-04.

Storage heater results (`dorfsteig-control replay --rooms`, 502 days
2024-11 to 2026-10, water heaters as recorded; assumes a core takes up to
8 h of full power at -5 °C daily mean, falling to nothing at the limit):
with a 15 °C limit ~650 kWh of PV heat over the period (~470 kWh/year),
mostly March-May and September-October, almost none in winter (no surplus)
or summer; ~11 switches/day across 5 heaters; ~0.10 CHF/kWh (lost export,
small imports) vs 0.25-0.27 CHF/kWh from the grid. 12 °C halves it, 18 °C
doubles it. With the water heaters on PV first, storage heaters get ~20%
less (54 vs 69 kWh on the 83 days with water data).

#### 3c. Water heaters as PV sinks (code done 2026-10-12)

- [x] `water.py`, modes always on / shadow / pv on the Solar page.
- [x] Shadow mode on the Pi (2026-10-12).
- [ ] Strengthen the backup rule (the corrected replay runs the tanks low,
      see 3b), then pv.

- The relay only permits heating; the internal thermostat still decides.
- Daytime: relay on only when there's PV surplus. Water heaters get surplus
  before room heaters.
- Backup, so the tank is never short:
  - "full" = relay on and no draw for ~10 min (measured downstairs;
    estimated upstairs, so stricter there);
  - if a tank hasn't been full for too long, or the estimated deficit is
    too big for the evening peak, switch it on until full;
  - when: overnight in low tariff if that saves money overall (cheaper kWh,
    but a full tank in the morning has less room for PV), otherwise in the
    afternoon before the evening peak. The replay decides; with 2027 prices
    there's no low-tariff saving, so afternoon. A good PV forecast for
    tomorrow can skip a night backup.
- If the controller or Pi fails, the boards fall back to water heaters on.
- Later, with water temperature sensors: replace "full" detection and the
  deficit estimate with the measured temperature.

#### 3d. Storage heaters on PV surplus (code done 2026-10-12)

- [x] Fit mode in `engine.fit_rooms`, modes legacy / shadow / fit on the
      Solar page; temperature limit 15 °C on today's mean (station + ICON-CH2).
- [x] Zone budgets instead of the priority order (2026-10-13, `budget.py`):
      each zone gets kWh per °C below its own "heats below" temperature,
      from the day's mean (station so far + forecast, updated through the
      day; days run 20:00-20:00). Fit mode serves the zone furthest behind
      its budget, stops a zone at its budget, and rotates a heater on for
      30 min to a zone 15 points further behind. Zones and all tuning
      values are editable on the Solar page.
- [ ] Shadow for a few sunny days, then fit.

Original notes (the temperature limit is now per zone):

- Only when it's cold enough to want heat: outdoor temperature (daily mean
  or forecast) below a heating limit, ~15 °C, to be tuned.
- Surplus = smoothed export (30-60 s) + measured power of PV-controlled
  heaters that are on.
- Each tick, choose the highest-priority set of heaters that fits
  surplus - margin, using measured wattages; several can switch at once.
- No grid charging in autumn/spring: fast shedding when import stays above
  a small limit for ~1 min; per-heater minimum on/off times.
- Heaters that draw nothing when switched on (disabled by hand, or core
  full) are skipped for a while; this also feeds the wattage estimates.

#### 3e. Rollout (code done 2026-10-12)

- [x] Status page: water/space heating split and sparklines, outdoor
      temperature and room heater decision, data source lights.
- [x] Morning report: heating line (water measured, space at most, PV share).

- Each part runs in shadow mode first (logs what it would do, alongside the
  live controller), then goes live with a switch on the Solar page.
- Status page: surplus, outdoor temperature, chosen set and the reason for
  each heater. Morning report: PV used by water and storage heaters, and
  what it saved.

### 4. Forecasts and night plan (code done 2026-10-12)

Where forecasts pay off: not for the water heaters (the replay showed that).
They matter for knowing today's and tomorrow's PV, and for winter, when the
storage heaters need charging regardless of PV: how much from the grid, and
how much to leave for PV. The tariff spread is small (2.2 Rp in 2026, none
in 2027), so the main value is using PV, not shifting to the low tariff.

Design: `dorfsteig-forecast` only predicts and writes tables; the controller
reads them and makes the heating decisions (it already knows the heaters,
their wattages and what PV gave today). Without forecasts the controller
still runs; the night plan then credits no PV.

#### 4a. PV forecast (code done)

- [x] `dorfsteig_forecast/pv.py`: forecast radiation × a factor per (week,
      hour UTC), fitted to day-ahead ICON-CH2 forecasts against our PV.
- [x] `pv_forecast` (hourly) and `day_forecast` (per day), written after
      each weather forecast; models refitted daily into `forecast_models`.
- [x] Status page (today, still to come, tomorrow); morning report
      (yesterday's forecast vs actual, today's forecast); data source light.
- [ ] Deploy `dorfsteig-forecast`.

Findings (`notebooks/pv_model.py`, month left out at a time, 2025-10 to
2026-10, mean PV 31 kWh/day): daily MAE 5.9 kWh for the ratio, 5.8 for
gradient boosting, 7.1 for a single factor, 10.4 for the monthly mean. With
perfect (measured) radiation it would be ~5.4, so most of the error is the
weather forecast. Gradient boosting isn't worth the extra dependencies.

#### 4b. Load and heating demand (code done)

- [x] `dorfsteig_forecast/load.py`: base load per hour (warm days of the
      last year, 28.7 kWh/day) and house use against degrees below 15 °C:
      1.7 kWh/day per °C, base 31 kWh/day, ±10 kWh/day scatter (heating by
      hand, the wood stove).
- [ ] Per heater, once the controller has logged a winter.

#### 4c. Night plan (code done)

- [x] `dorfsteig_control/night.py`: at 20:00, tomorrow's need =
      kwh_per_degree × (temp_limit - tomorrow's forecast mean), minus
      pv_credit × tomorrow's forecast PV surplus; the rest from the grid,
      all available heaters from 20:00 (low tariff), never past 07:00.
- [x] Per zone (2026-10-13): tomorrow's zone budgets, minus the PV credit
      shared by budget; each zone ticked for night charging runs for its
      own kWh. Off = PV only (a colder house on grey days).
- [x] Modes off / shadow / on on the Solar page; plan stored in
      `night_plan`, shown on the status and Solar pages and in the morning
      report.
- [ ] Shadow when the first cold days come, then on.

#### 4d. Winter replay (code done)

- [x] `dorfsteig-control replay --night`, day by day over the days with
      archived forecasts (from 2025-10; none older exist).

Findings (176 days, 112 below 15 °C, 1700 kWh of heat needed): on heating
days the heaters could have taken only 23% of the forecast PV surplus, and
almost nothing November-February: the house uses more than the warm-day base
load, and hand heating already took some PV. So pv_credit 0.2 (not 1). Costs
hardly differ (pv_credit 0: CHF 436, 0.2: CHF 425, perfect foresight: CHF
404, with heat left short priced at the high tariff); the choice is between
a slightly colder house and more night charging. The temperature forecast
(MAE 0.9 °C, ~1.6 kWh of need) and the rough need model matter more than
the PV forecast.

### 4e. Dashboards (code done 2026-10-12)

- [x] Dashboards share the heating UI's look (Pico, `dashboards/dashboards.css`)
      and circuit names (`dashboards/channels.js`), with links both ways.
- [x] Learning: forecasts vs measurements per day, with errors; new models
      or measurements are one entry in its `QUANTITIES` list.
- [x] Real time: every IoTaWatt series and MBMD value (per phase), refreshed
      every 5 s while open, via `/heating/api/live`; heater relays with
      estimated power.

### 5. Next

Paused 2026-10-12. Open, roughly in order:

- Deploy the latest commits on the Pi, including `dorfsteig-ingest energy
  --rebuild` for the two-phase correction, and `dorfsteig-forecast`.
- Water heaters: a stronger backup (e.g. an evening top-up when not full
  since the backup), replay until never cold, then pv mode.
- Storage heaters: shadow on sunny days, then fit; night plan in shadow when
  the first cold days come.
- Set the zones (heats below, kWh per °C) and tune from the shadow logs,
  with pv_credit.
- Kitchen PM sensor: reads a flat 200-800 µg/m³ since 2026-05-12 (and
  2025-05 to 07), while the balcony reads ~1: clean or replace it. An alert
  for implausible readings would catch this earlier.
- Upstairs: a CT on the upstairs water heater would replace the step
  estimate (~20% low); look for other third-phase loads with the real-time page.
- Water temperature sensors: replace "full" detection.
- Per-heater heat need, once a winter of relay and IoTaWatt data exists.
