#!/usr/bin/env python3
"""Sync historical @leomatchbot chat into the local database.

Iterates the full message history for each account in TG_PHONES and inserts
any profile (media + description) that doesn't already exist in the DB.

Usage:
    python3 extend_db.py
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from telethon import TelegramClient
from telethon.tl.custom import Message

from app import db
from app.config import API_HASH, API_ID, BOT_USERNAME, MEDIA_DIR, PENDING_DIR, PHONES, session_path
from app.hashing import hash_video_first_frame, sha256_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("extend_db")

LONG_DESC_THRESHOLD = 60  # must match profile.py


def _is_photo(msg: Message) -> bool:
    return msg.photo is not None


def _is_video(msg: Message) -> bool:
    return (
        msg.video is not None
        or getattr(msg, "video_note", None) is not None
        or getattr(msg, "gif", None) is not None
    )


def _has_media(msg: Message) -> bool:
    return _is_photo(msg) or _is_video(msg)


def _extract_text(messages: list[Message]) -> str:
    for m in messages:
        text = (m.message or "").strip()
        if text:
            return text
    return ""


async def _download_unit(messages: list[Message], temp_dir: Path) -> list[tuple[Message, Path]]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[Message, Path]] = []
    for msg in messages:
        if not _has_media(msg):
            continue
        path = await msg.download_media(file=str(temp_dir) + "/")
        if path:
            out.append((msg, Path(path)))
    return out


def _compute_hash(first_msg: Message, first_path: Path) -> str:
    if _is_photo(first_msg):
        return sha256_file(first_path)
    return hash_video_first_frame(first_path)


async def _process_unit(messages: list[Message], stats: dict) -> None:
    if not any(_has_media(m) for m in messages):
        return

    description = _extract_text(messages)
    head_id = messages[0].id
    temp_dir = PENDING_DIR / f"extend_{head_id}"

    try:
        # Download first media only for hashing
        first_downloaded: list[tuple[Message, Path]] = []
        temp_dir.mkdir(parents=True, exist_ok=True)
        for msg in messages:
            if not _has_media(msg):
                continue
            path = await msg.download_media(file=str(temp_dir) + "/")
            if path:
                first_downloaded.append((msg, Path(path)))
                break  # only need the first one to compute hash

        if not first_downloaded:
            log.warning("  Skip msg_id=%s: no downloadable media", head_id)
            return

        first_msg, first_path = first_downloaded[0]
        try:
            media_hash = _compute_hash(first_msg, first_path)
        except RuntimeError as e:
            log.warning("  Skip msg_id=%s: cannot hash — %s", head_id, e)
            return

        # Check DB before downloading the rest
        if len(description) >= LONG_DESC_THRESHOLD:
            existing = db.find_profile_by_description(description)
        else:
            existing = db.find_profile(description, media_hash)

        if existing is not None:
            stats["skipped"] += 1
            return

        # New profile — download remaining media
        for msg in messages:
            if not _has_media(msg) or msg.id == first_msg.id:
                continue
            path = await msg.download_media(file=str(temp_dir) + "/")
            if path:
                first_downloaded.append((msg, Path(path)))

        profile_id = db.insert_profile(description, media_hash)
        target_dir = MEDIA_DIR / str(profile_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        for _, src in first_downloaded:
            shutil.move(str(src), str(target_dir / src.name))

        log.info("  + profile id=%s  desc=%r", profile_id, description[:70])
        stats["inserted"] += 1

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def process_account(phone: str) -> dict:
    sf = session_path(phone)
    stats: dict = {"phone": phone, "inserted": 0, "skipped": 0, "errors": 0}

    if not Path(str(sf) + ".session").exists():
        log.warning("No session file for %s — run login.py first", phone)
        stats["error"] = "no_session"
        return stats

    log.info("=== Account %s ===", phone)
    client = TelegramClient(str(sf), API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Account %s is not authorized — run login.py", phone)
            stats["error"] = "not_authorized"
            return stats

        # Fetch all incoming messages and group albums
        album_buffer: dict[int, list[Message]] = {}
        singles: list[list[Message]] = []

        log.info("Fetching history from @%s (this may take a while)...", BOT_USERNAME)
        fetched = 0
        async for msg in client.iter_messages(BOT_USERNAME, limit=None):
            fetched += 1
            if fetched % 500 == 0:
                log.info("  ... %d messages fetched", fetched)
            if msg.out:
                continue
            if msg.grouped_id:
                album_buffer.setdefault(msg.grouped_id, []).append(msg)
            else:
                singles.append([msg])

        albums = [sorted(msgs, key=lambda m: m.id) for msgs in album_buffer.values()]
        units = singles + albums
        log.info("Fetched %d incoming messages → %d units to process", fetched, len(units))

        for i, unit in enumerate(units, 1):
            if i % 200 == 0:
                log.info("  [%d/%d]  inserted=%d  skipped=%d",
                         i, len(units), stats["inserted"], stats["skipped"])
            try:
                await _process_unit(unit, stats)
            except Exception:
                log.exception("  Error on unit starting at msg_id=%s", unit[0].id)
                stats["errors"] += 1

    finally:
        await client.disconnect()

    log.info("Account %s done: inserted=%d  skipped=%d  errors=%d",
             phone, stats["inserted"], stats["skipped"], stats["errors"])
    return stats


async def main() -> None:
    db.init()
    before = db.count_profiles()
    log.info("Profiles in DB before sync: %d", before)

    all_stats = []
    for phone in PHONES:
        s = await process_account(phone)
        all_stats.append(s)

    log.info("=== Summary ===")
    total_inserted = total_skipped = 0
    for s in all_stats:
        log.info("  %s  inserted=%s  skipped=%s  errors=%s",
                 s["phone"], s.get("inserted", "—"), s.get("skipped", "—"), s.get("errors", "—"))
        total_inserted += s.get("inserted", 0)
        total_skipped += s.get("skipped", 0)

    after = db.count_profiles()
    log.info("Profiles before: %d  after: %d  (added %d)", before, after, after - before)


if __name__ == "__main__":
    asyncio.run(main())
