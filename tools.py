"""Valetudo REST API client and tool handlers.

The plugin calls only the endpoints in this file. It does not send map edits, no-go zones,
Wi-Fi settings or other configuration changes to the robot.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret

API = "/api/v2"
TIMEOUT = 10
CAPABILITY_TTL = 300

PRESETS = {
    "fan_speed": "FanSpeedControlCapability",
    "water_usage": "WaterUsageControlCapability",
    "operation_mode": "OperationModeControlCapability",
}

# Valetudo state attribute names that differ from the preset names of valetudo_set_preset.
PRESET_ATTRIBUTE_NAMES = {"water_grade": "water_usage"}

DOCK_ACTIONS = {
    "empty_dustbin": ("AutoEmptyDockManualTriggerCapability", "trigger"),
    "wash_mop": ("MopDockCleanManualTriggerCapability", "start"),
    "stop_mop_wash": ("MopDockCleanManualTriggerCapability", "stop"),
    "dry_mop": ("MopDockDryManualTriggerCapability", "start"),
    "stop_mop_dry": ("MopDockDryManualTriggerCapability", "stop"),
}

_capability_cache: Dict[str, Tuple[float, List[str]]] = {}
_cache_lock = threading.Lock()


class ValetudoError(Exception):
    pass


# --- configuration -------------------------------------------------------------------------


def is_configured() -> bool:
    return bool((get_secret("VALETUDO_URL", "") or "").strip())


def _base_url() -> str:
    raw = (get_secret("VALETUDO_URL", "") or "").strip().rstrip("/")
    if not raw:
        raise ValetudoError("VALETUDO_URL is not set")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValetudoError("VALETUDO_URL must be an http:// or https:// URL")
    path = parsed.path.rstrip("/")
    if path.endswith(API):
        path = path[: -len(API)]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    try:
        from hermes_cli.version_info import hermes_user_agent

        headers["User-Agent"] = hermes_user_agent("Hermes-Agent")
    except Exception:
        headers["User-Agent"] = "Hermes-Agent"
    user = get_secret("VALETUDO_USERNAME", "") or ""
    password = get_secret("VALETUDO_PASSWORD", "") or ""
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


# --- HTTP ------------------------------------------------------------------------------------


def _open(request: urllib.request.Request):
    try:
        from hermes_cli.urllib_security import open_credentialed_url
    except ImportError:
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    return open_credentialed_url(request, timeout=TIMEOUT)


def _request(method: str, path: str, body: Optional[dict] = None) -> Any:
    url = _base_url() + API + path
    headers = _headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            detail = exc.read(300).decode("utf-8", "replace").strip()
        if exc.code == 401:
            raise ValetudoError("Valetudo refused the credentials (HTTP 401). Set VALETUDO_USERNAME and VALETUDO_PASSWORD.") from None
        raise ValetudoError(f"Valetudo returned HTTP {exc.code} for {method} {path}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ValetudoError(f"Cannot reach Valetudo at {_base_url()}: {exc.reason}") from None
    except TimeoutError:
        raise ValetudoError(f"Valetudo at {_base_url()} did not answer in {TIMEOUT} s") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def _capabilities(refresh: bool = False) -> List[str]:
    key = _base_url()
    now = time.monotonic()
    with _cache_lock:
        cached = _capability_cache.get(key)
        if cached and not refresh and now - cached[0] < CAPABILITY_TTL:
            return cached[1]
    caps = _request("GET", "/robot/capabilities")
    if not isinstance(caps, list):
        raise ValetudoError("Valetudo returned an unexpected capability list")
    with _cache_lock:
        _capability_cache[key] = (now, caps)
    return caps


def _require(capability: str) -> None:
    if capability not in _capabilities():
        raise ValetudoError(f"This robot does not support {capability}")


def _capability_path(capability: str, suffix: str = "") -> str:
    return f"/robot/capabilities/{capability}{suffix}"


def _segments() -> List[dict]:
    _require("MapSegmentationCapability")
    segments = _request("GET", _capability_path("MapSegmentationCapability"))
    if not isinstance(segments, list):
        raise ValetudoError("Valetudo returned an unexpected segment list")
    return [{"id": str(s.get("id")), "name": s.get("name")} for s in segments if isinstance(s, dict)]


# --- state formatting ---------------------------------------------------------------------


def _summarize_attributes(attributes: List[dict]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    presets: Dict[str, Any] = {}
    attachments: Dict[str, Any] = {}
    dock_components: Dict[str, Any] = {}
    for attr in attributes or []:
        if not isinstance(attr, dict):
            continue
        kind = attr.get("__class")
        if kind == "StatusStateAttribute":
            out["status"] = attr.get("value")
            if attr.get("flag") not in (None, "none"):
                out["status_flag"] = attr.get("flag")
        elif kind == "BatteryStateAttribute":
            out["battery_percent"] = attr.get("level")
            if attr.get("flag") not in (None, "none"):
                out["battery_flag"] = attr.get("flag")
        elif kind == "PresetSelectionStateAttribute":
            name = PRESET_ATTRIBUTE_NAMES.get(attr.get("type"), attr.get("type"))
            presets[name] = attr.get("value")
        elif kind == "DockStatusStateAttribute":
            out["dock_status"] = attr.get("value")
        elif kind == "AttachmentStateAttribute":
            attachments[attr.get("type")] = attr.get("attached")
        elif kind == "DockComponentStateAttribute":
            dock_components[attr.get("type")] = attr.get("value")
    if presets:
        out["presets"] = presets
    if attachments:
        out["attachments"] = attachments
    if dock_components:
        out["dock_components"] = dock_components
    return out


def _summarize_statistics(points: List[dict]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for point in points or []:
        if not isinstance(point, dict):
            continue
        value = point.get("value")
        if point.get("type") == "time" and isinstance(value, (int, float)):
            out["duration_minutes"] = round(value / 60, 1)
        elif point.get("type") == "area" and isinstance(value, (int, float)):
            out["area_m2"] = round(value / 10000, 1)
        elif point.get("type"):
            out[point["type"]] = value
    return out


def _summarize_consumables(items: List[dict]) -> List[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        remaining = item.get("remaining") or {}
        name = item.get("type")
        if item.get("subType") not in (None, "none"):
            name = f"{name}_{item['subType']}"
        out.append({"consumable": name, "remaining": remaining.get("value"), "unit": remaining.get("unit")})
    return out


def _summarize_events(events: Any) -> List[dict]:
    if isinstance(events, dict):
        events = list(events.values())
    out = []
    for event in events or []:
        if not isinstance(event, dict) or event.get("processed"):
            continue
        entry = {"event": str(event.get("__class", "")).replace("ValetudoEvent", ""), "time": event.get("timestamp")}
        if event.get("message"):
            entry["message"] = event["message"]
        out.append(entry)
    return out


# --- handlers ------------------------------------------------------------------------------


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data})


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


def _run(fn, args: dict) -> str:
    try:
        return fn(args or {})
    except ValetudoError as exc:
        return _error(str(exc))
    except Exception as exc:  # the handler contract forbids exceptions
        return _error(f"{type(exc).__name__}: {exc}")


def _status(args: dict) -> str:
    caps = _capabilities(refresh=True)
    robot = _request("GET", "/robot") or {}
    result: Dict[str, Any] = {
        "robot": f"{robot.get('manufacturer', '')} {robot.get('modelName', '')}".strip(),
    }
    result.update(_summarize_attributes(_request("GET", "/robot/state/attributes")))
    if "CurrentStatisticsCapability" in caps:
        result["current_statistics"] = _summarize_statistics(_request("GET", _capability_path("CurrentStatisticsCapability")))
    if "ConsumableMonitoringCapability" in caps:
        result["consumables"] = _summarize_consumables(_request("GET", _capability_path("ConsumableMonitoringCapability")))
    result["unread_events"] = _summarize_events(_request("GET", "/events"))
    return _ok(**result)


def _iteration_range() -> Tuple[int, int]:
    props = _request("GET", _capability_path("MapSegmentationCapability", "/properties")) or {}
    count = props.get("iterationCount") or {}
    return int(count.get("min", 1)), int(count.get("max", 1))


def _rooms(args: dict) -> str:
    segments = _segments()
    low, high = _iteration_range()
    rooms = [{"id": s["id"], "name": s["name"] or f"(unnamed, use ID {s['id']})"} for s in segments]
    return _ok(rooms=rooms, iterations={"min": low, "max": high})


def _resolve_rooms(requested: List[str], segments: List[dict]) -> List[str]:
    by_id = {s["id"]: s for s in segments}
    ids: List[str] = []
    unknown: List[str] = []
    for item in requested:
        key = str(item).strip()
        matches = [s for s in segments if s["name"] and s["name"].casefold() == key.casefold()]
        if len(matches) > 1:
            raise ValetudoError(f"More than one room has the name '{key}'. Use the segment ID.")
        if matches:
            segment_id = matches[0]["id"]
        elif key in by_id:
            segment_id = key
        else:
            unknown.append(key)
            continue
        if segment_id not in ids:
            ids.append(segment_id)
    if unknown:
        known = [s["name"] or s["id"] for s in segments]
        raise ValetudoError(f"Unknown room(s): {', '.join(unknown)}. Known rooms: {', '.join(known)}")
    return ids


def _clean_rooms(args: dict) -> str:
    requested = args.get("rooms")
    if isinstance(requested, str):
        requested = [requested]
    if not requested:
        raise ValetudoError("Give at least one room")
    segments = _segments()
    ids = _resolve_rooms(requested, segments)
    try:
        iterations = int(args.get("iterations") or 1)
    except (TypeError, ValueError):
        raise ValetudoError("iterations must be an integer") from None
    low, high = _iteration_range()
    if not low <= iterations <= high:
        raise ValetudoError(f"iterations must be from {low} to {high} on this robot")
    _request(
        "PUT",
        _capability_path("MapSegmentationCapability"),
        {"action": "start_segment_action", "segment_ids": ids, "iterations": iterations},
    )
    names = {s["id"]: s["name"] or s["id"] for s in segments}
    return _ok(started=True, rooms=[names[i] for i in ids], iterations=iterations)


def _control(args: dict) -> str:
    action = args.get("action")
    if action == "locate":
        _require("LocateCapability")
        _request("PUT", _capability_path("LocateCapability"), {"action": "locate"})
    elif action in ("start", "stop", "pause", "home"):
        _require("BasicControlCapability")
        _request("PUT", _capability_path("BasicControlCapability"), {"action": action})
    else:
        raise ValetudoError("action must be one of start, stop, pause, home, locate")
    return _ok(action=action)


def _set_preset(args: dict) -> str:
    preset = args.get("preset")
    capability = PRESETS.get(preset)
    if not capability:
        raise ValetudoError(f"preset must be one of {', '.join(PRESETS)}")
    _require(capability)
    allowed = _request("GET", _capability_path(capability, "/presets")) or []
    value = (args.get("value") or "").strip()
    if not value:
        return _ok(preset=preset, allowed_values=allowed)
    if value not in allowed:
        raise ValetudoError(f"'{value}' is not a {preset} value on this robot. Allowed: {', '.join(allowed)}")
    _request("PUT", _capability_path(capability, "/preset"), {"name": value})
    return _ok(preset=preset, value=value)


def _dock(args: dict) -> str:
    action = args.get("action")
    if action not in DOCK_ACTIONS:
        raise ValetudoError(f"action must be one of {', '.join(DOCK_ACTIONS)}")
    capability, verb = DOCK_ACTIONS[action]
    _require(capability)
    _request("PUT", _capability_path(capability), {"action": verb})
    return _ok(action=action)


def status(args: dict, **kwargs) -> str:
    return _run(_status, args)


def rooms(args: dict, **kwargs) -> str:
    return _run(_rooms, args)


def clean_rooms(args: dict, **kwargs) -> str:
    return _run(_clean_rooms, args)


def control(args: dict, **kwargs) -> str:
    return _run(_control, args)


def set_preset(args: dict, **kwargs) -> str:
    return _run(_set_preset, args)


def dock(args: dict, **kwargs) -> str:
    return _run(_dock, args)
