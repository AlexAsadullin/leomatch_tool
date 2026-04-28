from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, tg
from .config import MEDIA_DIR, STATIC_DIR
from .profile import handle_messages
from .state import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("leodv")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    tg.set_handler(handle_messages)
    await tg.start()
    log.info("Telethon connected; bootstrapping latest message")
    try:
        latest = await tg.fetch_latest_unit()
        if latest:
            await handle_messages(latest)
    except Exception:
        log.exception("Bootstrap failed")
    yield
    await tg.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/state")
async def get_state():
    return state.snapshot()


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
        async with state.lock:
            state.current_profile = None
    return {"ok": True}


@app.post("/api/only-new/toggle")
async def toggle_only_new():
    async with state.lock:
        state.only_new_mode = not state.only_new_mode
        if state.only_new_mode:
            state.auto_dislike_count = 0
        return state.snapshot()
