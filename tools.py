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
    try:
        parsed.hostname.encode("idna")
    except UnicodeError:
        raise ValetudoError("VALETUDO_URL is not a valid URL") from None
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
# A proxy in front of Valetudo can return these after it sent the request to the robot.
GATEWAY_CODES = {502, 504}
# These errors occur after the connection opened, so the robot possibly received the request.
SEND_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so that a request cannot leave the fixed endpoint set."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) ignores http_proxy and https_proxy: requests go directly to the robot.
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _secrets_in(text: str) -> bool:
    """Return True when text holds the credentials or the Authorization header."""
    folded = text.casefold()
    if "authorization" in folded or "basic " in folded:
        return True
    user = get_secret("VALETUDO_USERNAME", "") or ""
    password = get_secret("VALETUDO_PASSWORD", "") or ""
    token = base64.b64encode(f"{user}:{password}".encode()).decode() if user or password else ""
    return any(secret and secret in text for secret in (password, token))


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        with exc:
            # A UTF-8 character has at most 4 bytes: read enough bytes to count characters.
            body = exc.read(4 * MAX_ERROR_DETAIL + 4).decode("utf-8", "replace").strip()
    except (OSError, http.client.HTTPException, ValueError):
        return ""
    if not body or len(body) > MAX_ERROR_DETAIL or "<" in body or "\ufffd" in body or _secrets_in(body):
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
            content_type = response.headers.get("Content-Type", "")
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
        hint = _possibly_ran(method) if exc.code in GATEWAY_CODES else ""
        raise ValetudoError(f"Valetudo returned HTTP {exc.code} for {method} {path}{_error_detail(exc)}{hint}") from None
    except urllib.error.URLError as exc:
        # urllib wraps the errors of the connect and send steps in URLError.
        if isinstance(exc.reason, SEND_ERRORS):
            raise ValetudoError(
                f"Connection to Valetudo at {base} failed: {type(exc.reason).__name__}.{_possibly_ran(method)}"
            ) from None
        if isinstance(exc.reason, TimeoutError):
            raise ValetudoError(f"Cannot reach Valetudo at {base}: no connection in {TIMEOUT} s") from None
        raise ValetudoError(f"Cannot reach Valetudo at {base}: {exc.reason}") from None
    except TimeoutError:
        # The request went out, but the response did not arrive.
        raise ValetudoError(f"Valetudo at {base} did not answer in {TIMEOUT} s.{_possibly_ran(method)}") from None
    except (http.client.HTTPException, OSError) as exc:
        # The connection closed after the request was sent, so the robot possibly received it.
        raise ValetudoError(
            f"Connection to Valetudo at {base} failed: {type(exc).__name__}.{_possibly_ran(method)}"
        ) from None
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    if "html" in content_type.casefold() or text.lstrip().startswith("<"):
        # For example a login page of a reverse proxy: the robot did not get the request.
        raise ValetudoError(
            f"VALETUDO_URL returned an HTML page for {method} {path}, not a Valetudo API response. "
            "Set VALETUDO_URL to the address of the Valetudo web interface."
        )
    try:
        return json.loads(raw)
    except ValueError:
        return text


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
        if not isinstance(s, dict) or s.get("id") is None or isinstance(s["id"], bool) or not str(s["id"]).strip():
            continue
        name = s.get("name")
        out.append({"id": str(s["id"]).strip(), "name": name.strip() if isinstance(name, str) and name.strip() else None})
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
        elif kind == "DockStatusStateAttribute":
            out["dock_status"] = attr.get("value")
        elif not isinstance(attr.get("type"), str):
            continue
        elif kind == "PresetSelectionStateAttribute":
            presets[PRESET_ATTRIBUTE_NAMES.get(attr["type"], attr["type"])] = attr.get("value")
        elif kind == "AttachmentStateAttribute":
            attachments[attr["type"]] = attr.get("attached")
        elif kind == "DockComponentStateAttribute":
            dock_components[attr["type"]] = attr.get("value")
    if presets:
        out["presets"] = presets
    if attachments:
        out["attachments"] = attachments
    if dock_components:
        out["dock_components"] = dock_components
    return out


def _items(data: Any, what: str) -> List[dict]:
    """Return the entries of an optional status section, or raise when the shape is wrong."""
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValetudoError(f"Valetudo returned unexpected {what} data")
    return data


def _summarize_statistics(points: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for point in _items(points, "statistics"):
        value = point.get("value")
        if point.get("type") == "time" and isinstance(value, (int, float)):
            out["duration_minutes"] = round(value / 60, 1)
        elif point.get("type") == "area" and isinstance(value, (int, float)):
            out["area_m2"] = round(value / 10000, 1)
        elif isinstance(point.get("type"), str):
            out[point["type"]] = value
    return out


def _summarize_consumables(items: Any) -> List[dict]:
    out = []
    for item in _items(items, "consumable"):
        remaining = item.get("remaining") if isinstance(item.get("remaining"), dict) else {}
        name = item.get("type") if isinstance(item.get("type"), str) else "consumable"
        if isinstance(item.get("subType"), str) and item["subType"] != "none":
            name = f"{name}_{item['subType']}"
        out.append({"consumable": name, "remaining": remaining.get("value"), "unit": remaining.get("unit")})
    return out


def _summarize_events(events: Any) -> List[dict]:
    items = _items(events, "event")
    if not all(isinstance(event.get("__class"), str) for event in items):
        raise ValetudoError("Valetudo returned unexpected event data")
    out = []
    for event in items:
        if event.get("processed"):
            continue
        entry = {"event": event["__class"].replace("ValetudoEvent", ""), "time": event.get("timestamp")}
        # For example ConsumableDepletedValetudoEvent: type and subType name the consumable.
        for key in ("message", "type", "subType"):
            if isinstance(event.get(key), str) and event[key] not in ("", "none"):
                entry[key] = event[key]
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
    value = _whole(value)
    if value is None:
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
    return _summarize_events(_request("GET", "/events"))


def _robot_label() -> str:
    robot = _request("GET", "/robot")
    robot = robot if isinstance(robot, dict) else {}
    parts = [robot.get(key) for key in ("manufacturer", "modelName")]
    return " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def _status(args: dict) -> str:
    caps = _capabilities(refresh=True)
    # The robot state goes under "state": Hermes treats a top-level "error" key, or a
    # top-level "status" of "error", as a failed tool call.
    result: Dict[str, Any] = {"state": _summarize_attributes(_get_list("/robot/state/attributes"))}

    # The optional sections must not hide the main state when one of them fails.
    sections: List[Tuple[str, Callable[[], Any]]] = [("robot", _robot_label)]
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


def _whole(value: Any) -> Optional[int]:
    """Return value as an int when it is a whole JSON number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _iteration_range() -> Tuple[int, int]:
    props = _request("GET", _capability_path("MapSegmentationCapability", "/properties"))
    count = (props if isinstance(props, dict) else {}).get("iterationCount")
    count = count if isinstance(count, dict) else {}
    low, high = _whole(count.get("min")), _whole(count.get("max"))
    low = 1 if low is None else low
    high = low if high is None else high
    if low < 1 or high < low:
        raise ValetudoError(f"The robot reports a pass range that is not valid ({low} to {high})")
    return low, high


def _rooms(args: dict) -> str:
    segments = _segments()
    try:
        low, high = _iteration_range()
    except ValetudoError as exc:
        return _ok(rooms=segments, iterations_problem=str(exc))
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
