from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, dedup, limits as acct_limits, settings, stats, tg
from .config import DB_PATH, LIMITS_PATH, MEDIA_DIR, PHONES, STATIC_DIR, TG_BOT_TOKEN
from .profile import handle_messages, _deferred_rotate, _deferred_letter
from .state import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("leodv")


_WATCHDOG_INTERVAL = 30  # sec — раз в полминуты проверяем что бот не молчит


async def _watchdog() -> None:
    """Safety net: если давно ничего не приходило и нет активной анкеты —
    дотащить из @leomatchbot последнее сообщение и прогнать через handle_messages,
    чтобы поймать non-profile, который мог не докатиться по апдейтам."""
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL)
        try:
            idle = time.monotonic() - state.last_message_at
            if (
                idle < _WATCHDOG_INTERVAL
                or state.busy
                or state.current_profile is not None
                or state.warning
            ):
                continue
            log.info("Watchdog: idle %.0fs, re-fetching latest bot message", idle)
            latest = await tg.fetch_latest_unit()
            if latest:
                await handle_messages(latest)
        except Exception:
            log.exception("Watchdog error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    acct_limits.init(LIMITS_PATH)   # ← сбрасываем устаревшие лимиты (>8ч), загружаем актуальные
    settings.load()    # ← восстанавливаем сохранённые режимы/фильтры/счётчики
    stats.load_and_maybe_reset()   # ← per-account stats; сброс лениво на 05:00
    dedup.init_indexes()
    log.info("dedup indexes loaded: %s", dedup.stats())
    if DB_PATH.exists():
        backup = DB_PATH.with_name("data_backup.db")
        shutil.copy2(DB_PATH, backup)
        log.info("DB backed up to %s", backup)
    tg.set_handler(handle_messages)
    await tg.start(PHONES)
    state.active_account_idx = tg.current_idx()
    state.total_accounts = tg.total_accounts()

    # Optional Telegram-bot UI (mirror of the web frontend). Skipped if no token.
    # Admins выводятся автоматически из TG_PHONES — bootstrap делает /start от
    # каждого юзер-аккаунта боту, чтобы тот занёс их entity в свою сессию.
    if TG_BOT_TOKEN:
        from tg_bot.bot import init as init_bot
        bot = init_bot(TG_BOT_TOKEN)
        try:
            await bot.start()
            await bot.bootstrap_admins(PHONES)
            log.info("Telegram bot enabled, admins=%s", sorted(bot.admin_ids))
        except Exception:
            log.exception("Failed to start Telegram bot — continuing without it")

    log.info("Telethon connected; bootstrapping latest message")
    try:
        latest = await tg.fetch_latest_unit()
        if latest:
            await handle_messages(latest)
    except Exception:
        log.exception("Bootstrap failed")
    asyncio.create_task(_watchdog())
    yield
    await tg.stop()
    try:
        from tg_bot.bot import get_bot
        b = get_bot()
        if b:
            await b.stop()
    except Exception:
        log.exception("Bot stop failed")


app = FastAPI(lifespan=lifespan)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/state")
async def get_state():
    snap = state.snapshot()
    snap["total_profiles"] = db.count_profiles()
    snap["active_phone"] = tg._phones[tg._current_idx] if tg._phones else ""
    return snap


@app.post("/api/like")
async def like():
    return await _react("❤️")


@app.post("/api/dislike")
async def dislike():
    return await _react("👎")


async def _react(text: str):
    async with state.lock:
        if state.warning or state.current_profile is None:
            raise HTTPException(status_code=409, detail="No active profile to react to")
        state.busy = True
    try:
        await tg.send_reaction(text)
    finally:
        active_phone = tg._phones[tg._current_idx] if tg._phones else ""
        async with state.lock:
            state.current_profile = None
            state.priority_alert = False
            state.letter_pending = False
            if text == "❤️":
                state.like_count += 1
                stats.bump(active_phone, "likes")
            elif text == "👎":
                state.dislike_count += 1
                stats.bump(active_phone, "dislikes")
        settings.save()
    return {"ok": True}


class LetterPayload(BaseModel):
    text: str = ""


@app.post("/api/letter")
async def letter(payload: LetterPayload):
    async with state.lock:
        if state.warning or state.current_profile is None:
            raise HTTPException(status_code=409, detail="No active profile to react to")
    asyncio.create_task(_deferred_letter(payload.text))
    return {"ok": True}


@app.post("/api/switch-account")
async def switch_account():
    asyncio.create_task(_deferred_rotate())
    return {"ok": True}


@app.post("/api/auto-dislike/toggle")
async def toggle_auto_dislike():
    async with state.lock:
        state.auto_dislike_mode = not state.auto_dislike_mode
        if state.auto_dislike_mode:
            state.auto_like_mode = False
        settings.save()
        return state.snapshot()


@app.post("/api/auto-like/toggle")
async def toggle_auto_like():
    async with state.lock:
        state.auto_like_mode = not state.auto_like_mode
        if state.auto_like_mode:
            state.auto_dislike_mode = False
        settings.save()
        return state.snapshot()


@app.post("/api/only-new/toggle")
async def toggle_only_new():
    async with state.lock:
        state.only_new_mode = not state.only_new_mode
        if state.only_new_mode:
            state.auto_dislike_count = 0
        settings.save()
        return state.snapshot()


@app.post("/api/auto-rotate/toggle")
async def toggle_auto_rotate():
    """Авто-смена аккаунтов при срабатывании лимита (только лимит!)."""
    async with state.lock:
        state.auto_rotate_mode = not state.auto_rotate_mode
        settings.save()
        return state.snapshot()


@app.get("/gallery")
async def gallery_page():
    return FileResponse(str(STATIC_DIR / "gallery.html"))


@app.get("/rating")
async def rating_page():
    return FileResponse(str(STATIC_DIR / "rating.html"))


def _archive_urls(profile_id: int) -> list[str]:
    archive_dir = MEDIA_DIR / "_archive" / str(profile_id)
    if not archive_dir.exists():
        return []
    return [
        f"/media/_archive/{profile_id}/{f.name}"
        for f in sorted(archive_dir.iterdir())
        if f.is_file()
    ]


def _thumb_url(profile_id: int) -> str | None:
    urls = _archive_urls(profile_id)
    return urls[0] if urls else None


@app.get("/api/gallery")
async def api_gallery():
    profiles = db.get_all_profiles()
    return [
        {
            "id": p["id"],
            "description": p["description"],
            "rating": p["rating"],
            "seen_count": p["seen_count"],
            "registered_at": p["registered_at"],
            "thumb_url": _thumb_url(p["id"]),
        }
        for p in profiles
    ]


@app.get("/api/ratings/{profile_id}")
async def api_get_profile(profile_id: int):
    p = db.get_profile(profile_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    urls = _archive_urls(p["id"])
    return {
        "id": p["id"],
        "description": p["description"],
        "rating": p["rating"],
        "seen_count": p["seen_count"],
        "registered_at": p["registered_at"],
        "thumb_url": urls[0] if urls else None,
        "media_urls": urls,
    }


class RatingPayload(BaseModel):
    rating: int


@app.post("/api/ratings/{profile_id}/rate")
async def api_set_rating(profile_id: int, payload: RatingPayload):
    if not (1 <= payload.rating <= 10):
        raise HTTPException(status_code=400, detail="Rating must be 1–10")
    if db.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    db.set_rating(profile_id, payload.rating)
    return {"ok": True}


class AgeFilterPayload(BaseModel):
    min: int | None = None
    max: int | None = None


@app.post("/api/age-filter")
async def set_age_filter(payload: AgeFilterPayload):
    async with state.lock:
        if payload.min is None and payload.max is None:
            state.age_min = None
            state.age_max = None
            settings.save()
            return state.snapshot()
        if payload.min is None or payload.max is None:
            raise HTTPException(status_code=400, detail="Both min and max must be provided")
        if payload.min < 0 or payload.max < 0:
            raise HTTPException(status_code=400, detail="Age must be non-negative")
        if payload.min > payload.max:
            raise HTTPException(status_code=400, detail="min must be <= max")
        state.age_min = payload.min
        state.age_max = payload.max
        settings.save()
        return state.snapshot()
