from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    description      TEXT    NOT NULL,
    first_media_hash TEXT    NOT NULL,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    registered_at    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(description, first_media_hash)
);
"""

# 2026-06-04 00:01:00 UTC — проставляется существующим анкетам при миграции
_BACKFILL_TS = 1780531260


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        cols = {row[1] for row in c.execute("PRAGMA table_info(profiles)")}
        if "registered_at" not in cols:
            c.execute("ALTER TABLE profiles ADD COLUMN registered_at INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE profiles SET registered_at = ?", (_BACKFILL_TS,))
        else:
            c.execute("UPDATE profiles SET registered_at = ? WHERE registered_at = 0", (_BACKFILL_TS,))


def find_profile_by_description(description: str) -> Optional[sqlite3.Row]:
    with _conn() as c:
        cur = c.execute("SELECT * FROM profiles WHERE description = ?", (description,))
        return cur.fetchone()


def find_profile(description: str, media_hash: str) -> Optional[sqlite3.Row]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM profiles WHERE description = ? AND first_media_hash = ?",
            (description, media_hash),
        )
        return cur.fetchone()


def insert_profile(description: str, media_hash: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO profiles(description, first_media_hash, registered_at) VALUES (?, ?, ?)",
            (description, media_hash, int(time.time())),
        )
        return int(cur.lastrowid)


def count_profiles() -> int:
    with _conn() as c:
        cur = c.execute("SELECT COUNT(*) AS n FROM profiles")
        return int(cur.fetchone()["n"])


def bump_seen(profile_id: int) -> int:
    with _conn() as c:
        c.execute(
            "UPDATE profiles SET seen_count = seen_count + 1, last_seen_at = datetime('now') WHERE id = ?",
            (profile_id,),
        )
        cur = c.execute("SELECT seen_count FROM profiles WHERE id = ?", (profile_id,))
        row = cur.fetchone()
        return int(row["seen_count"])
