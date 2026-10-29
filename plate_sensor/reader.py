"""Plate sensor: the Raspberry Pi camera reads car plates (fast-alpr) and acts as presence sensor CP-001.

  * tells the cabinet agent whether the bay is occupied:  POST <--detector-url> {"occupied": true|false}
    on every change and every --detector-heartbeat seconds (a bay is occupied while a plate is confirmed,
    and for --occupied-hold seconds after the last confirmed read)
  * serves the live view and the current values to the IntelPark back office (HTTP, default 0.0.0.0:8081):
        GET /stream.mjpg     live MJPEG view with the plate boxes
        GET /status.json     current values of the sensor
    Both need the SENSOR_TOKEN (header X-Sensor-Token or ?token=) unless the request comes from 127.0.0.1.
  * optionally also reports each confirmed plate to the old Smart Parking web app (--server)

Run:   python plate_sensor/reader.py [--rotate 90]
Test:  python plate_sensor/reader.py --image car.jpg      (one image, nothing is sent)
"""
import argparse
import hmac
import io
import json
import logging
import os
import re
import signal
import socketserver
import statistics
import time
from collections import Counter, deque
from datetime import UTC, datetime
from http import server
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from urllib.parse import parse_qs, urlsplit

import cv2
import requests
from fast_alpr import ALPR

log = logging.getLogger("plate-sensor")
ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
OVERLAY_SECONDS = 1.0  # how long a detection box stays on the live view after it was seen
CPU_TEMP_FILE = Path("/sys/class/thermal/thermal_zone0/temp")
THERMAL_CHECK_SECONDS = 5.0
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def normalize_plate(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def rotate_frame(frame, degrees):
    """Rotate clockwise by 0/90/180/270 degrees."""
    return cv2.rotate(frame, ROTATIONS[degrees]) if degrees else frame


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


def find_plates(alpr, frame):
    """Return every plate the AI finds as (text, confidence, (x1, y1, x2, y2)); text may be empty."""
    found = []
    for result in alpr.predict(frame):
        b = result.detection.bounding_box
        text, conf = "", result.detection.confidence
        if result.ocr is not None and result.ocr.text:
            text = normalize_plate(result.ocr.text)
            conf = result.ocr.confidence
            conf = statistics.mean(conf) if isinstance(conf, list) else conf
        found.append((text, float(conf), (b.x1, b.y1, b.x2, b.y2)))
    return found


def usable_readings(found, min_conf):
    """Readings good enough to trust: [(plate, confidence)], best first."""
    good = [(text, conf) for text, conf, _box in found if text and conf >= min_conf]
    return sorted(good, key=lambda p: p[1], reverse=True)


class Confirmer:
    """Only confirms a plate seen in at least `min_votes` of the last `window` frames.

    A single OCR misread (a wrong character in one frame) is therefore never reported.
    """

    def __init__(self, window, min_votes):
        self.frames = deque(maxlen=window)
        self.min_votes = min_votes

    def update(self, plates):
        """Add one frame's readings; return (plate, mean_confidence) if confirmed, else None."""
        self.frames.append(dict(plates))  # plate -> confidence for this frame
        votes = Counter(p for frame in self.frames for p in frame)
        if not votes:
            return None
        plate, count = votes.most_common(1)[0]
        if count < self.min_votes:
            return None
        return plate, statistics.mean(f[plate] for f in self.frames if plate in f)


def read_cpu_temp():
    """CPU temperature in °C, or None if it cannot be read."""
    try:
        return int(CPU_TEMP_FILE.read_text()) / 1000
    except (OSError, ValueError):
        return None


class ThermalGuard:
    """Slows down, then pauses, plate analysis when the CPU gets hot (the live view keeps running).

    normal: --max-fps; hot (>= hot_temp): --hot-fps; paused (>= pause_temp): no analysis until the
    CPU is back under resume_temp. Leaving "hot" also needs resume_temp, so the rate does not flap.
    """

    def __init__(self, max_fps, hot_fps, hot_temp, pause_temp, resume_temp):
        self.rates = {"normal": max_fps, "hot": hot_fps, "paused": 0}
        self.hot_temp, self.pause_temp, self.resume_temp = hot_temp, pause_temp, resume_temp
        self.state, self.temp, self.checked = "normal", None, 0.0

    def update(self):
        """Re-read the temperature every few seconds; log state changes."""
        if time.time() - self.checked < THERMAL_CHECK_SECONDS:
            return
        self.checked, self.temp = time.time(), read_cpu_temp()
        if self.temp is None:
            return
        if self.temp >= self.pause_temp:
            new = "paused"
        elif self.temp >= self.hot_temp:
            new = "paused" if self.state == "paused" else "hot"
        elif self.temp < self.resume_temp:
            new = "normal"
        else:
            new = self.state  # between resume_temp and hot_temp: keep the current state
        if new != self.state:
            log.warning("CPU %.1f°C: analysis %s -> %s (%s)", self.temp, self.state, new,
                        "paused, live view continues" if new == "paused" else f"max {self.rates[new]:g} frames/s")
            self.state = new

    @property
    def fps(self):
        return self.rates[self.state]


class SensorState:
    """Current values of the sensor, shared by the analysis loop, the HTTP server and the agent notifier."""

    def __init__(self, device_code, rotate, hold_s):
        self.lock = Lock()
        self.device_code, self.rotate, self.hold_s = device_code, rotate, hold_s
        self.plate = self.confidence = None
        self.plate_since = self.last_confirmed = 0.0
        self.analysed_fps = self.stream_fps = 0.0
        self.cpu_temp, self.thermal_state = None, "normal"
        self.updated_at = time.time()
        self.paused = False

    def confirmed(self, plate, conf):
        now = time.time()
        with self.lock:
            if plate != self.plate or not self._occupied(now):
                self.plate_since = now  # a new car (or the same one coming back)
            self.plate, self.confidence, self.last_confirmed, self.updated_at = plate, conf, now, now

    def _occupied(self, now):
        if not self.last_confirmed:
            return False
        # While analysis is paused (too hot) the last known state is kept: "not seen" is not "free".
        return self.paused or now - self.last_confirmed <= self.hold_s

    @property
    def occupied(self):
        with self.lock:
            return self._occupied(time.time())

    def snapshot(self):
        now = time.time()
        with self.lock:
            occupied = self._occupied(now)
            return {
                "device_code": self.device_code,
                "plate": self.plate if occupied else None,
                "confidence": round(self.confidence, 3) if occupied and self.confidence is not None else None,
                "occupied": occupied,
                "plate_since": iso(self.plate_since) if occupied else None,
                "analysed_fps": round(self.analysed_fps, 1),
                "stream_fps": round(self.stream_fps, 1),
                "cpu_temp_c": round(self.cpu_temp, 1) if self.cpu_temp is not None else None,
                "thermal_state": self.thermal_state,
                "rotate": self.rotate,
                "updated_at": iso(self.updated_at),
            }


class FrameBuffer(io.BufferedIOBase):
    """Latest JPEG frame of the live view; wakes up the HTTP clients waiting for it."""

    def __init__(self):
        self.frame = None
        self.cond = Condition()

    def write(self, buf):
        with self.cond:
            self.frame = bytes(buf)
            self.cond.notify_all()
        return len(buf)


class Overlay:
    """Latest detections, drawn as boxes on the live view so you can see what the AI sees."""

    def __init__(self):
        self.lock = Lock()
        self.found, self.frame_size, self.at = [], (1, 1), 0.0

    def set(self, found, frame_size, min_conf):
        with self.lock:
            self.found = [(t, c, box, bool(t) and c >= min_conf) for t, c, box in found]
            self.frame_size, self.at = frame_size, time.time()

    def draw(self, img):
        with self.lock:
            found, (w, h), age = self.found, self.frame_size, time.time() - self.at
        if age > OVERLAY_SECONDS:
            return
        sx, sy = img.shape[1] / w, img.shape[0] / h
        for text, conf, (x1, y1, x2, y2), accepted in found:
            color = (76, 175, 80) if accepted else (0, 165, 255)  # green = reported, orange = too unsure
            p1, p2 = (int(x1 * sx), int(y1 * sy)), (int(x2 * sx), int(y2 * sy))
            cv2.rectangle(img, p1, p2, color, 2)
            label = f"{text or '?'} {conf:.0%}"
            cv2.putText(img, label, (p1[0], max(14, p1[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)


def stream_loop(cam, frames, overlay, state, rotate, stop):
    """Feeds the live view from the camera's small stream at the camera's own frame rate (~30 fps)."""
    count, since = 0, time.time()
    while not stop.is_set():
        yuv = cam.capture_array("lores")
        img = rotate_frame(cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420), rotate)
        overlay.draw(img)
        ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            frames.write(jpeg.tobytes())
            count += 1
        if time.time() - since >= 2:
            state.stream_fps, count, since = count / (time.time() - since), 0, time.time()


def start_http_server(frames, state, host, port, token):
    """Serve /stream.mjpg and /status.json on http://host:port (in a background thread)."""

    class Handler(server.BaseHTTPRequestHandler):
        def authorised(self, query):
            if not token or self.client_address[0] in LOOPBACK:
                return True
            given = self.headers.get("X-Sensor-Token") or (query.get("token") or [""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path not in ("/stream.mjpg", "/status.json"):
                self.send_error(404)
                return
            if not self.authorised(parse_qs(url.query)):
                self.send_error(401, "sensor token missing or wrong")
                return
            if url.path == "/status.json":
                body = json.dumps(state.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with frames.cond:
                        if not frames.cond.wait(timeout=5):
                            break  # camera stopped delivering frames
                        frame = frames.frame
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # viewer closed the page

        def log_message(self, *_args):
            pass

    class Server(socketserver.ThreadingMixIn, server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server((host, port), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def notify_agent_loop(state, url, token, heartbeat, stop):
    """Push the bay state to the cabinet agent on every change and every `heartbeat` seconds."""
    session = requests.Session()
    headers = {"X-Detector-Token": token} if token else {}
    sent, sent_at, failing = None, 0.0, False
    while not stop.wait(0.5):
        occupied = state.occupied
        if occupied == sent and time.time() - sent_at < heartbeat:
            continue
        try:
            r = session.post(url, json={"occupied": occupied}, headers=headers, timeout=3)
            r.raise_for_status()
            if occupied != sent:
                log.info("Bay %s -> agent", "occupied" if occupied else "free")
            if failing:
                log.info("Cabinet agent reachable again")
            sent, failing = occupied, False
        except requests.RequestException as exc:
            if not failing:
                log.warning("Could not reach the cabinet agent at %s: %s", url, exc)
            failing = True
        sent_at = time.time()  # on failure, retry at the next heartbeat


def open_camera(index, width, height, with_view):
    """Start the camera. Plates are read from the full-size stream; the live view uses a small `lores` one."""
    from picamera2 import Picamera2  # imported here so --image works without a camera

    cam = Picamera2(index)
    # "RGB888" is laid out as BGR in memory, which is what OpenCV / fast-alpr expect.
    config = {"main": {"size": (width, height), "format": "RGB888"}}
    if with_view:
        view_w = 640
        view_h = round(view_w * height / width / 2) * 2  # even height, same aspect ratio
        config["lores"] = {"size": (view_w, view_h), "format": "YUV420"}
    cam.configure(cam.create_video_configuration(**config))
    cam.start()
    try:
        cam.set_controls({"AfMode": 2})  # continuous autofocus (Camera Module 3)
    except Exception:
        log.info("Autofocus not available on this camera")
    time.sleep(1)  # let exposure settle
    return cam


def stop_on_sigterm(_signum, _frame):
    raise KeyboardInterrupt


def load_api_key(path):
    key = os.environ.get("PARKING_API_KEY")
    if key:
        return key
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    raise SystemExit("No API key for --server: set PARKING_API_KEY or --api-key-file")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0, help="camera index (see `rpicam-hello --list-cameras`)")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="turn the picture clockwise by this many degrees, for the AI and the live view")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--min-conf", type=float, default=0.7, help="ignore OCR readings below this confidence")
    ap.add_argument("--window", type=int, default=5, help="number of recent frames used to confirm a plate")
    ap.add_argument("--min-votes", type=int, default=2, help="frames (out of --window) that must agree")
    ap.add_argument("--max-fps", type=float, default=3.0,
                    help="analyse at most this many frames per second (the live view is not limited)")
    ap.add_argument("--hot-temp", type=float, default=75.0, help="CPU °C above which analysis slows to --hot-fps")
    ap.add_argument("--hot-fps", type=float, default=1.0, help="frames per second analysed while the CPU is hot")
    ap.add_argument("--pause-temp", type=float, default=82.0, help="CPU °C above which analysis is paused")
    ap.add_argument("--resume-temp", type=float, default=70.0,
                    help="CPU °C under which analysis goes back to --max-fps after being slowed or paused")
    # presence sensor for the IntelPark cabinet agent
    ap.add_argument("--device-code", default="CP-001", help="code of this presence sensor in IntelPark")
    ap.add_argument("--detector-url", default="http://127.0.0.1:8090/detectors/CP-001",
                    help="cabinet agent endpoint that receives {\"occupied\": ...}; empty = do not report")
    ap.add_argument("--detector-heartbeat", type=float, default=10.0, help="seconds between repeated reports")
    ap.add_argument("--occupied-hold", type=float, default=5.0,
                    help="the bay stays occupied this many seconds after the last confirmed plate")
    # live view and current values for the back office
    ap.add_argument("--http-host", default="0.0.0.0", help="address of the live view / status server")
    ap.add_argument("--http-port", type=int, default=8081, help="port of the live view / status server")
    ap.add_argument("--no-http", action="store_true", help="do not serve the live view and status")
    # optional: the old Smart Parking web app
    ap.add_argument("--server", default="", help="Smart Parking web app base URL to report plates to (optional)")
    ap.add_argument("--api-key-file", default="", help="API key file of that web app (or set PARKING_API_KEY)")
    ap.add_argument("--heartbeat", type=float, default=1.0, help="seconds between reports of a visible plate")
    ap.add_argument("--image", help="read one image file and exit (nothing is sent)")
    ap.add_argument("--dry-run", action="store_true", help="read the camera but do not send anything")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    log.info("Loading plate detection and OCR models…")
    alpr = ALPR(detector_model="yolo-v9-t-384-license-plate-end2end", ocr_model="cct-xs-v2-global-model")

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            raise SystemExit(f"Cannot read image {args.image}")
        found = find_plates(alpr, rotate_frame(frame, args.rotate))
        for text, conf, _box in found:
            print(f"{text or '(unreadable)'}  ({conf:.0%})")
        if not found:
            print("No plate found")
        return

    token = os.environ.get("SENSOR_TOKEN", "").strip()
    api_key = load_api_key(args.api_key_file) if args.server and not args.dry_run else None
    session = requests.Session()
    signal.signal(signal.SIGTERM, stop_on_sigterm)  # so `systemctl stop` shuts the camera down cleanly
    cam = open_camera(args.camera, args.width, args.height, with_view=not args.no_http)
    overlay, stop = Overlay(), Event()
    state = SensorState(args.device_code, args.rotate, args.occupied_hold)
    if not args.no_http:
        frames = FrameBuffer()
        start_http_server(frames, state, args.http_host, args.http_port, token)
        Thread(target=stream_loop, args=(cam, frames, overlay, state, args.rotate, stop), daemon=True).start()
        log.info("Live view and status on http://%s:%d/stream.mjpg and /status.json (%s)", args.http_host,
                 args.http_port, "token required except from 127.0.0.1" if token else "no token: open to all")
    if args.detector_url and not args.dry_run:
        Thread(target=notify_agent_loop, daemon=True,
               args=(state, args.detector_url, os.environ.get("DETECTOR_TOKEN", ""), args.detector_heartbeat, stop)).start()
        log.info("Presence sensor %s reports to %s", args.device_code, args.detector_url)

    confirmer = Confirmer(args.window, args.min_votes)
    last_sent = last_report = time.time()
    analysed = seen = 0
    thermal = ThermalGuard(args.max_fps, args.hot_fps, args.hot_temp, args.pause_temp, args.resume_temp)
    next_frame = time.time()
    log.info("Reading camera %d (rotated %d°), max %g frames/s. Press Ctrl+C to stop.",
             args.camera, args.rotate, args.max_fps)

    try:
        while True:
            thermal.update()
            state.cpu_temp, state.thermal_state = thermal.temp, thermal.state
            state.paused = thermal.state == "paused"
            if time.time() - last_report >= 10:  # proof of life: is the AI running, is it seeing plates?
                fps = analysed / (time.time() - last_report)
                state.analysed_fps, state.updated_at = fps, time.time()
                temp = f"{thermal.temp:.1f}°C" if thermal.temp is not None else "unknown"
                log.info("Analysed %d frames (%.1f/s); a plate shape was detected in %d of them; CPU %s, %s; bay %s",
                         analysed, fps, seen, temp, thermal.state, "occupied" if state.occupied else "free")
                analysed = seen = 0
                last_report = time.time()

            if thermal.fps <= 0:  # too hot: no analysis, the live view keeps streaming
                time.sleep(1)
                next_frame = time.time()
                continue
            wait = next_frame - time.time()
            if wait > 0:
                time.sleep(wait)
            next_frame = max(next_frame + 1 / thermal.fps, time.time())

            frame = rotate_frame(cam.capture_array(), args.rotate)
            found = find_plates(alpr, frame)
            overlay.set(found, (frame.shape[1], frame.shape[0]), args.min_conf)
            analysed += 1
            seen += bool(found)

            confirmed = confirmer.update(usable_readings(found, args.min_conf))
            if confirmed is None:
                continue
            state.confirmed(*confirmed)
            if time.time() - last_sent < args.heartbeat:
                continue
            plate, conf = confirmed
            log.info("Plate %s (%.0f%%)", plate, conf * 100)
            last_sent = time.time()
            if args.dry_run or not args.server:
                continue
            try:
                r = session.post(f"{args.server}/api/detections", json={"plate": plate, "confidence": conf},
                                 headers={"X-API-Key": api_key}, timeout=3)
                if r.status_code == 401:
                    raise SystemExit("Web app rejected the API key")
                r.raise_for_status()
            except requests.RequestException as exc:
                log.warning("Could not reach the web app: %s", exc)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        cam.stop()


if __name__ == "__main__":
    main()
