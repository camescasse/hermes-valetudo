"""Tests for the Valetudo plugin against a fake Valetudo server.

Run from the repository root: python3 -m unittest discover -s tests
The tests need only the Python standard library. They stub agent.secret_scope.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "valetudo_robot_under_test"

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

CAPABILITIES = [
    "BasicControlCapability",
    "LocateCapability",
    "MapSegmentationCapability",
    "FanSpeedControlCapability",
    "OperationModeControlCapability",
    "CurrentStatisticsCapability",
    "AutoEmptyDockManualTriggerCapability",
    "MopDockCleanManualTriggerCapability",
    "ConsumableMonitoringCapability",
]

GET_ROUTES = {
    "/api/v2/robot/capabilities": CAPABILITIES,
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
    "/api/v2/robot/capabilities/CurrentStatisticsCapability": [
        {"type": "time", "value": 5160},
        {"type": "area", "value": 360000},
    ],
    "/api/v2/robot/capabilities/ConsumableMonitoringCapability": [
        {"type": "brush", "subType": "main", "remaining": {"value": 1200, "unit": "minutes"}},
    ],
    "/api/v2/robot/capabilities/MapSegmentationCapability": [
        {"id": "1", "name": "Kitchen"},
        {"id": "2", "name": "Office"},
        {"id": "10"},
    ],
    "/api/v2/robot/capabilities/MapSegmentationCapability/properties": {"iterationCount": {"min": 1, "max": 3}},
    "/api/v2/robot/capabilities/FanSpeedControlCapability/presets": ["low", "medium", "high"],
}


class FakeValetudo(BaseHTTPRequestHandler):
    calls: list = []
    auth: str = ""

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if not FakeValetudo.auth:
            return True
        return self.headers.get("Authorization") == FakeValetudo.auth

    def do_GET(self):
        if not self._authorized():
            return self._send(401, "Unauthorized")
        if self.path in GET_ROUTES:
            return self._send(200, GET_ROUTES[self.path])
        self._send(404, "Not found")

    def do_PUT(self):
        if not self._authorized():
            return self._send(401, "Unauthorized")
        length = int(self.headers.get("Content-Length", 0))
        FakeValetudo.calls.append((self.path, json.loads(self.rfile.read(length) or b"null")))
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
        FakeValetudo.calls = []
        FakeValetudo.auth = ""
        tools._capability_cache.clear()

    def call(self, handler, args=None):
        return json.loads(handler(args or {}))

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
        SECRETS["VALETUDO_URL"] = "ftp://x"
        self.assertRaises(tools.ValetudoError, tools._base_url)

    def test_status(self):
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "docked")
        self.assertEqual(result["battery_percent"], 86)
        self.assertEqual(result["presets"], {"fan_speed": "medium", "water_usage": "high"})
        self.assertEqual(result["current_statistics"], {"duration_minutes": 86.0, "area_m2": 36.0})
        self.assertEqual(result["consumables"], [{"consumable": "brush_main", "remaining": 1200, "unit": "minutes"}])
        self.assertEqual(result["unread_events"], [{"event": "ErrorState", "time": "t1", "message": "Stuck"}])

    def test_rooms(self):
        result = self.call(tools.rooms)
        self.assertEqual(result["iterations"], {"min": 1, "max": 3})
        self.assertEqual(result["rooms"][0], {"id": "1", "name": "Kitchen"})
        self.assertIn("unnamed", result["rooms"][2]["name"])

    def test_clean_rooms_by_name_and_id(self):
        result = self.call(tools.clean_rooms, {"rooms": ["kitchen", "10", "Kitchen"], "iterations": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(FakeValetudo.calls, [("/api/v2/robot/capabilities/MapSegmentationCapability",
                                               {"action": "start_segment_action", "segment_ids": ["1", "10"], "iterations": 2})])

    def test_clean_rooms_rejects_unknown_room(self):
        result = self.call(tools.clean_rooms, {"rooms": ["Garage"]})
        self.assertFalse(result["ok"])
        self.assertIn("Garage", result["error"])
        self.assertEqual(FakeValetudo.calls, [])

    def test_clean_rooms_rejects_bad_iterations(self):
        result = self.call(tools.clean_rooms, {"rooms": ["Office"], "iterations": 5})
        self.assertFalse(result["ok"])
        self.assertEqual(FakeValetudo.calls, [])

    def test_control(self):
        self.assertTrue(self.call(tools.control, {"action": "home"})["ok"])
        self.assertTrue(self.call(tools.control, {"action": "locate"})["ok"])
        self.assertFalse(self.call(tools.control, {"action": "reset_map"})["ok"])
        self.assertEqual([c[1]["action"] for c in FakeValetudo.calls], ["home", "locate"])

    def test_set_preset(self):
        listed = self.call(tools.set_preset, {"preset": "fan_speed"})
        self.assertEqual(listed["allowed_values"], ["low", "medium", "high"])
        self.assertTrue(self.call(tools.set_preset, {"preset": "fan_speed", "value": "high"})["ok"])
        self.assertFalse(self.call(tools.set_preset, {"preset": "fan_speed", "value": "turbo"})["ok"])
        self.assertFalse(self.call(tools.set_preset, {"preset": "water_usage", "value": "high"})["ok"])
        self.assertEqual(FakeValetudo.calls, [("/api/v2/robot/capabilities/FanSpeedControlCapability/preset", {"name": "high"})])

    def test_dock(self):
        self.assertTrue(self.call(tools.dock, {"action": "empty_dustbin"})["ok"])
        self.assertFalse(self.call(tools.dock, {"action": "dry_mop"})["ok"])
        self.assertEqual(FakeValetudo.calls, [("/api/v2/robot/capabilities/AutoEmptyDockManualTriggerCapability", {"action": "trigger"})])

    def test_basic_auth(self):
        FakeValetudo.auth = "Basic " + base64.b64encode(b"user:pass").decode()
        self.assertIn("401", self.call(tools.status)["error"])
        SECRETS.update(VALETUDO_USERNAME="user", VALETUDO_PASSWORD="pass")
        self.assertTrue(self.call(tools.status)["ok"])

    def test_unreachable(self):
        SECRETS["VALETUDO_URL"] = "http://127.0.0.1:9"
        result = self.call(tools.status)
        self.assertFalse(result["ok"])
        self.assertIn("Cannot reach", result["error"])


if __name__ == "__main__":
    unittest.main()
