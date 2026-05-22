#!/usr/bin/env python3
"""Sync historical @leomatchbot chat into the local database.

Iterates the full message history for each account in TG_PHONES and inserts
any profile (media + description) that doesn't already exist in the DB.

Usage:
    python3 scripts/extend_db.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime as dt

# This script lives in scripts/; the project root is its parent directory.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telethon import TelegramClient
from telethon.tl.custom import Message

from app import db
from app.config import API_HASH, API_ID, BOT_USERNAME, DB_PATH, PENDING_DIR, PHONES, session_path
from app.hashing import hash_video_first_frame, sha256_file
from app.profile import LONG_DESC_THRESHOLD

CHUNK_SIZE = 100  # commit to the DB once per this many scanned messages (≈ one Telegram fetch chunk)
SAVE_EVERY = 500  # update the progress checkpoint (db_extension_info.json) every this many messages

CHECKPOINT_PATH = SCRIPT_DIR / "db_extension_info.json"

# ──────────────────────────────────────────────────────────────────────────────
# Аккаунты для парсинга. Отредактируй вручную: укажи номера в нужном порядке —
# скрипт обработает их сверху вниз. Если список пустой, берутся все аккаунты
# из TG_PHONES (.env).
# ──────────────────────────────────────────────────────────────────────────────
ACCOUNTS: list[str] = [
    "+79648395469",
    "+79334295469",
    "+79334195469",
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


def _load_checkpoint(accounts: list[str]) -> dict:
    """Load db_extension_info.json. If it's missing, unreadable, or the account
    list/order differs, return a fresh empty state (full restart, no checkpoints)."""
    fresh = {"accounts": accounts, "current_account": None, "progress": {}}
    if not CHECKPOINT_PATH.exists():
        return fresh
    try:
        data = json.loads(CHECKPOINT_PATH.read_text())
    except Exception:
        print("Чекпоинт повреждён — старт с начала")
        return fresh
    if data.get("accounts") != accounts:
        print("Список/порядок аккаунтов изменился — чекпоинт сброшен, старт с начала")
        return fresh
    return {
        "accounts": accounts,
        "current_account": data.get("current_account"),
        "progress": data.get("progress", {}),
    }


def _save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _clear_checkpoint() -> None:
    CHECKPOINT_PATH.unlink(missing_ok=True)


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


async def process_account(conn: sqlite3.Connection, phone: str, state: dict) -> dict:
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

        # Точка возобновления из чекпоинта — только если число сообщений в чате
        # совпадает с сохранённым (иначе порядок сместился — обрабатываем заново).
        cp = state["progress"].get(phone)
        resume_at = 0
        if cp and not cp.get("done") and cp.get("total") == total:
            resume_at = min(int(cp.get("processed", 0)), total)
            print(f"[{phone}] всего сообщений: {total} — возобновляю с {resume_at}")
        elif cp and cp.get("total") != total:
            print(f"[{phone}] всего сообщений: {total} "
                  f"(в чекпоинте {cp.get('total')}) — обрабатываю заново")
        else:
            print(f"[{phone}] всего сообщений: {total}")

        state["current_account"] = phone

        fetched = resume_at
        processed = 0
        last_inserted = 0
        iter_started = time.monotonic()
        current_unit: list[Message] = []
        current_gid = None

        async for msg in client.iter_messages(BOT_USERNAME, limit=None, add_offset=resume_at):
            fetched += 1
            if fetched % CHUNK_SIZE == 0:
                conn.commit()  # batch commit — one per scanned chunk
            if fetched % SAVE_EVERY == 0:
                conn.commit()  # make the DB durable BEFORE recording the checkpoint
                state["progress"][phone] = {"total": total, "processed": fetched, "done": False}
                _save_checkpoint(state)
                now = time.monotonic()
                delta = stats["inserted"] - last_inserted
                remaining = max(total - fetched, 0)
                # Средняя скорость по сообщениям, обработанным в этом запуске.
                elapsed_total = now - started
                scanned = fetched - resume_at
                speed = scanned / elapsed_total if elapsed_total > 0 else 0.0
                eta = remaining / speed if speed > 0 else 0.0
                print(f"[{dt.now().strftime("%Y-%b-%d %H:%M:%S")}] {fetched}/{total} | осталось {remaining} | "
                      f"+{delta} | "
                      f"{now - iter_started:.1f}с | "
                      f"осталось ~{_fmt_duration(eta)}")
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
        state["progress"][phone] = {"total": total, "processed": total, "done": True}
        _save_checkpoint(state)
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

        state = _load_checkpoint(accounts)

        all_stats = []
        for phone in accounts:
            cp = state["progress"].get(phone)
            if cp and cp.get("done"):
                print(f"\n[{phone}] уже обработан по чекпоинту — пропуск")
                continue
            s = await process_account(conn, phone, state)
            all_stats.append(s)

        # Чекпоинт чистим только когда ВСЕ аккаунты реально завершены.
        if all(state["progress"].get(p, {}).get("done") for p in accounts):
            _clear_checkpoint()
            print("\nВсе аккаунты обработаны — чекпоинт db_extension_info.json очищен")
        else:
            _save_checkpoint(state)
            print("\nНе все аккаунты завершены — чекпоинт сохранён для возобновления")

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
