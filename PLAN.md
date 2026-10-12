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
- As of 2026-10-11 the Pi runs the legacy controller with `HEATING = False`,
  so no heaters are switched on PV surplus.
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
- [ ] Deploy ingest + Datasette on the Pi and start collecting data.

### 1. Controller port (code done 2026-10-11)

- [x] Pure decision function `engine.decide`; schedule rules (always on,
      daily window incl. overnight, one-off range/boost).
- [x] Same behaviour as the legacy controller, with the bugs above fixed;
      PV heaters are also shed if grid data goes stale.
- [x] State, config versions and event log in SQLite; logging to journald.
- [x] ESP32 JSON served by the app (503 if the loop stalls); last poll per board.
- [x] Dry-run mode with `--compare-legacy`; verified identical outputs live.
- [ ] Deploy and cut over; retire supervisor's `control_heating`.

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
- [ ] Add Pushover credentials on the Pi; run "test all heaters" once.

### 2b. Energy and costs (code done 2026-10-12)

- [x] Upstairs consumption = whole house (MBMD) - downstairs panel (IoTaWatt);
      upstairs water heater estimated from 1498 W steps.
- [x] High/low tariff prices for import and export; hourly energy and cost table.
- [x] InfluxDB history import (MBMD since 2024-11); dashboards: upstairs in
      daily consumption, new energy & costs page.
- [ ] Import InfluxDB history and enable `dorfsteig-ingest@energy` on the Pi.

### 3. New PV control (draft 2026-10-12)

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
- Water heaters: 325 l at ~50 °C, so ~14 kWh of heat each. Downstairs use
  (measured, 484 days): 4.5 kWh/day on average, 2.6-7.1 for 80% of days,
  13.3 at most; ~6 kWh/day in Nov-Jan, ~3 in July. Upstairs (estimated):
  ~3.4 kWh/day. 45% of reheating happens 19:00-24:00; smaller peaks at 07:00
  on weekdays and spread over the day at weekends. A full tank covers ~3
  average days, and the evening peak comes after the PV hours.
- Low tariff is 0.2511 vs 0.2727 CHF/kWh high tariff in 2026; the same in 2027.

#### 3a. Outdoor temperature (code done 2026-10-12)

- [x] `weather`: hourly observations from the nearest SwissMetNet station
      (MeteoSwiss open data) and ERA5 (Open-Meteo archive).
- [x] `weather_forecast`: every hourly fetch of MeteoSwiss ICON-CH1/CH2 and
      Open-Meteo best_match, plus day-ahead forecasts backfilled from 2025-10.
- [x] Balcony and kitchen sensor history imported from InfluxDB.
- [ ] Deploy: location and station in `.env`, `backfill-weather`,
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
2026-04 to 2026-10; whatever data allows): PV share of water heating 25% →
68%, saving ~CHF 42 (~0.50/day) with 2026 prices and ~CHF 41 with 2027
prices; ~14 relay switches per day for both tanks; largest deficit 5.4 kWh
of ~10 usable; never cold. Backup time hardly matters (14:00-18:00 within
CHF 1); a 02:00 low-tariff backup is the worst option even with 2026 prices,
because a tank refilled at night has less room for PV. Skipping the night
backup on sunny forecasts changes nothing, so forecasts aren't needed for
the water heaters. Missing: winter, and most of autumn/spring, as IoTaWatt
history only overlaps the grid history from 2026-04.

#### 3c. Water heaters as PV sinks

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

#### 3d. Storage heaters on PV surplus

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

#### 3e. Rollout

- Each part runs in shadow mode first (logs what it would do, alongside the
  live controller), then goes live with a switch on the Solar page.
- Status page: surplus, outdoor temperature, chosen set and the reason for
  each heater. Morning report: PV used by water and storage heaters, and
  what it saved.

### 4. Forecasts and machine learning

- Open-Meteo forecasts and historical archive (radiation, temperature).
- First model: PV from forecast radiation, compared against a simple
  baseline. Then house load and heating demand.
- Forecaster writes a daily plan to SQLite; the controller follows it but
  keeps its own safety limits and falls back to rules without a plan.
- Train in notebooks on the Mac; run inference on the Pi.
