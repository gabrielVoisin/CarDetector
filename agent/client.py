"""HTTPS client for the central server (standard library only)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from agent import __version__


class ServerError(Exception):
    """Network failure or 5xx: retry later."""


class Rejected(Exception):
    """4xx: the request itself is wrong (bad key, invalid payload). Retrying will not help."""


class Client:
    def __init__(self, base_url: str, key: str, ca_file: str | None = None, timeout: float = 10.0):
        self.base = base_url.rstrip("/") + "/api/v1/device"
        self.key = key
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=ca_file) if base_url.startswith("https") else None

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-Controller-Key": self.key,
            "Content-Type": "application/json",
            "User-Agent": f"parking-agent/{__version__}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            if 400 <= exc.code < 500:
                raise Rejected(f"{exc.code} {detail}") from exc
            raise ServerError(f"{exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServerError(str(exc)) from exc

    def config(self) -> dict:
        return self._call("GET", "/config")

    def watchdog(self, body: dict) -> dict:
        return self._call("POST", "/watchdog", body, timeout=1.5)

    def heartbeat(self, body: dict) -> dict:
        return self._call("POST", "/heartbeat", body)

    def send(self, kind: str, items: list[dict], timeout: float | None = None) -> dict:
        return self._call("POST", f"/{kind}", {kind: items}, timeout)
