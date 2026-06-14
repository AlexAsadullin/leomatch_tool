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

# Secondary account — runs auto-processing in background while primary waits on priority profile
_secondary_idx: int | None = None
_secondary_on_messages: Optional[OnMessages] = None
_secondary_album_buffers: dict[int, list[Message]] = {}
_secondary_album_tasks: dict[int, asyncio.Task] = {}


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


async def _flush_secondary_album(gid: int) -> None:
    try:
        await asyncio.sleep(_ALBUM_FLUSH_DELAY)
    finally:
        msgs = _secondary_album_buffers.pop(gid, [])
        _secondary_album_tasks.pop(gid, None)
    msgs.sort(key=lambda m: m.id)
    if _secondary_on_messages:
        try:
            await _secondary_on_messages(msgs)
        except Exception:
            log.exception("secondary on_messages handler failed")


async def _secondary_event_handler(event) -> None:
    if _secondary_on_messages is None:
        return
    msg: Message = event.message
    if msg.grouped_id:
        gid = msg.grouped_id
        _secondary_album_buffers.setdefault(gid, []).append(msg)
        if gid not in _secondary_album_tasks:
            _secondary_album_tasks[gid] = asyncio.create_task(_flush_secondary_album(gid))
    else:
        try:
            await _secondary_on_messages([msg])
        except Exception:
            log.exception("secondary on_messages handler failed")


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


def _msg_has_media(m: Message) -> bool:
    return (
        m.photo is not None
        or m.video is not None
        or getattr(m, "video_note", None) is not None
        or getattr(m, "gif", None) is not None
    )


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
        sf = Path(str(session_path(phone)) + ".session")
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


async def start_secondary(idx: int, handler: OnMessages) -> bool:
    """Connect account at idx as secondary (event-driven, no rotation of primary)."""
    global _secondary_idx, _secondary_on_messages
    if idx == _current_idx:
        return False
    sf = Path(str(session_path(_phones[idx])) + ".session")
    if not sf.exists():
        log.warning("No session file for secondary idx=%s", idx)
        return False
    c = _clients[idx]
    try:
        await c.connect()
        if not await c.is_user_authorized():
            log.info("Secondary idx=%s not authorized", idx)
            await c.disconnect()
            return False
        _secondary_on_messages = handler
        c.add_event_handler(_secondary_event_handler, events.NewMessage(from_users=BOT_USERNAME, incoming=True))
        _secondary_idx = idx
        log.info("Secondary started idx=%s phone=%s", idx, _phones[idx])
        return True
    except Exception:
        log.exception("Failed to start secondary idx=%s", idx)
        try:
            await c.disconnect()
        except Exception:
            pass
        return False


async def stop_secondary() -> None:
    global _secondary_idx, _secondary_on_messages
    if _secondary_idx is None:
        return
    idx = _secondary_idx
    c = _clients[idx]
    try:
        c.remove_event_handler(_secondary_event_handler)
        await c.disconnect()
        log.info("Secondary stopped idx=%s", idx)
    except Exception:
        log.exception("Error stopping secondary idx=%s", idx)
    finally:
        _secondary_idx = None
        _secondary_on_messages = None
        for task in list(_secondary_album_tasks.values()):
            task.cancel()
        _secondary_album_tasks.clear()
        _secondary_album_buffers.clear()


async def secondary_send(text: str) -> None:
    if _secondary_idx is None:
        raise RuntimeError("No secondary account active")
    await _clients[_secondary_idx].send_message(BOT_USERNAME, text)


def secondary_idx() -> int | None:
    return _secondary_idx


async def find_and_rotate_to_profile_account() -> bool:
    """Iterate non-current accounts, check if the latest bot message is a profile.
    Rotates to the first account that has one. Returns True if rotated."""
    global _current_idx
    start_idx = _current_idx
    for offset in range(1, len(_clients)):
        idx = (start_idx + offset) % len(_clients)
        phone = _phones[idx]
        sf = Path(str(session_path(phone)) + ".session")
        if not sf.exists():
            log.warning("No session file for index=%s, skipping", idx)
            continue
        c = _clients[idx]
        has_profile = False
        try:
            await c.connect()
            if not await c.is_user_authorized():
                log.info("Account index=%s not authorized, skipping", idx)
                await c.disconnect()
                continue
            msgs: list[Message] = []
            async for m in c.iter_messages(BOT_USERNAME, limit=20):
                msgs.append(m)
            if msgs:
                head = msgs[0]
                unit = [m for m in msgs if m.grouped_id == head.grouped_id] if head.grouped_id else [head]
                has_profile = any(_msg_has_media(m) for m in unit)
                log.info("Account index=%s: latest unit has_profile=%s", idx, has_profile)
            else:
                log.info("Account index=%s: no messages from bot", idx)
            await c.disconnect()
        except Exception:
            log.exception("Error checking account index=%s for profile", idx)
            try:
                await c.disconnect()
            except Exception:
                pass
            continue
        if has_profile:
            log.info("Account index=%s has a profile — rotating to it", idx)
            await _disconnect_current()
            try:
                await _connect_and_register(idx)
                _current_idx = idx
                return True
            except Exception:
                log.exception("Failed to connect account index=%s after check", idx)
    return False


async def rotate_account() -> bool:
    global _current_idx
    from . import limits as acct_limits
    start_idx = _current_idx
    log.info("Rotating from account index=%s", start_idx)
    await _disconnect_current()
    candidate = (_current_idx + 1) % len(_clients)
    while candidate != start_idx:
        phone = _phones[candidate]
        sf = Path(str(session_path(phone)) + ".session")
        if not sf.exists():
            log.warning("No session file for index=%s, skipping", candidate)
        elif acct_limits.is_limited(phone):
            log.info("Account index=%s phone=%s is limited, skipping", candidate, phone)
        else:
            try:
                await _connect_and_register(candidate)
                _current_idx = candidate
                return True
            except Exception:
                log.exception("Account index=%s failed, trying next", candidate)
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
    try:
        path = await message.download_media(file=str(dest_dir) + "/")
        return Path(path) if path else None
    except Exception:
        log.warning("download_media failed for message id=%s — skipping", message.id, exc_info=True)
        return None
