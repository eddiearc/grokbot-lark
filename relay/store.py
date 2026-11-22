#!/usr/bin/env python3
"""SQLite state for reaction jobs and identity cache."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATH = Path(__file__).resolve().parent / "state.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs (
    message_id   TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    chat_type    TEXT,
    reaction_id  TEXT,
    emoji_type   TEXT,
    state        TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    error_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_chat ON jobs(chat_id, state);

CREATE TABLE IF NOT EXISTS identity_cache (
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

ACTIVE_STATES = ("pending", "error")


class Store:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def upsert_job(
        self,
        message_id: str,
        chat_id: str,
        chat_type: str | None = None,
        reaction_id: str | None = None,
        emoji_type: str | None = None,
        state: str = "pending",
        created_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        created = created_at if created_at is not None else ts
        self._conn.execute(
            """
            INSERT INTO jobs (message_id, chat_id, chat_type, reaction_id, emoji_type, state, created_at, updated_at, error_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'error' THEN ? ELSE NULL END)
            ON CONFLICT(message_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                chat_type=COALESCE(excluded.chat_type, jobs.chat_type),
                reaction_id=COALESCE(excluded.reaction_id, jobs.reaction_id),
                emoji_type=COALESCE(excluded.emoji_type, jobs.emoji_type),
                state=excluded.state,
                updated_at=excluded.updated_at,
                error_at=CASE WHEN excluded.state='error' THEN COALESCE(jobs.error_at, excluded.updated_at) ELSE jobs.error_at END
            """,
            (
                message_id,
                chat_id,
                chat_type,
                reaction_id,
                emoji_type,
                state,
                created,
                ts,
                state,
                ts,
            ),
        )
        self._conn.commit()
        return self.get_job(message_id) or {}

    def update_job(self, message_id: str, now: float | None = None, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_job(message_id)
        ts = time.time() if now is None else now
        allowed = {"chat_id", "chat_type", "reaction_id", "emoji_type", "state", "created_at", "error_at"}
        sets = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        if "state" in fields and fields["state"] == "error" and "error_at" not in fields:
            sets.append("error_at=COALESCE(error_at, ?)")
            vals.append(ts)
        sets.append("updated_at=?")
        vals.append(ts)
        vals.append(message_id)
        self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE message_id=?", vals)
        self._conn.commit()
        return self.get_job(message_id)

    def get_job(self, message_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def active_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state IN ('pending','error') ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def active_in_chat(self, chat_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE chat_id=? AND state IN ('pending','error') ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def cache_get(self, kind: str, key: str, now: float | None = None) -> dict[str, Any] | None:
        ts = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT payload_json, expires_at FROM identity_cache WHERE kind=? AND key=?",
            (kind, key),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < ts:
            return None
        import json

        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def cache_put(
        self,
        kind: str,
        key: str,
        payload: dict[str, Any],
        ttl: float,
        now: float | None = None,
    ) -> None:
        import json

        ts = time.time() if now is None else now
        self._conn.execute(
            """
            INSERT INTO identity_cache (kind, key, payload_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET
                payload_json=excluded.payload_json,
                fetched_at=excluded.fetched_at,
                expires_at=excluded.expires_at
            """,
            (kind, key, json.dumps(payload, ensure_ascii=False), ts, ts + ttl),
        )
        self._conn.commit()
