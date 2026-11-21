"""Tests for the Valetudo plugin against a fake Valetudo server.

Run from the repository root: python3 -m unittest discover -s tests
The tests need only the Python standard library. They put a stub agent.secret_scope module in
sys.modules, so that they run without a Hermes install. Hermes does not load this directory.
"""

from __future__ import annotations

import base64
import copy
import os
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
import urllib.error
from unittest import mock
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
    "MapSegmentRenameCapability",
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
    put_headers: dict = {}
    put_body: str = "Invalid request body"
    put_ok_body: str = "OK"
    put_drop: bool = False

    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client already timed out

    def _authorized(self):
        return not FakeValetudo.auth or self.headers.get("Authorization") == FakeValetudo.auth

    def do_GET(self):
        FakeValetudo.requests.append(("GET", self.path))
        if not self._authorized():
            return self._send(401, "Unauthorized")
        route = FakeValetudo.routes.get(self.path)
        if route == "slow":
            time.sleep(1.0)
            return self._send(200, [])
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
        if FakeValetudo.put_drop:
            self.close_connection = True
            self.connection.shutdown(2)
            return
        if FakeValetudo.put_delay:
            time.sleep(FakeValetudo.put_delay)
        if FakeValetudo.put_status != 200:
            return self._send(FakeValetudo.put_status, FakeValetudo.put_body, FakeValetudo.put_headers)
        self._send(200, FakeValetudo.put_ok_body, FakeValetudo.put_headers)


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
        FakeValetudo.put_headers = {}
        FakeValetudo.put_body = "Invalid request body"
        FakeValetudo.put_ok_body = "OK"
        FakeValetudo.put_drop = False
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
                                      "valetudo_rename_room", "valetudo_control", "valetudo_set_preset", "valetudo_dock"])

    def test_is_configured(self):
        self.assertTrue(tools.is_configured())
        SECRETS.clear()
        self.assertFalse(tools.is_configured())

    def test_base_url_forms(self):
        for raw in ("192.168.1.5", "http://192.168.1.5/", "http://192.168.1.5/api/v2"):
            SECRETS["VALETUDO_URL"] = raw
            self.assertEqual(tools._base_url(), "http://192.168.1.5")
        for raw in ("ftp://x", "http://host:port", "http://a..b"):
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
        self.assertNotIn("error", result)
        state = result["state"]
        self.assertEqual(state["status"], "docked")
        self.assertEqual(state["battery_percent"], 86)
        self.assertEqual(state["presets"], {"fan_speed": "medium", "water_usage": "high"})
        self.assertEqual(result["current_statistics"], {"duration_minutes": 86.0, "area_m2": 36.0})
        self.assertEqual(result["consumables"], [{"consumable": "brush_main", "remaining": 1200, "unit": "minutes"}])
        self.assertEqual(result["unread_events"], [{"event": "ErrorState", "time": "t1", "message": "Stuck"}])

    def test_status_error_detail(self):
        FakeValetudo.routes["/api/v2/robot/state/attributes"] = [
            {"__class": "StatusStateAttribute", "value": "error", "flag": "none",
             "error": {"severity": {"kind": "transient", "level": "error"}, "message": "Wheel stuck"}},
        ]
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)  # Hermes reads a top-level "error" key as a failed call
        self.assertNotIn("status", result)
        self.assertEqual((result["state"]["status"], result["state"]["error"]), ("error", "Wheel stuck"))

    def test_status_events_as_object(self):
        FakeValetudo.routes["/api/v2/events"] = {"a": {"__class": "DustBinFullValetudoEvent", "timestamp": "t", "processed": False}}
        self.assertEqual(self.call(tools.status)["unread_events"], [{"event": "DustBinFull", "time": "t"}])

    def test_status_keeps_main_state_when_a_section_fails(self):
        FakeValetudo.routes["/api/v2/events"] = (500, "boom", None)
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["battery_percent"], 86)
        self.assertIn("HTTP 500", result["unread_events_problem"])

    def test_status_keeps_main_state_on_bad_section_data(self):
        FakeValetudo.routes[f"{CAP}/ConsumableMonitoringCapability"] = [{"type": "brush", "remaining": 5}]
        FakeValetudo.routes[f"{CAP}/CurrentStatisticsCapability"] = {"not": "a list"}
        FakeValetudo.routes["/api/v2/events"] = (200, "plain text", None)
        result = self.call(tools.status)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["status"], "docked")
        self.assertEqual(result["consumables"], [{"consumable": "brush", "remaining": None, "unit": None}])
        self.assertIn("unexpected response", result["current_statistics_problem"])
        self.assertIn("unexpected event data", result["unread_events_problem"])

    def test_status_bad_items_are_problems_not_empty_lists(self):
        FakeValetudo.routes["/api/v2/events"] = {"error": "service unavailable"}
        FakeValetudo.routes[f"{CAP}/CurrentStatisticsCapability"] = [False]
        FakeValetudo.routes[f"{CAP}/ConsumableMonitoringCapability"] = ["brush"]
        result = self.call(tools.status)
        for section in ("unread_events", "current_statistics", "consumables"):
            self.assertNotIn(section, result)
            self.assertIn("unexpected", result[f"{section}_problem"])

    def test_status_robot_label_failure_keeps_state(self):
        FakeValetudo.routes["/api/v2/robot"] = (500, "boom", None)
        result = self.call(tools.status)
        self.assertEqual(result["state"]["status"], "docked")
        self.assertIn("HTTP 500", result["robot_problem"])
        FakeValetudo.routes["/api/v2/robot"] = {"manufacturer": None, "modelName": "X"}
        self.assertEqual(self.call(tools.status)["robot"], "X")

    def test_status_attribute_with_bad_type(self):
        FakeValetudo.routes["/api/v2/robot/state/attributes"].append(
            {"__class": "PresetSelectionStateAttribute", "type": ["fan_speed"], "value": "max"})
        result = self.call(tools.status)
        self.assertEqual(result["state"]["presets"], {"fan_speed": "medium", "water_usage": "high"})

    def test_status_robot_error_without_message(self):
        FakeValetudo.routes["/api/v2/robot/state/attributes"] = [
            {"__class": "StatusStateAttribute", "value": "error", "error": {"severity": {"kind": "unknown"}}}]
        self.assertIn("unknown", self.call(tools.status)["state"]["error"])

    def test_status_consumable_event_names_the_consumable(self):
        FakeValetudo.routes["/api/v2/events"] = [{"__class": "ConsumableDepletedValetudoEvent", "timestamp": "t",
                                                  "processed": False, "type": "filter", "subType": "main"}]
        self.assertEqual(self.call(tools.status)["unread_events"],
                         [{"event": "ConsumableDepleted", "time": "t", "type": "filter", "subType": "main"}])

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

    def test_clean_rooms_iterations_default_and_float(self):
        props = f"{CAP}/MapSegmentationCapability/properties"
        self.call(tools.clean_rooms, {"rooms": ["Kitchen"], "iterations": 2.0})
        FakeValetudo.routes[props] = {"iterationCount": {"min": 2, "max": 3}}
        self.call(tools.clean_rooms, {"rooms": ["Kitchen"]})
        FakeValetudo.routes[props] = {"iterationCount": {"min": 2.0, "max": 3.0}}
        self.call(tools.clean_rooms, {"rooms": ["Kitchen"]})
        self.assertEqual([c[1]["iterations"] for c in FakeValetudo.calls], [2, 2, 2])

    def test_iteration_range_fallback(self):
        props = f"{CAP}/MapSegmentationCapability/properties"
        for value in ({}, {"iterationCount": "x"}, {"iterationCount": {"min": True, "max": "3"}}, []):
            with self.subTest(value=value):
                FakeValetudo.routes[props] = value
                self.assertEqual(self.call(tools.rooms)["iterations"], {"min": 1, "max": 1})

    def test_invalid_iteration_range_sends_nothing(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability/properties"] = {"iterationCount": {"min": 2, "max": 1}}
        self.assert_error(self.call(tools.clean_rooms, {"rooms": ["Kitchen"]}), "not valid (2 to 1)")
        result = self.call(tools.rooms)
        self.assertEqual(len(result["rooms"]), 3)
        self.assertIn("not valid", result["iterations_problem"])
        self.assertEqual(FakeValetudo.calls, [])

    def test_blank_segment_ids_are_skipped(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability"] = [
            {"id": "", "name": "Office"}, {"id": " ", "name": "Hall"}, {"id": True, "name": "Bad"}, {"id": 1, "name": "Kitchen"}]
        self.assertEqual(self.call(tools.rooms)["rooms"], [{"id": "1", "name": "Kitchen"}])
        self.assert_error(self.call(tools.clean_rooms, {"rooms": ["Office"]}), "Unknown room name")
        self.call(tools.clean_rooms, {"segment_ids": [" 1 "]})
        self.assertEqual(FakeValetudo.calls[0][1]["segment_ids"], ["1"])

    def test_robot_room_name_with_spaces(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability"] = [{"id": "1", "name": "Kitchen "}, {"name": "no id"}]
        self.assertEqual(self.call(tools.rooms)["rooms"], [{"id": "1", "name": "Kitchen"}])
        self.assertTrue(self.call(tools.clean_rooms, {"rooms": ["kitchen"]})["ok"])

    def test_clean_rooms_refusals_send_nothing(self):
        FakeValetudo.routes[f"{CAP}/MapSegmentationCapability"].append({"id": "3", "name": "office"})
        cases = [
            ({"rooms": ["Garage"]}, "Garage"),
            ({"segment_ids": ["99"]}, "99"),
            ({"rooms": ["10"]}, "Unknown room name"),  # an unnamed room is available by ID only
            ({"rooms": ["Office"]}, "More than one room"),
            ({}, "at least one"),
            ({"rooms": 5}, "list of non-empty strings"),
            ({"rooms": "Kitchen"}, "list of non-empty strings"),
            ({"rooms": ["Kitchen", " "]}, "list of non-empty strings"),
            ({"segment_ids": [10]}, "list of non-empty strings"),
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

    # --- rename ---

    def test_rename_room(self):
        result = self.call(tools.rename_room, {"room": "kitchen", "new_name": "  Big \t kitchen "})
        self.assertEqual(result, {"ok": True, "renamed": True, "segment_id": "1",
                                  "old_name": "Kitchen", "new_name": "Big kitchen"})
        result = self.call(tools.rename_room, {"segment_id": "10", "new_name": "Hall"})
        self.assertEqual(result["old_name"], None)
        rename = f"{CAP}/MapSegmentRenameCapability"
        self.assertEqual(FakeValetudo.calls, [
            (rename, {"action": "rename_segment", "segment_id": "1", "name": "Big kitchen"}),
            (rename, {"action": "rename_segment", "segment_id": "10", "name": "Hall"}),
        ])

    def test_rename_room_to_same_name_sends_nothing(self):
        result = self.call(tools.rename_room, {"room": "Kitchen", "new_name": "Kitchen"})
        self.assertEqual(result, {"ok": True, "renamed": False, "segment_id": "1", "name": "Kitchen"})
        self.assertTrue(self.call(tools.rename_room, {"room": "Kitchen", "new_name": "kitchen"})["renamed"])
        self.assertEqual(len(FakeValetudo.calls), 1)

    def test_rename_room_refusals_send_nothing(self):
        cases = [
            ({"new_name": "Hall"}, "exactly one of room or segment_id"),
            ({"room": "Kitchen", "segment_id": "1", "new_name": "Hall"}, "exactly one"),
            ({"room": "", "new_name": "Hall"}, "room must be a non-empty string"),
            ({"segment_id": 10, "new_name": "Hall"}, "segment_id must be a non-empty string"),
            ({"room": "Kitchen"}, "new_name must be a non-empty string"),
            ({"room": "Kitchen", "new_name": "  "}, "new_name must be a non-empty string"),
            ({"room": "Kitchen", "new_name": 5}, "new_name must be a non-empty string"),
            ({"room": "Kitchen", "new_name": "x" * 33}, "32 characters or fewer"),
            ({"room": "Kitchen", "new_name": "Hall\x00"}, "control characters"),
            ({"room": "Garage", "new_name": "Hall"}, "Unknown room name"),
            ({"room": "10", "new_name": "Hall"}, "Unknown room name"),  # an ID is not a name
            ({"segment_id": "99", "new_name": "Hall"}, "Unknown segment ID"),
            ({"room": "Kitchen", "new_name": "office"}, "Another room already has the name"),
        ]
        for args, text in cases:
            with self.subTest(args=args):
                self.assert_error(self.call(tools.rename_room, args), text)
        FakeValetudo.routes[CAP] = [c for c in CAPABILITIES if c != "MapSegmentRenameCapability"]
        tools._capability_cache.clear()
        self.assert_error(self.call(tools.rename_room, {"room": "Kitchen", "new_name": "Hall"}),
                          "MapSegmentRenameCapability")
        self.assertEqual(FakeValetudo.calls, [])

    def test_rename_room_robot_error(self):
        FakeValetudo.put_status = 500
        FakeValetudo.put_body = "Invalid name. Max length 23"
        result = self.call(tools.rename_room, {"room": "Kitchen", "new_name": "Hall"})
        self.assert_error(result, "HTTP 500 for PUT /robot/capabilities/MapSegmentRenameCapability: Invalid name")

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

    REDIRECTS = (300, 301, 302, 303, 307, 308)  # independent of tools.REDIRECT_CODES

    def test_get_redirect_is_not_followed(self):
        for code in self.REDIRECTS:
            with self.subTest(code=code):
                FakeValetudo.requests = []
                tools._capability_cache.clear()
                FakeValetudo.routes[CAP] = (code, "", {"Location": "http://127.0.0.1:9/elsewhere"})
                self.assert_error(self.call(tools.status), "redirect")
                self.assertEqual(FakeValetudo.requests, [("GET", CAP)])

    def test_put_redirect_is_not_followed(self):
        FakeValetudo.put_headers = {"Location": "/api/v2/robot/capabilities/MapResetCapability"}
        for code in self.REDIRECTS:
            with self.subTest(code=code):
                FakeValetudo.requests = []
                FakeValetudo.put_status = code
                self.assert_error(self.call(tools.control, {"action": "home"}), "redirect")
                self.assertEqual([r for r in FakeValetudo.requests if r[0] == "PUT"],
                                 [("PUT", f"{CAP}/BasicControlCapability")])

    def test_304_and_305_are_not_redirects(self):
        for code in (304, 305):
            with self.subTest(code=code):
                FakeValetudo.put_status = code
                result = self.call(tools.control, {"action": "home"})
                self.assert_error(result, f"HTTP {code} for PUT")
                self.assertNotIn("redirect", result["error"])

    def test_proxy_settings_are_ignored(self):
        proxy_hits = []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                proxy_hits.append(self.path)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        names = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
        saved = {k: os.environ.get(k) for k in names + ("no_proxy", "NO_PROXY")}
        try:
            for name in names:
                os.environ[name] = f"http://127.0.0.1:{proxy.server_port}"
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.assertTrue(self.call(tools.status)["ok"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            proxy.shutdown()
            proxy.server_close()
        self.assertEqual(proxy_hits, [])

    def test_http_error_detail(self):
        FakeValetudo.put_status = 400
        self.assert_error(self.call(tools.control, {"action": "home"}), "HTTP 400 for PUT /robot/capabilities/BasicControlCapability: Invalid request body")
        FakeValetudo.routes[CAP] = ["BasicControlCapability", "LocateCapability"]
        FakeValetudo.routes["/api/v2/robot/state/attributes"] = None  # the fake server answers with an HTML 404 page
        result = self.call(tools.status)
        self.assertTrue(result["error"].endswith("HTTP 404 for GET /robot/state/attributes"), result)

    def test_error_body_with_credentials_is_dropped(self):
        SECRETS.update(VALETUDO_USERNAME="user", VALETUDO_PASSWORD="s3cret")
        token = base64.b64encode(b"user:s3cret").decode()
        FakeValetudo.put_status = 403
        for body in (f"Basic {token}", f"authorization: {token}", f"bad {token}", "password s3cret refused"):
            with self.subTest(body=body):
                FakeValetudo.put_body = body
                result = self.call(tools.control, {"action": "home"})
                self.assertTrue(result["error"].endswith("PUT /robot/capabilities/BasicControlCapability"), result)
                self.assertNotIn("s3cret", json.dumps(result))
                self.assertNotIn(token, json.dumps(result))

    def test_error_body_filter_counts_characters_and_drops_html(self):
        FakeValetudo.put_status = 400
        for body in ("é" * 250, "Reason: <b>invalid</b>"):
            with self.subTest(body=body[:20]):
                FakeValetudo.put_body = body
                result = self.call(tools.control, {"action": "home"})
                self.assertTrue(result["error"].endswith("PUT /robot/capabilities/BasicControlCapability"), result)
        FakeValetudo.put_body = "é" * 150
        self.assertIn("é" * 150, self.call(tools.control, {"action": "home"})["error"])

    def test_error_body_read_failure(self):
        class Broken:
            closed = False

            def read(self, *args):
                raise OSError("reset")

            def close(self):
                pass

        exc = urllib.error.HTTPError("http://x", 400, "Bad", {}, Broken())
        self.assertEqual(tools._error_detail(exc), "")

    def test_html_page_is_not_success(self):
        FakeValetudo.put_ok_body = "<!doctype html><title>Login</title>"
        self.assert_error(self.call(tools.control, {"action": "home"}), "HTML page")
        FakeValetudo.put_ok_body = "Login"
        FakeValetudo.put_headers = {"Content-Type": "text/html; charset=utf-8"}
        self.assert_error(self.call(tools.control, {"action": "home"}), "HTML page")
        FakeValetudo.routes[CAP] = (200, "<html>Login</html>", None)
        tools._capability_cache.clear()
        self.assert_error(self.call(tools.status), "HTML page")

    def test_gateway_error_on_put_says_state_is_unknown(self):
        for code in (502, 504):
            with self.subTest(code=code):
                FakeValetudo.put_status = code
                self.assert_error(self.call(tools.control, {"action": "home"}), "possibly ran")
        FakeValetudo.put_status = 500
        self.assertNotIn("possibly ran", self.call(tools.control, {"action": "home"})["error"])

    def test_send_error_says_state_is_unknown(self):
        self.call(tools.control, {"action": "home"})  # fill the capability cache
        failing = mock.Mock()
        failing.open.side_effect = urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
        with mock.patch.object(tools, "_opener", return_value=failing):
            self.assert_error(self.call(tools.control, {"action": "home"}), "possibly ran")
            failing.open.side_effect = urllib.error.URLError(TimeoutError("timed out"))
            result = self.call(tools.control, {"action": "home"})
            self.assert_error(result, "no connection in")
            self.assertNotIn("possibly ran", result["error"])

    def test_put_timeout_says_state_is_unknown(self):
        tools.TIMEOUT = 0.3
        FakeValetudo.put_delay = 1.0
        self.assert_error(self.call(tools.control, {"action": "home"}), "possibly ran")

    def test_get_timeout_has_no_hint(self):
        tools.TIMEOUT = 0.3
        FakeValetudo.routes[CAP] = "slow"
        result = self.call(tools.status)
        self.assert_error(result, "did not answer")
        self.assertNotIn("possibly ran", result["error"])

    def test_put_connection_drop_says_state_is_unknown(self):
        FakeValetudo.put_drop = True
        self.assert_error(self.call(tools.control, {"action": "home"}), "possibly ran")

    def test_long_error_body_is_dropped(self):
        FakeValetudo.put_status = 400
        FakeValetudo.put_body = "x" * 500
        result = self.call(tools.control, {"action": "home"})
        self.assertTrue(result["error"].endswith("PUT /robot/capabilities/BasicControlCapability"), result)

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
