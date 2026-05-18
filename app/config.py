from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


API_ID = int(_required("TG_API_ID"))
API_HASH = _required("TG_API_HASH")
PHONES: list[str] = [p.strip() for p in _required("TG_PHONES").split(",") if p.strip()]
SESSION_NAME = os.getenv("SESSION_NAME", "leodv")
BOT_USERNAME = os.getenv("BOT_USERNAME", "leomatchbot")


def session_path(phone: str) -> Path:
    digits = "".join(c for c in phone if c.isdigit())
    return ROOT / f"{SESSION_NAME}_{digits}"


DB_PATH = ROOT / "data.db"
LIMITS_PATH = ROOT / "account_limits.json"
MEDIA_DIR = ROOT / "media"
PENDING_DIR = MEDIA_DIR / "_pending"
STATIC_DIR = ROOT / "static"
PRIORITY_PATH = ROOT / "highest_priority.json"

MEDIA_DIR.mkdir(exist_ok=True)
PENDING_DIR.mkdir(exist_ok=True)
if not PRIORITY_PATH.exists():
    PRIORITY_PATH.write_text("[]")
