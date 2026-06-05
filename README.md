# leodv

Автоматизация просмотра анкет в `@leomatchbot` (Telegram). Дедупликация, авто-режимы, ротация аккаунтов, два интерфейса управления (веб + Telegram-бот), галерея анкет с оценками.

---

## Быстрый старт

```bash
# 1. Окружение
uv venv venv/
source venv/bin/activate
uv pip install -r requirements.txt

# 2. Конфиг
cp .env.example .env
# отредактировать .env (см. ниже)

# 3. Авторизация Telethon-аккаунтов (один раз)
python3 login.py

# 4. Запуск
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Это **единственная команда** — в одном процессе поднимаются Telethon-клиенты, FastAPI, и Telegram-бот (если задан `TG_BOT_TOKEN`).

Веб-интерфейс: **http://127.0.0.1:8000**

### `.env`

```ini
TG_API_ID=…           # https://my.telegram.org/apps
TG_API_HASH=…
TG_PHONES=+79991234567,+79998765432   # через запятую, порядок = порядок ротации
SESSION_NAME=leodv    # префикс для .session-файлов (опционально)
BOT_USERNAME=leomatchbot              # опционально
TG_BOT_TOKEN=         # Telegram-бот UI — опционально, оставить пустым чтобы выключить
```

### Зависимости

Python 3.13. Основные пакеты: `fastapi`, `uvicorn[standard]`, `telethon`, `sqlalchemy` (только в скриптах), `imageio-ffmpeg` (бандлит ffmpeg-бинарник для хеширования видео).

---

## Структура проекта

```
leodv/
├── app/
│   ├── main.py          # FastAPI + lifespan (точка входа)
│   ├── config.py        # все пути и env-переменные
│   ├── state.py         # AppState — единый in-memory стейт
│   ├── db.py            # SQLite (raw sqlite3)
│   ├── dedup.py         # in-memory дедупликация
│   ├── profile.py       # пайплайн обработки анкет
│   ├── tg.py            # Telethon-клиенты, ротация аккаунтов
│   ├── hashing.py       # sha256 файлов + ffmpeg-хеш первого кадра видео
│   ├── limits.py        # трекер дневных лимитов per-аккаунт
│   ├── settings.py      # JSON-персистентность настроек AppState
│   ├── stats.py         # дневная статистика per-аккаунт
│   ├── auto_skip.json   # подстроки описаний для авто-дизлайка
│   └── highest_priority.json  # подстроки для приоритетных анкет
├── tg_bot/
│   └── bot.py           # Telegram-бот с reply-клавиатурой
├── static/
│   ├── index.html       # главный экран (текущая анкета)
│   ├── gallery.html     # галерея всех анкет с сортировкой/фильтрами
│   ├── rating.html      # страница оценки конкретной анкеты
│   ├── app.js           # логика главного экрана
│   └── style.css        # общие стили
├── scripts/
│   ├── extend_db.py     # заливка истории чата в БД (разовая операция)
│   ├── find_duplicates.py
│   └── claude_rest.py
├── media/               # медиафайлы (gitignore)
│   ├── _pending/        # временная папка загрузки
│   └── _archive/        # постоянный архив первых фото по profile_id
│       └── {id}/
├── data.db              # SQLite БД
├── data_backup.db       # бэкап БД (создаётся при каждом старте)
├── app_settings.json    # персистентные настройки (режимы, фильтры)
├── app_stats.json       # дневная статистика
├── account_limits.json  # запись о лимитах аккаунтов
├── login.py             # скрипт авторизации Telethon
└── requirements.txt
```

---

## База данных (`data.db`)

Одна таблица `profiles`:

| Колонка | Тип | Описание |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `description` | TEXT NOT NULL | полный текст описания анкеты |
| `first_media_hash` | TEXT NOT NULL | sha256 первого фото или ffmpeg-хеш первого кадра видео |
| `seen_count` | INTEGER DEFAULT 1 | сколько раз анкета пришла от бота |
| `first_seen_at` | TEXT | `datetime('now')` при INSERT |
| `last_seen_at` | TEXT | обновляется при каждом `bump_seen` |
| `registered_at` | INTEGER | unix-timestamp первого появления (для новых записей — `int(time.time())`) |
| `rating` | INTEGER DEFAULT -1 | оценка пользователя 1–10, -1 = не оценена |
| UNIQUE | `(description, first_media_hash)` | |

`db.init()` запускает миграции через `ALTER TABLE ... ADD COLUMN` при каждом старте — БД никогда не дропается. Все новые колонки добавляются с обратно-совместимым DEFAULT.

---

## Медиа-архив

При каждой новой или повторно встреченной анкете все скачанные файлы **копируются** в `media/_archive/{profile_id}/`. Функция идемпотентна — уже существующие файлы пропускаются. Этот каталог **никогда не очищается** `_purge_media`.

Текущая анкета хранится в `media/{profile_id}/` и удаляется при появлении следующей анкеты (`_purge_media` оставляет только текущую и `_archive`).

Thumbnail для галереи — первый файл из `_archive/{id}/` по алфавиту. Все файлы архива отдаются через `/media/_archive/{id}/{filename}` (StaticFiles mount от `MEDIA_DIR`).

---

## Модули

### `app/config.py`
Все пути и переменные окружения. Создаёт директории `MEDIA_DIR`, `PENDING_DIR`, `ARCHIVE_DIR` при импорте. Ключевые экспорты: `DB_PATH`, `MEDIA_DIR`, `ARCHIVE_DIR`, `STATIC_DIR`, `PHONES`, `TG_BOT_TOKEN`.

### `app/state.py`
`AppState` — dataclass с `asyncio.Lock`. Все поля публичны, мутировать только под `async with state.lock`. Метод `snapshot()` возвращает dict для `/api/state`.

Постоянные поля (переживают рестарт через `settings.py`): `only_new_mode`, `auto_dislike_mode`, `auto_like_mode`, `auto_rotate_mode`, `age_min`, `age_max`, `auto_dislike_count`, `like_count`, `dislike_count`.

### `app/db.py`
Тонкая обёртка над `sqlite3`. Каждый вызов открывает соединение, делает запрос, закрывает. Нет connection pool — БД маленькая, latency не критична.

Публичные функции: `init`, `find_profile`, `find_profile_by_description`, `insert_profile`, `count_profiles`, `bump_seen`, `get_profile`, `get_all_profiles`, `set_rating`.

### `app/dedup.py`
In-memory индексы для дедупликации. Строится один раз при старте из полной таблицы `profiles`. Два индекса:
- `_by_desc`: canonicalize(description) → profile_id
- `_by_hash`: first_media_hash → profile_id

`canonicalize()`: NFKC + casefold + удаление zero-width символов + collapse whitespace.

Анкета считается дублем если совпадает ЛИБО текст, ЛИБО хотя бы один из хешей фото.

### `app/profile.py`
Центральный пайплайн. Точки входа: `handle_messages(messages)` (вызывается из `tg.py`).

Поток обработки одного «юнита» (одиночное сообщение или альбом):
1. Проверка на лимит-сообщение → ротация или блокировка UI
2. Проверка наличия медиа
3. Скачать медиа во временный каталог `media/_pending/{head_message_id}/`
4. Вычислить хеши (`_compute_all_media_hashes`)
5. Поиск дубля (`dedup.find_duplicate`)
6. Если новая → `db.insert_profile` → `dedup.register` → `_archive_media`
7. Если дубль → `db.bump_seen` → `_archive_media` (идемпотентно, дополняет архив)
8. Проверить `highest_priority.json` — если совпадение, показать без авто-фильтров
9. Проверить `auto_skip.json` — дизлайк без показа
10. Проверить авто-режимы (`_auto_dislike_reason`)
11. Если `auto_like_mode` → лайк + уведомление бота
12. Иначе → `_publish_media` → обновить `state.current_profile`

`_publish_media` перемещает файлы из `_pending` в `media/{profile_id}/` и вызывает `_purge_media` (удаляет все старые каталоги кроме текущего и `_archive`).

### `app/tg.py`
Управляет списком `TelegramClient` (по одному на каждый `TG_PHONES`). Активен только один клиент (`_current_idx`). Входящие сообщения от `@leomatchbot` обрабатываются через `events.NewMessage`. Альбомы буферизируются 1.2 сек перед диспетчеризацией.

`rotate_account()` — переключиться на следующий не-лимитированный аккаунт, послать kickoff `"1"` к боту.

`fetch_latest_unit()` — забрать последнее сообщение из истории (используется watchdog'ом).

### `app/hashing.py`
- `sha256_file(path)` — хеш файла блоками по 1 МБ
- `hash_video_first_frame(path)` — запускает `imageio_ffmpeg.get_ffmpeg_exe()` (бандлованный ffmpeg) для извлечения первого кадра в PNG, возвращает sha256 stdout

### `app/limits.py`
JSON-файл `account_limits.json`. TTL 8 часов — если файл старше, пересоздаётся (лимиты сбрасываются). `mark_limit(phone)`, `is_limited(phone)`, `all_limited(phones)`.

### `app/settings.py`
Простой JSON `app_settings.json`. `load()` при старте, `save()` после каждой мутации сохраняемых полей AppState. Неизвестные ключи игнорируются.

### `app/stats.py`
Дневная статистика per-аккаунт в `app_stats.json`. Окно сбрасывается при старте если прошли локальные 05:00. Поля: `likes`, `dislikes`, `auto_likes`, `auto_dislikes`, `new_profiles`. `summary()` возвращает totals + per_account для `/status`.

---

## HTTP API (`app/main.py`)

### Главный экран

| Метод | Путь | Описание |
|---|---|---|
| GET | `/` | `static/index.html` |
| GET | `/api/state` | снапшот AppState + `total_profiles` + `active_phone` |
| POST | `/api/like` | отправить ❤️, сбросить current_profile |
| POST | `/api/dislike` | отправить 👎, сбросить current_profile |
| POST | `/api/letter` | `{"text":""}` — отправить 💌 / 📹 (опционально с текстом) |
| POST | `/api/switch-account` | начать ротацию аккаунта (async task) |
| POST | `/api/auto-dislike/toggle` | вкл/выкл авто-дизлайк, возвращает новый snapshot |
| POST | `/api/auto-like/toggle` | вкл/выкл авто-лайк |
| POST | `/api/only-new/toggle` | вкл/выкл режим «только новые» |
| POST | `/api/auto-rotate/toggle` | вкл/выкл авто-смену аккаунта при лимите |
| POST | `/api/age-filter` | `{"min":18,"max":25}` или `{"min":null,"max":null}` для сброса |

### Галерея и оценки

| Метод | Путь | Описание |
|---|---|---|
| GET | `/gallery` | `static/gallery.html` |
| GET | `/rating` | `static/rating.html` (использует `?id=N`) |
| GET | `/api/gallery` | список всех анкет: id, description, rating, seen_count, registered_at, thumb_url |
| GET | `/api/ratings/{id}` | одна анкета + `media_urls` (все файлы архива) |
| POST | `/api/ratings/{id}/rate` | `{"rating": 1-10}` — сохранить оценку |

### Статика

`/media/*` → `media/` (включая `_archive/`). `/static/*` → `static/`.

---

## Веб-интерфейс

### Главный экран (`/`)
Показывает текущую анкету (фото/видео + описание). Кнопки: ❤️ Лайк, 👎 Дизлайк, 💌 / 📹. Режимы: авто-дизлайк, авто-лайк, только новые, авто-смена аккаунтов. Фильтр возраста. Счётчики и активный телефон. Polling `/api/state` каждую секунду.

### Галерея (`/gallery`)
Сетка анкет с thumbnail, именем, датой, оценкой. Функционал:
- **Мультисортировка**: кнопки «Дата» и «Оценка» независимы, можно активировать обе. Каждая кнопка цикличнее: неактивна → ↓ → ↑ → неактивна. При двух активных — приоритет ①/②.
- **Фильтры** (кнопка «Фильтры», коллапсируется): диапазон оценки, диапазон дат, наличие фото, чекбокс «только не оценённые».
- Сортировка и фильтры сохраняются в `localStorage` — переживают перезапуск браузера. Только кнопка **Сбросить** возвращает к дефолту.
- После рендеринга отсортированный список ID сохраняется в `localStorage['leodv_gallery_ids']` для навигации на странице оценки.

### Страница оценки (`/rating?id=N`)
- Все фото и видео из архива
- Карточка с датой добавления и счётчиком показов
- Полный текст описания
- Нижняя полоска: [◀ Предыдущая] [N / Total] [Следующая ▶] + оценка + кнопки 1–10
- Порядок «предыдущая/следующая» соответствует текущей сортировке галереи (из `localStorage`)
- Клавиши ← → для навигации
- Оценка сохраняется немедленно при клике

---

## Telegram-бот (`tg_bot/bot.py`)

Опциональный второй интерфейс. Включается при наличии `TG_BOT_TOKEN`.

При старте бот автоматически делает `/start` от лица каждого `TG_PHONES` — это кешируем `InputPeer` в сессии бота (иначе Telegram не даст отправлять файлы).

Кнопки reply-клавиатуры: ❤️ Лайк, 👎 Дизлайк, 💌/📹, авто-режимы, фильтр возраста, переключить аккаунт, статус. Новые анкеты приходят пушем. Доступ только для `TG_PHONES`.

---

## Авто-режимы

| Режим | Поведение |
|---|---|
| `auto_dislike_mode` | все входящие анкеты → 👎 без показа |
| `auto_like_mode` | все входящие → ❤️ без показа, уведомление в TG-бот |
| `only_new_mode` | дубли (seen_count > 1) → 👎 автоматически |
| `age_filter_active` | анкеты вне диапазона возраста → 👎 |
| `auto_skip.json` | совпадение подстроки → 👎 |
| `highest_priority.json` | совпадение подстроки → показать поверх всех фильтров |
| `auto_rotate_mode` | лимит на аккаунте → автоматически ротировать |

Авто-лайк и авто-дизлайк взаимоисключающие.

---

## Дедупликация — детали

Два независимых индекса в памяти. Анкета — дубль если:
- `canonicalize(description)` совпадает с сохранённым, ИЛИ
- любой sha256 из фото совпадает с `first_media_hash` любой строки БД

Это ловит переупорядочение фото и вариации zero-width символов в тексте.

Фото-хеш сравнивается по всем фото в альбоме против `first_media_hash` в БД. Это не квадратичный поиск — индекс плоский (`hash → id`), O(k) где k = число фото в альбоме.

Видео хешируется только первый в альбоме, только первый кадр — один вызов ffmpeg на альбом.

---

## Watchdog

Фоновая задача `_watchdog()` проверяет каждые 30 сек: если бот молчит и нет текущей анкеты — забирает последнее сообщение из истории и прогоняет через пайплайн. Защита от пропущенных событий.

---

## Файлы данных (не в git)

| Файл | Описание |
|---|---|
| `.env` | секреты (API ключи, токены, телефоны) |
| `data.db` | основная БД SQLite |
| `data_backup.db` | копия БД, пересоздаётся при каждом старте |
| `app_settings.json` | персистентные режимы и счётчики |
| `app_stats.json` | дневная статистика |
| `account_limits.json` | записи о дневных лимитах аккаунтов |
| `leodv_*.session` | Telethon-сессии (по одной на аккаунт + бот) |
| `media/` | медиафайлы текущей и архивных анкет |

---

## Скрипты

- **`login.py`** — интерактивная авторизация всех `TG_PHONES` через Telethon (вводить коды подтверждения).
- **`scripts/extend_db.py`** — парсинг истории чата `@leomatchbot` и заливка в БД для начального наполнения дедуп-индекса. Использует чекпоинт `scripts/db_extension_info.json`.
- **`scripts/find_duplicates.py`** — отчёт по уже существующим дублям (через SQLAlchemy).
- **`backup.py`** — ручное резервное копирование БД.

Запускать из корня: `python3 scripts/<script>.py`.

---

## Соглашения кодовой базы

- Весь I/O с Telethon — только через `app/tg.py`. Остальные модули не импортируют Telethon напрямую.
- `state.lock` — единственный мьютекс. Все мутации `AppState` под ним. Никакого другого синхронизации нет.
- HTTP-ответы не кешируются (нет заголовков Cache-Control) — клиент polling каждую секунду.
- `dedup.py` не пишет в БД — только читает при `init_indexes()`. `db.py` не знает о dedup.
- `profile.py` единственный, кто одновременно работает с `db`, `dedup`, `tg`, `state`. Это намеренно — пайплайн сквозной.
- Авто-режимы проверяются в строгом порядке: priority → auto_skip → авто-режимы → показ пользователю.
