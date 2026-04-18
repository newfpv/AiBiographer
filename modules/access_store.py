from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class AccessStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_keys (
                    user_id INTEGER PRIMARY KEY,
                    api_key TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def upsert_key(self, user_id: int, api_key: str, is_active: bool = True) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_keys (user_id, api_key, is_active, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    api_key=excluded.api_key,
                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """,
                (user_id, api_key, 1 if is_active else 0, int(time.time())),
            )

    def revoke_key(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE user_keys SET is_active=0, updated_at=? WHERE user_id=?", (int(time.time()), user_id))

    def get_active_key(self, user_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT api_key FROM user_keys WHERE user_id=? AND is_active=1",
                (user_id,),
            ).fetchone()
        return row[0] if row else None

    def list_active_keys(self) -> list[tuple[int, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, api_key FROM user_keys WHERE is_active=1 ORDER BY updated_at DESC"
            ).fetchall()
        return [(int(uid), key) for uid, key in rows]
