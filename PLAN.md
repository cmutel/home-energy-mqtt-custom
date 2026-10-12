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

### 3. New PV control

- Surplus = export + power of automatic heaters currently on.
- Choose the highest-priority set of heaters that fits the smoothed surplus
  minus a margin; per-heater minimum on/off times; fast shedding on large
  imports.
- Replay harness over historical data to tune parameters offline.

### 4. Forecasts and machine learning

- Open-Meteo forecasts and historical archive (radiation, temperature).
- First model: PV from forecast radiation, compared against a simple
  baseline. Then house load and heating demand.
- Forecaster writes a daily plan to SQLite; the controller follows it but
  keeps its own safety limits and falls back to rules without a plan.
- Train in notebooks on the Mac; run inference on the Pi.
