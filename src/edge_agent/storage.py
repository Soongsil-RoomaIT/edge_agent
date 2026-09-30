"""SQLite 로컬 저장소.

모든 측정값과 이벤트를 먼저 로컬에 저장하고, 클라우드 전송에 성공하면 synced=1 로 표시한다.
오프라인 동안 쌓인 데이터는 재연결 시 한꺼번에 전송한다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .devices import Reading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at REAL    NOT NULL,
    temperature REAL    NOT NULL,
    humidity    REAL    NOT NULL,
    co2         REAL    NOT NULL,
    pm25        REAL    NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_readings_synced ON readings (synced, id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL    NOT NULL,
    type        TEXT    NOT NULL,
    detail      TEXT    NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_synced ON events (synced, id);
"""


class LocalStore:
    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- readings -------------------------------------------------------

    def save_reading(self, r: Reading) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO readings (measured_at, temperature, humidity, co2, pm25)"
                " VALUES (?, ?, ?, ?, ?)",
                (r.measured_at, r.temperature, r.humidity, r.co2, r.pm25),
            )
        return cur.lastrowid

    def unsynced_readings(self, limit: int = 500) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM readings WHERE synced = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_readings_synced(self, ids: list[int]) -> None:
        self._mark_synced("readings", ids)

    # --- events ---------------------------------------------------------

    def log_event(self, type_: str, **detail: Any) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (occurred_at, type, detail) VALUES (?, ?, ?)",
                (time.time(), type_, json.dumps(detail, ensure_ascii=False)),
            )
        return cur.lastrowid

    def unsynced_events(self, limit: int = 500) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE synced = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_events_synced(self, ids: list[int]) -> None:
        self._mark_synced("events", ids)

    # --- maintenance ----------------------------------------------------

    def prune(self, retention_days: int) -> None:
        """전송이 끝난 오래된 데이터 삭제 (SD 카드 용량 관리)."""
        cutoff = time.time() - retention_days * 86400
        with self._conn:
            self._conn.execute(
                "DELETE FROM readings WHERE synced = 1 AND measured_at < ?", (cutoff,)
            )
            self._conn.execute(
                "DELETE FROM events WHERE synced = 1 AND occurred_at < ?", (cutoff,)
            )

    def _mark_synced(self, table: str, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        with self._conn:
            self._conn.execute(
                f"UPDATE {table} SET synced = 1 WHERE id IN ({placeholders})", ids
            )
