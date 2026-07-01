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

from . import db, dedup, limits as acct_limits, tg
from .config import ARCHIVE_DIR, AUTO_SKIP_PATH, DB_PATH, MEDIA_DIR, NON_PROFILES_PATH, PENDING_DIR, PRIORITY_PATH
from .hashing import hash_video_first_frame, sha256_file
from .state import state

log = logging.getLogger("leodv.profile")


def _is_priority_match(description: str) -> bool:
    try:
        entries = json.loads(PRIORITY_PATH.read_text())
        return any(entry.lower() in description.lower() for entry in entries)
    except Exception:
        return False


def _is_auto_skip(description: str) -> bool:
    try:
        entries = json.loads(AUTO_SKIP_PATH.read_text())
        return any(entry.lower() in description.lower() for entry in entries)
    except Exception:
        return False


def _non_profile_reply(messages: list[Message]) -> str | None:
    """Check all non-profile messages (newest first by date) against non_profiles.json keys.
    Returns the reply string for the first matching key, or None."""
    try:
        mapping: dict[str, str] = json.loads(NON_PROFILES_PATH.read_text())
        if not mapping:
            return None
        for msg in sorted(messages, key=lambda m: m.date, reverse=True):
            text_lower = (msg.message or "").lower()
            for key, reply in mapping.items():
                if key.lower() in text_lower:
                    return reply
        return None
    except Exception:
        return None


async def _deferred_letter(text: str) -> None:
    """💌 / 📹: отправить запрос, дождаться не-анкеты и ответить ручным текстом.
    Если text пустой — только '💌 / 📹' уходит, без второго сообщения."""
    async with state.lock:
        state.letter_pending = True
        state.status_message = "Отправка 💌 / 📹…"
        state.current_profile = None
        state.warning = False
        state.priority_alert = False
    if tg.secondary_idx() is not None:
        await tg.stop_secondary()
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


async def _notify_all_limited(msg: str) -> None:
    """Notify admins that all accounts hit their limits. App keeps running for gallery/ratings."""
    log.warning("All accounts limited — app stays running for gallery and ratings")
    try:
        from tg_bot.bot import get_bot
        bot = get_bot()
        if bot:
            await bot.notify_shutdown(msg)
    except Exception:
        log.exception("All-limited notification failed")


async def _do_rotate_on_non_profile() -> None:
    """Core non-profile rotation logic. Must be called while state.lock is held."""
    current_phone = tg._phones[tg._current_idx] if tg._phones else ""
    if current_phone:
        state.non_profile_phones.add(current_phone)
        log.info("Marked phone=%s as non-profile (total waiting: %s)", current_phone, state.non_profile_phones)

    log.info("=== Checking other accounts for available profile (non-profile trigger) ===")
    rotated = await tg.find_and_rotate_to_profile_account(skip_phones=frozenset(state.non_profile_phones))
    if rotated:
        state.active_account_idx = tg.current_idx()
        new_phone = tg._phones[tg.current_idx()]
        state.non_profile_phones.discard(new_phone)
        log.info("Rotated to account index=%s phone=%s", tg.current_idx(), new_phone)
        state.status_message = ""
        state.warning = False
        try:
            await tg.send_reaction("1")
            log.info("Sent kickoff '1' to bot on phone=%s", new_phone)
        except Exception:
            log.exception("Kickoff send failed on phone=%s", new_phone)
        return
    log.warning("No non-waiting account has a profile — will retry in 60s")
    state.status_message = "Нет анкет ни на одном аккаунте — жду действий пользователя"
    state.warning = True
    try:
        from tg_bot.bot import get_bot
        bot = get_bot()
        if bot:
            asyncio.create_task(bot.notify_status(state.status_message))
    except Exception:
        log.exception("notify failed after non-profile rotation check")


async def _deferred_rotate_on_non_profile() -> None:
    """Check other accounts for an available profile; rotate to the first that has one."""
    async with state.lock:
        try:
            await _do_rotate_on_non_profile()
        except Exception:
            log.exception("Non-profile account rotation failed")
            state.warning = True
            state.status_message = ""


async def _deferred_wait_after_non_profile_reply() -> None:
    """After replying to a non-profile message, wait 3s for a profile to arrive.
    If none arrived, trigger account rotation as usual."""
    await asyncio.sleep(3)
    async with state.lock:
        if state.current_profile is not None or state.warning:
            # Profile arrived during the wait, or rotation already triggered elsewhere
            return
        log.info("No profile arrived after non-profile reply — triggering rotation")
        try:
            if state.auto_rotate_mode and len(tg._phones) > 1:
                await _do_rotate_on_non_profile()
            else:
                state.warning = True
        except Exception:
            log.exception("Deferred wait after non-profile reply failed")
            state.warning = True


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
            log.warning("All accounts exhausted — staying alive for gallery/ratings")
            backup = DB_PATH.with_name("data_backup.db")
            shutil.copy2(DB_PATH, backup)
            log.info("DB backed up to %s", backup)
            msg = "⛔️ Лимиты на всех аккаунтах — жду сброса. Галерея и оценки работают"
            state.status_message = msg
            state.warning = True
            asyncio.create_task(_notify_all_limited(msg))
        except Exception:
            log.exception("Deferred rotation failed unexpectedly")
            state.status_message = ""
            state.warning = True


def _soft_limit_hit(phone: str) -> bool:
    from . import stats
    return (
        stats.get_field(phone, "auto_dislikes") >= state.auto_dislike_soft_limit
        or stats.get_field(phone, "auto_likes") >= state.auto_like_soft_limit
    )


def _trigger_soft_limit(phone: str) -> None:
    from . import stats
    d = stats.get_field(phone, "auto_dislikes")
    lk = stats.get_field(phone, "auto_likes")
    log.info("Soft limit reached for %s (auto_dislikes=%s, auto_likes=%s)", phone, d, lk)
    acct_limits.mark_limit(phone)

    if acct_limits.all_limited(tg._phones):
        log.warning("All accounts soft-limited — staying alive for gallery/ratings")
        msg = "⛔️ Лимиты на всех аккаунтах — жду сброса. Галерея и оценки работают"
        state.status_message = msg
        state.warning = True
        asyncio.create_task(_notify_all_limited(msg))
        return

    if state.auto_rotate_mode:
        log.info("Soft limit — auto-rotating account")
        state.status_message = "Лимит авто-действий — авто-смена аккаунта…"
        state.warning = False
        old_idx = tg._current_idx
        asyncio.create_task(_deferred_rotate())
        try:
            from tg_bot.bot import get_bot
            bot = get_bot()
            if bot:
                asyncio.create_task(bot.notify_rotate_start(phone, old_idx))
        except Exception:
            log.exception("notify rotate start failed")
    else:
        state.status_message = "Лимит авто-действий — смените аккаунт вручную"
        state.warning = True


async def _handle_secondary_messages(messages: list[Message]) -> None:
    """Auto-process profiles on secondary account while primary holds a priority profile.
    Never updates state.current_profile — UI stays on priority."""
    if not messages:
        return

    from . import db, dedup, limits as acct_limits, settings, stats

    sec_idx = tg.secondary_idx()
    secondary_phone = tg._phones[sec_idx] if sec_idx is not None else ""

    text = _extract_text(messages)

    if _is_limit_message(text):
        log.info("Secondary: limit hit on phone=%s", secondary_phone)
        if secondary_phone:
            acct_limits.mark_limit(secondary_phone)
        await tg.stop_secondary()
        return

    has_media = any(_has_media(m) for m in messages)
    if not has_media:
        log.info("Secondary: non-profile message — stopping background processing")
        await tg.stop_secondary()
        return

    description = _extract_text(messages)

    if _is_auto_skip(description):
        try:
            await tg.secondary_send("👎")
        except Exception:
            log.exception("Secondary: failed to send auto-skip")
        return

    if _is_priority_match(description):
        log.info("Secondary: priority match — stopping background processing")
        await tg.stop_secondary()
        return

    head_id = messages[0].id
    temp_dir = PENDING_DIR / f"sec_{head_id}"
    try:
        downloaded = await _download_unit(messages, temp_dir)
        if not downloaded:
            log.warning("Secondary: no downloadable media — skipping")
            await tg.secondary_send("👎")
            return

        try:
            first_hash, all_hashes = _compute_all_media_hashes(downloaded)
        except RuntimeError as exc:
            log.warning("Secondary: cannot hash media: %s", exc)
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.secondary_send("👎")
            return

        dup_id = dedup.find_duplicate(description, all_hashes)
        if dup_id is None:
            profile_id = db.insert_profile(description, first_hash)
            dedup.register(profile_id, description, first_hash)
            stats.bump(secondary_phone, "new_profiles")
            seen_count = 1
        else:
            profile_id = dup_id
            seen_count = db.bump_seen(profile_id)

        _archive_media(profile_id, downloaded)
        shutil.rmtree(temp_dir, ignore_errors=True)

        reason = _auto_dislike_reason(description, seen_count)
        if reason is not None:
            await tg.secondary_send("👎")
            stats.bump(secondary_phone, "auto_dislikes")
            settings.save()
            if secondary_phone and _soft_limit_hit(secondary_phone):
                log.info("Secondary: soft limit on phone=%s — stopping", secondary_phone)
                acct_limits.mark_limit(secondary_phone)
                await tg.stop_secondary()
            return

        if state.auto_like_mode:
            await tg.secondary_send("❤️")
            stats.bump(secondary_phone, "auto_likes")
            settings.save()
            if secondary_phone and _soft_limit_hit(secondary_phone):
                log.info("Secondary: soft limit on phone=%s — stopping", secondary_phone)
                acct_limits.mark_limit(secondary_phone)
                await tg.stop_secondary()
            return

        # No auto action configured — dislike to keep feed moving on secondary
        await tg.secondary_send("👎")

    except Exception:
        log.exception("Secondary processing error for messages %s", [m.id for m in messages])
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _start_secondary_processing() -> None:
    """Find a non-limited account and start background auto-processing on it."""
    await tg.stop_secondary()

    from . import limits as acct_limits
    primary_idx = tg.current_idx()

    for offset in range(1, len(tg._phones)):
        idx = (primary_idx + offset) % len(tg._clients)
        if idx >= len(tg._phones):
            continue
        phone = tg._phones[idx]
        if acct_limits.is_limited(phone):
            log.info("Secondary candidate idx=%s limited, skipping", idx)
            continue
        started = await tg.start_secondary(idx, _handle_secondary_messages)
        if started:
            try:
                await tg.secondary_send("1")
                log.info("Secondary kicked off on idx=%s", idx)
            except Exception:
                log.exception("Failed to kick off secondary on idx=%s", idx)
                await tg.stop_secondary()
            return

    log.info("No secondary account available for background processing")


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
    """Delete every profile dir in MEDIA_DIR except the current one, _pending, and _archive."""
    if not MEDIA_DIR.exists():
        return
    keep = str(keep_pid) if keep_pid is not None else None
    for child in MEDIA_DIR.iterdir():
        if child.name in (PENDING_DIR.name, ARCHIVE_DIR.name) or child.name == keep:
            continue
        shutil.rmtree(child, ignore_errors=True)


def _archive_media(profile_id: int, downloaded: list[tuple]) -> None:
    """Copy all downloaded media to the permanent archive (idempotent — skips existing files)."""
    dest_dir = ARCHIVE_DIR / str(profile_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for _, path in downloaded:
        dest = dest_dir / path.name
        if not dest.exists():
            shutil.copy2(str(path), str(dest))


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
            log.warning("All accounts are limited — staying alive for gallery/ratings")
            msg = "⛔️ Лимиты на всех аккаунтах — жду сброса. Галерея и оценки работают"
            state.status_message = msg
            state.warning = True
            asyncio.create_task(_notify_all_limited(msg))
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
        log.info("Bot sent a non-profile message(s): %s msg(s)", len(messages))
        reply = _non_profile_reply(messages)
        if reply:
            log.info("Non-profile reply matched, sending: %r", reply)
            await tg.send_reaction(reply)
            state.current_profile = None
            state.warning = False
            # Don't rotate yet — wait 3s to see if the bot responds with a profile
            asyncio.create_task(_deferred_wait_after_non_profile_reply())
            return
        state.current_profile = None
        if state.auto_rotate_mode and len(tg._phones) > 1:
            state.status_message = "Не-анкета — проверяю другие аккаунты…"
            state.warning = False
            asyncio.create_task(_deferred_rotate_on_non_profile())
        else:
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
        # Profile received — this account is no longer stuck in non-profile state
        state.non_profile_phones.discard(active_phone)

        dup_id = dedup.find_duplicate(description, all_hashes)
        if dup_id is None:
            profile_id = db.insert_profile(description, first_hash)
            dedup.register(profile_id, description, first_hash)
            stats.bump(active_phone, "new_profiles")
            seen_count = 1
            _archive_media(profile_id, downloaded)
        else:
            profile_id = dup_id
            seen_count = db.bump_seen(profile_id)
            _archive_media(profile_id, downloaded)

        if _is_auto_skip(description):
            log.info("Auto-skip match: profile id=%s desc=%r", profile_id, description[:60])
            shutil.rmtree(temp_dir, ignore_errors=True)
            await tg.send_reaction("👎")
            state.current_profile = None
            state.warning = False
            return

        if _is_priority_match(description):
            log.info("PRIORITY MATCH: profile id=%s desc=%r", profile_id, description[:60])
            files = _publish_media(profile_id, downloaded, temp_dir)
            state.current_profile = _profile_payload(profile_id, description, files, seen_count)
            state.priority_alert = True
            state.warning = False
            state.letter_pending = False
            state.status_message = ""
            asyncio.create_task(_start_secondary_processing())
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
            if active_phone and _soft_limit_hit(active_phone):
                _trigger_soft_limit(active_phone)
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
            if active_phone and _soft_limit_hit(active_phone):
                _trigger_soft_limit(active_phone)
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
