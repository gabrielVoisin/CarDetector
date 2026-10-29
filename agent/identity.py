"""Bluetooth identification of drivers' phones by the cabinet ("smart bay"), without BlueZ.

This module holds everything that can be tested without a Bluetooth adapter: the challenges,
the framing of long messages, the verification of the phone's token and proof, and the
identification record sent to the server. The BlueZ GATT server (agent/ble_gatt.py) only moves
bytes between the phone and these functions.

Protocol (docs/BLUETOOTH.md):
  1. phone reads CHALLENGE -> {"v":1, "ctrl": "CTRL-01", "ch": "<16 random bytes, base64url>"}
  2. phone writes IDENTIFY, in frames of at most MTU-3 bytes:
         frame = [header byte][chunk]; header = index (0..127) | 0x80 on the last frame
     the reassembled message is JSON {"token": "...", "proof": "<base64 DER ECDSA>", "bay": "<uuid>|", "rssi": -60}
  3. cabinet checks: challenge issued to this phone, unused, < 60 s old; token signed by the server
     (Ed25519 public key from GET /device/config) and not expired; proof = SHA256withECDSA signature of
     "intelpark-ble-v1|<ctrl>|<ch>|<bay>" by the phone key "pk" written in the token.
  4. phone reads (or is notified on) RESULT -> {"v":1, "ch": "...", "ok": true, "status": "authorised", ...}
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL_VERSION = 1
PROOF_PREFIX = "intelpark-ble-v1"
CHALLENGE_TTL_S = 60.0
LAST_FRAME = 0x80
MAX_MESSAGE = 8 * 1024

# GATT UUIDs (same values in the Android app: ble/BleProtocol.kt)
SERVICE_UUID = "6e1f0001-5a2b-4c8e-9a7d-1f2e3d4c5b6a"
INFO_UUID = "6e1f0002-5a2b-4c8e-9a7d-1f2e3d4c5b6a"
CHALLENGE_UUID = "6e1f0003-5a2b-4c8e-9a7d-1f2e3d4c5b6a"
IDENTIFY_UUID = "6e1f0004-5a2b-4c8e-9a7d-1f2e3d4c5b6a"
RESULT_UUID = "6e1f0005-5a2b-4c8e-9a7d-1f2e3d4c5b6a"


class IdentifyError(ValueError):
    """The message code is sent back to the phone in RESULT.status."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def to_json_bytes(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode()


# --- Challenges ---------------------------------------------------------------------------
class Challenges:
    """One fresh challenge per phone (key = BlueZ device path), single use, 60 s."""

    def __init__(self, ttl_s: float = CHALLENGE_TTL_S, clock=time.monotonic):
        self.ttl_s = ttl_s
        self.clock = clock
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, device: str) -> str:
        challenge = b64u(os.urandom(16))
        with self._lock:
            now = self.clock()
            self._items = {k: v for k, v in self._items.items() if now - v[1] < self.ttl_s}  # forget old ones
            self._items[device] = (challenge, now)
        return challenge

    def take(self, device: str) -> str:
        with self._lock:
            item = self._items.pop(device, None)
        if item is None or self.clock() - item[1] > self.ttl_s:
            raise IdentifyError("bad_challenge")
        return item[0]

    def discard(self, device: str) -> None:
        with self._lock:
            self._items.pop(device, None)

    def peek(self, device: str) -> str | None:
        with self._lock:
            item = self._items.get(device)
        return item[0] if item else None


# --- Framing ------------------------------------------------------------------------------
def split_frames(message: bytes, mtu: int) -> list[bytes]:
    """What the phone does: cut a message into frames of at most mtu - 3 bytes (ATT header)."""
    size = max(1, mtu - 3 - 1)
    chunks = [message[i:i + size] for i in range(0, len(message), size)] or [b""]
    if len(chunks) > 128:
        raise ValueError("message too long for this MTU")
    return [bytes([i | (LAST_FRAME if i == len(chunks) - 1 else 0)]) + c for i, c in enumerate(chunks)]


class Reassembler:
    """Rebuilds the messages of several phones written at the same time, frame by frame."""

    def __init__(self):
        self._buffers: dict[str, tuple[int, bytearray]] = {}
        self._lock = threading.Lock()

    def feed(self, device: str, frame: bytes) -> bytes | None:
        """Return the full message when the last frame arrives, else None."""
        if not frame:
            raise IdentifyError("bad_frame")
        header, chunk = frame[0], frame[1:]
        index, last = header & 0x7F, bool(header & LAST_FRAME)
        with self._lock:
            if index == 0:
                self._buffers[device] = (0, bytearray())
            expected, buffer = self._buffers.get(device, (-1, bytearray()))
            if index != expected:
                self._buffers.pop(device, None)
                raise IdentifyError("bad_frame")
            buffer += chunk
            if len(buffer) > MAX_MESSAGE:
                self._buffers.pop(device, None)
                raise IdentifyError("bad_frame")
            if last:
                self._buffers.pop(device, None)
                return bytes(buffer)
            self._buffers[device] = (index + 1, buffer)
            return None

    def drop(self, device: str) -> None:
        with self._lock:
            self._buffers.pop(device, None)


# --- Verification ---------------------------------------------------------------------------
def verify_token(token: str, server_public_key_b64: str, now: float | None = None) -> dict:
    """Server Ed25519 signature over the ASCII payload, version, expiry. Same rules as the server."""
    try:
        encoded, sig = token.split(".")
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(server_public_key_b64))
        key.verify(b64u_decode(sig), encoded.encode("ascii"))
        payload = json.loads(b64u_decode(encoded))
    except (ValueError, InvalidSignature, UnicodeEncodeError) as exc:
        raise IdentifyError("invalid_token") from exc
    if payload.get("v") != PROTOCOL_VERSION or "cid" not in payload or "pk" not in payload:
        raise IdentifyError("invalid_token")
    if int(payload["exp"]) < int(now if now is not None else time.time()):
        raise IdentifyError("expired_token")
    return payload


def proof_message(controller_code: str, challenge: str, bay_id: str | None) -> bytes:
    return f"{PROOF_PREFIX}|{controller_code}|{challenge}|{bay_id or ''}".encode()


def verify_proof(phone_key_b64: str, message: bytes, proof_b64: str) -> None:
    try:
        key = serialization.load_der_public_key(base64.b64decode(phone_key_b64))
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            raise IdentifyError("bad_proof")
        key.verify(base64.b64decode(proof_b64), message, ec.ECDSA(hashes.SHA256()))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise IdentifyError("bad_proof") from exc


@dataclass
class Identified:
    challenge: str
    item: dict          # what is sent to POST /api/v1/device/identifications
    plates: list[str]


def identify(
    message: bytes,
    device: str,
    challenges: Challenges,
    controller_code: str,
    server_public_key_b64: str,
    known_bays: set[str],
) -> Identified:
    """Check one IDENTIFY message from a phone and build the identification record.

    Raises IdentifyError(code) with: bad_message, bad_challenge, invalid_token, expired_token, bad_proof.
    """
    try:
        data = json.loads(message)
        token, proof = str(data["token"]), str(data["proof"])
    except (ValueError, KeyError, TypeError) as exc:
        challenges.discard(device)  # a challenge is used once, even by a malformed message
        raise IdentifyError("bad_message") from exc
    challenge = challenges.take(device)
    payload = verify_token(token, server_public_key_b64)
    bay = str(data.get("bay") or "") or None
    verify_proof(payload["pk"], proof_message(controller_code, challenge, bay), proof)
    if bay is not None and bay not in known_bays:
        bay = None  # a bay of another cabinet: the server decides from the car detections instead
    rssi = data.get("rssi")
    vehicles = payload.get("veh") or []
    now = datetime.now(UTC)
    item = {
        "uid": f"ble-{controller_code}-{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:6]}",
        "customer_id": payload["cid"],
        "vehicle_id": vehicles[0]["id"] if len(vehicles) == 1 else None,
        "bay_id": bay,
        "rssi": int(rssi) if isinstance(rssi, (int, float)) and -127 <= rssi <= 20 else None,
        "identified_at": now.isoformat(),
    }
    return Identified(challenge=challenge, item=item, plates=[v.get("plate", "") for v in vehicles])


def result(challenge: str | None, status: str, ok: bool, **extra) -> dict:
    """RESULT characteristic value. The phone ignores results whose "ch" is not its own challenge."""
    return {"v": PROTOCOL_VERSION, "ch": challenge, "ok": ok, "status": status, **extra}
