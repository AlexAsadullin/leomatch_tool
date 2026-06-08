#!/usr/bin/env python3
"""Скачать медиа из Telegram Stories.

Использование:
    python3 scripts/download_tg_media.py https://t.me/username/s/16
    python3 scripts/download_tg_media.py https://t.me/username/s/16 [папка]

Скачивает ВСЕ сторисы канала/пользователя из ссылки (активные + архив).
Папка по умолчанию: scripts/downloads/
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# ─── .env ────────────────────────────────────────────────────────────────────

def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env(PROJECT_ROOT / ".env")

try:
    API_ID   = int(os.environ["TG_API_ID"])
    API_HASH = os.environ["TG_API_HASH"]
    PHONES   = [p.strip() for p in os.environ["TG_PHONES"].split(",") if p.strip()]
except KeyError as e:
    sys.exit(f"Не найдена переменная {e} в .env")

SESSION_NAME = os.environ.get("SESSION_NAME", "leodv")

def _session(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return str(PROJECT_ROOT / f"{SESSION_NAME}_{digits}")

# ─── telethon ────────────────────────────────────────────────────────────────

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError, FileReferenceExpiredError,
    FloodWaitError, UserNotParticipantError,
)
from telethon.tl.functions.stories import (
    GetPeerStoriesRequest,
    GetPinnedStoriesRequest,
    GetStoriesArchiveRequest,
    GetStoriesByIDRequest,
)
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, StoryItem

# ─── разбор ссылки ────────────────────────────────────────────────────────────

def parse_link(link: str) -> tuple[str, int | None]:
    """Извлечь username и ID стори из ссылки вида https://t.me/username/s/16"""
    link = link.strip()
    m = re.search(r't\.me/([A-Za-z0-9_]+)(?:/s/(\d+))?', link)
    if m:
        story_id = int(m.group(2)) if m.group(2) else None
        return m.group(1), story_id
    sys.exit(f"Не удалось разобрать ссылку: {link!r}\nФормат: https://t.me/username/s/16")

# ─── расширение файла ─────────────────────────────────────────────────────────

_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/mpeg": ".mpeg",
}

def _ext(story: StoryItem) -> str:
    m = story.media
    if isinstance(m, MessageMediaPhoto):
        return ".jpg"
    if isinstance(m, MessageMediaDocument):
        doc = m.document
        for attr in doc.attributes:
            fn = getattr(attr, "file_name", None)
            if fn:
                e = Path(fn).suffix
                if e:
                    return e
        mime = getattr(doc, "mime_type", "") or ""
        return _MIME_EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    return ".bin"

# ─── получение всех сторисов ─────────────────────────────────────────────────

async def fetch_all_stories(
    client: TelegramClient, entity, hint_max_id: int | None = None,
) -> list[StoryItem]:
    stories: dict[int, StoryItem] = {}

    # Активные (закреплённые / ещё не истёкшие)
    try:
        r = await client(GetPeerStoriesRequest(peer=entity))
        peer_stories = getattr(r, "stories", None)
        items = getattr(peer_stories, "stories", []) if peer_stories else []
        for s in items:
            if isinstance(s, StoryItem):
                stories[s.id] = s
        print(f"  Активных сторисов: {len(stories)}")
    except Exception as e:
        print(f"  GetPeerStories: {e}")

    # Закреплённые / выделенные
    try:
        r = await client(GetPinnedStoriesRequest(peer=entity, offset_id=0, limit=100))
        pinned_count = 0
        for s in (r.stories if hasattr(r, "stories") else []):
            if isinstance(s, StoryItem) and s.id not in stories:
                stories[s.id] = s
                pinned_count += 1
        if pinned_count:
            print(f"  Закреплённых сторисов: {pinned_count}")
    except Exception:
        pass

    # Архив через GetStoriesArchiveRequest (работает для каналов и своего аккаунта)
    offset_id = 0
    archive_count = 0
    while True:
        try:
            r = await client(GetStoriesArchiveRequest(
                peer=entity, offset_id=offset_id, limit=100,
            ))
            batch = r.stories if hasattr(r, "stories") else []
            if not batch:
                break
            for s in batch:
                if isinstance(s, StoryItem) and s.id not in stories:
                    stories[s.id] = s
                    archive_count += 1
            if len(batch) < 100:
                break
            offset_id = batch[-1].id
        except Exception:
            break  # не поддерживается для этого типа peer

    if archive_count:
        print(f"  Из архива (archive API): {archive_count}")

    # Сканирование по ID — надёжный фолбэк для чужих аккаунтов/каналов
    max_id = max((s.id for s in stories.values()), default=0)
    if hint_max_id:
        max_id = max(max_id, hint_max_id)

    if max_id > 0:
        scan_ids = [i for i in range(1, max_id + 1) if i not in stories]
        scan_found = 0
        for i in range(0, len(scan_ids), 100):
            chunk = scan_ids[i:i + 100]
            try:
                r = await client(GetStoriesByIDRequest(peer=entity, id=chunk))
                for s in (r.stories if hasattr(r, "stories") else []):
                    if isinstance(s, StoryItem) and s.id not in stories:
                        stories[s.id] = s
                        scan_found += 1
            except Exception:
                pass
        if scan_found:
            print(f"  Из сканирования по ID (1–{max_id}): {scan_found}")

    print(f"  Итого уникальных: {len(stories)}")
    return sorted(stories.values(), key=lambda s: s.id)

# ─── скачивание одного стори ──────────────────────────────────────────────────

async def _download_story(
    client: TelegramClient, entity, story: StoryItem, dest: Path,
) -> bool:
    current = story
    for attempt in range(4):
        try:
            result = await client.download_media(current.media, str(dest))
            if result is None:
                print(f"    download_media вернул None (story_id={current.id})")
                return False
            return True
        except FileReferenceExpiredError:
            print(f"    FileReferenceExpired story_id={current.id}, обновляю…")
            try:
                r = await client(GetStoriesByIDRequest(peer=entity, id=[current.id]))
                fresh = r.stories[0] if r.stories else None
                if fresh:
                    current = fresh
            except Exception as e:
                print(f"    Не удалось обновить: {e}")
        except FloodWaitError as e:
            print(f"    FloodWait {e.seconds}с — жду…")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"    Ошибка (попытка {attempt+1}): {type(e).__name__}: {e}")
            if attempt == 3:
                return False
            await asyncio.sleep(2)
    return False

# ─── точка входа ──────────────────────────────────────────────────────────────

async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    link = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2].startswith("http"):
        sys.exit(f"Ошибка: второй аргумент выглядит как ссылка, а не папка.\n"
                 f"  Скрипт принимает только одну ссылку:\n"
                 f"  python3 scripts/download_tg_media.py <ссылка> [папка]")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else SCRIPT_DIR / "downloads"
    username, hint_max_id = parse_link(link)

    print(f"Канал    : @{username}")
    print(f"Папка    : {out_dir}")
    print(f"Аккаунтов: {len(PHONES)}\n")

    for phone in PHONES:
        print(f"→ Пробую {phone}…")
        client = TelegramClient(_session(phone), API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print("  Не авторизован — пропускаю.")
                continue

            try:
                entity = await client.get_entity(username)
            except (ChannelPrivateError, UserNotParticipantError):
                print("  Нет доступа — пробую следующий.")
                continue
            except Exception as e:
                print(f"  get_entity: {e}")
                continue

            title = getattr(entity, "title", None) or getattr(entity, "username", None)
            print(f"  Доступ есть! «{title}»")

            all_stories = await fetch_all_stories(client, entity, hint_max_id=hint_max_id)
            if not all_stories:
                print("  Сторисов не найдено.")
                await client.disconnect()
                continue

            out_dir.mkdir(parents=True, exist_ok=True)
            done = failed = skipped = 0

            for story in all_stories:
                if story.media is None:
                    continue

                ext  = _ext(story)
                dest = out_dir / f"{story.id}{ext}"

                if dest.exists():
                    n = 2
                    while dest.exists():
                        dest = out_dir / f"{story.id}_{n}{ext}"
                        n += 1

                ok = await _download_story(client, entity, story, dest)
                if ok:
                    done += 1
                    print(f"  [{done}] {dest.name}")
                else:
                    failed += 1

            print(f"\n✓ Скачано: {done}  |  Не удалось: {failed}  |  Уже было: {skipped}")
            print(f"  Файлы: {out_dir}")
            return

        except KeyboardInterrupt:
            print("\nПрервано.")
            return
        except Exception as e:
            import traceback
            print(f"  Неожиданная ошибка: {e}")
            traceback.print_exc()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    print("\n✗ Ни один аккаунт не смог получить доступ.")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
