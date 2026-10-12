"""Web server: ESP32 JSON, legacy status.json, JSON API and the HTML UI.

The UI lives under /heating/ so nginx can proxy it next to the other sites
on the Pi. Plain HTML forms with POST → redirect; HTMX only refreshes the
live parts of the status and wattage pages. No login: local network only.
"""

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from dorfsteig_control import forecasts, wattage
from dorfsteig_control.config import AlwaysOn, ControlConfig, DailyWindow, OneOff
from dorfsteig_control.heaters import BY_ID, HEATERS
from dorfsteig_control.live import LiveView
from dorfsteig_control.night import night_of
from dorfsteig_control.outputs import downstairs_payload, legacy_status, upstairs_payload
from dorfsteig_control.schedule import rule_active
from dorfsteig_control.service import Controller
from dorfsteig_control.sources import data_sources
from dorfsteig_control.sparkline import sparkline, stacked
from dorfsteig_control.summary import day_summary, heating_power
from dorfsteig_core.iotawatt import WATER_HEATER_W
from dorfsteig_core.settings import settings

HERE = Path(__file__).parent
SPARK_HOURS = 6
SPACE_COLOUR, WATER_COLOUR = "#f58518", "#4c78a8"
PREFIX = "/heating"


def _fmt_ts(ts: float | None, fmt: str = "%d %b %H:%M") -> str:
    if not ts:
        return "–"
    return datetime.fromtimestamp(ts).strftime(fmt)


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    if s < 90:
        return f"{s:.0f} s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.0f} days ago"


def _watts(w: float | None) -> str:
    if w is None:
        return "–"
    return f"{w / 1000:.2f} kW" if abs(w) >= 1000 else f"{w:.0f} W"


def create_app(controller: Controller) -> FastAPI:
    app = FastAPI(title="Dorfsteig heating control", docs_url=f"{PREFIX}/api/docs", openapi_url=None)
    app.mount(f"{PREFIX}/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(ts=_fmt_ts, ago=_ago, watts=_watts)
    templates.env.globals.update(prefix=PREFIX, heaters=HEATERS, by_id=BY_ID)

    def page(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {
            "c": controller,
            "msg": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
            "locked": controller.legacy_config is not None,
            **context,
        })

    def back(path: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
        url = f"{PREFIX}{path}"
        if msg:
            url += f"?msg={quote(msg)}"
        elif error:
            url += f"?error={quote(error)}"
        return RedirectResponse(url, status_code=303)

    def edit(path: str, change: Callable[[ControlConfig], None], msg: str) -> RedirectResponse:
        """Apply `change` to a copy of the config, validate, save, redirect back."""
        config = controller.config.model_copy(deep=True)
        try:
            change(config)
            controller.save_config(ControlConfig.model_validate(config.model_dump()))
        except ValidationError as e:
            return back(path, error="; ".join(err["msg"] for err in e.errors()))
        except Exception as e:
            return back(path, error=str(e))
        return back(path, msg=msg)

    # ---- ESP32 boards and machine-readable status

    def board_json(board: str, payload: dict, request: Request) -> JSONResponse:
        controller.board_polls[board] = {
            "ts": time.time(),
            "client": request.headers.get("x-real-ip") or (request.client.host if request.client else None),
        }
        if not controller.healthy:
            return JSONResponse({"error": "controller not running"}, status_code=503)
        return JSONResponse(payload)

    @app.get("/upstairs.json")
    async def upstairs(request: Request) -> JSONResponse:
        return board_json("upstairs", upstairs_payload(controller.states), request)

    @app.get("/downstairs.json")
    async def downstairs(request: Request) -> JSONResponse:
        return board_json("downstairs", downstairs_payload(controller.states), request)

    @app.get("/status.json")
    async def status_legacy() -> dict:
        return legacy_status(controller.states, controller.grid_power, time.time())

    @app.get(f"{PREFIX}/api/status")
    async def api_status() -> dict:
        return controller.status()

    live = LiveView()

    @app.get(f"{PREFIX}/api/live")
    async def api_live() -> dict:
        """Power per circuit right now, for dashboards/realtime.html (see live.py)."""
        return await live.snapshot(controller)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"healthy": controller.healthy}, status_code=200 if controller.healthy else 503)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(f"{PREFIX}/")

    # ---- status

    def minute_series(table: str, column: str, start: float) -> list[tuple[float, float]]:
        try:
            return controller.store.conn.execute(
                f"SELECT ts, {column} FROM {table} WHERE ts >= ? AND {column} IS NOT NULL ORDER BY ts",
                (start,)).fetchall()
        except sqlite3.OperationalError:
            return []

    def pv_outlook(now: datetime) -> dict:
        """Forecast PV for today (total, and what's still to come) and tomorrow."""
        conn = controller.store.conn
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        return {
            "today": forecasts.day_forecast(conn, now.date(), now),
            "rest_kwh": forecasts.pv_remaining_kwh(conn, now, midnight),
            "tomorrow": forecasts.day_forecast(conn, now.date() + timedelta(days=1), now),
        }

    def night_context(now: datetime) -> dict:
        """The night plan to show: tonight's (or last night's, until the evening) if night mode isn't off."""
        params = controller.config.night
        plan = controller.night_plan
        return {
            "mode": params.mode,
            "plan": plan if params.mode != "off" else None,
            "tonight": plan is not None and plan.night == night_of(now, params),
            "charge_from": params.charge_from,
        }

    def status_context() -> dict:
        now = datetime.now()
        end = now.timestamp()
        start = end - SPARK_HOURS * 3600
        space, water = heating_power(controller.store.conn, start, end, controller.dry_run)
        return {
            "outlook": pv_outlook(now),
            "night": night_context(now),
            "spark": {
                "grid": sparkline(minute_series("grid_readings", "power", start), start, end, "grid"),
                "pv": sparkline(minute_series("inverter_readings", "power_ac", start), start, end, "pv"),
                "heating": stacked([(space, SPACE_COLOUR), (water, WATER_COLOUR)], start, end),
                "hours": SPARK_HOURS,
            },
            "sources": data_sources(controller),
            "now": end,
            "colours": {"space": SPACE_COLOUR, "water": WATER_COLOUR},
            "status": controller.status(),
            "today": day_summary(controller.store.conn, now, dry_run=controller.dry_run),
            "boosted": {r.heater for r in controller.config.rules
                        if r.note == "boost" and rule_active(r, now)},
        }

    @app.get(f"{PREFIX}/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return page(request, "status.html", **status_context())

    @app.get(f"{PREFIX}/fragments/status", response_class=HTMLResponse)
    async def status_fragment(request: Request) -> HTMLResponse:
        return page(request, "_status.html", **status_context())

    @app.post(f"{PREFIX}/boost")
    async def boost(heater: str = Form(), hours: float = Form()) -> RedirectResponse:
        start = datetime.now().replace(microsecond=0)
        rule = OneOff(heater=heater, start=start, stop=start + timedelta(hours=hours), note="boost")
        return edit("/", lambda c: c.rules.append(rule),
                    f"{BY_ID[heater].name} on until {rule.stop:%H:%M}")

    @app.post(f"{PREFIX}/boost/stop")
    async def boost_stop(heater: str = Form()) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.rules = [r for r in c.rules if not (r.heater == heater and r.note == "boost")]
        return edit("/", change, f"Boost for {BY_ID[heater].name} stopped")

    # ---- schedules

    @app.get(f"{PREFIX}/schedules", response_class=HTMLResponse)
    async def schedules(request: Request) -> HTMLResponse:
        now = datetime.now()
        rules = [(i, r, rule_active(r, now), isinstance(r, OneOff) and r.stop <= now)
                 for i, r in enumerate(controller.config.rules)]
        return page(request, "schedules.html", rules=rules)

    @app.post(f"{PREFIX}/schedules/add")
    async def schedule_add(
        heater: str = Form(), kind: str = Form(), note: str = Form(""),
        start_time: str = Form(""), stop_time: str = Form(""),
        start_dt: str = Form(""), stop_dt: str = Form(""),
    ) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            match kind:
                case "always_on":
                    c.rules.append(AlwaysOn(heater=heater, note=note))
                case "daily":
                    c.rules.append(DailyWindow(heater=heater, note=note, start=start_time, stop=stop_time))
                case "once":
                    c.rules.append(OneOff(heater=heater, note=note, start=start_dt, stop=stop_dt))
        return edit("/schedules", change, "Rule added")

    @app.post(f"{PREFIX}/schedules/{{index}}/toggle")
    async def schedule_toggle(index: int) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.rules[index].enabled = not c.rules[index].enabled
        return edit("/schedules", change, "Rule updated")

    @app.post(f"{PREFIX}/schedules/{{index}}/delete")
    async def schedule_delete(index: int) -> RedirectResponse:
        return edit("/schedules", lambda c: c.rules.pop(index), "Rule deleted")

    @app.post(f"{PREFIX}/schedules/remove-expired")
    async def schedule_remove_expired() -> RedirectResponse:
        now = datetime.now()

        def change(c: ControlConfig) -> None:
            c.rules = [r for r in c.rules if not (isinstance(r, OneOff) and r.stop <= now)]
        return edit("/schedules", change, "Expired rules removed")

    # ---- solar

    @app.get(f"{PREFIX}/solar", response_class=HTMLResponse)
    async def solar(request: Request) -> HTMLResponse:
        now = datetime.now()
        return page(request, "solar.html", solar=controller.config.solar, water=controller.config.water,
                    water_full=controller.water_full,
                    outdoor=(controller.outdoor_mean, controller.outdoor_source),
                    room_skip={h: t for h, t in controller.room_skip.items() if t > time.time()},
                    night_params=controller.config.night, night=night_context(now),
                    outlook=pv_outlook(now), fitted=forecasts.fitted_heating(controller.store.conn),
                    heater_w=WATER_HEATER_W)

    @app.post(f"{PREFIX}/solar/night")
    async def night_save(
        mode: str = Form(), charge_from: str = Form(), charge_until: str = Form(),
        kwh_per_degree: float = Form(), pv_credit: float = Form(), min_kwh: float = Form(),
    ) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.night.mode = mode
            c.night.charge_from = charge_from
            c.night.charge_until = charge_until
            c.night.kwh_per_degree = kwh_per_degree
            c.night.pv_credit = pv_credit
            c.night.min_kwh = min_kwh
        return edit("/solar", change, "Night plan settings saved")

    @app.post(f"{PREFIX}/solar/water")
    async def water_save(
        mode: str = Form(), backup_at: str = Form(), full_since: str = Form(),
        safety_down_h: float = Form(), safety_up_h: float = Form(),
    ) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.water.mode = mode
            c.water.backup_at = backup_at
            c.water.full_since = full_since
            c.water.safety_h = {"downstairs_water": safety_down_h, "upstairs_water": safety_up_h}
        return edit("/solar", change, "Water heater settings saved")

    @app.post(f"{PREFIX}/solar")
    async def solar_save(
        enabled: bool = Form(False), export_on_w: float = Form(), import_off_w: float = Form(),
        min_switch_interval_s: float = Form(), stale_after_s: float = Form(),
        mode: str = Form("legacy"), temp_limit: float = Form(15.0),
    ) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.solar.enabled = enabled
            c.solar.mode = mode
            c.solar.temp_limit = temp_limit
            c.solar.export_on_w = export_on_w
            c.solar.import_off_w = import_off_w
            c.solar.min_switch_interval_s = min_switch_interval_s
            c.solar.stale_after_s = stale_after_s
        return edit("/solar", change, "Solar settings saved")

    @app.post(f"{PREFIX}/solar/priority")
    async def solar_priority(heater: str = Form(), action: str = Form()) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            p = c.solar.priority
            if action == "add":
                p.append(heater)
            elif action == "remove":
                p.remove(heater)
            elif action in ("up", "down"):
                i = p.index(heater)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(p):
                    p[i], p[j] = p[j], p[i]
        return edit("/solar", change, "Priority updated")

    # ---- notifications

    @app.get(f"{PREFIX}/notifications", response_class=HTMLResponse)
    async def notifications(request: Request) -> HTMLResponse:
        return page(request, "notifications.html", notify=controller.config.notify,
                    pushover=bool(settings.pushover_token and settings.pushover_user))

    @app.post(f"{PREFIX}/notifications")
    async def notifications_save(
        daily_summary_at: str = Form(""), midday_status_at: str = Form(""), morning_report_at: str = Form(""),
        board_offline_min: float = Form(), alert_repeat_h: float = Form(),
        watched_sensors: str = Form(""), sensor_offline_min: float = Form(30),
    ) -> RedirectResponse:
        def change(c: ControlConfig) -> None:
            c.notify.daily_summary_at = daily_summary_at or None
            c.notify.midday_status_at = midday_status_at or None
            c.notify.morning_report_at = morning_report_at or None
            c.notify.board_offline_after_s = board_offline_min * 60
            c.notify.alert_repeat_s = alert_repeat_h * 3600
            c.notify.watched_sensors = [r.strip() for r in watched_sensors.split(",") if r.strip()]
            c.notify.sensor_offline_after_s = sensor_offline_min * 60
        return edit("/notifications", change, "Notification settings saved")

    @app.post(f"{PREFIX}/notifications/send")
    async def notifications_send(kind: str = Form()) -> RedirectResponse:
        if kind == "test":
            await controller.notifier.send("Test", "Test message from the heating controller.")
        else:
            await controller.send_scheduled(kind)
        sent = "Sent" if controller.notifier.enabled else "Logged (Pushover not configured or dry run)"
        return back("/notifications", msg=sent)

    # ---- wattage

    def wattage_context() -> dict:
        conn = controller.store.conn
        rows = wattage.measurements(conn)
        for row in rows:
            active = row["method"] == "active"
            row["zone"] = wattage.zone_match(conn, row["ts"], row["window_end"]) if active else None
        room_heaters = sum(not h.water for h in HEATERS)
        return {
            "estimates": {h.id: wattage.estimated_watts(conn, h.id) for h in HEATERS},
            "measurements": rows,
            "test": controller.test,
            "running": controller.test_running,
            "all_minutes": round(room_heaters * 3.05),
        }

    @app.get(f"{PREFIX}/wattage", response_class=HTMLResponse)
    async def wattage_page(request: Request) -> HTMLResponse:
        return page(request, "wattage.html", **wattage_context())

    @app.get(f"{PREFIX}/fragments/test", response_class=HTMLResponse)
    async def test_fragment(request: Request) -> HTMLResponse:
        return page(request, "_test.html", **wattage_context())

    @app.post(f"{PREFIX}/wattage/test")
    async def wattage_test(heater: str = Form()) -> RedirectResponse:
        heaters = [h.id for h in HEATERS if not h.water] if heater == "all" else [heater]
        try:
            controller.start_tests(heaters)
        except RuntimeError as e:
            return back("/wattage", error=str(e))
        return back("/wattage", msg=f"Testing {len(heaters)} heater(s); about 3 minutes each")

    @app.post(f"{PREFIX}/wattage/cancel")
    async def wattage_cancel() -> RedirectResponse:
        controller.cancel_test()
        return back("/wattage", msg="Test cancelled")

    # ---- history

    @app.get(f"{PREFIX}/history", response_class=HTMLResponse)
    async def history(request: Request) -> HTMLResponse:
        configs = controller.store.conn.execute(
            "SELECT id, saved_at, source, config FROM control_config ORDER BY id DESC LIMIT 30"
        ).fetchall()
        return page(request, "history.html", events=controller.store.recent_events(300), configs=configs)

    return app
