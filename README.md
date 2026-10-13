# dorfsteig

Home energy monitoring and heating control for Dorfsteig. One repository and
one Python project, split into libraries that run as separate services on the
Raspberry Pi.

| Library | Purpose | Status |
|---|---|---|
| `dorfsteig_core` | Settings, domain models, SQLite schema, MQTT helpers | In use |
| `dorfsteig_ingest` | Store IoTaWatt, MBMD power and air-quality data in SQLite | In use |
| `dorfsteig_control` | Heating controller, ESP32 outputs, web UI, wattage tests, Pushover | In use |
| `dorfsteig_forecast` | PV forecast, base load and heating demand models | In use |

Dependency rules between the libraries are enforced by import-linter
(see `[tool.importlinter]` in `pyproject.toml`). Most importantly, the
controller never imports the forecaster: plans are exchanged through SQLite,
so heating keeps working if forecasting breaks.

See [PLAN.md](PLAN.md) for the roadmap.

## Layout

```
src/dorfsteig_core/       shared code
src/dorfsteig_ingest/     data collection (CLI: dorfsteig-ingest)
src/dorfsteig_control/    heating controller + web UI (CLI: dorfsteig-control)
src/dorfsteig_forecast/   forecasts (CLI: dorfsteig-forecast)
tests/                    pytest, one folder per library
dashboards/               static HTML dashboards served by nginx, query Datasette
datasette.yml             Datasette metadata and canned queries
firmware/esp32/           MicroPython for the upstairs/downstairs relay boards
deploy/systemd/           service units
deploy/nginx/             site config
notebooks/                exploration and model training (run on the Mac)
```

## Sign convention

Grid power from MBMD (`mbmd/solaredge1-1-1/Power`) is **positive when
exporting** and **negative when importing**. House consumption is
`inverter.power_ac - grid.power`.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check
uv run lint-imports
uv run --extra ml python notebooks/pv_model.py /tmp/dorfsteig.sqlite   # PV model comparison
```

Settings come from environment variables with the `DORFSTEIG_` prefix, or a
`.env` file; see `.env.example`.

To run a service against the Pi's broker from the Mac, either set
`DORFSTEIG_MQTT_HOST=192.168.1.192` or, if macOS blocks Python from the local
network, tunnel the broker over SSH:

```bash
ssh -f -N -L 18830:localhost:1883 rpi
DORFSTEIG_MQTT_HOST=127.0.0.1 DORFSTEIG_MQTT_PORT=18830 uv run dorfsteig-ingest listen 'mbmd/#' 1
```

## Ingest commands

```bash
uv run dorfsteig-ingest power               # MBMD inverter + grid → 1-minute averages
uv run dorfsteig-ingest iotawatt            # poll IoTaWatt every 5 minutes
uv run dorfsteig-ingest climate             # air quality sensors
uv run dorfsteig-ingest backfill-iotawatt   # history since 2023-08-10 (resumable)
uv run dorfsteig-ingest backfill-iotawatt --from 2w
uv run dorfsteig-ingest listen 'haus/#' 2   # show one payload per topic for 2 minutes
uv run dorfsteig-ingest import-influx       # one-off: MBMD history from the legacy InfluxDB
uv run dorfsteig-ingest energy              # keep house_minutes / energy_hourly up to date
uv run dorfsteig-ingest energy --rebuild    # recompute them from scratch
uv run dorfsteig-ingest backfill-weather    # MeteoSwiss station, ERA5 and day-ahead forecast history
uv run dorfsteig-ingest weather             # hourly forecasts and station data
uv run dorfsteig-control replay             # compare water heater policies on recorded data
uv run dorfsteig-control replay --rooms     # storage heaters on PV surplus
uv run dorfsteig-control replay --night     # night plan policies (needs dorfsteig-forecast backfill)
```

## Forecasts

`dorfsteig-forecast run` (a service) writes, after each hourly weather
forecast, the PV forecast per hour (`pv_forecast`) and a summary per day
(`day_forecast`: mean temperature, PV, usual house load and the PV surplus
beyond it). The models are refitted daily from the history and stored in
`forecast_models`; each module's docstring explains its model, and
`notebooks/pv_model.py` compares the PV model with gradient boosting.

```bash
uv run dorfsteig-forecast fit         # refit now and print the models
uv run dorfsteig-forecast backfill    # past day forecasts (replay --night) and hourly PV forecasts
uv run dorfsteig-forecast run         # service
```

The Learning dashboard (`dashboards/learning.html`) charts forecasts against
measurements for any day, with their errors. Each chart is one entry in its
`QUANTITIES` list: a new forecast model or measurement is one more series
there (a SQL query returning `ts, value` per hour). PV models are told apart
by `pv_forecast.model`.

The controller reads these tables for the status page, the morning report
and the night plan (`dorfsteig_control/night.py`): on cold days, the storage
heaters charge from the grid overnight for tomorrow's zone budgets minus part
of tomorrow's forecast PV.

## Energy, upstairs and costs

The house has two breaker panels; IoTaWatt only measures the downstairs one.
MBMD measures the whole house at the grid connection, so `upstairs = (PV -
grid) - downstairs`, where downstairs = mains A + B + the third-phase
channels (zone C, washer) + the second leg of the two-phase water heater,
oven and stovetop, which draw twice their channel (see
`dorfsteig_core/iotawatt.py`). The upstairs water heater (2950 W) is
estimated from switching steps in that signal (it finds ~80% of the energy).
`dorfsteig_ingest.energy` writes the results to `house_minutes` and, with
high/low tariff prices from `dorfsteig_core/tariff.py`, to `energy_hourly`.
Add next year's prices there. After changing how these are computed, run
`dorfsteig-ingest energy --rebuild` (with `dorfsteig-ingest@energy` stopped).

## Dashboards

Static pages in `dashboards/`, served by nginx at `/dashboards/`. They query
Datasette from the browser, except the real-time page, which polls the
controller's `/heating/api/live`. They use the heating UI's stylesheet
(`pico.min.css` is a symlink to it) plus `dashboards.css`, and share circuit
names from `channels.js`.

| Page | What |
|---|---|
| `daily.html` | One day, every circuit per minute, totals per circuit |
| `power.html` | One day of PV, house and grid power |
| `energy.html` | Energy and costs by day or month, high/low tariff |
| `climate.html` | Air quality sensors |
| `learning.html` | Forecasts vs measurements (temperature, radiation, PV) with errors |
| `realtime.html` | Everything measured right now, every 5 s: IoTaWatt series, MBMD per phase, heater relays |
| `status.html` | Age of each data source |

## Heating controller

One process (`dorfsteig-control run`) reads MBMD from MQTT, decides every
20 s which heaters should be on, and serves:

| URL | What |
|---|---|
| `/upstairs.json`, `/downstairs.json` | Relay states for the ESP32 boards (unchanged format). 503 if the control loop stalls, so the boards fall back to their defaults |
| `/status.json` | Same shape as the legacy controller's status file |
| `/heating/` | Web UI: status, boosts, schedules, PV settings, wattage tests, notifications, history |
| `/heating/api/status` | Everything on the status page as JSON |
| `/heating/api/live` | Every live value, for the real-time dashboard (`live.py`) |

Config (schedule rules, PV settings and zones, notification times) is
stored in SQLite and edited in the web UI; every version is kept. The control
logic is a pure function in `engine.py`; its docstring describes the rules.
Each new behaviour has a shadow mode that only logs what it would do (History
page, "dry run"), set on the Solar page:

| Setting | Modes | Module |
|---|---|---|
| Water heaters | always on / shadow / pv (PV first, with backup and safety) | `water.py` |
| Room heaters on PV | legacy / shadow / fit (fit to the surplus, shared by zone budget) | `engine.py`, `budget.py` |
| Night plan | off / shadow / on (grid charging before cold days) | `night.py` |

Room heaters are zones with a daily heat budget from the day's mean outdoor
temperature: kWh per °C below a "heats below" temperature, set per zone on
the Solar page (upper floors start heating on colder days). Fit mode gives
the PV surplus to the zone furthest behind its budget, and rotates heaters
so the heat goes round the zones; the night plan charges each zone that may
charge at night for its own budget. All tuning values are on the Solar page
under "More settings".

Pushover sends alerts (grid data, boards, sensors offline, wattage drift),
the evening summary and the 06:00 morning report (`report.py`).

```bash
uv run dorfsteig-control import-legacy-config ~/config.json   # once, from write_config.py output
uv run dorfsteig-control show-config
uv run dorfsteig-control run                                   # live
uv run dorfsteig-control run --dry-run --compare-legacy ~/Web  # alongside the legacy controller
```

Pushover needs `DORFSTEIG_PUSHOVER_TOKEN` and `DORFSTEIG_PUSHOVER_USER` in
`.env`; without them (and always in dry runs) messages are only logged.

## Deployment on the Pi

The Pi has system Python 3.11; uv installs 3.14 for the project.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/cmutel/home-energy-mqtt-custom.git ~/dorfsteig
cd ~/dorfsteig
uv sync --no-dev --extra datasette
cp .env.example .env && chmod 600 .env   # then edit
```

Optionally copy an existing database instead of backfilling (same tables), to
`~/.local/share/dorfsteig/dorfsteig.sqlite`.

Services:

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dorfsteig-ingest@power dorfsteig-ingest@iotawatt dorfsteig-ingest@climate \
    dorfsteig-ingest@energy dorfsteig-ingest@weather dorfsteig-forecast
sudo systemctl enable --now dorfsteig-datasette

journalctl -u dorfsteig-ingest@power -f
```

Controller cutover (replaces supervisor's `control_heating` and `warn_mbmd_down`):

```bash
uv run dorfsteig-control import-legacy-config ~/config.json
SC="$HOME/venvs/supervisor/bin/supervisorctl -c $HOME/venvs/supervisor/etc/supervisord.conf"
$SC stop control_heating warn_mbmd_down
sudo systemctl enable --now dorfsteig-control
journalctl -u dorfsteig-control -f
```

To go back: `sudo systemctl stop dorfsteig-control`, `$SC start control_heating`,
and restore the old nginx site (below).

nginx (replaces the default site; the ESP32 files, `/status.json` and
`/heating/` go to the controller, everything else is served from `~/Web/` as before):

```bash
sudo cp deploy/nginx/dorfsteig.conf /etc/nginx/sites-available/dorfsteig
sudo ln -s /etc/nginx/sites-available/dorfsteig /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Updating: `git pull`, then `uv sync --no-dev --extra datasette
--reinstall-package dorfsteig` if a command was added (uv may not notice new
entry points), and restart the services whose code changed.

Heating UI: `http://192.168.1.192/heating/`. Dashboards:
`http://192.168.1.192/dashboards/`. Datasette: `http://192.168.1.192/datasette/`.
