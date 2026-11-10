"""Crash-safe SQLite persistence layer (Sprint 1).

Uses WAL journal mode with NORMAL synchronous and foreign keys ON.
Session state is serialized as JSON in the ``sessions`` table.

Thread-safe: all DB access is serialized behind a re-entrant lock so the
store can be shared across threads (e.g. FastAPI TestClient running the app
in a worker thread while the test runs on the main thread).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Set

from .protocol import SessionState


class SessionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                delivered_revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                channel TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                url TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                vision_ok INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def get_or_create_session(self, session_id: str) -> SessionState:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                return SessionState.model_validate_json(row["state_json"])

            now = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
            state = SessionState(
                session_id=session_id,
                created_at=now,
                updated_at=now,
            )
            self.save_session(state)
            return state

    def save_session(self, state: SessionState) -> None:
        with self._lock:
            now = (
                datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
                if not state.updated_at
                else state.updated_at
            )
            state.updated_at = now
            payload = state.model_dump_json()
            self._conn.execute(
                """
                INSERT INTO sessions (session_id, state_json, revision, delivered_revision, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    revision = excluded.revision,
                    delivered_revision = excluded.delivered_revision,
                    updated_at = excluded.updated_at
                """,
                (
                    state.session_id,
                    payload,
                    state.revision,
                    state.delivered_revision,
                    state.updated_at,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Events / timeline
    # ------------------------------------------------------------------

    def append_event(
        self,
        session_id: str,
        channel: str,
        event_type: str,
        payload: Any,
        timestamp: Optional[str] = None,
    ) -> None:
        with self._lock:
            if timestamp is None:
                timestamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
            payload_json = payload if isinstance(payload, str) else json.dumps(payload)
            self._conn.execute(
                """
                INSERT INTO events (session_id, timestamp, channel, event_type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, timestamp, channel, event_type, payload_json),
            )
            self._conn.commit()

    def get_timeline(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, timestamp, channel, event_type, payload_json
                FROM events
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "channel": r["channel"],
                    "event_type": r["event_type"],
                    "payload": json.loads(r["payload_json"]),
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------

    def register_upload(
        self,
        session_id: str,
        filename: str,
        url: str,
        mime_type: str,
        size_bytes: int,
        vision_ok: bool,
        created_at: Optional[str] = None,
    ) -> int:
        with self._lock:
            if created_at is None:
                created_at = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
            cur = self._conn.execute(
                """
                INSERT INTO uploads (session_id, filename, url, mime_type, size_bytes, vision_ok, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, filename, url, mime_type, size_bytes, int(vision_ok), created_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_session_upload_urls(self, session_id: str) -> Set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM uploads WHERE session_id = ? AND vision_ok = 1",
                (session_id,),
            ).fetchall()
            return {r["url"] for r in rows}

    # ------------------------------------------------------------------
    # Revision delivery bookkeeping
    # ------------------------------------------------------------------

    def mark_revision_delivered(self, session_id: str, revision: int) -> None:
        with self._lock:
            state = self.get_or_create_session(session_id)
            state.delivered_revision = revision
            state.updated_at = datetime.now(
                timezone(timedelta(hours=5, minutes=30))
            ).isoformat()
            self.save_session(state)
