from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from telethon.tl.custom import Message

LONG_DESC_THRESHOLD = 60


def _is_limit_message(text: str) -> bool:
    # Match without emoji to avoid variation-selector encoding mismatches
    return "Слишком много" in text and "за сегодня" in text

from . import db, tg
from .config import DB_PATH, MEDIA_DIR, PENDING_DIR, PRIORITY_PATH
from .hashing import hash_video_first_frame, sha256_file
from .state import state

log = logging.getLogger("leodv.profile")


def _is_priority_match(description: str) -> bool:
    try:
        entries = json.loads(PRIORITY_PATH.read_text())
        return any(entry in description for entry in entries)
    except Exception:
        return False


def _auto_letter_reply(age: int | None) -> str:
    if age is not None and age <= 19:
        return "привки, мне 19"
    return "привки, где учишься?"


async def _deferred_letter(description: str, custom_text: str, auto: bool) -> None:
    """💌 / 📹: отправить запрос, дождаться не-анкеты и ответить (кастомным или авто-текстом по возрасту)."""
    age = _parse_age(description)
    async with state.lock:
        state.letter_pending = True
        state.status_message = "Отправка 💌 / 📹…"
        state.current_profile = None
        state.warning = False
        state.priority_alert = False
    try:
        await tg.send_reaction("💌 / 📹")
        await asyncio.sleep(1.5)
        text = (custom_text or "").strip()
        if auto or not text:
            reply = _auto_letter_reply(age)
        else:
            reply = text
        await tg.send_reaction(reply)
        log.info("Letter: sent '💌 / 📹' + reply=%r (auto=%s age=%s)", reply, auto, age)
    except Exception:
        log.exception("Letter send failed")
        async with state.lock:
            state.letter_pending = False
            state.status_message = ""


async def _deferred_rotate() -> None:
    """Rotation runs as separate task to avoid CancelledError when disconnecting from inside event handler."""
    async with state.lock:
        try:
            log.info("=== Deferred rotation starting ===")
            switched = await tg.rotate_account()
            if switched:
                state.active_account_idx = tg.current_idx()
                new_phone = tg._phones[tg.current_idx()]
                log.info("Switched to account index=%s phone=%s, kicking off session", tg.current_idx(), new_phone)
                state.status_message = ""
                try:
                    await tg.send_reaction("1")
                    log.info("Sent kickoff '1' to bot on phone=%s", new_phone)
                except Exception:
                    log.exception("Kickoff send failed on phone=%s — waiting for bot to message us", new_phone)
                return
            log.warning("All accounts exhausted")
            backup = DB_PATH.with_name("data_backup.db")
            shutil.copy2(DB_PATH, backup)
            log.info("DB backed up to %s", backup)
            state.status_message = "Лимиты на всех аккаунтах исчерпаны — авто-скроллинг окончен до завтра"
            state.warning = False
        except Exception:
            log.exception("Deferred rotation failed unexpectedly")
            state.status_message = ""
            state.warning = True

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


def _purge_media(keep_pid: int | None) -> None:
    """Delete every profile dir in MEDIA_DIR except the current one (and _pending)."""
    if not MEDIA_DIR.exists():
        return
    keep = str(keep_pid) if keep_pid is not None else None
    for child in MEDIA_DIR.iterdir():
        if child.name == PENDING_DIR.name or child.name == keep:
            continue
        shutil.rmtree(child, ignore_errors=True)


def _publish_media(profile_id: int, downloaded: list[tuple[Message, Path]], temp_dir: Path) -> list[Path]:
    """Move freshly downloaded media into the serving dir and purge all other profiles' media."""
    target = MEDIA_DIR / str(profile_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for _, src in downloaded:
        dst = target / src.name
        shutil.move(str(src), str(dst))
        files.append(dst)
    shutil.rmtree(temp_dir, ignore_errors=True)
    _purge_media(keep_pid=profile_id)
    return files


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
    # Check for limit message first — bot may attach media to this message too
    text = _extract_text(messages)
    if _is_limit_message(text):
        log.info("Limit hit — blocking UI, manual account switch required")
        state.current_profile = None
        state.priority_alert = False
        state.letter_pending = False
        state.status_message = "Лимит исчерпан — смените аккаунт вручную (кнопка «Переключить аккаунт»)"
        state.warning = True
        return

    has_any_media = any(_has_media(m) for m in messages)
    if not has_any_media:
        if state.current_profile is not None:
            log.info("Non-profile message while a profile is displayed — ignoring (keep photo)")
            return
        if state.letter_pending:
            log.info("Non-profile during letter flow — ignoring, waiting for profiles")
            return
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

        try:
            media_hash = _compute_first_media_hash(downloaded)
        except RuntimeError as exc:
            log.warning("Cannot hash media, skipping profile with dislike: %s", exc)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.current_profile = None
            state.warning = False
            return
        if len(description) >= LONG_DESC_THRESHOLD:
            existing = db.find_profile_by_description(description)
        else:
            existing = db.find_profile(description, media_hash)

        if existing is None:
            profile_id = db.insert_profile(description, media_hash)
            seen_count = 1
        else:
            profile_id = int(existing["id"])
            seen_count = db.bump_seen(profile_id)

        if _is_priority_match(description):
            log.info("PRIORITY MATCH: profile id=%s desc=%r", profile_id, description[:60])
            files = _publish_media(profile_id, downloaded, temp_dir)
            state.current_profile = _profile_payload(profile_id, description, files, seen_count)
            state.priority_alert = True
            state.warning = False
            state.letter_pending = False
            state.status_message = ""
            return

        reason = _auto_dislike_reason(description, seen_count)
        if reason is not None:
            log.info("Auto-disliking profile id=%s seen=%s reason=%s", profile_id, seen_count, reason)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.auto_dislike_count += 1
            state.current_profile = None
            state.warning = False
            return

        if state.auto_like_mode:
            log.info("Auto-liking profile id=%s seen=%s", profile_id, seen_count)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("❤️")
            state.like_count += 1
            state.current_profile = None
            state.warning = False
            return

        files = _publish_media(profile_id, downloaded, temp_dir)
        state.current_profile = _profile_payload(profile_id, description, files, seen_count)
        state.warning = False
        state.letter_pending = False
        state.status_message = ""
        log.info("Profile id=%s seen_count=%s shown", profile_id, seen_count)
    except Exception:
        log.exception("Failed to process messages %s", [m.id for m in messages])
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
