"""Per-account daily limit tracker.

JSON structure:
{
  "created_at": "2024-01-15T10:30:00+00:00",   # when this file was (re)created
  "limits": {
    "+79991234567": "2024-01-15T10:30:00+00:00"  # when the limit was hit
  }
}

If the file is older than LIMIT_TTL_HOURS it is recreated (limits have reset).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("leodv.limits")

LIMIT_TTL_HOURS = 8

_path: Path | None = None


def init(path: Path) -> None:
    global _path
    _path = path
    _ensure_fresh()
    data = _load_raw()
    if data.get("limits"):
        log.info("Loaded limits: %s", list(data["limits"].keys()))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw() -> dict:
    if _path is None:
        return {}
    try:
        return json.loads(_path.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    if _path is None:
        return
    _path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _ensure_fresh() -> None:
    """Recreate file if it doesn't exist or is older than LIMIT_TTL_HOURS."""
    data = _load_raw()
    created_at = data.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_hours < LIMIT_TTL_HOURS:
                return
            log.info("Limits file is %.1f hours old — resetting", age_hours)
        except Exception:
            pass
    _save({"created_at": _now_iso(), "limits": {}})


def mark_limit(phone: str) -> None:
    """Record that phone has just hit its daily limit."""
    _ensure_fresh()
    data = _load_raw()
    data.setdefault("limits", {})[phone] = _now_iso()
    _save(data)
    log.info("Limit recorded for %s", phone)


def is_limited(phone: str) -> bool:
    """True if phone has a recorded limit in the current (non-expired) file."""
    _ensure_fresh()
    return phone in _load_raw().get("limits", {})


def all_limited(phones: list[str]) -> bool:
    """True if every phone in the list has a current limit recorded."""
    if not phones:
        return False
    _ensure_fresh()
    limited = set(_load_raw().get("limits", {}).keys())
    return all(p in limited for p in phones)
