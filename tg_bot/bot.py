"""Telegram-bot UI — mirrors the web frontend functionality.

Все кнопки на reply-клавиатуре (не inline). Состояние режимов отображается
прямо на тексте кнопок (ON/OFF, текущий диапазон возраста и т.д.).

Bot session file: leodv_bot.session (рядом с пользовательскими `.session`).

Требует:
- `TG_BOT_TOKEN` в .env — токен от @BotFather.

Админы = аккаунты `TG_PHONES`. При старте бот делает `/start` от каждого из них
через их юзер-клиенты — это (а) кеширует их `InputPeer` в сессии бота
(иначе `send_file`/`send_message` валится с `Could not find input entity`),
(б) даёт боту реальные `user_id` через `get_me()`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Optional

from telethon import Button, TelegramClient, events

from app.config import API_HASH, API_ID, ROOT, session_path
from app.state import state

log = logging.getLogger("leodv.tg_bot")


def _btn(text: str) -> Button:
    """Reply-keyboard button (NOT inline)."""
    return Button.text(text, resize=True)


def _rating_inline_kb(profile_id: int, saved: int = -1) -> list[list[Button]]:
    """10 inline-кнопок оценки (1–10). profile_id зашит в callback data."""
    def lbl(i: int) -> str:
        return f"[{i}]" if i == saved else str(i)
    return [
        [Button.inline(lbl(i), f"rate:{i}:{profile_id}".encode()) for i in range(1, 6)],
        [Button.inline(lbl(i), f"rate:{i}:{profile_id}".encode()) for i in range(6, 11)],
    ]


def _format_age() -> str:
    if state.age_filter_active:
        return f"Возраст: {state.age_min}-{state.age_max}"
    return "Возраст: —"


def _on_off(value: bool) -> str:
    return "ON" if value else "OFF"


class TgBot:
    """Reply-keyboard UI for the operator."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.admin_ids: set[int] = set()  # populated by bootstrap_admins()
        self._client: Optional[TelegramClient] = None
        self._username: Optional[str] = None
        # user_id → mode: 'await_age' | 'await_letter'
        self._pending_input: dict[int, str] = {}
        # last profile_id we already notified about — to dedupe sends
        self._last_profile_id: Optional[int] = None
        self._last_status: str = ""

    # ─── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        bot_session = ROOT / "leodv_bot"
        client = TelegramClient(str(bot_session), API_ID, API_HASH)
        await client.start(bot_token=self.token)
        self._client = client
        me = await client.get_me()
        self._username = me.username
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self._on_callback, events.CallbackQuery)
        log.info("Telegram bot started: @%s (id=%s)", me.username, me.id)

    async def bootstrap_admins(self, phones: list[str]) -> None:
        """Для каждого TG_PHONES: коротко поднять юзер-клиент, выяснить user_id
        через get_me(), отправить /start боту (чтобы бот занёс entity в свою
        sqlite-сессию). Без этого бот не сможет ничего им отправить."""
        if not self._client or not self._username:
            return
        from app import tg as user_tg  # активный клиент уже подключён там

        bot_username = self._username
        for idx, phone in enumerate(phones):
            sf = Path(str(session_path(phone)) + ".session")
            if not sf.exists():
                log.warning("Bootstrap: нет session для %s — пропуск", phone)
                continue

            # Если этот phone — текущий активный, используем уже подключённый
            # клиент. Иначе временный.
            owns_temp = False
            c: Optional[TelegramClient] = None
            try:
                if (
                    0 <= idx < len(user_tg._clients)
                    and idx == user_tg._current_idx
                    and user_tg._clients[idx].is_connected()
                ):
                    c = user_tg._clients[idx]
                else:
                    c = TelegramClient(str(session_path(phone)), API_ID, API_HASH)
                    await c.connect()
                    owns_temp = True
                if not await c.is_user_authorized():
                    log.warning("Bootstrap: %s не авторизован — пропуск", phone)
                    continue
                me = await c.get_me()
                self.admin_ids.add(me.id)
                await c.send_message(bot_username, "/start")
                log.info("Bootstrap: %s (id=%s) → /start @%s", phone, me.id, bot_username)
            except Exception:
                log.exception("Bootstrap не сработал для %s", phone)
            finally:
                if owns_temp and c is not None:
                    try:
                        await c.disconnect()
                    except Exception:
                        pass

        # Даём боту секунду-другую обработать пришедшие /start, чтобы entity
        # попали в его кеш до первой попытки send_file.
        await asyncio.sleep(2)
        log.info("Bot admin_ids после bootstrap: %s", sorted(self.admin_ids))
        if self.admin_ids:
            await self._broadcast("✅ Бот запущен. Используй клавиатуру внизу.")

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("Bot disconnect failed")
            self._client = None

    # ─── keyboard ───────────────────────────────────────────────────────

    def _keyboard(self) -> list[list[Button]]:
        return [
            [
                _btn(f"Авто-лайк: {_on_off(state.auto_like_mode)}"),
                _btn(f"Лимит авто-лайков: {state.auto_like_soft_limit}"),
            ],
            [
                _btn(f"Авто-дизлайк: {_on_off(state.auto_dislike_mode)}"),
                _btn(f"Лимит авто-дизлайков: {state.auto_dislike_soft_limit}"),
            ],
            [
                _btn(f"Только новые: {_on_off(state.only_new_mode)}"),
                _btn(f"Авто-смена акков: {_on_off(state.auto_rotate_mode)}"),
            ],
            [_btn(_format_age())],
            [_btn("🔄 Переключить аккаунт"), _btn("ℹ️ Статус")],
            [_btn("🛑 Стоп")],
        ]

    # ─── outbound helpers ───────────────────────────────────────────────

    async def _broadcast(self, text: str, **kwargs) -> None:
        """Send a text message to every admin with the current keyboard."""
        if not self._client:
            return
        kb = self._keyboard()
        # Snapshot — bootstrap_admins может дозаписывать admin_ids в фоне, и
        # await внутри цикла отдаёт control, что без снапшота даёт
        # "Set changed size during iteration".
        for uid in tuple(self.admin_ids):
            try:
                await self._client.send_message(uid, text, buttons=kb, **kwargs)
            except Exception:
                log.exception("Failed to send text to admin %s", uid)

    async def _send_to(self, uid: int, text: str) -> None:
        if not self._client:
            return
        try:
            await self._client.send_message(uid, text, buttons=self._keyboard())
        except Exception:
            log.exception("Failed to send to %s", uid)

    # ─── public notifications (called from app/profile.py) ──────────────

    async def notify_profile(self) -> None:
        """Send the freshly-shown profile (photo + description) to admins."""
        if not self._client:
            return
        p = state.current_profile
        if not p:
            return
        pid = p.get("id")
        if pid == self._last_profile_id:
            return
        self._last_profile_id = pid

        desc = p.get("description") or "(без описания)"
        seen = p.get("seen_count", 1)
        header = "⚠️ ПРИОРИТЕТНАЯ АНКЕТА!\n\n" if state.priority_alert else ""
        caption = f"{header}id={pid} · встречалась {seen} раз\n\n{desc}"

        media_paths: list[str] = []
        for m in p.get("media", []):
            url = m.get("url", "")
            # url like "/media/{id}/{name}"
            local = ROOT / url.lstrip("/")
            if local.exists():
                media_paths.append(str(local))

        # Inline-кнопки под анкетой. Тонкость: Telegram НЕ поддерживает кнопки
        # на альбомах (group of media) — Telethon в этом случае их молча
        # игнорирует. Поэтому если медиа > 1 — сначала шлём альбом без кнопок,
        # а кнопки + caption уходят отдельным follow-up сообщением.
        inline_kb = [[
            Button.inline("❤️ Лайк", b"like"),
            Button.inline("👎 Дизлайк", b"dislike"),
            Button.inline("💌 Сообщение", b"letter"),
        ]]
        for uid in tuple(self.admin_ids):
            try:
                if len(media_paths) > 1:
                    await self._client.send_file(uid, media_paths)
                    await self._client.send_message(uid, caption, buttons=inline_kb)
                elif len(media_paths) == 1:
                    await self._client.send_file(
                        uid, media_paths[0], caption=caption, buttons=inline_kb,
                    )
                else:
                    await self._client.send_message(uid, caption, buttons=inline_kb)
            except Exception:
                log.exception("Failed to send profile to admin %s", uid)
        # Сбрасываем dedup статусов: следующий warning после новой анкеты
        # должен дойти, даже если текст совпадёт с предыдущим.
        self._last_status = ""

    async def notify_status(self, text: str) -> None:
        """Send a status message (used for warnings, rotation progress, etc)."""
        if not text or text == self._last_status:
            return
        self._last_status = text
        await self._broadcast(text)

    async def notify_auto_like(self, profile: dict) -> None:
        """Send auto-liked profile: media + caption with inline rating buttons (1–10).

        profile_id зашит в callback data каждой кнопки — оценка работает
        в любое время, даже если после прислали ещё сотню авто-лайков.
        Reply-клавиатура в сообщении не выставляется: Telegram сохраняет
        последнюю reply-клавиатуру бота, поэтому кнопки управления остаются.
        """
        if not self._client:
            return
        pid = profile.get("id")
        desc = profile.get("description") or "(без описания)"
        seen = profile.get("seen_count", 1)
        caption = f"❤️ Авто-лайк · id={pid} · встречалась {seen} раз\n\n{desc}"

        media_paths: list[str] = []
        for m in profile.get("media", []):
            url = m.get("url", "")
            local = ROOT / url.lstrip("/")
            if local.exists():
                media_paths.append(str(local))

        from app import db
        row = db.get_profile(pid) if pid else None
        rating_kb = _rating_inline_kb(pid, row["rating"] if row else -1)

        for uid in tuple(self.admin_ids):
            try:
                if len(media_paths) > 1:
                    await self._client.send_file(uid, media_paths)
                    await self._client.send_message(uid, caption, buttons=rating_kb)
                elif len(media_paths) == 1:
                    await self._client.send_file(
                        uid, media_paths[0], caption=caption, buttons=rating_kb,
                    )
                else:
                    await self._client.send_message(uid, caption, buttons=rating_kb)
            except Exception:
                log.exception("Failed to send auto-like profile to admin %s", uid)

    async def notify_status_dump(self) -> None:
        """Send the full stats dump to all admins (used before shutdown)."""
        for uid in tuple(self.admin_ids):
            try:
                await self._send_status_dump(uid)
            except Exception:
                log.exception("Failed to send status dump to admin %s", uid)

    async def notify_shutdown(self, text: str) -> None:
        """Force-send a critical message, bypassing the dedup filter."""
        await self.notify_status_dump()
        self._last_status = ""
        await self.notify_status(text)

    async def notify_keyboard(self) -> None:
        """Send a short message to refresh the displayed keyboard after a state change."""
        await self._broadcast("⌨️")

    def reset_profile_dedupe(self) -> None:
        """Clear the per-bot 'last sent profile' memo (call when current_profile clears)."""
        self._last_profile_id = None

    # ─── inbound dispatch ───────────────────────────────────────────────

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        uid = event.sender_id
        if uid not in self.admin_ids:
            try:
                await event.respond(
                    "Доступ запрещён. Только аккаунты из TG_PHONES могут "
                    "управлять ботом."
                )
            except Exception:
                pass
            return

        text = (event.text or "").strip()

        # — continuation of multi-step inputs —
        pending = self._pending_input.pop(uid, None)
        if pending == "await_age":
            await self._handle_age_input(uid, text)
            return
        if pending == "await_letter":
            await self._send_letter(text)
            return
        if pending == "await_like_limit":
            await self._handle_like_limit_input(uid, text)
            return
        if pending == "await_dislike_limit":
            await self._handle_dislike_limit_input(uid, text)
            return

        # — bare commands —
        if text in ("/start", "/help"):
            await self._send_to(uid, "Привет! Используй клавиатуру внизу. ℹ️ Статус покажет текущее состояние.")
            return

        # — button presses (match by prefix because labels include state) —
        if text.startswith("Авто-лайк"):
            await self._toggle_auto_like()
        elif text.startswith("Лимит авто-лайков"):
            self._pending_input[uid] = "await_like_limit"
            await self._send_to(uid, f"✏️ Текущий лимит авто-лайков: {state.auto_like_soft_limit}\nПришли новое число (например `30`).")
        elif text.startswith("Авто-дизлайк"):
            await self._toggle_auto_dislike()
        elif text.startswith("Лимит авто-дизлайков"):
            self._pending_input[uid] = "await_dislike_limit"
            await self._send_to(uid, f"✏️ Текущий лимит авто-дизлайков: {state.auto_dislike_soft_limit}\nПришли новое число (например `1400`).")
        elif text.startswith("Только новые"):
            await self._toggle_only_new()
        elif text.startswith("Авто-смена акков"):
            await self._toggle_auto_rotate()
        elif text.startswith("Возраст"):
            self._pending_input[uid] = "await_age"
            await self._send_to(uid, "✏️ Пришли диапазон в формате `18-25` или `off`.")
        elif text.startswith("🔄"):
            await self._switch_account()
        elif text.startswith("🛑 Стоп"):
            await self._stop_app()
        elif text.startswith("ℹ️ Статус"):
            await self._send_status_dump(uid)
        else:
            await self._send_to(uid, "Не понял команду. Используй кнопки клавиатуры.")

    # ─── handlers ──────────────────────────────────────────────────────

    async def _react(self, emoji: str) -> None:
        from app import settings, stats, tg
        async with state.lock:
            if state.warning or state.current_profile is None:
                await self._broadcast("Нет активной анкеты — нечего отправлять.")
                return
            state.busy = True
        try:
            await tg.send_reaction(emoji)
        finally:
            active_phone = tg._phones[tg._current_idx] if tg._phones else ""
            async with state.lock:
                state.current_profile = None
                state.priority_alert = False
                state.letter_pending = False
                if emoji == "❤️":
                    state.like_count += 1
                    stats.bump(active_phone, "likes")
                elif emoji == "👎":
                    state.dislike_count += 1
                    stats.bump(active_phone, "dislikes")
                state.busy = False
            settings.save()
            self._last_profile_id = None
        if tg.secondary_idx() is not None:
            asyncio.create_task(tg.stop_secondary())
        await self._broadcast(f"✅ Отправлено: {emoji}")

    async def _send_letter(self, text: str) -> None:
        from app.profile import _deferred_letter
        async with state.lock:
            if state.warning or state.current_profile is None:
                await self._broadcast("Нет активной анкеты — нечего слать письмо.")
                return
        asyncio.create_task(_deferred_letter(text))
        await self._broadcast("💌 Отправляю письмо…")

    async def _toggle_auto_like(self) -> None:
        from app import settings
        async with state.lock:
            state.auto_like_mode = not state.auto_like_mode
            if state.auto_like_mode:
                state.auto_dislike_mode = False
            settings.save()
        await self.notify_keyboard()

    async def _toggle_auto_dislike(self) -> None:
        from app import settings
        async with state.lock:
            state.auto_dislike_mode = not state.auto_dislike_mode
            if state.auto_dislike_mode:
                state.auto_like_mode = False
            settings.save()
        await self.notify_keyboard()

    async def _toggle_only_new(self) -> None:
        from app import settings
        async with state.lock:
            state.only_new_mode = not state.only_new_mode
            if state.only_new_mode:
                state.auto_dislike_count = 0
            settings.save()
        await self.notify_keyboard()

    async def _toggle_auto_rotate(self) -> None:
        from app import settings
        async with state.lock:
            state.auto_rotate_mode = not state.auto_rotate_mode
            settings.save()
        await self.notify_keyboard()

    async def _stop_app(self) -> None:
        await self._broadcast("🛑 Останавливаю программу…")
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    async def notify_rotate_start(self, old_phone: str, old_idx: int) -> None:
        """Send rotation-start message and watch for result (called from auto-rotate)."""
        await self._broadcast(f"🔄 Смена аккаунта: {old_phone} → …")
        asyncio.create_task(self._await_rotation_result(old_phone, old_idx))

    async def _switch_account(self) -> None:
        from app import tg as user_tg
        from app.profile import _deferred_rotate
        old_phone = (
            user_tg._phones[user_tg._current_idx] if user_tg._phones else "?"
        )
        old_idx = user_tg._current_idx
        await self._broadcast(f"🔄 Смена аккаунта: {old_phone} → …")
        asyncio.create_task(_deferred_rotate())
        # Параллельно ждём, когда ротация реально завершится. Эндпоинт сам по
        # себе не пуляет уведомлений; следим за изменением _current_idx или
        # появлением status_message «Лимиты на всех аккаунтах…».
        asyncio.create_task(self._await_rotation_result(old_phone, old_idx))

    async def _await_rotation_result(self, old_phone: str, old_idx: int) -> None:
        from app import tg as user_tg
        deadline = asyncio.get_event_loop().time() + 60.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            # успешная ротация: _current_idx сменился
            if user_tg._current_idx != old_idx:
                new_phone = (
                    user_tg._phones[user_tg._current_idx]
                    if user_tg._phones else "?"
                )
                await self._broadcast(f"✅ Аккаунт сменён: {old_phone} → {new_phone}")
                return
            # все аккаунты исчерпаны: status_message выставлен в эндпоинте
            if state.status_message and "Лимиты" in state.status_message:
                await self._broadcast(f"⛔️ {state.status_message}")
                return
        await self._broadcast(
            "⚠️ Смена аккаунта не подтверждена за 60 с. "
            "Проверь логи uvicorn."
        )

    async def _handle_age_input(self, uid: int, text: str) -> None:
        from app import settings
        t = text.strip().lower().replace("—", "-")
        if t in ("off", "выкл", "-", "—"):
            async with state.lock:
                state.age_min = None
                state.age_max = None
                settings.save()
            await self.notify_keyboard()
            return
        parts = [p.strip() for p in t.split("-") if p.strip()]
        if len(parts) != 2:
            await self._send_to(uid, "Не понял. Формат: `18-25` или `off`.")
            self._pending_input[uid] = "await_age"
            return
        try:
            mn, mx = int(parts[0]), int(parts[1])
        except ValueError:
            await self._send_to(uid, "Нужны целые числа. Формат: `18-25` или `off`.")
            self._pending_input[uid] = "await_age"
            return
        if mn < 0 or mx < 0 or mn > mx:
            await self._send_to(uid, "Возраст: 0 ≤ min ≤ max.")
            self._pending_input[uid] = "await_age"
            return
        async with state.lock:
            state.age_min = mn
            state.age_max = mx
            settings.save()
        await self.notify_keyboard()

    async def _handle_like_limit_input(self, uid: int, text: str) -> None:
        from app import settings
        try:
            val = int(text.strip())
            if val <= 0:
                raise ValueError
        except ValueError:
            await self._send_to(uid, "Нужно целое положительное число. Попробуй ещё раз.")
            self._pending_input[uid] = "await_like_limit"
            return
        async with state.lock:
            state.auto_like_soft_limit = val
            settings.save()
        await self.notify_keyboard()

    async def _handle_dislike_limit_input(self, uid: int, text: str) -> None:
        from app import settings
        try:
            val = int(text.strip())
            if val <= 0:
                raise ValueError
        except ValueError:
            await self._send_to(uid, "Нужно целое положительное число. Попробуй ещё раз.")
            self._pending_input[uid] = "await_dislike_limit"
            return
        async with state.lock:
            state.auto_dislike_soft_limit = val
            settings.save()
        await self.notify_keyboard()

    # ─── inline-button callback ─────────────────────────────────────────

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        uid = event.sender_id
        if uid not in self.admin_ids:
            try:
                await event.answer("Доступ запрещён.", alert=True)
            except Exception:
                pass
            return
        data = event.data or b""
        try:
            if data == b"like":
                await self._react("❤️")
                try:
                    await event.edit("✅ Лайк отправлен")
                except Exception:
                    pass
            elif data == b"dislike":
                await self._react("👎")
                try:
                    await event.edit("✅ Дизлайк отправлен")
                except Exception:
                    pass
            elif data == b"letter":
                async with state.lock:
                    if state.warning or state.current_profile is None:
                        await event.answer("Нет активной анкеты.", alert=True)
                        return
                self._pending_input[uid] = "await_letter"
                try:
                    await event.edit("💌 Жду текст одним сообщением…")
                except Exception:
                    pass
            elif data.startswith(b"rate:"):
                try:
                    _, rating_str, pid_str = data.decode().split(":")
                    rating_val = int(rating_str)
                    pid = int(pid_str)
                except (ValueError, AttributeError):
                    await event.answer("Ошибка формата.", alert=True)
                    return
                from app import db
                db.set_rating(pid, rating_val)
                await event.answer(f"★ {rating_val}/10 — сохранено")
                try:
                    await event.edit(buttons=None)
                except Exception:
                    pass
            else:
                await event.answer("Неизвестно.", alert=True)
        except Exception:
            log.exception("Callback handler failed")
            try:
                await event.answer("Ошибка.", alert=True)
            except Exception:
                pass

    async def _send_status_dump(self, uid: int) -> None:
        from app import stats, tg
        snap = state.snapshot()
        active = tg._phones[tg._current_idx] if tg._phones else "—"
        head = (
            f"📊 Статус\n"
            f"Аккаунт: {active}\n"
            f"Авто-лайк: {_on_off(state.auto_like_mode)} (лимит: {state.auto_like_soft_limit})\n"
            f"Авто-дизлайк: {_on_off(state.auto_dislike_mode)} (лимит: {state.auto_dislike_soft_limit})\n"
            f"Только новые: {_on_off(state.only_new_mode)}\n"
            f"Авто-смена акков: {_on_off(state.auto_rotate_mode)}\n"
            f"{_format_age()}\n"
            f"Статус: {snap.get('status_message') or '—'}\n"
            f"Warning: {snap.get('warning')}"
        )

        s = stats.summary()
        since = s.get("since", "—")
        per = s.get("per_account", {})
        totals = s.get("totals", {})

        # Шапка таблицы — короткие колонки, чтобы помещалось в моноширинный блок
        lines = [
            f"\n\n📈 Stats с {since[:16] if since else '—'}",
            "<pre>",
            f"{'аккаунт':<14}{'❤️':>4}{'👎':>5}{'aL':>5}{'aD':>5}{'new':>6}",
        ]
        for phone, ast in sorted(per.items()):
            short = phone if len(phone) <= 14 else phone[-13:]
            lines.append(
                f"{short:<14}"
                f"{ast.get('likes',0):>4}"
                f"{ast.get('dislikes',0):>5}"
                f"{ast.get('auto_likes',0):>5}"
                f"{ast.get('auto_dislikes',0):>5}"
                f"{ast.get('new_profiles',0):>6}"
            )
        lines.append(
            f"{'ИТОГО':<14}"
            f"{totals.get('likes',0):>4}"
            f"{totals.get('dislikes',0):>5}"
            f"{totals.get('auto_likes',0):>5}"
            f"{totals.get('auto_dislikes',0):>5}"
            f"{totals.get('new_profiles',0):>6}"
        )
        lines.append("</pre>")
        lines.append("aL=авто-лайк · aD=авто-дизлайк · new=новых в БД")

        await self._client.send_message(
            uid, head + "\n".join(lines), buttons=self._keyboard(), parse_mode="html",
        )


# ─── module-level singleton (so app.profile can find the bot) ──────────

_bot: Optional[TgBot] = None


def init(token: str) -> TgBot:
    global _bot
    _bot = TgBot(token)
    return _bot


def get_bot() -> Optional[TgBot]:
    return _bot
