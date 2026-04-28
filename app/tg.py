from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events
from telethon.tl.custom import Message

from .config import API_HASH, API_ID, BOT_USERNAME, PHONE, SESSION_PATH

log = logging.getLogger("leodv.tg")

OnMessages = Callable[[list[Message]], Awaitable[None]]

client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)

_on_messages: Optional[OnMessages] = None
_album_buffers: dict[int, list[Message]] = {}
_album_tasks: dict[int, asyncio.Task] = {}
_ALBUM_FLUSH_DELAY = 1.2


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


@client.on(events.NewMessage(from_users=BOT_USERNAME, incoming=True))
async def _on_new_message(event):
    msg: Message = event.message
    if msg.grouped_id:
        gid = msg.grouped_id
        _album_buffers.setdefault(gid, []).append(msg)
        if gid not in _album_tasks:
            _album_tasks[gid] = asyncio.create_task(_flush_album(gid))
    else:
        await _dispatch([msg])


async def fetch_latest_unit() -> list[Message]:
    """Return the most recent 'unit' from the bot: a single message or a full album."""
    msgs: list[Message] = []
    async for m in client.iter_messages(BOT_USERNAME, limit=20):
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
    await client.send_message(BOT_USERNAME, text)


async def start() -> None:
    if not Path(str(SESSION_PATH) + ".session").exists():
        raise RuntimeError(
            "Telethon session not found. Run `python3 login.py` first to authorize."
        )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon session is not authorized. Run `python3 login.py` to (re)authorize."
        )


async def stop() -> None:
    await client.disconnect()


async def download_media(message: Message, dest_dir: Path) -> Optional[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = await message.download_media(file=str(dest_dir) + "/")
    return Path(path) if path else None
