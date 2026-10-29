"""Agent main loop.

Threads:
  sampler    reads every presence sensor once a second, debounces, queues bay changes
  cameras    checks each camera answers (TCP connect on its RTSP/HTTP port)
  lanes      entry/exit loops: reads the plate, asks the server, opens the barrier
  uploader   sends the SQLite outbox (readings, events, passages, identifications) with exponential backoff
  heartbeat  tells the server the cabinet is alive + the status of each device
  detectors  (io_backend: network) HTTP endpoint where the Ethernet car detectors push their state
  ble        (ble.enabled) Bluetooth GATT server: identifies the drivers' phones ("smart bay")
Faults become events (alarm / warning) that the back office shows on "Appareils de contrôle".
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from agent import __version__
from agent import config as config_mod
from agent import identity
from agent.client import Client, Rejected, ServerError
from agent.io_backends import IOBackend, channel_of, make
from agent.outbox import KINDS, Outbox

log = logging.getLogger("parking-agent")

MSG = {
    "camera": "device.camera_faulty",
    "front_presence_sensor": "device.front_sensor_faulty",
    "vertical_presence_sensor": "device.vertical_sensor_faulty",
}
PRIORITY = {"camera": "alarm", "front_presence_sensor": "warning", "vertical_presence_sensor": "warning"}
UNREADABLE_SAMPLES = 10  # consecutive unreadable samples before a sensor is declared faulty


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BayWatch:
    bay_number: str
    front: str | None = None
    vertical: str | None = None
    reported: bool | None = None         # last state sent to the server
    candidate: bool | None = None
    candidate_since: float = 0.0
    disagree_since: float | None = None


@dataclass
class Fault:
    uid: str
    code: str
    priority: str
    message_key: str
    raised_at: datetime
    reason: str = "fault"                # unreadable | disagree | offline | network


@dataclass
class State:
    devices: dict[str, dict] = field(default_factory=dict)       # code -> server config item
    bays: dict[str, BayWatch] = field(default_factory=dict)      # bay_id -> watch
    faults: dict[str, Fault] = field(default_factory=dict)       # code -> active fault
    unreadable: dict[str, int] = field(default_factory=dict)
    heartbeat_s: int = 30
    upload_s: int = 5
    last_server_ok: float = field(default_factory=time.monotonic)
    controller_code: str = ""
    site_name: str = ""
    ble_key: str | None = None                                   # server Ed25519 public key (base64)
    ble_enabled: bool = False


class Agent:
    def __init__(self, cfg: config_mod.Config, io: IOBackend | None = None, client: Client | None = None):
        self.cfg = cfg
        self.io = io or make(cfg)
        self.client = client or Client(cfg.server_url, cfg.controller_key, cfg.ca_file)
        self.outbox = Outbox(cfg.data_dir / "outbox.sqlite3")
        self.state = State()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.ble = None            # agent.ble_gatt.BleServer once started
        self.detector_server = None

    # ---- configuration -----------------------------------------------------------------
    def load_config(self, allow_cache: bool = False) -> bool:
        cache = self.cfg.data_dir / "config-cache.json"
        try:
            data = self.client.config()
            cache.write_text(json.dumps(data), encoding="utf-8")
        except (ServerError, Rejected) as exc:
            if not (allow_cache and cache.exists()):
                log.warning("cannot download configuration: %s", exc)
                return False
            log.warning("server unreachable (%s): starting with the last known configuration", exc)
            data = json.loads(cache.read_text(encoding="utf-8"))
        ble = data.get("ble") or {}
        with self.lock:
            self.state.controller_code = data.get("controller_code", "")
            self.state.site_name = ble.get("site_name") or data.get("site_code", "")
            self.state.ble_key = ble.get("public_key")
            self.state.ble_enabled = bool(ble.get("enabled", True))
            self.state.heartbeat_s = int(data["heartbeat_interval_s"])
            self.state.upload_s = int(data["upload_interval_s"])
            self.state.devices = {d["code"]: d for d in data["devices"]}
            old = self.state.bays
            bays: dict[str, BayWatch] = {}
            for d in data["devices"]:
                if d["bay_id"] and d["kind"] in ("front_presence_sensor", "vertical_presence_sensor"):
                    watch = bays.setdefault(d["bay_id"], old.get(d["bay_id"]) or BayWatch(d["bay_number"] or "?"))
                    setattr(watch, "front" if d["kind"] == "front_presence_sensor" else "vertical", d["code"])
            self.state.bays = bays
        log.info("configuration: %d devices, %d bays", len(data["devices"]), len(bays))
        return True

    # ---- faults -> events ---------------------------------------------------------------
    def raise_fault(self, code: str, kind: str, reason: str, priority: str | None = None, message_key: str | None = None) -> None:
        with self.lock:
            if code in self.state.faults:
                return
            at = now()
            fault = Fault(f"{code}-{int(at.timestamp())}", code, priority or PRIORITY[kind], message_key or MSG[kind], at, reason)
            self.state.faults[code] = fault
        log.warning("fault raised %s %s", code, fault.message_key)
        self.outbox.put("events", self._event(fault, None))

    def clear_fault(self, code: str, reason: str | None = None) -> None:
        with self.lock:
            fault = self.state.faults.get(code)
            if fault is None or (reason and fault.reason != reason):
                return
            del self.state.faults[code]
        if fault:
            log.info("fault cleared %s", code)
            self.outbox.put("events", self._event(fault, now()))  # same uid: the server updates the event

    @staticmethod
    def _event(f: Fault, cleared_at: datetime | None) -> dict:
        return {"uid": f.uid, "code": f.code, "priority": f.priority, "message_key": f.message_key,
                "raised_at": f.raised_at.isoformat(), "cleared_at": cleared_at.isoformat() if cleared_at else None}

    # ---- presence sensors ---------------------------------------------------------------
    def _read_sensor(self, code: str | None) -> bool | None:
        if not code:
            return None
        device = self.state.devices.get(code, {})
        value = self.io.read(channel_of(device.get("address")) or -1)
        kind = device.get("kind", "front_presence_sensor")
        if value is None:
            n = self.state.unreadable[code] = self.state.unreadable.get(code, 0) + 1
            if n >= UNREADABLE_SAMPLES:
                self.raise_fault(code, kind, "unreadable")
        else:
            self.state.unreadable.pop(code, None)
            self.clear_fault(code, "unreadable")
        return value

    def sample_once(self) -> None:
        t = time.monotonic()
        for bay_id, w in list(self.state.bays.items()):
            front, vertical = self._read_sensor(w.front), self._read_sensor(w.vertical)
            # The overhead (vertical) sensor is the reference; the front sensor confirms it.
            if front is not None and vertical is not None and front != vertical:
                w.disagree_since = w.disagree_since or t
                if t - w.disagree_since > self.cfg.disagree_fault_s and w.front:
                    self.raise_fault(w.front, "front_presence_sensor", "disagree")
            elif front is not None and vertical is not None:
                if w.disagree_since and w.front:
                    self.clear_fault(w.front, "disagree")
                w.disagree_since = None
            occupied = vertical if vertical is not None else front
            if occupied is None:
                continue
            if occupied != w.candidate:
                w.candidate, w.candidate_since = occupied, t
            if occupied != w.reported and t - w.candidate_since >= self.cfg.debounce_s:
                w.reported = occupied
                at = now()
                self.outbox.put("readings", {
                    "uid": f"{bay_id[:8]}-{int(at.timestamp() * 1000)}",
                    "device_code": w.vertical if vertical is not None else w.front,
                    "is_occupied": occupied, "plate": None, "confidence": None, "measured_at": at.isoformat(),
                })

    # ---- cameras ------------------------------------------------------------------------
    @staticmethod
    def camera_alive(address: str | None, timeout: float = 3.0) -> bool:
        if not address:
            return False
        url = urlparse(address)
        port = url.port or {"rtsp": 554, "http": 80, "https": 443}.get(url.scheme, 554)
        try:
            with socket.create_connection((url.hostname, port), timeout=timeout):
                return True
        except OSError:
            return False

    def check_cameras(self, misses: dict[str, int]) -> None:
        for code, d in list(self.state.devices.items()):
            if d["kind"] != "camera":
                continue
            if self.camera_alive(d.get("address")):
                misses[code] = 0
                self.clear_fault(code, "offline")
            else:
                misses[code] = misses.get(code, 0) + 1
                if misses[code] >= 2:
                    self.raise_fault(code, "camera", "offline")

    # ---- entry / exit lanes ---------------------------------------------------------------
    def read_plate(self, camera: str) -> tuple[str | None, float | None]:
        if not self.cfg.anpr_url:
            return None, None
        try:
            req = urllib.request.Request(f"{self.cfg.anpr_url.rstrip('/')}/read?camera={camera}", method="POST")
            with urllib.request.urlopen(req, timeout=3) as res:
                data = json.loads(res.read())
            return data.get("plate"), data.get("confidence")
        except OSError as exc:
            log.warning("ANPR failed on %s: %s", camera, exc)
            return None, None

    def handle_vehicle(self, lane: config_mod.Lane) -> None:
        plate, confidence = self.read_plate(lane.camera)
        at = now()
        passage = {"uid": f"{lane.camera}-{int(at.timestamp() * 1000)}", "device_code": lane.camera,
                   "direction": lane.direction, "plate": plate, "confidence": confidence, "at": at.isoformat()}
        try:
            decision = self.client.send("passages", [passage])["decisions"][0]
            self.state.last_server_ok = time.monotonic()
            open_barrier = decision["open_barrier"]
        except (ServerError, Rejected, KeyError, IndexError):
            self.outbox.put("passages", passage)  # recorded later; offline rule: always let vehicles out
            open_barrier = lane.direction == "out"
        if open_barrier:
            self.io.pulse(lane.barrier_channel, lane.pulse_s)

    # ---- Bluetooth identification of the phones ("smart bay") ----------------------------------
    def ble_info(self) -> dict:
        online = time.monotonic() - self.state.last_server_ok < self.cfg.offline_warning_after_s
        return {"site": self.state.site_name, "online": online}

    def handle_ble(self, device: str, message: bytes) -> dict:
        """A phone wrote its identity. Verify it here, then ask the server whether it is the right driver."""
        with self.lock:
            key, code, enabled = self.state.ble_key, self.state.controller_code, self.state.ble_enabled
            bays = set(self.state.bays)
        if not key or not enabled:
            return identity.result(None, "not_configured", False)
        try:
            found = identity.identify(message, device, self.ble.challenges, code, key, bays)
        except identity.IdentifyError as exc:
            log.info("phone refused: %s", exc)
            return identity.result(None, str(exc), False)
        log.info("phone identified: customer %s, bay %s", found.item["customer_id"], found.item["bay_id"])
        try:
            decision = self.client.send("identifications", [found.item], timeout=4)["decisions"][0]
            self.state.last_server_ok = time.monotonic()
            return identity.result(found.challenge, decision["status"], True, bay=decision.get("bay_label"),
                                   bay_id=decision.get("bay_id"), online=True)
        except (ServerError, Rejected, KeyError, IndexError) as exc:
            log.info("server not reached for the identification (%s): queued", exc)
            self.outbox.put("identifications", found.item)  # the server decides when the network is back
            return identity.result(found.challenge, "pending", True, online=False)

    def start_ble(self) -> None:
        if not self.cfg.ble.enabled:
            return
        from agent.ble_gatt import BleServer

        try:
            self.ble = BleServer(self.cfg.ble.adapter, self.state.controller_code, self.ble_info, self.handle_ble)
            self.ble.start()
        except Exception as exc:  # missing adapter or BlueZ: the cabinet keeps running without Bluetooth
            log.error("Bluetooth not started: %s", exc)
            self.ble = None
            self.raise_fault("BLE", "network", "network", "warning", "device.bluetooth_unavailable")

    def start_detectors(self) -> None:
        from agent.detectors import DetectorServer, NetworkIO

        if not isinstance(self.io, NetworkIO):
            return
        d = self.cfg.detectors
        self.detector_server = DetectorServer(self.io, d.listen, d.token, lambda: self.state.devices, channel_of)
        self.detector_server.start()

    # ---- upload ---------------------------------------------------------------------------
    def flush_once(self) -> bool:
        """Send what is queued. Returns False when the server could not be reached."""
        for kind in KINDS:
            while True:
                batch = self.outbox.peek(kind, 200)
                if not batch:
                    break
                try:
                    self.client.send(kind, [item for _, item in batch])
                except ServerError as exc:
                    log.info("server unreachable (%s), %d item(s) waiting", exc, self.outbox.size())
                    return False
                except Rejected as exc:
                    if str(exc).startswith(("401", "403")):
                        log.error("controller key refused by the server: %s", exc)
                        return False
                    log.error("server rejected %d %s, dropped: %s", len(batch), kind, exc)
                self.outbox.ack([row_id for row_id, _ in batch])
                self.state.last_server_ok = time.monotonic()
        return True

    def watch_connection(self, reachable: bool) -> None:
        offline_for = time.monotonic() - self.state.last_server_ok
        if not reachable and offline_for > self.cfg.offline_warning_after_s:
            self.raise_fault("NET-SRV", "network", "network", "warning", "device.server_connection_lost")
        elif reachable and "NET-SRV" in self.state.faults:
            self.clear_fault("NET-SRV", "network")
            self.flush_once()

    def heartbeat_once(self) -> bool:
        with self.lock:
            statuses = [{"code": code, "status": "fault" if code in self.state.faults else "ok"} for code in self.state.devices]
        try:
            self.client.heartbeat({"agent_version": __version__, "uptime_s": int(time.monotonic() - self.started),
                                   "cpu_temp_c": cpu_temperature(), "devices": statuses})
            self.state.last_server_ok = time.monotonic()
            return True
        except (ServerError, Rejected) as exc:
            log.info("heartbeat failed: %s", exc)
            return False

    # ---- threads ------------------------------------------------------------------------
    def _loop(self, name: str, interval, step) -> threading.Thread:
        def body():
            while not self.stop.is_set():
                try:
                    delay = step()
                except Exception:  # keep the cabinet running whatever happens
                    log.exception("%s loop error", name)
                    delay = None
                self.stop.wait(delay if delay is not None else interval())
        thread = threading.Thread(target=body, name=name, daemon=True)
        thread.start()
        return thread

    def run_forever(self) -> None:
        backoff = 1.0
        while not self.load_config(allow_cache=backoff >= 8) and not self.stop.is_set():
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        self.start_detectors()
        self.start_ble()
        misses: dict[str, int] = {}
        lane_state = {id(lane): False for lane in self.cfg.lanes}

        def upload():
            ok = self.flush_once()
            self.watch_connection(ok)
            upload.backoff = self.state.upload_s if ok else min(getattr(upload, "backoff", 1) * 2, 60)
            return upload.backoff

        def lanes():
            for lane in self.cfg.lanes:
                present = bool(self.io.read(lane.loop_channel))
                if present and not lane_state[id(lane)]:
                    self.handle_vehicle(lane)
                lane_state[id(lane)] = present
            return 0.2

        self._loop("sampler", lambda: self.cfg.sample_interval_s, self.sample_once)
        self._loop("cameras", lambda: self.cfg.camera_check_interval_s, lambda: self.check_cameras(misses))
        self._loop("uploader", lambda: self.state.upload_s, upload)
        self._loop("heartbeat", lambda: self.state.heartbeat_s, self.heartbeat_once)
        self._loop("config", lambda: 600, lambda: self.stop.wait(600) or (self.load_config() and 0))
        if self.cfg.lanes:
            self._loop("lanes", lambda: 0.2, lanes)
        log.info("agent %s running", __version__)
        self.stop.wait()


def cpu_temperature() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return int(fh.read()) / 1000
    except OSError:
        return None


def run() -> None:
    parser = argparse.ArgumentParser(prog="parking-agent")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    agent = Agent(config_mod.load(args.config))
    signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop.set()
    finally:
        if agent.ble is not None:
            agent.ble.stop()
        if agent.detector_server is not None:
            agent.detector_server.stop()
