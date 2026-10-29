"""Bluetooth identification and Ethernet detectors, without Bluetooth hardware (python -m unittest)."""

from __future__ import annotations

import base64
import json
import tempfile
import time
import types
import unittest
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent import identity
from agent.client import ServerError
from agent.config import Config
from agent.detectors import DetectorServer, NetworkIO
from agent.io_backends import channel_of
from agent.main import Agent, BayWatch

BAY = str(uuid.uuid4())


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Server:
    """Signs tokens exactly like backend/app/services/ble_identity.py."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_b64 = b64(self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def token(self, phone_pk: str, exp_in: int = 3600, cid: str | None = None) -> str:
        payload = {"v": 1, "cid": cid or str(uuid.uuid4()), "veh": [{"id": str(uuid.uuid4()), "plate": "GE215908"}],
                   "pk": phone_pk, "iat": int(time.time()), "exp": int(time.time()) + exp_in}
        encoded = identity.b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return f"{encoded}.{identity.b64u(self.key.sign(encoded.encode()))}"


class Phone:
    """What the Android app does (Keystore EC P-256 key, SHA256withECDSA)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pk = b64(self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    def message(self, token: str, ctrl: str, challenge: str, bay: str | None = None) -> bytes:
        proof = self.key.sign(identity.proof_message(ctrl, challenge, bay), ec.ECDSA(hashes.SHA256()))
        return identity.to_json_bytes({"token": token, "proof": b64(proof), "bay": bay or "", "rssi": -58})


class ChallengeTest(unittest.TestCase):
    def test_single_use_and_expiry(self):
        now = [0.0]
        c = identity.Challenges(ttl_s=60, clock=lambda: now[0])
        ch = c.issue("phone-a")
        self.assertEqual(c.take("phone-a"), ch)
        with self.assertRaises(identity.IdentifyError):
            c.take("phone-a")  # used once
        c.issue("phone-a")
        now[0] = 61
        with self.assertRaises(identity.IdentifyError):
            c.take("phone-a")  # expired
        self.assertNotEqual(c.issue("phone-b"), c.issue("phone-c"))


class FramingTest(unittest.TestCase):
    def test_round_trip_small_and_large_mtu(self):
        message = bytes(range(256)) * 4
        for mtu in (23, 185, 247, 517):
            r = identity.Reassembler()
            frames = identity.split_frames(message, mtu)
            self.assertTrue(all(len(f) <= mtu - 3 for f in frames))
            out = [r.feed("dev", f) for f in frames]
            self.assertEqual(out[-1], message)
            self.assertTrue(all(o is None for o in out[:-1]))

    def test_two_phones_at_once(self):
        r = identity.Reassembler()
        a, b = identity.split_frames(b"A" * 100, 23), identity.split_frames(b"B" * 100, 23)
        results = {}
        for fa, fb in zip(a, b, strict=True):
            results["a"] = r.feed("a", fa) or results.get("a")
            results["b"] = r.feed("b", fb) or results.get("b")
        self.assertEqual(results, {"a": b"A" * 100, "b": b"B" * 100})

    def test_missing_frame_is_refused(self):
        r = identity.Reassembler()
        frames = identity.split_frames(b"x" * 100, 23)
        r.feed("dev", frames[0])
        with self.assertRaises(identity.IdentifyError):
            r.feed("dev", frames[2])


class IdentifyTest(unittest.TestCase):
    def setUp(self):
        self.server, self.phone, self.challenges = Server(), Phone(), identity.Challenges()

    def run_identify(self, message_fn, bays=frozenset({BAY})):
        ch = self.challenges.issue("dev")
        return identity.identify(message_fn(ch), "dev", self.challenges, "CTRL-01", self.server.public_b64, set(bays))

    def test_valid_phone(self):
        token = self.server.token(self.phone.pk, cid="c-1")
        found = self.run_identify(lambda ch: self.phone.message(token, "CTRL-01", ch, BAY))
        self.assertEqual(found.item["customer_id"], "c-1")
        self.assertEqual(found.item["bay_id"], BAY)
        self.assertEqual(found.item["rssi"], -58)
        self.assertEqual(found.plates, ["GE215908"])

    def test_unknown_bay_is_dropped_not_refused(self):
        token = self.server.token(self.phone.pk)
        other = str(uuid.uuid4())
        found = self.run_identify(lambda ch: self.phone.message(token, "CTRL-01", ch, other))
        self.assertIsNone(found.item["bay_id"])

    def assert_refused(self, code, message_fn):
        with self.assertRaises(identity.IdentifyError) as ctx:
            self.run_identify(message_fn)
        self.assertEqual(str(ctx.exception), code)

    def test_refusals(self):
        token = self.server.token(self.phone.pk)
        self.assert_refused("invalid_token", lambda ch: self.phone.message(Server().token(self.phone.pk), "CTRL-01", ch))
        self.assert_refused("expired_token", lambda ch: self.phone.message(self.server.token(self.phone.pk, exp_in=-5), "CTRL-01", ch))
        # token copied from this phone, used by another phone: its proof does not match the key in the token
        self.assert_refused("bad_proof", lambda ch: Phone().message(token, "CTRL-01", ch))
        # proof made for another cabinet, or for an old challenge (replay)
        self.assert_refused("bad_proof", lambda ch: self.phone.message(token, "CTRL-02", ch))
        self.assert_refused("bad_proof", lambda ch: self.phone.message(token, "CTRL-01", "old-challenge"))
        self.assert_refused("bad_message", lambda ch: b"not json")

    def test_challenge_is_needed(self):
        token = self.server.token(self.phone.pk)
        message = self.phone.message(token, "CTRL-01", "never-issued")
        with self.assertRaises(identity.IdentifyError) as ctx:
            identity.identify(message, "dev", self.challenges, "CTRL-01", self.server.public_b64, set())
        self.assertEqual(str(ctx.exception), "bad_challenge")


class StubClient:
    def __init__(self, online: bool):
        self.online = online
        self.sent = []

    def send(self, kind, items, timeout=None):
        if not self.online:
            raise ServerError("network down")
        self.sent.append((kind, items))
        return {"decisions": [{"uid": items[0]["uid"], "status": "authorised", "bay_id": BAY, "bay_label": "P1 · EXT · 05"}]}


class AgentBleTest(unittest.TestCase):
    def make_agent(self, online: bool) -> Agent:
        tmp = Path(tempfile.mkdtemp())
        agent = Agent(Config(server_url="http://unused", controller_key="ctrl0001.x", data_dir=tmp), io=NetworkIO(),
                      client=StubClient(online))
        server = Server()
        agent.state.ble_key, agent.state.ble_enabled, agent.state.controller_code = server.public_b64, True, "CTRL-01"
        agent.state.bays[BAY] = BayWatch("05")
        agent.ble = types.SimpleNamespace(challenges=identity.Challenges())
        self.server = server
        return agent

    def identify_with(self, agent: Agent) -> dict:
        phone = Phone()
        ch = agent.ble.challenges.issue("/org/bluez/hci0/dev_AA")
        return agent.handle_ble("/org/bluez/hci0/dev_AA", phone.message(self.server.token(phone.pk), "CTRL-01", ch, BAY))

    def test_online_decision_from_server(self):
        agent = self.make_agent(online=True)
        result = self.identify_with(agent)
        self.assertEqual((result["ok"], result["status"], result["bay"]), (True, "authorised", "P1 · EXT · 05"))
        self.assertEqual(agent.client.sent[0][0], "identifications")
        self.assertEqual(agent.outbox.size(), 0)

    def test_offline_identification_is_queued(self):
        agent = self.make_agent(online=False)
        result = self.identify_with(agent)
        self.assertEqual((result["ok"], result["status"], result["online"]), (True, "pending", False))
        queued = agent.outbox.peek("identifications", 10)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][1]["bay_id"], BAY)


class DetectorTest(unittest.TestCase):
    def test_push_simulate_and_timeout(self):
        now = [100.0]
        io = NetworkIO(timeout_s=30, clock=lambda: now[0])
        devices = {
            "CPF-EXT-05": {"kind": "front_presence_sensor", "address": "gpio:9", "zone_code": "EXT", "bay_number": "05"},
            "CPV-EXT-05": {"kind": "vertical_presence_sensor", "address": "gpio:10", "zone_code": "EXT", "bay_number": "05"},
        }
        server = DetectorServer(io, "127.0.0.1:0", "secret", lambda: devices, channel_of)
        server.start()
        port = server.httpd.server_address[1]

        def post(path, body, token="secret"):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Detector-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=5) as res:
                    return res.status, json.loads(res.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        try:
            self.assertIsNone(io.read(10))                                    # never heard: unreadable
            self.assertEqual(post("/simulate/bay/EXT/5", {"occupied": True})[0], 200)
            self.assertEqual((io.read(9), io.read(10)), (True, True))         # both sensors of bay 05
            self.assertEqual(post("/detectors/CPV-EXT-05", {"occupied": False})[0], 200)
            self.assertFalse(io.read(10))
            self.assertEqual(post("/detectors/CPV-EXT-05", {"occupied": True}, token="wrong")[0], 401)
            self.assertEqual(post("/detectors/NOPE", {"occupied": True})[0], 404)
            now[0] += 31
            self.assertIsNone(io.read(9))                                     # silent detector -> faulty
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
