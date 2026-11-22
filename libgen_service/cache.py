"""A tiny SQLite-backed TTL cache.

The service is read-heavy and mirror calls are slow, so caching search results
and metadata keeps the UI responsive and avoids hammering Libgen mirrors.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class TTLCache:
    def __init__(self, path: Path | str | None, default_ttl: int = 600) -> None:
        self.default_ttl = max(0, default_ttl)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._conn = sqlite3.connect(
                    str(self._path), check_same_thread=False, timeout=10
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS cache ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)"
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn = None

    @property
    def persistent(self) -> bool:
        return self._conn is not None

    def get(self, key: str) -> Any | None:
        if self._conn is None:
            return None
        now = time.time()
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT value, expires FROM cache WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None:
                return None
            value, expires = row
            if expires and expires < now:
                try:
                    self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if self._conn is None:
            return
        ttl = self.default_ttl if ttl is None else ttl
        expires = time.time() + ttl if ttl > 0 else 0
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO cache (key, value, expires) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "expires=excluded.expires",
                    (key, payload, expires),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def delete(self, key: str) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._conn.commit()
            except sqlite3.Error:
                pass

    def prune(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM cache WHERE expires != 0 AND expires < ?",
                    (time.time(),),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
