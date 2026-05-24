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
from pathlib import Path
from typing import Optional

from telethon import Button, TelegramClient, events

from app.config import API_HASH, API_ID, ROOT, session_path
from app.state import state

log = logging.getLogger("leodv.tg_bot")


def _btn(text: str) -> Button:
    """Reply-keyboard button (NOT inline)."""
    return Button.text(text, resize=True)


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
            [_btn("❤️ Лайк"), _btn("👎 Дизлайк")],
            [_btn("💌 / 📹 авто"), _btn("💌 / 📹 своё")],
            [
                _btn(f"Авто-лайк: {_on_off(state.auto_like_mode)}"),
                _btn(f"Авто-дизлайк: {_on_off(state.auto_dislike_mode)}"),
            ],
            [_btn(f"Только новые: {_on_off(state.only_new_mode)}")],
            [_btn(_format_age())],
            [_btn("🔄 Переключить аккаунт"), _btn("ℹ️ Статус")],
        ]

    # ─── outbound helpers ───────────────────────────────────────────────

    async def _broadcast(self, text: str, **kwargs) -> None:
        """Send a text message to every admin with the current keyboard."""
        if not self._client:
            return
        kb = self._keyboard()
        for uid in self.admin_ids:
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

        kb = self._keyboard()
        for uid in self.admin_ids:
            try:
                if media_paths:
                    await self._client.send_file(
                        uid, media_paths, caption=caption, buttons=kb,
                    )
                else:
                    await self._client.send_message(uid, caption, buttons=kb)
            except Exception:
                log.exception("Failed to send profile to admin %s", uid)

    async def notify_status(self, text: str) -> None:
        """Send a status message (used for warnings, rotation progress, etc)."""
        if not text or text == self._last_status:
            return
        self._last_status = text
        await self._broadcast(text)

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
            await self._send_letter(custom_text=text, auto=False)
            return

        # — bare commands —
        if text in ("/start", "/help"):
            await self._send_to(uid, "Привет! Используй клавиатуру внизу. ℹ️ Статус покажет текущее состояние.")
            return

        # — button presses (match by prefix because labels include state) —
        if text.startswith("❤️ Лайк"):
            await self._react("❤️")
        elif text.startswith("👎 Дизлайк"):
            await self._react("👎")
        elif text.startswith("💌 / 📹 авто"):
            await self._send_letter(custom_text="", auto=True)
        elif text.startswith("💌 / 📹 своё"):
            self._pending_input[uid] = "await_letter"
            await self._send_to(uid, "✏️ Пришли текст одним сообщением — отправлю его после 💌 / 📹.")
        elif text.startswith("Авто-лайк"):
            await self._toggle_auto_like()
        elif text.startswith("Авто-дизлайк"):
            await self._toggle_auto_dislike()
        elif text.startswith("Только новые"):
            await self._toggle_only_new()
        elif text.startswith("Возраст"):
            self._pending_input[uid] = "await_age"
            await self._send_to(uid, "✏️ Пришли диапазон в формате `18-25` или `off`.")
        elif text.startswith("🔄 Переключить аккаунт"):
            await self._switch_account()
        elif text.startswith("ℹ️ Статус"):
            await self._send_status_dump(uid)
        else:
            await self._send_to(uid, "Не понял команду. Используй кнопки клавиатуры.")

    # ─── handlers ──────────────────────────────────────────────────────

    async def _react(self, emoji: str) -> None:
        from app import tg
        async with state.lock:
            if state.warning or state.current_profile is None:
                await self._broadcast("Нет активной анкеты — нечего отправлять.")
                return
            state.busy = True
        try:
            await tg.send_reaction(emoji)
        finally:
            async with state.lock:
                state.current_profile = None
                state.priority_alert = False
                state.letter_pending = False
                if emoji == "❤️":
                    state.like_count += 1
                elif emoji == "👎":
                    state.dislike_count += 1
                state.busy = False
            self._last_profile_id = None
        await self._broadcast(f"✅ Отправлено: {emoji}")

    async def _send_letter(self, custom_text: str, auto: bool) -> None:
        from app.profile import _deferred_letter
        async with state.lock:
            if state.warning or state.current_profile is None:
                await self._broadcast("Нет активной анкеты — нечего слать письмо.")
                return
            description = state.current_profile.get("description", "")
        asyncio.create_task(_deferred_letter(description, custom_text, auto))
        suffix = " (авто)" if auto or not custom_text.strip() else ""
        await self._broadcast(f"💌 Отправляю письмо{suffix}…")

    async def _toggle_auto_like(self) -> None:
        async with state.lock:
            state.auto_like_mode = not state.auto_like_mode
            if state.auto_like_mode:
                state.auto_dislike_mode = False
        await self.notify_keyboard()

    async def _toggle_auto_dislike(self) -> None:
        async with state.lock:
            state.auto_dislike_mode = not state.auto_dislike_mode
            if state.auto_dislike_mode:
                state.auto_like_mode = False
        await self.notify_keyboard()

    async def _toggle_only_new(self) -> None:
        async with state.lock:
            state.only_new_mode = not state.only_new_mode
            if state.only_new_mode:
                state.auto_dislike_count = 0
        await self.notify_keyboard()

    async def _switch_account(self) -> None:
        from app.profile import _deferred_rotate
        asyncio.create_task(_deferred_rotate())
        await self._broadcast("🔄 Переключаю аккаунт…")

    async def _handle_age_input(self, uid: int, text: str) -> None:
        t = text.strip().lower().replace("—", "-")
        if t in ("off", "выкл", "-", "—"):
            async with state.lock:
                state.age_min = None
                state.age_max = None
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
        await self.notify_keyboard()

    async def _send_status_dump(self, uid: int) -> None:
        from app import tg
        snap = state.snapshot()
        active = tg._phones[tg._current_idx] if tg._phones else "—"
        msg = (
            f"📊 Статус\n"
            f"Аккаунт: {active}\n"
            f"Авто-лайк: {_on_off(state.auto_like_mode)}\n"
            f"Авто-дизлайк: {_on_off(state.auto_dislike_mode)}\n"
            f"Только новые: {_on_off(state.only_new_mode)}\n"
            f"{_format_age()}\n"
            f"❤️ {state.like_count} · 👎 {state.dislike_count} · "
            f"авто 👎 {state.auto_dislike_count}\n"
            f"Статус: {snap.get('status_message') or '—'}\n"
            f"Warning: {snap.get('warning')}"
        )
        await self._send_to(uid, msg)


# ─── module-level singleton (so app.profile can find the bot) ──────────

_bot: Optional[TgBot] = None


def init(token: str) -> TgBot:
    global _bot
    _bot = TgBot(token)
    return _bot


def get_bot() -> Optional[TgBot]:
    return _bot
