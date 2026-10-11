"""The running controller: one asyncio process with four tasks.

- MQTT: record grid power (and PV power for the status page)
- control loop: every `tick_s`, run `engine.decide` and persist the result
- monitor: alerts, scheduled Pushover messages, passive wattage estimates
- web: ESP32 JSON files, legacy status.json, /api/status and the UI (web.py)

In dry-run mode heater state isn't persisted, events are flagged dry_run,
notifications are only logged, and outputs can be compared with the legacy
controller's files each tick.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import aiomqtt
import uvicorn

from dorfsteig_control import wattage
from dorfsteig_control.config import ControlConfig, from_legacy
from dorfsteig_control.engine import GridSample, decide
from dorfsteig_control.heaters import BY_ID, HEATERS
from dorfsteig_control.outputs import downstairs_payload, upstairs_payload
from dorfsteig_control.store import Store
from dorfsteig_control.summary import day_summary, format_status, format_summary
from dorfsteig_core.notify import Notifier
from dorfsteig_core.settings import settings

log = logging.getLogger(__name__)

GRID_POWER_TOPIC = "mbmd/solaredge1-1-1/Power"
PV_POWER_TOPIC = "mbmd/solaredge1-1/Power"
TICK_S = 20            # legacy controller acted on every 11th ~2 s reading
STALE_TICKS = 3        # serve 503 to the boards if the loop stops for this many ticks
MONITOR_S = 15
BOARDS = ("upstairs", "downstairs")


class ConfigLocked(Exception):
    """Config comes from a legacy config.json file and can't be edited here."""


class Controller:
    def __init__(
        self,
        store: Store,
        dry_run: bool = False,
        legacy_config: Path | None = None,
        legacy_solar_enabled: bool = True,
        compare_dir: Path | None = None,
        tick_s: float = TICK_S,
        notifier: Notifier | None = None,
    ) -> None:
        self.store = store
        self.dry_run = dry_run
        self.legacy_config = legacy_config
        self.legacy_solar_enabled = legacy_solar_enabled
        self.compare_dir = compare_dir
        self.tick_s = tick_s
        self.notifier = notifier or Notifier(enabled=not dry_run)

        self.states = store.load_states()
        self.config = self.load_config()
        self.started = time.time()
        self.grid_power: float | None = None
        self.grid_ts = self.started         # treat startup as the last "reading" for staleness
        self.pv_power: float | None = None
        self.history = wattage.GridHistory()
        self.last_tick = 0.0
        self.board_polls: dict[str, dict] = {}
        self.legacy_mismatch: dict[str, object] | None = None

        # Wattage tests
        self.overrides: dict[str, bool] = {}
        self.hold_solar = False
        self.test: wattage.TestStatus | None = None
        self.test_task: asyncio.Task | None = None
        self.pending_switches: list[wattage.PendingSwitch] = []
        self.switch_times: list[float] = []   # recent switch timestamps, to isolate passive estimates

        self.sent_on: dict[str, str] = {}     # scheduled message kind → date sent

    # ---- inputs

    def on_grid_power(self, power: float) -> None:
        self.grid_power = power
        self.grid_ts = time.time()
        self.history.add(self.grid_ts, power)

    def load_config(self) -> ControlConfig:
        if self.legacy_config is not None:
            data = json.loads(self.legacy_config.read_text())
            return from_legacy(data, solar_enabled=self.legacy_solar_enabled)
        return self.store.load_config()

    def save_config(self, config: ControlConfig, source: str = "web") -> None:
        if self.legacy_config is not None:
            raise ConfigLocked("Running with --legacy-config; edit config.json instead")
        self.store.save_config(config, source=source)
        self.config = config
        self.tick()

    # ---- control

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        try:
            self.config = self.load_config()
        except Exception:
            log.exception("Could not load config; using the previous one")

        grid = GridSample(power=self.grid_power, age_s=now.timestamp() - self.grid_ts)
        self.states, events = decide(self.states, self.config, now, grid,
                                     overrides=self.overrides, hold_solar=self.hold_solar)

        for e in events:
            log.info("%s%s %s (%s, grid %s W)", "[dry run] " if self.dry_run else "",
                     "ON " if e.on else "OFF", BY_ID[e.heater].name, e.reason,
                     "?" if e.grid_power is None else f"{e.grid_power:.0f}")
            self.switch_times.append(e.ts)
            if e.reason != "override":
                self.pending_switches.append(wattage.PendingSwitch(e.ts, e.heater, e.on))
        if events:
            self.store.log_events(events, dry_run=self.dry_run)
            if not self.dry_run:
                self.store.save_states(self.states)

        if self.compare_dir is not None:
            self.compare_with_legacy()
        self.last_tick = now.timestamp()

    def compare_with_legacy(self) -> None:
        ours = {"upstairs": upstairs_payload(self.states), "downstairs": downstairs_payload(self.states)}
        try:
            theirs = {
                board: json.loads((self.compare_dir / f"{board}.json").read_text())  # type: ignore[operator]
                for board in ours
            }
        except (OSError, ValueError) as e:
            log.warning("Could not read legacy outputs: %s", e)
            return
        mismatch = {board: {"ours": ours[board], "legacy": theirs[board]}
                    for board in ours if ours[board] != theirs[board]}
        if mismatch and mismatch != self.legacy_mismatch:
            log.warning("Differs from legacy controller: %s", mismatch)
        elif not mismatch and self.legacy_mismatch:
            log.info("Matches legacy controller again")
        self.legacy_mismatch = mismatch or None

    # ---- wattage tests

    def start_tests(self, heaters: list[str]) -> None:
        if self.test_running:
            raise RuntimeError("A wattage test is already running")
        self.test_task = asyncio.get_running_loop().create_task(wattage.run_tests(self, heaters))

    def cancel_test(self) -> None:
        if self.test_task is not None:
            self.test_task.cancel()

    @property
    def test_running(self) -> bool:
        return self.test_task is not None and not self.test_task.done()

    # ---- monitoring

    async def monitor(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        ts = now.timestamp()
        notify = self.config.notify
        repeat = notify.alert_repeat_s

        grid_age = ts - self.grid_ts
        if grid_age > self.config.solar.stale_after_s:
            await self.notifier.alert("grid-stale", "No grid power data",
                                      f"No reading from MBMD for {grid_age / 60:.0f} min; "
                                      "PV-controlled heaters are switched off.", repeat)
        elif self.notifier.clear("grid-stale"):
            await self.notifier.send("Grid power data is back", "MBMD readings are arriving again.")

        for board in BOARDS:
            last = self.board_polls.get(board, {}).get("ts", self.started)
            key = f"board-{board}"
            if ts - last > notify.board_offline_after_s:
                await self.notifier.alert(key, f"{board.title()} board offline",
                                          f"No request from the {board} ESP32 for "
                                          f"{(ts - last) / 60:.0f} min.", repeat)
            elif self.notifier.clear(key):
                await self.notifier.send(f"{board.title()} board back online", "Polling again.")

        await self.process_passive(ts)

        today = now.date().isoformat()
        for kind, at in (("summary", notify.daily_summary_at), ("midday", notify.midday_status_at)):
            if at is None or now.time() < at or self.sent_on.get(kind) == today:
                continue
            self.sent_on[kind] = today
            if ts - datetime.combine(now.date(), at).timestamp() > 3600:
                continue   # started long after the scheduled time; skip until tomorrow
            await self.send_scheduled(kind, now)

    async def send_scheduled(self, kind: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        summary = day_summary(self.store.conn, now, dry_run=self.dry_run)
        if kind == "summary":
            await self.notifier.send(f"Summary {now:%a %d %b}", format_summary(summary))
        else:
            await self.notifier.send("Status", format_status(self.states, self.grid_power, summary))

    async def process_passive(self, ts: float) -> None:
        ready = [p for p in self.pending_switches if ts > p.ts + wattage.PASSIVE_AFTER[1]]
        self.pending_switches = [p for p in self.pending_switches if p not in ready]
        self.switch_times = [t for t in self.switch_times if ts - t < 600]
        for p in ready:
            if self.test_running:
                continue
            others = [t for t in self.switch_times if t != p.ts]
            result = wattage.evaluate_passive(p, self.history, others)
            if result is None:
                continue
            watts, spread = result
            wattage.record(self.store.conn, p.ts, p.heater, "passive", watts, spread,
                           p.ts + wattage.PASSIVE_AFTER[1])
            log.info("Passive estimate %s: %.0f W", p.heater, watts)
            if (d := wattage.drift(self.store.conn, p.heater)) is not None:
                tested, recent = d
                await self.notifier.alert(
                    f"drift-{p.heater}", f"{BY_ID[p.heater].name} power changed",
                    f"Recent switching suggests ≈{recent:.0f} W, but the last test measured "
                    f"{tested:.0f} W. Run a new wattage test?", 7 * 24 * 3600)

    # ---- status

    @property
    def healthy(self) -> bool:
        return time.time() - self.last_tick < STALE_TICKS * self.tick_s

    def status(self) -> dict:
        now = time.time()
        return {
            "mode": "dry run" if self.dry_run else "live",
            "now": now,
            "healthy": self.healthy,
            "last_tick": self.last_tick,
            "grid": {"power": self.grid_power, "age_s": round(now - self.grid_ts, 1)},
            "pv": {"power": self.pv_power},
            "heaters": [
                {"id": h.id, "name": h.name, **asdict(self.states[h.id]),
                 "watts": wattage.estimated_watts(self.store.conn, h.id)[0]}
                for h in HEATERS
            ],
            "boards": self.board_polls,
            "test": asdict(self.test) if self.test else None,
            "legacy_mismatch": self.legacy_mismatch,
            "events": self.store.recent_events(20),
        }


# ---- tasks

async def mqtt_loop(controller: Controller) -> None:
    delay = 5
    while True:
        try:
            async with aiomqtt.Client(settings.mqtt_host, settings.mqtt_port) as client:
                await client.subscribe(GRID_POWER_TOPIC)
                await client.subscribe(PV_POWER_TOPIC)
                log.info("Subscribed to MBMD power topics on %s:%s", settings.mqtt_host, settings.mqtt_port)
                delay = 5
                async for message in client.messages:
                    try:
                        value = float(message.payload)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        log.warning("Bad payload on %s: %r", message.topic, message.payload)
                        continue
                    if message.topic.matches(GRID_POWER_TOPIC):
                        controller.on_grid_power(value)
                    else:
                        controller.pv_power = value
        except aiomqtt.MqttError as e:
            log.warning("MQTT error (%s), reconnecting in %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def control_loop(controller: Controller) -> None:
    while True:
        try:
            controller.tick()
        except Exception:
            # Don't update last_tick: if this keeps failing the boards get 503
            # and fall back to their safe defaults.
            log.exception("Control tick failed")
        await asyncio.sleep(controller.tick_s)


async def monitor_loop(controller: Controller) -> None:
    mode = "dry run" if controller.dry_run else "live"
    await controller.notifier.send("Controller started", f"Heating controller running ({mode}).")
    while True:
        await asyncio.sleep(MONITOR_S)
        try:
            await controller.monitor()
        except Exception:
            log.exception("Monitor failed")


async def serve(controller: Controller, host: str, port: int) -> None:
    from dorfsteig_control.web import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(controller), host=host, port=port,
                                           log_level="warning"))
    async with asyncio.TaskGroup() as tg:
        tg.create_task(mqtt_loop(controller))
        tg.create_task(control_loop(controller))
        tg.create_task(monitor_loop(controller))
        tg.create_task(server.serve())
