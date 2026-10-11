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

### 1. Controller port

- Pure decision function `decide(state, reading, config, now)`; schedule
  rules (always on, daily window incl. overnight, one-off range, boost).
- Same behaviour as the legacy controller, with the bugs above fixed.
- State and event log in SQLite; logging to journald.
- Serve the ESP32 JSON from the app so a dead controller means the boards
  fall back to safe defaults; record when each board last polled.
- Dry-run mode alongside the legacy controller, then cut over and retire
  supervisor's `heating_controller.py`.

### 2. Web UI, wattage test, Pushover

- Status page (live via HTMX), heaters, schedules, settings, history.
- Import `config.json` once; retire `write_config.py`.
- Active wattage test: switch one heater on alone, measure the step in grid
  power and in the IoTaWatt zone; also maps heaters to zones.
- Passive wattage estimate from every switching event; alert on drift.
- Pushover client with rate limiting; replaces `warn_mbmd_down.py`. Alerts
  for stale MQTT data, a board not polling, controller restart, forced
  shedding. Daily summary in the evening.

### 3. New PV control

- Surplus = export + power of automatic heaters currently on.
- Choose the highest-priority set of heaters that fits the smoothed surplus
  minus a margin; per-heater minimum on/off times; fast shedding on large
  imports.
- Replay harness over historical data to tune parameters offline.

### 4. Forecasts and machine learning

- Import InfluxDB history (years of 1-minute PV and grid data) into SQLite.
- Open-Meteo forecasts and historical archive (radiation, temperature).
- First model: PV from forecast radiation, compared against a simple
  baseline. Then house load and heating demand.
- Forecaster writes a daily plan to SQLite; the controller follows it but
  keeps its own safety limits and falls back to rules without a plan.
- Train in notebooks on the Mac; run inference on the Pi.
