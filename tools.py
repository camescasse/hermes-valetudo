"""Valetudo REST API client and tool handlers.

The plugin calls only the endpoints in this file. It does not send map edits, no-go zones,
Wi-Fi settings or other configuration changes to the robot. It does not follow redirects.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret

API = "/api/v2"
TIMEOUT = 10
CAPABILITY_TTL = 300
MAX_ERROR_DETAIL = 200

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
    try:
        parsed = urllib.parse.urlsplit(raw)
        parsed.port  # raises ValueError for a port that is not a number
    except ValueError:
        raise ValetudoError("VALETUDO_URL is not a valid URL") from None
    if "@" in parsed.netloc:
        # Never echo the URL here: the userinfo part can hold a password.
        raise ValetudoError(
            "VALETUDO_URL must not contain a username or password. "
            "Set VALETUDO_USERNAME and VALETUDO_PASSWORD instead."
        )
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
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


REDIRECT_CODES = {300, 301, 302, 303, 307, 308}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so that a request cannot leave the fixed endpoint set."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores http_proxy and https_proxy: requests go directly to the robot.
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        with exc:
            body = exc.read(MAX_ERROR_DETAIL + 1).decode("utf-8", "replace").strip()
    except (OSError, http.client.HTTPException, ValueError):
        return ""
    if not body or body.startswith("<") or len(body) > MAX_ERROR_DETAIL:
        return ""
    return ": " + " ".join(body.split())


def _possibly_ran(method: str) -> str:
    if method == "GET":
        return ""
    return " The command possibly ran. Call valetudo_status before you send it again."


def _request(method: str, path: str, body: Optional[dict] = None) -> Any:
    base = _base_url()
    headers = _headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + API + path, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in REDIRECT_CODES:
            exc.close()
            raise ValetudoError(
                f"Valetudo answered with a redirect (HTTP {exc.code}). The plugin does not follow "
                "redirects. Set VALETUDO_URL to the address of the Valetudo web interface."
            ) from None
        if exc.code == 401:
            exc.close()
            raise ValetudoError(
                "Valetudo refused the credentials (HTTP 401). Set VALETUDO_USERNAME and VALETUDO_PASSWORD."
            ) from None
        raise ValetudoError(f"Valetudo returned HTTP {exc.code} for {method} {path}{_error_detail(exc)}") from None
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise ValetudoError(f"Valetudo at {base} did not answer in {TIMEOUT} s.{_possibly_ran(method)}") from None
        raise ValetudoError(f"Cannot reach Valetudo at {base}: {reason}") from None
    except (http.client.HTTPException, OSError) as exc:
        # The connection closed after the request was sent, so the robot possibly received it.
        raise ValetudoError(
            f"Connection to Valetudo at {base} failed: {type(exc).__name__}.{_possibly_ran(method)}"
        ) from None
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


def _get_list(path: str) -> list:
    data = _request("GET", path)
    if not isinstance(data, list):
        raise ValetudoError(f"Valetudo returned an unexpected response for GET {path}")
    return data


def _segments() -> List[dict]:
    _require("MapSegmentationCapability")
    out = []
    for s in _get_list(_capability_path("MapSegmentationCapability")):
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        name = s.get("name")
        out.append({"id": str(s["id"]), "name": name.strip() if isinstance(name, str) and name.strip() else None})
    return out


# --- state formatting ---------------------------------------------------------------------


def _robot_error(error: Any) -> Any:
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)[:MAX_ERROR_DETAIL]
    return error


def _summarize_attributes(attributes: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    presets: Dict[str, Any] = {}
    attachments: Dict[str, Any] = {}
    dock_components: Dict[str, Any] = {}
    for attr in attributes if isinstance(attributes, list) else []:
        if not isinstance(attr, dict):
            continue
        kind = attr.get("__class")
        if kind == "StatusStateAttribute":
            out["status"] = attr.get("value")
            if attr.get("flag") not in (None, "none"):
                out["status_flag"] = attr.get("flag")
            if attr.get("error"):
                out["error"] = _robot_error(attr["error"])
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


def _summarize_statistics(points: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for point in points if isinstance(points, list) else []:
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


def _summarize_consumables(items: Any) -> List[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        remaining = item.get("remaining") if isinstance(item.get("remaining"), dict) else {}
        name = item.get("type") or "consumable"
        if item.get("subType") not in (None, "none"):
            name = f"{name}_{item['subType']}"
        out.append({"consumable": name, "remaining": remaining.get("value"), "unit": remaining.get("unit")})
    return out


def _summarize_events(events: Any) -> List[dict]:
    if isinstance(events, dict):
        events = list(events.values())
    out = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("processed"):
            continue
        entry = {"event": str(event.get("__class", "")).replace("ValetudoEvent", ""), "time": event.get("timestamp")}
        if event.get("message"):
            entry["message"] = event["message"]
        out.append(entry)
    return out


# --- argument checks ---------------------------------------------------------------------


def _string_list(args: dict, key: str) -> List[str]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValetudoError(f"{key} must be a list of non-empty strings")
    return [v.strip() for v in value]


def _iterations(args: dict, low: int, high: int) -> int:
    value = args.get("iterations")
    if value is None:
        value = low  # the robot minimum, normally 1
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise ValetudoError("iterations must be a whole number")
    if not low <= value <= high:
        raise ValetudoError(f"iterations must be from {low} to {high} on this robot")
    return value


# --- handlers ------------------------------------------------------------------------------


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data})


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


def _run(fn: Callable[[dict], str], args: Any) -> str:
    try:
        return fn(args if isinstance(args, dict) else {})
    except ValetudoError as exc:
        return _error(str(exc))
    except Exception as exc:  # the handler contract forbids exceptions
        return _error(f"Unexpected error: {type(exc).__name__}")


def _events() -> Any:
    events = _request("GET", "/events")
    if not isinstance(events, (list, dict)):
        raise ValetudoError("Valetudo returned an unexpected response for GET /events")
    return _summarize_events(events)


def _status(args: dict) -> str:
    caps = _capabilities(refresh=True)
    robot = _request("GET", "/robot")
    robot = robot if isinstance(robot, dict) else {}
    # The robot state goes under "state": Hermes treats a top-level "error" key, or a
    # top-level "status" of "error", as a failed tool call.
    result: Dict[str, Any] = {
        "robot": f"{robot.get('manufacturer', '')} {robot.get('modelName', '')}".strip(),
        "state": _summarize_attributes(_get_list("/robot/state/attributes")),
    }

    # The optional sections must not hide the main state when one of them fails.
    sections: List[Tuple[str, Callable[[], Any]]] = []
    if "CurrentStatisticsCapability" in caps:
        sections.append(("current_statistics", lambda: _summarize_statistics(
            _get_list(_capability_path("CurrentStatisticsCapability")))))
    if "ConsumableMonitoringCapability" in caps:
        sections.append(("consumables", lambda: _summarize_consumables(
            _get_list(_capability_path("ConsumableMonitoringCapability")))))
    sections.append(("unread_events", _events))
    for name, read in sections:
        try:
            result[name] = read()
        except ValetudoError as exc:
            result[f"{name}_problem"] = str(exc)
        except Exception as exc:
            result[f"{name}_problem"] = f"Unexpected response: {type(exc).__name__}"
    return _ok(**result)


def _iteration_range() -> Tuple[int, int]:
    props = _request("GET", _capability_path("MapSegmentationCapability", "/properties"))
    count = (props if isinstance(props, dict) else {}).get("iterationCount")
    count = count if isinstance(count, dict) else {}
    low, high = count.get("min"), count.get("max")
    low = low if isinstance(low, int) and not isinstance(low, bool) and low >= 1 else 1
    high = high if isinstance(high, int) and not isinstance(high, bool) and high >= low else low
    return low, high


def _rooms(args: dict) -> str:
    segments = _segments()
    low, high = _iteration_range()
    return _ok(rooms=segments, iterations={"min": low, "max": high})


def _resolve_rooms(names: List[str], ids: List[str], segments: List[dict]) -> List[str]:
    known_ids = {s["id"] for s in segments}
    resolved: List[str] = []
    unknown: List[str] = []
    for name in names:
        matches = [s["id"] for s in segments if s["name"] and s["name"].casefold() == name.casefold()]
        if len(matches) > 1:
            raise ValetudoError(f"More than one room has the name '{name}'. Use segment_ids.")
        if not matches:
            unknown.append(name)
        elif matches[0] not in resolved:
            resolved.append(matches[0])
    bad_ids = [i for i in ids if i not in known_ids]
    if unknown or bad_ids:
        parts = []
        if unknown:
            named = [s["name"] for s in segments if s["name"]]
            parts.append(f"Unknown room name(s): {', '.join(unknown)}. Known names: {', '.join(named)}.")
        if bad_ids:
            parts.append(f"Unknown segment ID(s): {', '.join(bad_ids)}. Known IDs: {', '.join(sorted(known_ids))}.")
        raise ValetudoError(" ".join(parts))
    for segment_id in ids:
        if segment_id not in resolved:
            resolved.append(segment_id)
    return resolved


def _clean_rooms(args: dict) -> str:
    names = _string_list(args, "rooms")
    ids = _string_list(args, "segment_ids")
    if not names and not ids:
        raise ValetudoError("Give at least one room name in rooms or one ID in segment_ids")
    segments = _segments()
    resolved = _resolve_rooms(names, ids, segments)
    low, high = _iteration_range()
    iterations = _iterations(args, low, high)
    _request(
        "PUT",
        _capability_path("MapSegmentationCapability"),
        {"action": "start_segment_action", "segment_ids": resolved, "iterations": iterations},
    )
    labels = {s["id"]: s["name"] or f"segment {s['id']}" for s in segments}
    return _ok(started=True, rooms=[labels[i] for i in resolved], iterations=iterations)


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
    capability = PRESETS.get(preset) if isinstance(preset, str) else None
    if not capability:
        raise ValetudoError(f"preset must be one of {', '.join(PRESETS)}")
    _require(capability)
    allowed = _request("GET", _capability_path(capability, "/presets"))
    allowed = [a for a in allowed if isinstance(a, str)] if isinstance(allowed, list) else []
    value = args.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        return _ok(preset=preset, allowed_values=allowed)
    if not isinstance(value, str) or value.strip() not in allowed:
        raise ValetudoError(f"'{value}' is not a {preset} value on this robot. Allowed: {', '.join(allowed)}")
    _request("PUT", _capability_path(capability, "/preset"), {"name": value.strip()})
    return _ok(preset=preset, value=value.strip())


def _dock(args: dict) -> str:
    action = args.get("action")
    if not isinstance(action, str) or action not in DOCK_ACTIONS:
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
