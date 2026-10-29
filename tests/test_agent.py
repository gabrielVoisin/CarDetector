"""Agent tests against an in-memory fake of the server API (python -m unittest)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.client import Client
from agent.config import Config, Lane
from agent.io_backends import IOBackend
from agent.main import UNREADABLE_SAMPLES, Agent

KEY = "ctrl0001.test-secret"
DEVICES = [
    {"code": "CPF-EXT-01", "kind": "front_presence_sensor", "address": "gpio:1", "bay_id": "b1", "bay_number": "01", "zone_code": "EXT"},
    {"code": "CPV-EXT-01", "kind": "vertical_presence_sensor", "address": "gpio:2", "bay_id": "b1", "bay_number": "01", "zone_code": "EXT"},
    {"code": "CAM-01", "kind": "camera", "address": "rtsp://127.0.0.1:1/stream", "bay_id": None, "bay_number": None, "zone_code": None},
]


class FakeServer:
    def __init__(self):
        self.received = {"readings": {}, "events": {}, "passages": {}}
        self.heartbeats = []
        self.watchdog = []
        self.down = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _guard(self):
                if server.down:
                    self._reply(503, {"detail": "maintenance"})
                    return False
                if self.headers.get("X-Controller-Key") != KEY:
                    self._reply(401, {"detail": "invalid_controller_key"})
                    return False
                return True

            def do_GET(self):
                if self._guard():
                    self._reply(200, {"controller_id": "c1", "controller_code": "CTRL-01", "site_code": "P1",
                                      "heartbeat_interval_s": 30, "upload_interval_s": 5, "devices": DEVICES,
                                      "server_time": "2026-10-28T10:00:00Z"})

            def do_POST(self):
                if not self._guard():
                    return
                kind = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if kind == "watchdog":
                    server.watchdog.append(body)
                    return self._reply(200, {"ok": True, "period_s": 2.0})
                if kind == "heartbeat":
                    server.heartbeats.append(body)
                    return self._reply(200, {"ok": True})
                dup = 0
                for item in body[kind]:
                    dup += item["uid"] in server.received[kind] and kind != "events"
                    server.received[kind][item["uid"]] = item
                if kind == "passages":
                    return self._reply(200, {"decisions": [{"uid": p["uid"], "open_barrier": p["plate"] == "AB-123-CD"} for p in body[kind]]})
                self._reply(200, {"accepted": len(body[kind]) - dup, "duplicates": dup, "rejected": []})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"


class ScriptedIO(IOBackend):
    def __init__(self):
        self.values = {1: False, 2: False, 9: False}
        self.pulses = []

    def read(self, channel):
        return self.values.get(channel)

    def pulse(self, channel, seconds):
        self.pulses.append(channel)


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(server_url=self.server.url, controller_key=KEY, data_dir=Path(self.tmp.name),
                          debounce_s=0, disagree_fault_s=0, offline_warning_after_s=0,
                          lanes=[Lane(direction="in", camera="CAM-01", loop_channel=9, barrier_channel=20)])
        self.io = ScriptedIO()
        self.agent = Agent(self.cfg, io=self.io, client=Client(self.server.url, KEY, timeout=2))
        self.assertTrue(self.agent.load_config())

    def tearDown(self):
        self.server.httpd.shutdown()
        self.tmp.cleanup()

    def test_bay_change_is_sent_once(self):
        self.io.values.update({1: True, 2: True})
        self.agent.sample_once()
        self.agent.sample_once()  # unchanged: nothing new
        self.assertTrue(self.agent.flush_once())
        readings = list(self.server.received["readings"].values())
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["device_code"], "CPV-EXT-01")
        self.assertTrue(readings[0]["is_occupied"])
        self.assertEqual(self.agent.outbox.size(), 0)

    def test_watchdog_signal_and_outage(self):
        self.agent.watchdog_once()
        self.agent.watchdog_once()
        self.assertEqual([w["seq"] for w in self.server.watchdog], [1, 2])
        self.assertEqual(self.server.watchdog[0]["source"], "agent")
        self.assertIs(self.agent.state.watchdog_ok, True)
        self.server.down = True
        self.agent.watchdog_once()
        self.assertIs(self.agent.state.watchdog_ok, False)
        self.server.down = False
        self.agent.watchdog_once()
        self.assertIs(self.agent.state.watchdog_ok, True)
        self.assertEqual(self.server.watchdog[-1]["seq"], 4)              # the lost signal shows in the numbering

    def test_unreadable_sensor_raises_then_clears_same_event(self):
        self.io.values.pop(2)
        for _ in range(UNREADABLE_SAMPLES):
            self.agent.sample_once()
        self.io.values[2] = False
        self.agent.sample_once()
        self.agent.flush_once()
        events = list(self.server.received["events"].values())
        self.assertEqual(len(events), 1)  # raise + clear share one uid
        self.assertEqual(events[0]["message_key"], "device.vertical_sensor_faulty")
        self.assertIsNotNone(events[0]["cleared_at"])

    def test_disagreeing_front_sensor_is_flagged(self):
        self.io.values.update({1: True, 2: False})
        self.agent.sample_once()
        self.agent.sample_once()
        self.agent.flush_once()
        keys = [e["message_key"] for e in self.server.received["events"].values()]
        self.assertIn("device.front_sensor_faulty", keys)

    def test_unreachable_camera_alarm(self):
        misses = {}
        self.agent.check_cameras(misses)
        self.agent.check_cameras(misses)
        self.agent.flush_once()
        cams = [e for e in self.server.received["events"].values() if e["code"] == "CAM-01"]
        self.assertEqual(cams[0]["priority"], "alarm")
        self.assertEqual(cams[0]["message_key"], "device.camera_faulty")

    def test_offline_queue_and_server_connection_warning(self):
        self.server.down = True
        self.io.values.update({1: True, 2: True})
        self.agent.sample_once()
        ok = self.agent.flush_once()
        self.assertFalse(ok)
        self.agent.watch_connection(ok)
        self.assertIn("NET-SRV", self.agent.state.faults)
        self.server.down = False
        self.agent.watch_connection(self.agent.flush_once())
        self.assertEqual(len(self.server.received["readings"]), 1)  # nothing lost
        net = [e for e in self.server.received["events"].values() if e["code"] == "NET-SRV"][0]
        self.assertEqual(net["message_key"], "device.server_connection_lost")
        self.assertIsNotNone(net["cleared_at"])

    def test_barrier_opens_only_when_server_allows(self):
        self.agent.read_plate = lambda camera: ("AB-123-CD", 0.98)
        self.agent.handle_vehicle(self.cfg.lanes[0])
        self.agent.read_plate = lambda camera: ("ZZ-999-ZZ", 0.95)
        self.agent.handle_vehicle(self.cfg.lanes[0])
        self.assertEqual(self.io.pulses, [20])

    def test_heartbeat_reports_device_status(self):
        self.agent.raise_fault("CAM-01", "camera", "offline")
        self.assertTrue(self.agent.heartbeat_once())
        statuses = {d["code"]: d["status"] for d in self.server.heartbeats[-1]["devices"]}
        self.assertEqual(statuses["CAM-01"], "fault")
        self.assertEqual(statuses["CPV-EXT-01"], "ok")


if __name__ == "__main__":
    unittest.main()
