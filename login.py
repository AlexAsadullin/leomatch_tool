"""Authorize all accounts listed in TG_PHONES. Run once (or after session expiry).

Usage:
    python3 login.py
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from app.config import API_HASH, API_ID, PHONES, session_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


async def _authorize(phone: str) -> None:
    sp = session_path(phone)
    sf = Path(str(sp) + ".session")

    if sf.exists():
        client = TelegramClient(str(sp), API_ID, API_HASH)
        await client.connect()
        authorized = await client.is_user_authorized()
        if authorized:
            me = await client.get_me()
            print(f"  ✓ {phone}: {me.first_name} (@{me.username})")
            await client.disconnect()
            return
        # Disconnect BEFORE deleting — SQLite must be closed first
        await client.disconnect()
        sf.unlink()
        log.info("Deleted stale session for %s, re-authorizing", phone)

    print(f"[{phone}] Enter the code from SMS:")
    client = TelegramClient(str(sp), API_ID, API_HASH)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"  ✓ {phone}: {me.first_name} (@{me.username})")
    await client.disconnect()


async def main() -> None:
    print(f"Authorizing {len(PHONES)} account(s)...\n")
    for phone in PHONES:
        await _authorize(phone)
    print("\nAll accounts authorized.")


if __name__ == "__main__":
    asyncio.run(main())
