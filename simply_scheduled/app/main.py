"""Scheduler: Home Assistant Ingress app for controlling device entities on a timetable."""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession, ClientTimeout, web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("scheduler")
DATA_FILE = Path(os.environ.get("SCHEDULES_FILE", "/data/schedules.json"))
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", str(DATA_FILE.parent / "settings.json")))
BASE = Path(__file__).parent
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DEVICE_DOMAINS = {"switch", "light", "climate", "fan", "cover", "input_boolean", "humidifier"}
LEGACY_DOMAINS = {"scene", "script", "automation", "media_player"}
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_SCHEDULES = 300
MAX_EXCLUSIONS = 4000
DEFAULT_SETTINGS = {"group_devices": True, "group_schedules": True, "excluded_entities": []}


def get_timezone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo("UTC")


def finite_number(value, low, high, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    if integer and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{label} must be a whole number")
    return value


def validate_params(domain, action, supplied):
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError("Invalid device options")
    if action != "turn_on":
        if supplied:
            raise ValueError("Device options are only available when turning on")
        return {}
    possible = {
        "light": {"brightness_pct", "color_temp_kelvin", "rgb_color", "transition"},
        "climate": {"temperature", "target_temp_low", "target_temp_high", "hvac_mode"},
        "fan": {"percentage"},
        "humidifier": {"humidity"},
        "cover": {"position"},
        "switch": set(),
        "input_boolean": set(),
    }[domain]
    if set(supplied) - possible:
        raise ValueError("Unsupported options for this type of device")
    params = dict(supplied)
    if domain == "light":
        if "brightness_pct" in params:
            finite_number(params["brightness_pct"], 1, 100, "Brightness", integer=True)
        if "color_temp_kelvin" in params:
            finite_number(params["color_temp_kelvin"], 1000, 10000, "Colour temperature", integer=True)
        if "rgb_color" in params:
            value = params["rgb_color"]
            if not isinstance(value, str) or not HEX_RE.fullmatch(value):
                raise ValueError("Colour must be a six-digit hex colour")
            params["rgb_color"] = value.lower()
        if "rgb_color" in params and "color_temp_kelvin" in params:
            raise ValueError("Choose either colour or colour temperature")
        if "transition" in params:
            finite_number(params["transition"], 0, 300, "Transition")
    elif domain == "climate":
        if "temperature" in params:
            finite_number(params["temperature"], -50, 100, "Temperature")
        if "target_temp_low" in params or "target_temp_high" in params:
            if "target_temp_low" not in params or "target_temp_high" not in params or "temperature" in params:
                raise ValueError("Supply both temperature range values or one target temperature")
            low = finite_number(params["target_temp_low"], -50, 100, "Lower temperature")
            high = finite_number(params["target_temp_high"], -50, 100, "Upper temperature")
            if low >= high:
                raise ValueError("Upper temperature must be greater than lower temperature")
        if "hvac_mode" in params:
            if params["hvac_mode"] not in {"heat", "cool", "auto", "heat_cool", "dry", "fan_only"}:
                raise ValueError("Invalid heating/cooling mode")
    elif domain == "fan" and "percentage" in params:
        finite_number(params["percentage"], 1, 100, "Fan speed", integer=True)
    elif domain == "humidifier" and "humidity" in params:
        finite_number(params["humidity"], 0, 100, "Target humidity", integer=True)
    elif domain == "cover" and "position" in params:
        finite_number(params["position"], 0, 100, "Cover position", integer=True)
    return params


def validate(data, old_id=None):
    if not isinstance(data, dict):
        raise ValueError("Invalid schedule")
    name = data.get("name", "")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 80):
        raise ValueError("Enter a schedule name (maximum 80 characters)")
    entity = data.get("entity_id", "")
    if not isinstance(entity, str) or not ENTITY_RE.fullmatch(entity) or entity.split(".")[0] not in DEVICE_DOMAINS:
        raise ValueError("Choose a supported device entity")
    action = data.get("action", "")
    domain = entity.split(".")[0]
    if action not in {"turn_on", "turn_off"}:
        raise ValueError("Choose turn on or turn off")
    if domain == "cover" and action not in {"turn_on", "turn_off"}:
        raise ValueError("Invalid cover action")
    time = data.get("time", "")
    if not isinstance(time, str) or not TIME_RE.fullmatch(time):
        raise ValueError("Select a valid time")
    days = data.get("days", [])
    if not isinstance(days, list) or not days or len(days) != len(set(days)) or any(not isinstance(d, str) or d not in DAYS for d in days):
        raise ValueError("Select at least one day")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("Invalid enabled setting")
    identifier = old_id or uuid.uuid4().hex
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid schedule identifier")
    return {"id": identifier, "name": name.strip(), "entity_id": entity, "action": action,
            "params": validate_params(domain, action, data.get("params", {})),
            "time": time, "days": days, "enabled": enabled}


def validate_settings(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid settings")
    if not isinstance(data.get("group_devices"), bool) or not isinstance(data.get("group_schedules"), bool):
        raise ValueError("Grouping setting must be on or off")
    excluded = data.get("excluded_entities")
    if not isinstance(excluded, list) or len(excluded) > MAX_EXCLUSIONS or any(
        not isinstance(e, str) or not ENTITY_RE.fullmatch(e) or e.split(".")[0] not in DEVICE_DOMAINS for e in excluded
    ):
        raise ValueError("Invalid hidden devices list")
    return {"group_devices": data["group_devices"], "group_schedules": data["group_schedules"],
            "excluded_entities": sorted(set(excluded))}


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, path, settings_path):
        self.path = path
        self.settings_path = settings_path
        self.items = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.lock = asyncio.Lock()

    def load(self):
        if self.settings_path.exists():
            self.settings = validate_settings(json.loads(self.settings_path.read_text(encoding="utf-8")))
        if not self.path.exists():
            return
        original = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(original, list):
            raise ValueError("Expected a list of schedules")
        removed = []
        for item in original:
            domain = str(item.get("entity_id", "")).split(".")[0] if isinstance(item, dict) else ""
            if domain in LEGACY_DOMAINS:
                removed.append(item)
            else:
                self.items.append(validate(item, old_id=item["id"]))
        if len({i["id"] for i in self.items}) != len(self.items):
            raise ValueError("Duplicate schedule IDs")
        if removed:
            backup = self.path.with_name("schedules-before-device-only-migration.json")
            if not backup.exists():
                shutil.copy2(self.path, backup)
            self.save_schedules()
            LOG.warning("Removed %s unsupported old schedules. Original backed up to %s", len(removed), backup)

    def save_schedules(self):
        atomic_json(self.path, self.items)

    def save_settings(self):
        atomic_json(self.settings_path, self.settings)


def build_service_calls(schedule):
    """Translate checked schedule options into Home Assistant service calls."""
    entity = schedule["entity_id"]
    domain = entity.split(".")[0]
    action = schedule["action"]
    params = schedule.get("params", {})
    body = {"entity_id": entity}
    if action == "turn_off":
        return [("cover" if domain == "cover" else domain,
                 "close_cover" if domain == "cover" else "turn_off", body)]
    if domain == "light":
        data = {**body, **{k: v for k, v in params.items() if k != "rgb_color"}}
        if "rgb_color" in params:
            color = params["rgb_color"].lstrip("#")
            data["rgb_color"] = [int(color[pos:pos + 2], 16) for pos in (0, 2, 4)]
        return [(domain, "turn_on", data)]
    if domain == "climate":
        if any(k in params for k in ("temperature", "target_temp_low", "target_temp_high")):
            return [(domain, "set_temperature", {**body, **params})]
        if "hvac_mode" in params:
            return [(domain, "set_hvac_mode", {**body, "hvac_mode": params["hvac_mode"]})]
        return [(domain, "turn_on", body)]
    if domain == "cover":
        if "position" in params:
            return [(domain, "set_cover_position", {**body, "position": params["position"]})]
        return [(domain, "open_cover", body)]
    if domain == "humidifier" and "humidity" in params:
        return [(domain, "turn_on", body), (domain, "set_humidity", {**body, "humidity": params["humidity"]})]
    return [(domain, "turn_on", {**body, **params})]


class Engine:
    def __init__(self, store, timezone):
        self.store = store
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=timezone, job_defaults={
            "coalesce": True, "max_instances": 1, "misfire_grace_time": 45})
        self.client = None

    def rebuild(self):
        self.scheduler.remove_all_jobs()
        for item in self.store.items:
            if item["enabled"]:
                hh, mm = map(int, item["time"].split(":"))
                trigger = CronTrigger(day_of_week=",".join(item["days"]), hour=hh, minute=mm, timezone=self.timezone)
                self.scheduler.add_job(self.execute, trigger, args=[item["id"]], id=item["id"], replace_existing=True)

    def next_run(self, item):
        job = self.scheduler.get_job(item["id"])
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    async def ha_request(self, method, path, **kwargs):
        if not TOKEN:
            raise RuntimeError("SUPERVISOR_TOKEN is missing; run this inside Home Assistant")
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with self.client.request(method, f"{SUPERVISOR}/core/api/{path}", headers=headers, **kwargs) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Home Assistant API returned {response.status}: {text[:180]}")
            return json.loads(text) if text else None

    async def execute(self, identifier):
        item = next((x for x in self.store.items if x["id"] == identifier and x["enabled"]), None)
        if not item:
            return
        try:
            for domain, service, data in build_service_calls(item):
                await self.ha_request("POST", f"services/{domain}/{service}", json=data)
            LOG.info("Executed schedule %s (%s)", item["name"], identifier)
        except Exception:
            LOG.exception("Schedule failed: %s", item["name"])


async def create_app():
    app = web.Application(client_max_size=128 * 1024)
    store = Store(DATA_FILE, SETTINGS_FILE)
    store.load()
    engine = Engine(store, get_timezone(os.environ.get("TZ", "UTC")))
    app["store"] = store
    app["engine"] = engine

    async def on_startup(app):
        engine.client = ClientSession(timeout=ClientTimeout(total=12))
        engine.rebuild()
        engine.scheduler.start()

    async def on_cleanup(app):
        engine.scheduler.shutdown(wait=False)
        await engine.client.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    @web.middleware
    async def no_cache_api(request, handler):
        response = await handler(request)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    app.middlewares.append(no_cache_api)

    def json_response(payload, status=200):
        return web.json_response(payload, status=status)

    def with_next(item):
        return {**item, "next_run": engine.next_run(item)}

    async def list_schedules(request):
        return json_response({"schedules": [with_next(i) for i in store.items], "timezone": str(engine.timezone)})

    async def get_settings(request):
        return json_response(store.settings)

    async def put_settings(request):
        try:
            settings = validate_settings(await request.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return json_response({"error": str(exc)}, 400)
        async with store.lock:
            store.settings = settings
            store.save_settings()
        return json_response(store.settings)

    async def entities(request):
        try:
            states = await engine.ha_request("GET", "states")
            result = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if isinstance(entity_id, str) and ENTITY_RE.fullmatch(entity_id) and entity_id.split(".")[0] in DEVICE_DOMAINS:
                    attributes = state.get("attributes") or {}
                    result.append({"entity_id": entity_id, "name": attributes.get("friendly_name") or entity_id,
                                   "state": state.get("state", "unknown"), "attributes": {
                                       key: attributes[key] for key in (
                                           "supported_color_modes", "supported_features", "min_color_temp_kelvin",
                                           "max_color_temp_kelvin", "min_mireds", "max_mireds", "brightness",
                                           "color_temp_kelvin", "rgb_color", "temperature", "target_temp_low",
                                           "target_temp_high", "hvac_modes", "min_temp", "max_temp", "target_temp_step",
                                           "temperature_unit", "percentage", "percentage_step", "humidity",
                                           "min_humidity", "max_humidity", "current_position") if key in attributes
                                   }})
            result.sort(key=lambda x: (x["entity_id"].split(".")[0], str(x["name"]).lower()))
            return json_response({"entities": result})
        except Exception as exc:
            LOG.warning("Could not list entities: %s", exc)
            return json_response({"error": "Could not connect to Home Assistant. Check the add-on logs."}, 502)

    async def add_schedule(request):
        try:
            new = validate(await request.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return json_response({"error": str(exc)}, 400)
        async with store.lock:
            if len(store.items) >= MAX_SCHEDULES:
                return json_response({"error": "Schedule limit reached"}, 400)
            if new["entity_id"] in store.settings["excluded_entities"]:
                return json_response({"error": "This device is hidden in Settings; show it first"}, 400)
            store.items.append(new)
            store.save_schedules()
            engine.rebuild()
        return json_response(with_next(new), 201)

    async def update_schedule(request):
        identifier = request.match_info["id"]
        async with store.lock:
            index = next((n for n, i in enumerate(store.items) if i["id"] == identifier), None)
            if index is None:
                return json_response({"error": "Schedule not found"}, 404)
            try:
                modified = validate(await request.json(), old_id=identifier)
            except (ValueError, json.JSONDecodeError) as exc:
                return json_response({"error": str(exc)}, 400)
            if modified["entity_id"] in store.settings["excluded_entities"] and modified["entity_id"] != store.items[index]["entity_id"]:
                return json_response({"error": "This device is hidden in Settings; show it first"}, 400)
            store.items[index] = modified
            store.save_schedules()
            engine.rebuild()
        return json_response(with_next(modified))

    async def delete_schedule(request):
        identifier = request.match_info["id"]
        async with store.lock:
            new = [i for i in store.items if i["id"] != identifier]
            if len(new) == len(store.items):
                return json_response({"error": "Schedule not found"}, 404)
            store.items = new
            store.save_schedules()
            engine.rebuild()
        return json_response({"ok": True})

    async def index(request):
        return web.FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_get("/api/schedules", list_schedules)
    app.router.add_get("/api/entities", entities)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_post("/api/schedules", add_schedule)
    app.router.add_put("/api/schedules/{id}", update_schedule)
    app.router.add_delete("/api/schedules/{id}", delete_schedule)
    app.router.add_static("/static/", BASE / "static", show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8099")), access_log=None)
