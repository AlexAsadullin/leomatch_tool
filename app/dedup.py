"""In-memory multi-signal dedup index.

Two indexes built once at startup from the existing `profiles` table:
  - canonical description → profile_id
  - first_media_hash       → profile_id

A unit is considered a duplicate if EITHER:
  S1) its canonical description matches a stored row, OR
  S2) any photo hash from the incoming unit matches the stored first_media_hash
      of some existing row.

No schema changes; existing rows are read as-is. New inserts call `register()`.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from typing import Iterable

from .config import DB_PATH

# Zero-width / variation-selector codepoints that don't change visible text
# but flip SHA256 of the description: ZWSP, ZWNJ, ZWJ, VS16, VS15.
_INVISIBLES = "​‌‍️︎"

# Module-level indexes; populated by init_indexes() once at app startup.
_by_desc: dict[str, int] = {}
_by_hash: dict[str, int] = {}


def canonicalize(text: str) -> str:
    """NFKC + casefold + drop zero-width chars + collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = text.translate(str.maketrans("", "", _INVISIBLES))
    return " ".join(text.split())


def init_indexes() -> None:
    """Load every (id, description, first_media_hash) row and build the indexes.

    Idempotent — safe to call again (e.g., in tests). If two rows collide on
    a key (existing dups in the DB), we keep the first one seen — bump_seen
    works on any of them, so the choice doesn't matter.
    """
    _by_desc.clear()
    _by_hash.clear()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT id, description, first_media_hash FROM profiles")
        for pid, desc, h in cur:
            _by_desc.setdefault(canonicalize(desc), pid)
            _by_hash.setdefault(h, pid)


def find_duplicate(description: str, hashes: Iterable[str]) -> int | None:
    """Return the profile_id of a stored row this unit duplicates, or None."""
    pid = _by_desc.get(canonicalize(description))
    if pid is not None:
        return pid
    for h in hashes:
        pid = _by_hash.get(h)
        if pid is not None:
            return pid
    return None


def register(profile_id: int, description: str, first_hash: str) -> None:
    """Update the indexes after a fresh INSERT into profiles."""
    _by_desc.setdefault(canonicalize(description), profile_id)
    _by_hash.setdefault(first_hash, profile_id)


def stats() -> dict[str, int]:
    """Index sizes — useful for one-liner sanity checks."""
    return {"by_desc": len(_by_desc), "by_hash": len(_by_hash)}
