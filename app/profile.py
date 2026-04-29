from __future__ import annotations

import logging
import shutil
from pathlib import Path

from telethon.tl.custom import Message

LONG_DESC_THRESHOLD = 60

from . import db, tg
from .config import MEDIA_DIR, PENDING_DIR
from .hashing import hash_video_first_frame, sha256_file
from .state import state

log = logging.getLogger("leodv.profile")


def _is_photo(msg: Message) -> bool:
    return msg.photo is not None


def _is_video(msg: Message) -> bool:
    return msg.video is not None or getattr(msg, "video_note", None) is not None or getattr(msg, "gif", None) is not None


def _has_media(msg: Message) -> bool:
    return _is_photo(msg) or _is_video(msg)


def _parse_age(description: str) -> int | None:
    parts = description.split(",")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None


def _extract_text(messages: list[Message]) -> str:
    for m in messages:
        text = (m.message or "").strip()
        if text:
            return text
    return ""


def _profile_payload(profile_id: int, description: str, files: list[Path], seen_count: int) -> dict:
    return {
        "id": profile_id,
        "description": description,
        "media": [
            {
                "url": f"/media/{profile_id}/{p.name}",
                "kind": "video" if p.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"} else "photo",
                "name": p.name,
            }
            for p in sorted(files, key=lambda x: x.name)
        ],
        "seen_count": seen_count,
    }


async def _download_unit(messages: list[Message], temp_dir: Path) -> list[tuple[Message, Path]]:
    """Download all media in the unit, preserving order. Returns [(message, path), ...]."""
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    out: list[tuple[Message, Path]] = []
    for msg in messages:
        if not _has_media(msg):
            continue
        path = await tg.download_media(msg, temp_dir)
        if path is not None:
            out.append((msg, path))
    return out


def _compute_first_media_hash(downloaded: list[tuple[Message, Path]]) -> str:
    photos = [(m, p) for m, p in downloaded if _is_photo(m)]
    if photos:
        return sha256_file(photos[0][1])
    videos = [(m, p) for m, p in downloaded if _is_video(m)]
    if not videos:
        raise RuntimeError("No photo or video media to hash")
    return hash_video_first_frame(videos[0][1])


def _auto_dislike_reason(description: str, seen_count: int) -> str | None:
    if state.auto_dislike_mode:
        return "auto"
    if state.only_new_mode and seen_count > 1:
        return "duplicate"
    if state.age_filter_active:
        age = _parse_age(description)
        if age is None or not (state.age_min <= age <= state.age_max):
            return "age"
    return None


async def handle_messages(messages: list[Message]) -> None:
    """Pipeline entry point. Called for each unit (single message or album) from the bot."""
    async with state.lock:
        state.busy = True
        try:
            await _process(messages)
        finally:
            state.busy = False


async def _process(messages: list[Message]) -> None:
    has_any_media = any(_has_media(m) for m in messages)
    if not has_any_media:
        log.info("Bot sent a non-profile message; raising warning")
        state.current_profile = None
        state.warning = True
        return

    description = _extract_text(messages)
    head_id = messages[0].id
    temp_dir = PENDING_DIR / str(head_id)

    try:
        downloaded = await _download_unit(messages, temp_dir)
        if not downloaded:
            log.warning("No downloadable media for messages %s", [m.id for m in messages])
            state.current_profile = None
            state.warning = True
            return

        media_hash = _compute_first_media_hash(downloaded)
        if len(description) >= LONG_DESC_THRESHOLD:
            existing = db.find_profile_by_description(description)
        else:
            existing = db.find_profile(description, media_hash)

        if existing is None:
            profile_id = db.insert_profile(description, media_hash)
            target_dir = MEDIA_DIR / str(profile_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            files: list[Path] = []
            for _, src in downloaded:
                dst = target_dir / src.name
                shutil.move(str(src), str(dst))
                files.append(dst)
            shutil.rmtree(temp_dir, ignore_errors=True)
            seen_count = 1
        else:
            profile_id = int(existing["id"])
            seen_count = db.bump_seen(profile_id)
            shutil.rmtree(temp_dir, ignore_errors=True)
            existing_dir = MEDIA_DIR / str(profile_id)
            files = list(existing_dir.iterdir()) if existing_dir.exists() else []

        reason = _auto_dislike_reason(description, seen_count)
        if reason is not None:
            log.info("Auto-disliking profile id=%s seen=%s reason=%s", profile_id, seen_count, reason)
            await tg.send_reaction("👎")
            state.auto_dislike_count += 1
            state.current_profile = None
            state.warning = False
            return

        if state.auto_like_mode:
            log.info("Auto-liking profile id=%s seen=%s", profile_id, seen_count)
            await tg.send_reaction("❤️")
            state.current_profile = None
            state.warning = False
            return

        state.current_profile = _profile_payload(profile_id, description, files, seen_count)
        state.warning = False
        log.info("Profile id=%s seen_count=%s shown", profile_id, seen_count)
    except Exception:
        log.exception("Failed to process messages %s", [m.id for m in messages])
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
