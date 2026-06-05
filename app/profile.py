from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import time
from pathlib import Path

from telethon.tl.custom import Message

LONG_DESC_THRESHOLD = 60


def _is_limit_message(text: str) -> bool:
    # Match without emoji to avoid variation-selector encoding mismatches
    return "Слишком много" in text and "за сегодня" in text

from . import db, dedup, limits as acct_limits, tg
from .config import AUTO_SKIP_PATH, DB_PATH, MEDIA_DIR, PENDING_DIR, PRIORITY_PATH
from .hashing import hash_video_first_frame, sha256_file
from .state import state

log = logging.getLogger("leodv.profile")


def _is_priority_match(description: str) -> bool:
    try:
        entries = json.loads(PRIORITY_PATH.read_text())
        return any(entry in description for entry in entries)
    except Exception:
        return False


def _is_auto_skip(description: str) -> bool:
    try:
        entries = json.loads(AUTO_SKIP_PATH.read_text())
        return any(entry in description for entry in entries)
    except Exception:
        return False


async def _deferred_letter(text: str) -> None:
    """💌 / 📹: отправить запрос, дождаться не-анкеты и ответить ручным текстом.
    Если text пустой — только '💌 / 📹' уходит, без второго сообщения."""
    async with state.lock:
        state.letter_pending = True
        state.status_message = "Отправка 💌 / 📹…"
        state.current_profile = None
        state.warning = False
        state.priority_alert = False
    try:
        await tg.send_reaction("💌 / 📹")
        await asyncio.sleep(1.5)
        if text.strip():
            await tg.send_reaction(text)
            log.info("Letter: sent '💌 / 📹' + %r", text)
        else:
            log.info("Letter: sent '💌 / 📹' (no follow-up text)")
    except Exception:
        log.exception("Letter send failed")
        async with state.lock:
            state.letter_pending = False
            state.status_message = ""


async def _deferred_shutdown(msg: str) -> None:
    """Notify the bot and then send SIGINT to the process."""
    log.warning("Shutting down — all accounts limited")
    try:
        from tg_bot.bot import get_bot
        bot = get_bot()
        if bot:
            await bot.notify_shutdown(msg)
            await asyncio.sleep(1.5)
    except Exception:
        log.exception("Shutdown notification failed")
    os.kill(os.getpid(), signal.SIGINT)


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
            msg = "⛔️ Лимиты на всех аккаунтах — останавливаю программу"
            state.status_message = msg
            state.warning = False
            asyncio.create_task(_deferred_shutdown(msg))
        except Exception:
            log.exception("Deferred rotation failed unexpectedly")
            state.status_message = ""
            state.warning = True


async def _notify_bot_status(text: str) -> None:
    """Тонкий хелпер: пнуть TG-бот, если он есть. НЕ блокирует state.lock —
    отправка идёт в фоне через create_task. Используется ТОЛЬКО для авто-фильтра
    в _process (не трогает рротационный эндпоинт)."""
    try:
        from tg_bot.bot import get_bot
        bot = get_bot()
        if bot:
            asyncio.create_task(bot.notify_status(text))
    except Exception:
        log.exception("notify_bot_status failed")

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


def _compute_all_media_hashes(
    downloaded: list[tuple[Message, Path]],
) -> tuple[str, list[str]]:
    """Returns (first_hash_for_db, all_hashes_for_dedup_lookup).

    `first_hash_for_db` matches today's storage semantics (sha256 of first
    photo, or first-frame hash of first video).
    `all_hashes_for_dedup_lookup` lets us catch the case where the bot
    reordered photos — we compare every incoming photo against the stored
    first-photo hash of existing rows. Videos are not re-frame-hashed per
    item (one ffmpeg call per unit is enough).
    """
    photos = [(m, p) for m, p in downloaded if _is_photo(m)]
    videos = [(m, p) for m, p in downloaded if _is_video(m)]
    if photos:
        photo_hashes = [sha256_file(p) for _, p in photos]
        return photo_hashes[0], photo_hashes
    if not videos:
        raise RuntimeError("No photo or video media to hash")
    h = hash_video_first_frame(videos[0][1])
    return h, [h]


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
        state.last_message_at = time.monotonic()
        prev_pid = state.current_profile.get("id") if state.current_profile else None
        prev_status = state.status_message
        prev_warning = state.warning
        try:
            await _process(messages)
        finally:
            state.busy = False
    # Outside the lock — push to optional Telegram-bot UI without blocking _process.
    await _notify_tg_bot(prev_pid, prev_status, prev_warning)


async def _notify_tg_bot(prev_pid, prev_status: str, prev_warning: bool) -> None:
    try:
        from tg_bot.bot import get_bot
    except Exception:
        return
    bot = get_bot()
    if bot is None:
        return
    try:
        cp = state.current_profile
        if cp and cp.get("id") != prev_pid:
            await bot.notify_profile()
        if state.status_message and state.status_message != prev_status:
            await bot.notify_status(state.status_message)
        # Independent: any False→True warning transition gets its own alert.
        if state.warning and not prev_warning:
            await bot.notify_status(
                "⚠️ Проблема: бот прислал не-анкету или сломалась сессия. "
                "Открой @leomatchbot."
            )
    except Exception:
        log.exception("tg_bot notification failed")


async def _process(messages: list[Message]) -> None:
    # Check for limit message first — bot may attach media to this message too
    text = _extract_text(messages)
    if _is_limit_message(text):
        state.current_profile = None
        state.priority_alert = False
        state.letter_pending = False

        current_phone = tg._phones[tg._current_idx] if tg._phones else ""
        if current_phone:
            acct_limits.mark_limit(current_phone)

        if acct_limits.all_limited(tg._phones):
            log.warning("All accounts are limited — shutting down")
            msg = "⛔️ Лимиты на всех аккаунтах — останавливаю программу"
            state.status_message = msg
            state.warning = False
            asyncio.create_task(_deferred_shutdown(msg))
            return

        if state.auto_rotate_mode:
            log.info("Limit hit — auto-rotating (re-using manual switch endpoint)")
            state.status_message = "Лимит — авто-смена аккаунта…"
            state.warning = False
            old_phone = current_phone or "?"
            old_idx = tg._current_idx
            asyncio.create_task(_deferred_rotate())
            try:
                from tg_bot.bot import get_bot
                bot = get_bot()
                if bot:
                    asyncio.create_task(bot.notify_rotate_start(old_phone, old_idx))
            except Exception:
                log.exception("notify rotate start failed")
        else:
            log.info("Limit hit — blocking UI, manual account switch required")
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
            first_hash, all_hashes = _compute_all_media_hashes(downloaded)
        except RuntimeError as exc:
            log.warning("Cannot hash media, skipping profile with dislike: %s", exc)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.current_profile = None
            state.warning = False
            return

        from . import settings, stats
        active_phone = tg._phones[tg._current_idx] if tg._phones else ""

        dup_id = dedup.find_duplicate(description, all_hashes)
        if dup_id is None:
            profile_id = db.insert_profile(description, first_hash)
            dedup.register(profile_id, description, first_hash)
            stats.bump(active_phone, "new_profiles")
            seen_count = 1
        else:
            profile_id = dup_id
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

        if _is_auto_skip(description):
            log.info("Auto-skip match: profile id=%s desc=%r", profile_id, description[:60])
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.current_profile = None
            state.warning = False
            return

        reason = _auto_dislike_reason(description, seen_count)
        if reason is not None:
            log.info("Auto-disliking profile id=%s seen=%s reason=%s", profile_id, seen_count, reason)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.auto_dislike_count += 1
            stats.bump(active_phone, "auto_dislikes")
            state.current_profile = None
            state.warning = False
            settings.save()
            # сообщения в чат не шлём — авто-дизлайк бьёт пачками,
            # счётчики видно в "ℹ️ Статус"
            return

        if state.auto_like_mode:
            log.info("Auto-liking profile id=%s seen=%s", profile_id, seen_count)
            files = _publish_media(profile_id, downloaded, temp_dir)
            await tg.send_reaction("❤️")
            state.like_count += 1
            stats.bump(active_phone, "auto_likes")
            state.current_profile = None
            state.warning = False
            settings.save()
            profile_payload = _profile_payload(profile_id, description, files, seen_count)
            try:
                from tg_bot.bot import get_bot
                bot = get_bot()
                if bot:
                    asyncio.create_task(bot.notify_auto_like(profile_payload))
            except Exception:
                log.exception("notify auto like failed")
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
