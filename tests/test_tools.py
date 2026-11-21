"""Tests for the Valetudo plugin against a fake Valetudo server.

Run from the repository root: python3 -m unittest discover -s tests
The tests need only the Python standard library. They put a stub agent.secret_scope module in
sys.modules, so that they run without a Hermes install. Hermes does not load this directory.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "valetudo_plugin_under_test"

SECRETS: dict = {}
secret_scope = types.ModuleType("agent.secret_scope")
secret_scope.get_secret = lambda name, default=None: SECRETS.get(name, default)
sys.modules.setdefault("agent", types.ModuleType("agent"))
sys.modules["agent.secret_scope"] = secret_scope

spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = plugin
spec.loader.exec_module(plugin)
tools = sys.modules[f"{PACKAGE}.tools"]

CAP = "/api/v2/robot/capabilities"

CAPABILITIES = [
    "BasicControlCapability",
    "LocateCapability",
    "MapSegmentationCapability",
    "FanSpeedControlCapability",
    "WaterUsageControlCapability",
    "OperationModeControlCapability",
    "CurrentStatisticsCapability",
    "AutoEmptyDockManualTriggerCapability",
    "MopDockCleanManualTriggerCapability",
    "MopDockDryManualTriggerCapability",
    "ConsumableMonitoringCapability",
]

BASE_ROUTES = {
    CAP: CAPABILITIES,
    "/api/v2/robot": {"manufacturer": "Test", "modelName": "Bot 1"},
    "/api/v2/robot/state/attributes": [
        {"__class": "StatusStateAttribute", "value": "docked", "flag": "none"},
        {"__class": "BatteryStateAttribute", "level": 86, "flag": "none"},
        {"__class": "PresetSelectionStateAttribute", "type": "fan_speed", "value": "medium"},
        {"__class": "PresetSelectionStateAttribute", "type": "water_grade", "value": "high"},
        {"__class": "DockStatusStateAttribute", "value": "drying"},
        {"__class": "AttachmentStateAttribute", "type": "mop", "attached": True},
        {"__class": "DockComponentStateAttribute", "type": "dustbag", "value": "ok"},
    ],
    "/api/v2/events": [
        {"__class": "ErrorStateValetudoEvent", "id": "1", "timestamp": "t1", "processed": False, "message": "Stuck"},
        {"__class": "DustBinFullValetudoEvent", "id": "2", "timestamp": "t2", "processed": True},
    ],
    f"{CAP}/CurrentStatisticsCapability": [{"type": "time", "value": 5160}, {"type": "area", "value": 360000}],
    f"{CAP}/ConsumableMonitoringCapability": [
        {"type": "brush", "subType": "main", "remaining": {"value": 1200, "unit": "minutes"}},
    ],
    f"{CAP}/MapSegmentationCapability": [
        {"id": "1", "name": "Kitchen"},
        {"id": "2", "name": "Office"},
        {"id": "10"},
    ],
    f"{CAP}/MapSegmentationCapability/properties": {"iterationCount": {"min": 1, "max": 3}},
    f"{CAP}/FanSpeedControlCapability/presets": ["low", "medium", "high"],
    f"{CAP}/WaterUsageControlCapability/presets": ["low", "high"],
    f"{CAP}/OperationModeControlCapability/presets": ["vacuum", "mop", "vacuum_and_mop"],
}


class FakeValetudo(BaseHTTPRequestHandler):
    routes: dict = {}
    calls: list = []
    requests: list = []
    auth: str = ""
    put_status: int = 200
    put_delay: float = 0.0

    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return not FakeValetudo.auth or self.headers.get("Authorization") == FakeValetudo.auth

    def do_GET(self):
        FakeValetudo.requests.append(("GET", self.path))
        if not self._authorized():
            return self._send(401, "Unauthorized")
        route = FakeValetudo.routes.get(self.path)
        if isinstance(route, tuple):  # (status, body, headers)
            return self._send(*route)
        if route is not None:
            return self._send(200, route)
        self._send(404, "<!doctype html><html><body>Not found</body></html>")

    def do_PUT(self):
        FakeValetudo.requests.append(("PUT", self.path))
        if not self._authorized():
            return self._send(401, "Unauthorized")
        length = int(self.headers.get("Content-Length", 0))
        FakeValetudo.calls.append((self.path, json.loads(self.rfile.read(length) or b"null")))
        if FakeValetudo.put_delay:
            time.sleep(FakeValetudo.put_delay)
        if FakeValetudo.put_status != 200:
            return self._send(FakeValetudo.put_status, "Invalid request body")
        self._send(200, "OK")


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeValetudo)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SECRETS.clear()
        SECRETS["VALETUDO_URL"] = f"http://127.0.0.1:{self.server.server_port}/"
        FakeValetudo.routes = copy.deepcopy(BASE_ROUTES)
        FakeValetudo.calls = []
        FakeValetudo.requests = []
        FakeValetudo.auth = ""
        FakeValetudo.put_status = 200
        FakeValetudo.put_delay = 0.0
        tools._capability_cache.clear()
        self._timeout = tools.TIMEOUT

    def tearDown(self):
        tools.TIMEOUT = self._timeout

    def call(self, handler, args=None):
        return json.loads(handler({} if args is None else args))

    def assert_error(self, result, text):
        self.assertFalse(result["ok"], result)
        self.assertIn(text, result["error"])

    # --- registration and configuration ---

    def test_register(self):
        registered = []
        ctx = types.SimpleNamespace(register_tool=lambda **kw: registered.append(kw["name"]))
        plugin.register(ctx)
        self.assertEqual(registered, ["valetudo_status", "valetudo_rooms", "valetudo_clean_rooms",
                                      "valetudo_control", "valetudo_set_preset", "valetudo_dock"])

    def test_is_configured(self):
        self.assertTrue(tools.is_configured())
        SECRETS.clear()
        self.assertFalse(tools.is_configured())

    def test_base_url_forms(self):
        for raw in ("192.168.1.5", "http://192.168.1.5/", "http://192.168.1.5/api/v2"):
            SECRETS["VALETUDO_URL"] = raw
            self.assertEqual(tools._base_url(), "http://192.168.1.5")
        for raw in ("ftp://x", "http://host:port"):
            SECRETS["VALETUDO_URL"] = raw
            self.assertRaises(tools.ValetudoError, tools._base_url)

    def test_url_with_password_is_refused_without_echo(self):
        for raw in ("http://admin:hunter2@127.0.0.1", "http://admin:hunter2@127.0.0.1:9"):
            SECRETS["VALETUDO_URL"] = raw
            result = self.call(tools.status)
            self.assert_error(result, "VALETUDO_USERNAME")
            self.assertNotIn("hunter2", json.dumps(result))

    # --- status ---

    def test_status(self):
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "docked")
        self.assertEqual(result["battery_percent"], 86)
        self.assertEqual(result["presets"], {"fan_speed": "medium", "water_usage": "high"})
        self.assertEqual(result["current_statistics"], {"duration_minutes": 86.0, "area_m2": 36.0})
        self.assertEqual(result["consumables"], [{"consumable": "brush_main", "remaining": 1200, "unit": "minutes"}])
        self.assertEqual(result["unread_events"], [{"event": "ErrorState", "time": "t1", "message": "Stuck"}])

    def test_status_error_detail(self):
        FakeValetudo.routes["/api/v2/robot/state/attributes"] = [
            {"__class": "StatusStateAttribute", "value": "error", "flag": "none",
             "error": {"severity": {"kind": "transient", "level": "error"}, "message": "Wheel stuck"}},
        ]
        result = self.call(tools.status)
        self.assertEqual((result["status"], result["error"]), ("error", "Wheel stuck"))

    def test_status_events_as_object(self):
        FakeValetudo.routes["/api/v2/events"] = {"a": {"__class": "DustBinFullValetudoEvent", "timestamp": "t", "processed": False}}
        self.assertEqual(self.call(tools.status)["unread_events"], [{"event": "DustBinFull", "time": "t"}])

    def test_status_keeps_main_state_when_a_section_fails(self):
        FakeValetudo.routes["/api/v2/events"] = (500, "boom", None)
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertEqual(result["battery_percent"], 86)
        self.assertIn("HTTP 500", result["unread_events_error"])

    # --- rooms and cleaning ---

    def test_rooms(self):
        result = self.call(tools.rooms)
        self.assertEqual(result["iterations"], {"min": 1, "max": 3})
        self.assertEqual(result["rooms"], [{"id": "1", "name": "Kitchen"}, {"id": "2", "name": "Office"}, {"id": "10", "name": None}])

    def test_rooms_without_capability(self):
        FakeValetudo.routes[CAP] = ["BasicControlCapability"]
        self.assert_error(self.call(tools.rooms), "MapSegmentationCapability")

    def test_clean_rooms_by_name_and_id(self):
        result = self.call(tools.clean_rooms, {"rooms": ["kitchen", "Kitchen"], "segment_ids": ["10"], "iterations": 2})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rooms"], ["Kitchen", "segment 10"])
        self.assertEqual(FakeValetudo.calls, [(f"{CAP}/MapSegmentationCapability",
                                               {"action": "start_segment_action", "segment_ids": ["1", "10"], "iterations": 2})])

    def test_name_and_id_do_not_collide(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability"] = [{"id": "1"}, {"id": "2", "name": "1"}]
        self.call(tools.clean_rooms, {"segment_ids": ["1"]})
        self.call(tools.clean_rooms, {"rooms": ["1"]})
        self.assertEqual([c[1]["segment_ids"] for c in FakeValetudo.calls], [["1"], ["2"]])

    def test_clean_rooms_refusals_send_nothing(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability"].append({"id": "3", "name": "office"})
        cases = [
            ({"rooms": ["Garage"]}, "Garage"),
            ({"segment_ids": ["99"]}, "99"),
            ({"rooms": ["10"]}, "Unknown room name"),  # an unnamed room is available by ID only
            ({"rooms": ["Office"]}, "More than one room"),
            ({}, "at least one"),
            ({"rooms": 5}, "list of strings"),
            ({"rooms": ["Kitchen"], "iterations": 5}, "from 1 to 3"),
            ({"rooms": ["Kitchen"], "iterations": 0}, "from 1 to 3"),
            ({"rooms": ["Kitchen"], "iterations": 1.9}, "whole number"),
            ({"rooms": ["Kitchen"], "iterations": True}, "whole number"),
            ({"rooms": ["Kitchen"], "iterations": "2"}, "whole number"),
        ]
        for args, text in cases:
            with self.subTest(args=args):
                self.assert_error(self.call(tools.clean_rooms, args), text)
        self.assertEqual(FakeValetudo.calls, [])

    # --- control, presets, dock ---

    def test_control(self):
        for action in ("start", "stop", "pause", "home", "locate"):
            self.assertTrue(self.call(tools.control, {"action": action})["ok"])
        self.assert_error(self.call(tools.control, {"action": "reset_map"}), "action must be")
        self.assertEqual(FakeValetudo.calls[-1], (f"{CAP}/LocateCapability", {"action": "locate"}))
        self.assertEqual([c[1]["action"] for c in FakeValetudo.calls[:4]], ["start", "stop", "pause", "home"])

    def test_set_preset(self):
        listed = self.call(tools.set_preset, {"preset": "fan_speed"})
        self.assertEqual(listed["allowed_values"], ["low", "medium", "high"])
        self.assertTrue(self.call(tools.set_preset, {"preset": "fan_speed", "value": "high"})["ok"])
        self.assertTrue(self.call(tools.set_preset, {"preset": "water_usage", "value": "low"})["ok"])
        self.assertTrue(self.call(tools.set_preset, {"preset": "operation_mode", "value": "mop"})["ok"])
        self.assert_error(self.call(tools.set_preset, {"preset": "fan_speed", "value": "turbo"}), "Allowed")
        self.assert_error(self.call(tools.set_preset, {"preset": "fan_speed", "value": 3}), "Allowed")
        self.assert_error(self.call(tools.set_preset, {"preset": "speaker_volume", "value": "1"}), "preset must be")
        self.assertEqual(FakeValetudo.calls, [
            (f"{CAP}/FanSpeedControlCapability/preset", {"name": "high"}),
            (f"{CAP}/WaterUsageControlCapability/preset", {"name": "low"}),
            (f"{CAP}/OperationModeControlCapability/preset", {"name": "mop"}),
        ])

    def test_dock(self):
        for action in tools.DOCK_ACTIONS:
            self.assertTrue(self.call(tools.dock, {"action": action})["ok"])
        self.assertEqual(FakeValetudo.calls, [(f"{CAP}/{cap}", {"action": verb}) for cap, verb in tools.DOCK_ACTIONS.values()])
        FakeValetudo.routes[CAP] = ["BasicControlCapability"]
        tools._capability_cache.clear()
        self.assert_error(self.call(tools.dock, {"action": "dry_mop"}), "does not support")

    # --- HTTP behaviour ---

    def test_basic_auth(self):
        FakeValetudo.auth = "Basic " + base64.b64encode(b"user:pass").decode()
        self.assert_error(self.call(tools.status), "401")
        SECRETS.update(VALETUDO_USERNAME="user", VALETUDO_PASSWORD="pass")
        self.assertTrue(self.call(tools.status)["ok"])

    def test_redirect_is_not_followed(self):
        FakeValetudo.routes["/api/v2/robot"] = (302, "", {"Location": "http://127.0.0.1:9/elsewhere"})
        FakeValetudo.routes[CAP] = (301, "", {"Location": "/api/v2/robot/state/map"})
        self.assert_error(self.call(tools.status), "redirect")
        self.assertEqual(FakeValetudo.requests, [("GET", CAP)])

    def test_http_error_detail(self):
        FakeValetudo.put_status = 400
        self.assert_error(self.call(tools.control, {"action": "home"}), "HTTP 400 for PUT /robot/capabilities/BasicControlCapability: Invalid request body")
        FakeValetudo.routes[CAP] = ["BasicControlCapability", "LocateCapability"]
        FakeValetudo.routes["/api/v2/robot/state/attributes"] = None  # the fake server answers with an HTML 404 page
        result = self.call(tools.status)
        self.assertTrue(result["error"].endswith("HTTP 404 for GET /robot/state/attributes"), result)

    def test_put_timeout_says_state_is_unknown(self):
        tools.TIMEOUT = 0.3
        FakeValetudo.put_delay = 1.0
        self.assert_error(self.call(tools.control, {"action": "home"}), "possibly ran")

    def test_unreachable(self):
        SECRETS["VALETUDO_URL"] = "http://127.0.0.1:9"
        self.assert_error(self.call(tools.status), "Cannot reach")

    def test_capability_cache(self):
        self.call(tools.control, {"action": "home"})
        self.call(tools.control, {"action": "stop"})
        self.assertEqual(FakeValetudo.requests.count(("GET", CAP)), 1)
        self.call(tools.status)  # status always reads the capabilities again
        self.assertEqual(FakeValetudo.requests.count(("GET", CAP)), 2)


if __name__ == "__main__":
    unittest.main()
