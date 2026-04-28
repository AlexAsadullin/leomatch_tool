"""One-shot Telethon authorization. Run before starting the server.

Usage:
    python3 login.py
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from app.config import API_HASH, API_ID, PHONE, SESSION_PATH


async def main() -> None:
    client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
    await client.start(phone=lambda: PHONE or input("Phone: "))
    me = await client.get_me()
    print(f"Authorized as: {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
