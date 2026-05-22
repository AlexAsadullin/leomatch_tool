#!/usr/bin/env python3
"""Sync historical @leomatchbot chat into the local database.

Iterates the full message history for each account in TG_PHONES and inserts
any profile (media + description) that doesn't already exist in the DB.

Usage:
    python3 extend_db.py
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from telethon import TelegramClient
from telethon.tl.custom import Message

from app import db
from app.config import API_HASH, API_ID, BOT_USERNAME, DB_PATH, PENDING_DIR, PHONES, session_path
from app.hashing import hash_video_first_frame, sha256_file
from app.profile import LONG_DESC_THRESHOLD

CHUNK_SIZE = 100  # commit to the DB once per this many scanned messages (≈ one Telegram fetch chunk)

# ──────────────────────────────────────────────────────────────────────────────
# Аккаунты для парсинга. Отредактируй вручную: укажи номера в нужном порядке —
# скрипт обработает их сверху вниз. Если список пустой, берутся все аккаунты
# из TG_PHONES (.env).
# ──────────────────────────────────────────────────────────────────────────────
ACCOUNTS: list[str] = [
    "+79334195469",
    "+79648395469",
    "+79334295469",
]


def _is_photo(msg: Message) -> bool:
    return msg.photo is not None


def _is_video(msg: Message) -> bool:
    return (
        msg.video is not None
        or getattr(msg, "video_note", None) is not None
        or getattr(msg, "gif", None) is not None
    )


def _has_media(msg: Message) -> bool:
    return _is_photo(msg) or _is_video(msg)


def _extract_text(messages: list[Message]) -> str:
    for m in messages:
        text = (m.message or "").strip()
        if text:
            return text
    return ""


def _purge_temp() -> None:
    """Remove any leftover extend_* temp dirs from a previously crashed run."""
    if not PENDING_DIR.exists():
        return
    for child in PENDING_DIR.iterdir():
        if child.name.startswith("extend_"):
            shutil.rmtree(child, ignore_errors=True)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}с"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}м {s}с"
    h, m = divmod(m, 60)
    return f"{h}ч {m}м"


def _compute_hash(first_msg: Message, first_path: Path) -> str:
    if _is_photo(first_msg):
        return sha256_file(first_path)
    return hash_video_first_frame(first_path)


def _find_profile(conn: sqlite3.Connection, description: str, media_hash: str):
    """Dedup lookup mirroring app/profile.py: by description if long, else description+hash.
    Runs on the shared connection so it also sees this run's not-yet-committed inserts."""
    if len(description) >= LONG_DESC_THRESHOLD:
        cur = conn.execute("SELECT id FROM profiles WHERE description = ?", (description,))
    else:
        cur = conn.execute(
            "SELECT id FROM profiles WHERE description = ? AND first_media_hash = ?",
            (description, media_hash),
        )
    return cur.fetchone()


def _insert_profile(conn: sqlite3.Connection, description: str, media_hash: str) -> int:
    """Insert a profile WITHOUT committing — commits are batched per chunk."""
    cur = conn.execute(
        "INSERT INTO profiles(description, first_media_hash) VALUES (?, ?)",
        (description, media_hash),
    )
    return int(cur.lastrowid)


async def _process_unit(conn: sqlite3.Connection, messages: list[Message], stats: dict) -> None:
    if not any(_has_media(m) for m in messages):
        return

    description = _extract_text(messages)
    head_id = messages[0].id
    temp_dir = PENDING_DIR / f"extend_{head_id}"

    try:
        # Download first media only for hashing
        first_downloaded: list[tuple[Message, Path]] = []
        temp_dir.mkdir(parents=True, exist_ok=True)
        for msg in messages:
            if not _has_media(msg):
                continue
            path = await msg.download_media(file=str(temp_dir) + "/")
            if path:
                first_downloaded.append((msg, Path(path)))
                break  # only need the first one to compute hash

        if not first_downloaded:
            return

        first_msg, first_path = first_downloaded[0]
        try:
            media_hash = _compute_hash(first_msg, first_path)
        except RuntimeError:
            return

        existing = _find_profile(conn, description, media_hash)
        if existing is not None:
            stats["skipped"] += 1
            return

        # New profile — only the hash is stored; media is not kept
        # (the app re-downloads media on display, so media/ stays empty here).
        # Insert is not committed here — commits are batched per chunk.
        _insert_profile(conn, description, media_hash)
        stats["inserted"] += 1

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _safe_process(conn: sqlite3.Connection, unit: list[Message], stats: dict) -> None:
    """Process one unit (single message or album), isolating per-unit errors."""
    unit = sorted(unit, key=lambda m: m.id)
    try:
        await _process_unit(conn, unit, stats)
    except Exception:
        print(f"  ОШИБКА на юните msg_id={unit[0].id}:")
        traceback.print_exc()
        stats["errors"] += 1


async def process_account(conn: sqlite3.Connection, phone: str) -> dict:
    sf = session_path(phone)
    stats: dict = {"phone": phone, "inserted": 0, "skipped": 0, "errors": 0}

    if not Path(str(sf) + ".session").exists():
        print(f"[{phone}] нет session-файла — сначала запусти login.py")
        stats["error"] = "no_session"
        return stats

    print(f"\n=== Аккаунт {phone} ===")
    client = TelegramClient(str(sf), API_ID, API_HASH)
    started = time.monotonic()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"[{phone}] не авторизован — запусти login.py")
            stats["error"] = "not_authorized"
            return stats

        # Stream history: group albums on the fly so RAM stays flat at any scale.
        # Album messages share a grouped_id and arrive consecutively in iter_messages,
        # so we only ever hold one unit (one album) in memory at a time.
        total = (await client.get_messages(BOT_USERNAME, limit=0)).total
        print(f"[{phone}] всего сообщений в чате: {total}")
        fetched = 0
        processed = 0
        last_inserted = 0
        iter_started = time.monotonic()
        current_unit: list[Message] = []
        current_gid = None

        async for msg in client.iter_messages(BOT_USERNAME, limit=None):
            fetched += 1
            if fetched % CHUNK_SIZE == 0:
                conn.commit()  # batch commit — one per scanned chunk
            if fetched % 500 == 0:
                now = time.monotonic()
                delta = stats["inserted"] - last_inserted
                remaining = max(total - fetched, 0)
                # Реальная средняя скорость по уже обработанным сообщениям этого аккаунта.
                elapsed_total = now - started
                speed = fetched / elapsed_total if elapsed_total > 0 else 0.0
                eta = remaining / speed if speed > 0 else 0.0
                print(f"[{phone}] прочитано {fetched}/{total} | осталось {remaining} | "
                      f"+{delta} анкет за итерацию (всего {stats['inserted']}) | "
                      f"итерация {now - iter_started:.1f}с | "
                      f"средняя {speed:.0f} сообщ/с | прогноз ~{_fmt_duration(eta)}")
                last_inserted = stats["inserted"]
                iter_started = now
            if msg.out:
                continue
            gid = msg.grouped_id
            if gid is not None and gid == current_gid:
                current_unit.append(msg)
                continue
            # grouped_id changed (or single message) → flush the accumulated unit
            if current_unit:
                processed += 1
                await _safe_process(conn, current_unit, stats)
            current_unit = [msg]
            current_gid = gid

        if current_unit:
            processed += 1
            await _safe_process(conn, current_unit, stats)

        conn.commit()  # flush this account's remainder
        print(f"[{phone}] прочитано {fetched} сообщений, обработано {processed} анкет-юнитов")

    finally:
        await client.disconnect()

    elapsed = time.monotonic() - started
    print(f"=== Аккаунт {phone}: добавлено {stats['inserted']}, "
          f"пропущено {stats['skipped']}, ошибок {stats['errors']}, за {elapsed:.1f}с ===")
    return stats


async def main() -> None:
    db.init()
    _purge_temp()
    # One shared connection for the whole run: dedup sees this run's uncommitted
    # inserts, and commits are batched (one per CHUNK_SIZE scanned messages).
    conn = sqlite3.connect(DB_PATH)
    run_started = time.monotonic()
    try:
        before = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        print(f"Анкет в БД до синхронизации: {before}")

        accounts = ACCOUNTS or PHONES
        print(f"Аккаунтов к обработке: {len(accounts)} (по порядку) {accounts}")

        all_stats = []
        for phone in accounts:
            s = await process_account(conn, phone)
            all_stats.append(s)

        print("\n=== ИТОГ ===")
        for s in all_stats:
            print(f"  {s['phone']}: добавлено {s.get('inserted', '—')}, "
                  f"пропущено {s.get('skipped', '—')}, ошибок {s.get('errors', '—')}")

        after = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        elapsed = time.monotonic() - run_started
        print(f"Анкет в БД: было {before}, стало {after} (добавлено {after - before})")
        print(f"Общее время: {elapsed:.1f}с")
    finally:
        conn.commit()  # persist anything since the last chunk commit (incl. on Ctrl+C)
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
