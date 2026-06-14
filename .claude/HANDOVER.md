# HANDOVER — leodv

Дата последнего обновления: 2026-06-14

---

## Что это такое

FastAPI-приложение для автоматизации просмотра анкет в `@leomatchbot` (Telegram).
Работает через Telethon (MTProto, user-account, не Bot API).

Стек: FastAPI + uvicorn, Telethon, SQLite (raw sqlite3), in-memory дедупликация.

---

## Архитектура

```
@leomatchbot
     │ NewMessage events (Telethon)
     ▼
app/tg.py          ← единственный Telethon-слой; альбомы буферизуются 1.2 с
     │
     ▼
app/profile.py     ← пайплайн: дедуп → авто-фильтры → UI / авто-реакция
     │
     ├── app/db.py       ← raw sqlite3, таблица profiles
     ├── app/dedup.py    ← in-memory _by_desc / _by_hash, строится при старте
     ├── app/stats.py    ← per-account дневная статистика (JSON, сброс в 05:00)
     ├── app/limits.py   ← per-account лимиты (JSON, TTL 8 ч)
     └── app/settings.py ← персистентность AppState → app_settings.json

app/main.py        ← FastAPI lifespan, HTTP API, watchdog (30 с)
app/state.py       ← AppState dataclass, единственный asyncio.Lock
tg_bot/bot.py      ← зеркало веб-интерфейса через Telegram reply-клавиатуру
```

---

## Порядок фильтров (строгий, нельзя менять без теста)

1. **Лимит-сообщение от бота** («Слишком много… за сегодня») → ротация аккаунта
2. **Нет медиа (не-анкета)** → авто-ротация на аккаунт с анкетой (если `auto_rotate_mode`)
3. **auto_skip** (`app/auto_skip.json`) → 👎, регистронезависимо
4. **highest_priority** (`app/highest_priority.json`) → показать + запустить фоновый аккаунт
5. **auto_dislike / only_new / age_filter** → 👎
6. **auto_like** → ❤️
7. **Показать пользователю**

---

## Ключевые поведения

### Мягкие лимиты (soft limits)

Хранятся в `state.auto_dislike_soft_limit` (default 1400) и `state.auto_like_soft_limit` (default 30).
Персистируются в `app_settings.json`. Меняются через TG-бот (кнопки «Лимит авто-лайков / авто-дизлайков»).

При срабатывании: `_trigger_soft_limit()` → `limits.mark_limit(phone)` → ротация (если `auto_rotate_mode`).

### Авто-ротация при не-анкете

`_deferred_rotate_on_non_profile()` вызывает `tg.find_and_rotate_to_profile_account()`:
- временно подключается к каждому другому аккаунту (connect → iter_messages → disconnect)
- если находит аккаунт с анкетой → делает его активным, отправляет «1»
- если ни у кого нет анкеты → `state.warning = True`

### Фоновый (secondary) аккаунт при приоритетной анкете

При показе приоритетной анкеты → `_start_secondary_processing()`:
- находит первый незалимиченный аккаунт (кроме текущего)
- `tg.start_secondary(idx, _handle_secondary_messages)` — подключает его как второй клиент с отдельным event handler'ом и album buffers
- отправляет «1» для запуска
- `_handle_secondary_messages` обрабатывает анкеты: авто-дизлайк/авто-лайк, dedup, stats, archive — но **НЕ** обновляет `state.current_profile`
- останавливается сам при: лимите, не-анкете, другой приоритетной анкете, мягком лимите

Остановка secondary при действии пользователя:
- `main.py::_react()` → `asyncio.create_task(tg.stop_secondary())`
- `bot.py::_react()` → то же
- `profile.py::_deferred_letter()` → `await tg.stop_secondary()`

### Скачивание медиа

`tg.download_media()` оборачивает `message.download_media()` в try/except — при ошибке возвращает `None` вместо исключения. Если все медиа не скачались → `_process` выставляет `warning=True`, не крашится.

---

## База данных

Файл: `data.db` (SQLite). Таблица `profiles`:

| Колонка | Тип | Примечание |
|---|---|---|
| id | INTEGER PK | |
| description | TEXT | полный текст |
| first_media_hash | TEXT | sha256 первого фото или ffmpeg-хеш первого кадра видео |
| seen_count | INTEGER | сколько раз пришла от бота |
| first_seen_at / last_seen_at | TEXT | ISO timestamps |
| registered_at | INTEGER | unix timestamp |
| rating | INTEGER | 1–10, -1 = не оценена |

Миграции через `ALTER TABLE ADD COLUMN` прямо в `db.init()` — Alembic не используется на main.

---

## Персистентные файлы (не в git)

| Файл | Что хранит |
|---|---|
| `app_settings.json` | режимы, мягкие лимиты, счётчики |
| `app_stats.json` | дневная статистика per-account |
| `account_limits.json` | записи о лимитах, TTL 8 ч |
| `data.db` | SQLite база анкет |
| `data_backup.db` | автобэкап при старте и при исчерпании всех лимитов |
| `leodv_*.session` | Telethon-сессии аккаунтов |
| `leodv_bot.session` | Telethon-сессия TG-бота |
| `media/_archive/{id}/` | постоянный архив медиа по profile_id |

---

## Конфигурируемые параметры

Через `app_settings.json` (или TG-бот):

| Параметр | По умолчанию | Описание |
|---|---|---|
| `auto_dislike_soft_limit` | 1400 | порог авто-дизлайков для смены аккаунта |
| `auto_like_soft_limit` | 30 | порог авто-лайков для смены аккаунта |
| `only_new_mode` | false | дизлайкать дубли |
| `auto_dislike_mode` | false | дизлайкать всё автоматически |
| `auto_like_mode` | false | лайкать всё автоматически |
| `auto_rotate_mode` | false | авто-смена аккаунта при лимите / не-анкете |
| `age_min` / `age_max` | null | фильтр возраста |

---

## Ветки и незавершённая работа

**`feature/fileid`** — реализован третий dedup-сигнал по MTProto media ID (`photo.id` / `document.id`), Alembic-миграция для колонки `file_unique_id`, `metadata.json` в архиве.
Ветка не слита — ждёт подтверждения стабильности file_id через скрипт `scripts/analyze_fuid.py`.

**`scripts/analyze_fuid.py`** — парсит `metadata.json` в архиве, сравнивает tg_media_ids между повторными показами анкеты. Запускать когда наберётся `encounter_count >= 3` у нескольких анкет.

---

## Что НЕ трогать

- `db.init()` применяет инкрементальные ALTER TABLE — не ломать и не дублировать
- `dedup.init_indexes()` вызывается ровно один раз при старте через `lifespan`
- `state.lock` — все мутации AppState строго под ним; secondary handler работает **вне** lock намеренно (asyncio single-thread, нет race condition)
- `tg._current_idx` — менять только через `rotate_account()` или `find_and_rotate_to_profile_account()`; менять напрямую можно только внутри `tg.py`
