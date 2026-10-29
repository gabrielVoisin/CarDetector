"""Car detectors connected to the cabinet over Ethernet (io_backend: network).

Each detector pushes its state to the cabinet's Raspberry Pi over the local network:

    POST http://<cabinet>:8090/detectors/<device code>      {"occupied": true}
    header X-Detector-Token: <detectors.token>                  (if a token is configured)

and repeats it at least every `detectors.timeout_s` seconds (heartbeat). A detector that stays
silent longer reads as "unreadable", which raises the usual "sensor faulty" warning.

The same endpoint drives the prototype where everything runs on one Raspberry Pi:

    POST /simulate/bay/EXT/05   {"occupied": true}    both sensors of bay EXT 05 at once
    GET  /detectors                                    current state of every detector
    simulate_traffic: true                             random arrivals and departures (demo mode)
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.io_backends import IOBackend

log = logging.getLogger("parking-agent.detectors")


class NetworkIO(IOBackend):
    """Presence values pushed by the detectors; barrier relays (if any) on the Pi's own GPIO."""

    def __init__(self, timeout_s: float = 30.0, outputs: IOBackend | None = None, simulate_traffic: bool = False,
                 clock: Callable[[], float] = time.monotonic, seed: int | None = None):
        self.timeout_s = timeout_s
        self.outputs = outputs
        self.simulate_traffic = simulate_traffic
        self.clock = clock
        self.rng = random.Random(seed)
        self.values: dict[int, tuple[bool, float]] = {}
        self.pulses: list[tuple[int, float]] = []
        self.lock = threading.Lock()

    def set(self, channel: int, occupied: bool) -> None:
        with self.lock:
            self.values[channel] = (bool(occupied), self.clock())

    def read(self, channel: int) -> bool | None:
        with self.lock:
            item = self.values.get(channel)
            if item is None and self.simulate_traffic:
                item = self.values[channel] = (self.rng.random() < 0.6, self.clock())
            if item is None:
                return None
            value, seen = item
            if self.simulate_traffic:
                if self.rng.random() < 0.002:
                    value = not value
                self.values[channel] = (value, self.clock())  # simulated detectors never go silent
                return value
            if self.timeout_s and self.clock() - seen > self.timeout_s:
                return None
            return value

    def pulse(self, channel: int, seconds: float) -> None:
        self.pulses.append((channel, seconds))
        if self.outputs is not None:
            self.outputs.pulse(channel, seconds)

    def snapshot(self) -> dict[int, dict]:
        now = self.clock()
        with self.lock:
            return {ch: {"occupied": v, "age_s": round(now - seen, 1)} for ch, (v, seen) in self.values.items()}


class DetectorServer:
    """Small HTTP server on the cabinet's LAN (standard library only)."""

    def __init__(self, io: NetworkIO, listen: str, token: str | None,
                 devices: Callable[[], dict[str, dict]], channel_of: Callable[[str | None], int | None]):
        host, _, port = listen.rpartition(":")
        self.address = (host or "0.0.0.0", int(port))
        self.io, self.token, self.devices, self.channel_of = io, token, devices, channel_of
        self.httpd: ThreadingHTTPServer | None = None

    def _channels_for_bay(self, zone: str, number: str) -> list[int]:
        out = []
        for d in self.devices().values():
            if d.get("kind") in ("front_presence_sensor", "vertical_presence_sensor") and d.get("zone_code") == zone \
                    and (d.get("bay_number") or "").lstrip("0") == number.lstrip("0"):
                ch = self.channel_of(d.get("address"))
                if ch is not None:
                    out.append(ch)
        return out

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("detector %s " + fmt, self.client_address[0], *args)

            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self) -> bool:
                if server.token and self.headers.get("X-Detector-Token") != server.token:
                    self._reply(401, {"detail": "detector_token_invalid"})
                    return False
                return True

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(min(length, 4096)) or b"{}")

            def do_GET(self):  # noqa: N802
                if not self._authorised():
                    return
                if self.path.rstrip("/") == "/detectors":
                    by_channel = server.io.snapshot()
                    states = {}
                    for code, d in server.devices().items():
                        ch = server.channel_of(d.get("address"))
                        if ch in by_channel:
                            states[code] = {**by_channel[ch], "bay": f"{d.get('zone_code')}/{d.get('bay_number')}"}
                    self._reply(200, states)
                else:
                    self._reply(404, {"detail": "not_found"})

            def do_POST(self):  # noqa: N802
                if not self._authorised():
                    return
                try:
                    occupied = bool(self._body()["occupied"])
                except (ValueError, KeyError, TypeError):
                    self._reply(422, {"detail": "expected {\"occupied\": true|false}"})
                    return
                parts = [p for p in self.path.split("/") if p]
                if len(parts) == 2 and parts[0] == "detectors":
                    device = server.devices().get(parts[1])
                    channel = server.channel_of(device.get("address")) if device else None
                    channels = [channel] if channel is not None else []
                elif len(parts) == 4 and parts[:2] == ["simulate", "bay"]:
                    channels = server._channels_for_bay(parts[2], parts[3])
                else:
                    self._reply(404, {"detail": "not_found"})
                    return
                if not channels:
                    self._reply(404, {"detail": "unknown_detector_or_bay"})
                    return
                for ch in channels:
                    server.io.set(ch, occupied)
                self._reply(200, {"ok": True, "channels": channels, "occupied": occupied})

        self.httpd = ThreadingHTTPServer(self.address, Handler)
        threading.Thread(target=self.httpd.serve_forever, name="detectors", daemon=True).start()
        log.info("detectors endpoint on http://%s:%d", *self.address)

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
