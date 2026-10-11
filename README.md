# dorfsteig

Home energy monitoring and heating control for Dorfsteig. One repository and
one Python project, split into libraries that run as separate services on the
Raspberry Pi.

| Library | Purpose | Status |
|---|---|---|
| `dorfsteig_core` | Settings, domain models, SQLite schema, MQTT helpers | In use |
| `dorfsteig_ingest` | Store IoTaWatt, MBMD power and air-quality data in SQLite | In use |
| `dorfsteig_control` | Heating controller, ESP32 outputs, web UI, wattage tests, Pushover | In use |
| `dorfsteig_forecast` | Weather/PV forecasts and heating plans | Planned (phase 4) |

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
src/dorfsteig_forecast/   forecasting (not yet started)
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
```

## Heating controller

One process (`dorfsteig-control run`) reads grid power from MQTT, decides
every 20 s which heaters should be on, and serves:

| URL | What |
|---|---|
| `/upstairs.json`, `/downstairs.json` | Relay states for the ESP32 boards (unchanged format). 503 if the control loop stalls, so the boards fall back to their defaults |
| `/status.json` | Same shape as the legacy controller's status file |
| `/heating/` | Web UI: status, boosts, schedules, PV settings, wattage tests, notifications, history |
| `/heating/api/status` | Everything on the status page as JSON |

Config (schedule rules, PV thresholds and priority, notification times) is
stored in SQLite and edited in the web UI; every version is kept. The control
logic is a pure function in `engine.py`; its docstring describes the rules.

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
sudo systemctl enable --now dorfsteig-ingest@power dorfsteig-ingest@iotawatt dorfsteig-ingest@climate
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

Heating UI: `http://192.168.1.192/heating/`. Dashboards:
`http://192.168.1.192/dashboards/`. Datasette: `http://192.168.1.192/datasette/`.
