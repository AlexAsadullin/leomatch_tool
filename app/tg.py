from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events
from telethon.tl.custom import Message

from .config import API_HASH, API_ID, BOT_USERNAME, session_path

log = logging.getLogger("leodv.tg")

OnMessages = Callable[[list[Message]], Awaitable[None]]

_clients: list[TelegramClient] = []
_current_idx: int = 0
_phones: list[str] = []

_on_messages: Optional[OnMessages] = None
_album_buffers: dict[int, list[Message]] = {}
_album_tasks: dict[int, asyncio.Task] = {}
_ALBUM_FLUSH_DELAY = 1.2


def active_client() -> TelegramClient:
    return _clients[_current_idx]


def set_handler(handler: OnMessages) -> None:
    global _on_messages
    _on_messages = handler


async def _dispatch(messages: list[Message]) -> None:
    if _on_messages is None or not messages:
        return
    try:
        await _on_messages(messages)
    except Exception:
        log.exception("on_messages handler failed")


async def _flush_album(gid: int) -> None:
    try:
        await asyncio.sleep(_ALBUM_FLUSH_DELAY)
    finally:
        msgs = _album_buffers.pop(gid, [])
        _album_tasks.pop(gid, None)
    msgs.sort(key=lambda m: m.id)
    await _dispatch(msgs)


async def _event_handler(event) -> None:
    msg: Message = event.message
    if msg.grouped_id:
        gid = msg.grouped_id
        _album_buffers.setdefault(gid, []).append(msg)
        if gid not in _album_tasks:
            _album_tasks[gid] = asyncio.create_task(_flush_album(gid))
    else:
        await _dispatch([msg])


async def _connect_and_register(idx: int) -> None:
    c = _clients[idx]
    await c.connect()
    if not await c.is_user_authorized():
        raise RuntimeError(f"Account index={idx} ({_phones[idx]}) is not authorized")
    c.add_event_handler(_event_handler,
                        events.NewMessage(from_users=BOT_USERNAME, incoming=True))
    log.info("Connected account index=%s phone=%s", idx, _phones[idx])


async def _disconnect_current() -> None:
    if not _clients:
        return
    c = _clients[_current_idx]
    try:
        c.remove_event_handler(_event_handler)
        await c.disconnect()
    except Exception:
        log.exception("Error disconnecting account index=%s", _current_idx)
    # cancel pending album tasks for old account
    for task in list(_album_tasks.values()):
        task.cancel()
    _album_tasks.clear()
    _album_buffers.clear()


async def fetch_latest_unit() -> list[Message]:
    msgs: list[Message] = []
    async for m in active_client().iter_messages(BOT_USERNAME, limit=20):
        msgs.append(m)
    if not msgs:
        return []
    head = msgs[0]
    if head.grouped_id is None:
        return [head]
    same = [m for m in msgs if m.grouped_id == head.grouped_id]
    same.sort(key=lambda m: m.id)
    return same


async def send_reaction(text: str) -> None:
    await active_client().send_message(BOT_USERNAME, text)


async def start(phones: list[str]) -> None:
    global _clients, _current_idx, _phones
    _phones = phones
    _clients = [TelegramClient(str(session_path(p)), API_ID, API_HASH) for p in phones]
    for idx, phone in enumerate(phones):
        sf = Path(_clients[idx].session.filename)
        if not sf.exists():
            log.warning("No session file for phone=%s, skipping", phone)
            continue
        try:
            await _connect_and_register(idx)
            _current_idx = idx
            return
        except Exception:
            log.exception("Account index=%s failed to connect, trying next", idx)
    raise RuntimeError(
        "No working account found. Run `python3 login.py` to authorize all accounts."
    )


async def rotate_account() -> bool:
    global _current_idx
    start_idx = _current_idx
    log.info("Rotating from account index=%s", start_idx)
    await _disconnect_current()
    candidate = (_current_idx + 1) % len(_clients)
    while candidate != start_idx:
        sf = Path(_clients[candidate].session.filename)
        if sf.exists():
            try:
                await _connect_and_register(candidate)
                _current_idx = candidate
                return True
            except Exception:
                log.exception("Account index=%s failed, trying next", candidate)
        else:
            log.warning("No session file for index=%s, skipping", candidate)
        candidate = (candidate + 1) % len(_clients)
    log.error("All %s accounts exhausted", len(_clients))
    return False


def current_idx() -> int:
    return _current_idx


def total_accounts() -> int:
    return len(_clients)


async def stop() -> None:
    await _disconnect_current()


async def download_media(message: Message, dest_dir: Path) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = await message.download_media(file=str(dest_dir) + "/")
    return Path(path) if path else None
