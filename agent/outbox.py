"""Durable queue (SQLite) so nothing is lost while the server or the network is down.

Every item carries its own uid; the server ignores duplicates, so resending is safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

KINDS = ("readings", "events", "passages", "identifications")


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, uid TEXT NOT NULL, "
            "payload TEXT NOT NULL, UNIQUE (kind, uid) ON CONFLICT REPLACE)"
        )
        self._lock = threading.Lock()

    def put(self, kind: str, item: dict) -> None:
        assert kind in KINDS
        with self._lock:
            self._db.execute("INSERT INTO outbox (kind, uid, payload) VALUES (?, ?, ?)", (kind, item["uid"], json.dumps(item)))

    def peek(self, kind: str, limit: int) -> list[tuple[int, dict]]:
        with self._lock:
            rows = self._db.execute("SELECT id, payload FROM outbox WHERE kind = ? ORDER BY id LIMIT ?", (kind, limit)).fetchall()
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def ack(self, ids: list[int]) -> None:
        if ids:
            with self._lock:
                self._db.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids)

    def size(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM outbox").fetchone()[0]
