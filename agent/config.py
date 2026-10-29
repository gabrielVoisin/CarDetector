"""Local configuration of the Pi (/etc/parking-agent/config.yaml).

Only what the Pi cannot learn from the server lives here: server URL, its key, the I/O
hardware. The device list (sensors, cameras and the bay each one watches) is downloaded
from GET /api/v1/device/config at start-up and every 10 minutes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Lane:
    """An entry or exit lane: loop detector -> plate read by the camera -> barrier decision."""

    direction: str            # "in" | "out"
    camera: str               # device code, e.g. CAM-01
    loop_channel: int         # input channel of the vehicle loop detector
    barrier_channel: int      # output channel driving the barrier relay
    pulse_s: float = 1.0


@dataclass
class BleSettings:
    """Bluetooth identification of the drivers' phones (agent/ble_gatt.py)."""

    enabled: bool = False
    adapter: str = "hci0"


@dataclass
class DetectorSettings:
    """Car detectors connected over Ethernet, or simulated (agent/detectors.py, io_backend: network)."""

    listen: str = "0.0.0.0:8090"
    token: str | None = None           # X-Detector-Token expected from the detectors (recommended)
    timeout_s: float = 30.0            # a detector silent longer than this reads as faulty (0 = never)
    simulate_traffic: bool = False     # random arrivals / departures for demos


@dataclass
class Config:
    server_url: str
    controller_key: str
    ca_file: str | None = None                 # custom CA for a private server, else system store
    data_dir: Path = Path("/var/lib/parking-agent")
    io_backend: str = "simulator"              # simulator | gpio | mcp23017 | network (Ethernet detectors)
    gpio_pins: dict[int, int] = field(default_factory=dict)      # channel -> BCM pin (gpio backend)
    mcp23017: list[dict] = field(default_factory=list)           # [{bus: 1, address: 0x20}, ...] 16 channels each
    anpr_url: str | None = None                # local plate-recognition service (POST image -> {plate, confidence})
    sample_interval_s: float = 1.0
    debounce_s: float = 3.0                    # a bay must stay in the new state this long before it is reported
    disagree_fault_s: float = 600.0            # front and vertical sensors disagree this long -> sensor warning
    camera_check_interval_s: float = 30.0
    offline_warning_after_s: float = 60.0      # server unreachable this long -> NET-SRV warning (stored locally)
    lanes: list[Lane] = field(default_factory=list)
    ble: BleSettings = field(default_factory=BleSettings)
    detectors: DetectorSettings = field(default_factory=DetectorSettings)


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.getenv("PARKING_AGENT_CONFIG", "/etc/parking-agent/config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    key = os.getenv("PARKING_CONTROLLER_KEY") or raw.pop("controller_key", None)
    if not key or "." not in key:
        raise SystemExit("controller_key missing or malformed (expected '<prefix>.<secret>')")
    lanes = [Lane(**lane) for lane in raw.pop("lanes", []) or []]
    raw["ble"] = BleSettings(**(raw.get("ble") or {}))
    raw["detectors"] = DetectorSettings(**(raw.get("detectors") or {}))
    raw["gpio_pins"] = {int(k): int(v) for k, v in (raw.get("gpio_pins") or {}).items()}
    if "data_dir" in raw:
        raw["data_dir"] = Path(raw["data_dir"])
    return Config(controller_key=key, lanes=lanes, **raw)
