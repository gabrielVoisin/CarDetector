"""Presence sensor simulator: a web page to park and remove cars by hand, to test the IntelPark dashboard.

It plays the simulated sensors of cabinet CTRL-01 (CP-002..CP-056, every outdoor bay of P1 except 05,
which is the real camera CP-001) and sends their readings over the network to the server, exactly like a
cabinet: POST /api/v1/device/readings with the header X-Controller-Key.

Server side, switch the simulated sensors on first:  python -m app.cli simulated-sensors on
Run:    python3 sensor_simulator/simulator.py           then open http://<pi>:8082
Config: sensor_simulator/simulator.env (SERVER_URL, CONTROLLER_KEY, LISTEN), see simulator.env.example.
Only the Python standard library is needed.
"""
import argparse
import json
import logging
import os
import random
import string
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from http import server
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Event, Lock, Thread

log = logging.getLogger("sensor-simulator")
HERE = Path(__file__).parent
CANTONS = ["VD", "GE", "VS", "FR", "NE", "BE", "ZH", "JU"]
RESEND_SECONDS = 60   # every sensor repeats its state, so the server stays right after a restart
CONFIG_SECONDS = 60   # the list of simulated sensors is read again from the server
WATCHDOG_SECONDS = 2  # communication watchdog with the server: one signal every 2 s (0.5 Hz)


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def random_plate():
    return random.choice(CANTONS) + "".join(random.choices(string.digits, k=random.randint(4, 6)))


class Simulator:
    def __init__(self, server_url, key):
        self.base = server_url.rstrip("/") + "/api/v1/device"
        self.key = key
        self.lock = Lock()
        self.sensors = {}          # code -> {"code", "bay", "zone", "occupied", "plate", "since"}
        self.traffic = False
        self.last_error = None
        self.last_sent = None
        self.wd_seq = 0
        self.wd_ok = None          # None = not tried yet
        self.wd_last_ok = None
        self.stop = Event()

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Controller-Key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read() or b"null")

    def load_config(self):
        try:
            cfg = self._call("GET", "/config?simulated=true")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"configuration: {e}"
            log.warning("cannot read the configuration: %s", e)
            return
        with self.lock:
            known = self.sensors
            self.sensors = {}
            for d in cfg["devices"]:
                old = known.get(d["code"], {})
                self.sensors[d["code"]] = {"code": d["code"], "bay": d["bay_number"], "zone": d["zone_code"],
                                           "occupied": old.get("occupied", False), "plate": old.get("plate"),
                                           "since": old.get("since")}
        if not known:
            log.info("%d simulated sensors (%s)", len(self.sensors), cfg["controller_code"])
            self.send(list(self.sensors))      # start from a known state: what the page shows is true

    def send(self, codes):
        if not codes:
            return
        at = datetime.now(UTC).isoformat()
        with self.lock:
            readings = [{"uid": f"sim-{uuid.uuid4().hex}", "device_code": c, "is_occupied": s["occupied"],
                         "plate": s["plate"] if s["occupied"] else None,
                         "confidence": 0.99 if s["occupied"] and s["plate"] else None, "measured_at": at}
                        for c in codes if (s := self.sensors.get(c))]
        try:
            for i in range(0, len(readings), 500):
                result = self._call("POST", "/readings", {"readings": readings[i:i + 500]})
                if result.get("rejected"):
                    log.warning("rejected by the server: %s", result["rejected"])
            self.last_error, self.last_sent = None, at
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"envoi: {e}"
            log.warning("cannot send the readings: %s", e)

    def watchdog(self):
        """Signal to the server (0.5 Hz). When the link comes back, the whole state is sent again at once,
        so that nothing parked or freed during the outage is missing on the website."""
        self.wd_seq += 1
        try:
            self._call("POST", "/watchdog", {"source": "simulator", "seq": self.wd_seq})
        except (urllib.error.URLError, OSError, ValueError) as e:
            if self.wd_ok is not False:
                log.warning("watchdog: server unreachable (%s)", e)
            self.wd_ok = False
            return
        if self.wd_ok is False:
            log.info("watchdog: server reachable again, sending the whole state")
            self.wd_ok = True
            self.load_config()
            self.send(list(self.sensors))
        self.wd_ok, self.wd_last_ok = True, datetime.now(UTC).isoformat()

    def set(self, code, occupied, plate=None):
        with self.lock:
            s = self.sensors.get(code)
            if s is None:
                return False
            s["occupied"] = occupied
            s["plate"] = ((plate or "").upper().replace(" ", "") or random_plate()) if occupied else None
            s["since"] = time.time() if occupied else None
        self.send([code])
        return True

    def set_many(self, rate):
        """rate 0 = every bay free, 1 = every bay occupied, in between = random."""
        with self.lock:
            for s in self.sensors.values():
                occupied = random.random() < rate
                if occupied != s["occupied"]:
                    s["occupied"], s["plate"] = occupied, random_plate() if occupied else None
                    s["since"] = time.time() if occupied else None
            codes = list(self.sensors)
        self.send(codes)

    def traffic_step(self):
        """One car arrives or leaves; the car park tends to stay between 30 % and 80 % full."""
        with self.lock:
            free = [c for c, s in self.sensors.items() if not s["occupied"]]
            taken = [c for c, s in self.sensors.items() if s["occupied"]]
        if not free and not taken:
            return
        rate = len(taken) / (len(free) + len(taken))
        arrive = free and (not taken or random.random() < (0.75 if rate < 0.3 else 0.25 if rate > 0.8 else 0.5))
        self.set(random.choice(free), True) if arrive else self.set(random.choice(taken), False)

    def snapshot(self):
        with self.lock:
            return {"sensors": sorted(self.sensors.values(), key=lambda s: s["bay"] or ""),
                    "traffic": self.traffic, "error": self.last_error, "last_sent": self.last_sent,
                    "server": self.base.rsplit("/api/", 1)[0],
                    "watchdog": {"ok": self.wd_ok, "seq": self.wd_seq, "last_ok": self.wd_last_ok,
                                 "period_s": WATCHDOG_SECONDS}}

    def background(self):
        next_config = next_resend = next_watchdog = 0.0
        next_traffic = time.time()
        while not self.stop.wait(0.25):
            now = time.time()
            if now >= next_watchdog:
                next_watchdog = now + WATCHDOG_SECONDS
                self.watchdog()
            if now >= next_config:
                self.load_config()
                next_config = now + CONFIG_SECONDS
            if now >= next_resend:
                self.send(list(self.sensors))
                next_resend = now + RESEND_SECONDS
            if self.traffic and now >= next_traffic:
                self.traffic_step()
                next_traffic = now + random.uniform(3, 8)


class Handler(server.BaseHTTPRequestHandler):
    sim: Simulator = None

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "page.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/state.json":
            self._send(200, self.sim.snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/bay":
            ok = self.sim.set(str(body.get("code")), bool(body.get("occupied")), body.get("plate"))
            return self._send(200 if ok else 404, self.sim.snapshot())
        if self.path == "/fill":
            self.sim.set_many(min(1.0, max(0.0, float(body.get("rate", 0)))))
            return self._send(200, self.sim.snapshot())
        if self.path == "/traffic":
            self.sim.traffic = bool(body.get("on"))
            return self._send(200, self.sim.snapshot())
        self._send(404, {"error": "not found"})


class Server(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    load_env(HERE / "simulator.env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=os.environ.get("SERVER_URL", "http://localhost"))
    p.add_argument("--listen", default=os.environ.get("LISTEN", "0.0.0.0:8082"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = os.environ.get("CONTROLLER_KEY")
    if not key:
        raise SystemExit("CONTROLLER_KEY is missing: copy simulator.env.example to simulator.env")
    sim = Simulator(args.server, key)
    Handler.sim = sim
    Thread(target=sim.background, daemon=True).start()
    host, port = args.listen.rsplit(":", 1)
    httpd = Server((host, int(port)), Handler)
    log.info("simulator page on http://%s:%s, server %s", host, port, args.server)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop.set()


if __name__ == "__main__":
    main()
