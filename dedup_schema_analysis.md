# Dedup filter — анализ улучшений со свободой менять схему БД

Аналитический документ, не план к исполнению. Цель — разобрать кейсы пропусков фильтра «только новые», какие схематические изменения какой кейс закрывают, и ответить: **отдельная БД vs миграция основной**.

---

## 0. Текущее состояние (для контекста)

Таблица `profiles`:
```sql
id              INTEGER PRIMARY KEY
description     TEXT  NOT NULL
first_media_hash TEXT NOT NULL
seen_count      INTEGER
first_seen_at   TEXT
last_seen_at    TEXT
UNIQUE(description, first_media_hash)
```

Текущая логика (после последних правок):
- **S1** — канонизированный текст описания (NFKC + casefold + collapse whitespace).
- **S2** — SHA256 **любого** фото инкоминга против сохранённого `first_media_hash`.
- Дубликат, если **S1 OR S2**.

Что фильтр всё ещё пропускает — ниже.

---

## 1. Кейсы пропусков: какой случай чем ловится

| # | Сценарий | Что ломается у нас сегодня | Что нужно для надёжного отлова |
|---|---|---|---|
| **A** | Бот переставил фото местами | `first_media_hash` смотрит на старое первое фото; S2 (any-hash) спасает, **если** хотя бы одно фото — то же самое | Хранить хеши **всех** фото в отдельной таблице → точечный матч |
| **B** | Фото перекодировано (jpeg quality / resize) | SHA256 ломается → S2 фейлит. Если текст тоже изменён → пропуск | **Перцептивный хеш** (pHash/dHash) с Hamming-distance ≤ 6 |
| **C** | Описание отредактировано (слово, эмодзи) | NFKC + casefold помогает, но любое **смысловое** изменение байтов ломает S1 | Хранить **parsed (name, age, city)** отдельной колонкой; матч по этой тройке |
| **D** | Фото целиком заменены **и** текст переписан, но это тот же человек | Ни S1, ни S2 не сработают | Только проксированный signal — `(name, age, city)` или явный telegram user_id. В leomatchbot user_id не отдаётся → почти не решается |
| **E** | Чужие профили с одинаковым фото (stock-картинка / бот делится медиа) | S2 даст false-positive — посчитает чужую анкету дубликатом | Требовать **two-of-N** signals (см. ниже) |
| **F** | Видео-анкета вместо фото или наоборот | Хешируем только первый кадр первого видео — слабая сигнатура | pHash кадра + pHash превью; хранить как fingerprint |
| **G** | Очень короткое описание `"Маша, 18, Москва"` + новое фото | Часто это **разные** Маши, и legitimately нужно показать. Сейчас S1 ловит как дубль | `(name, age, city)` **без** дополнительных подтверждений — слабый signal |

---

## 2. Что бы я добавил в схему

### 2.1. Таблица всех медиа-хешей (закрывает **A**, частично **F**)

```sql
CREATE TABLE profile_media (
    profile_id   INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,        -- порядок в анкете (0 = первое)
    kind         TEXT    NOT NULL,        -- 'photo' | 'video'
    sha256       TEXT    NOT NULL,
    phash        INTEGER,                 -- 64-bit perceptual hash, NULL если не считали
    PRIMARY KEY (profile_id, idx)
);
CREATE INDEX idx_pm_sha256 ON profile_media(sha256);
```

Lookup на дубликат: `SELECT profile_id FROM profile_media WHERE sha256 IN (?, ?, ?)`. Один SQL-запрос, использует индекс.

### 2.2. Перцептивный хеш (закрывает **B**)

`phash` (64-bit int) от каждого фото. Библиотека: `imagehash` (PIL). Считается один раз при первом скачивании. Сравнение: Hamming distance.

Для 5k анкет × ~3 фото = ~15k хешей — линейный скан по запросу ~ 1-2 ms. Если разрастётся до >100k — BK-tree (в памяти, строится при старте, обновляется на каждый INSERT).

```sql
ALTER TABLE profile_media ADD COLUMN phash INTEGER;
```

Lookup: SELECT всех phash, в памяти `popcount(x XOR query) <= 6` → candidate_profile_ids.

### 2.3. Канонический ключ описания (закрывает **C** частично)

Не отдельная таблица — **дополнительная колонка** в `profiles`:

```sql
ALTER TABLE profiles ADD COLUMN description_canonical TEXT;
ALTER TABLE profiles ADD COLUMN name_canonical TEXT;
ALTER TABLE profiles ADD COLUMN age INTEGER;
ALTER TABLE profiles ADD COLUMN city_canonical TEXT;
CREATE INDEX idx_p_desc_canon ON profiles(description_canonical);
CREATE INDEX idx_p_nac ON profiles(name_canonical, age, city_canonical);
```

`description_canonical` — то же, что сейчас считает `app/dedup.canonicalize()`, но **сохраняется в БД и индексируется** → лукап `O(log n)` SQL'ем, не in-memory словарём (важно при большом росте).

`name_canonical`, `age`, `city_canonical` — парсинг `f"{name}, {age}, {city}[ – {about}]"` один раз на вставке. Парсинг fail-safe: если не разобрали — кладём `NULL`, такая строка просто не участвует в этом signal.

### 2.4. История правок (опционально, закрывает **C**, **D** на длинной дистанции)

```sql
CREATE TABLE profile_descriptions (
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    description  TEXT    NOT NULL,
    seen_at      TEXT    NOT NULL,
    PRIMARY KEY (profile_id, description)
);
```

Каждое **отличающееся** описание для уже-известного профиля → новая строка. На лукапе сравниваем входящий текст с `description IN (SELECT description FROM profile_descriptions WHERE profile_id = ?)`. Catches: «текст слегка переписан N раз», т.е. эволюционирующий профиль.

Те же 5k анкет → таблица в худшем случае ~ 15k строк. Места занимает копейки.

### 2.5. Что **не** имеет смысла добавлять

- **Telegram user_id анкеты.** В leomatchbot не отдаётся (анкеты анонимизированы). Если бы отдавалось — это был бы single-best signal, гасит почти всё.
- **Эмбеддинги фото (CLIP / face embeddings).** Дорого по CPU, нужен GPU для разумной скорости, и pHash покрывает 80% case **B** при copy-paste-easy implementation. Возвращаться, только если pHash не хватит.

---

## 3. Политика срабатывания (как комбинировать signals)

Без схема-изменений мы вынуждены делать **OR** между двумя слабыми signals. С обогащённой схемой можно делать **взвешенное N-of-M голосование**:

| Signal | Вес | Что считает |
|---|---|---|
| Канонический текст description | strong | exact match canonical |
| Любой SHA256 из инкоминга совпал | strong | `profile_media.sha256` exact |
| Любой pHash в Hamming ≤ 6 | strong | NN-поиск в pHash-индексе |
| `(name, age, city)` совпал | weak | `idx_p_nac` |
| `description` есть в истории профиля | strong | `profile_descriptions` |

**Правило:** дубликат, если **≥ 1 strong** signal матчит, ИЛИ **2+ weak** signal матчат **один и тот же** `profile_id`. То есть `(name, age, city)` сам по себе не триггерит — но если он подтверждён ещё чем-то, считаем как сильный сигнал.

Кейсы и какой signal их теперь покрывает:

| Кейс | Покрытие |
|---|---|
| A — фото переставлены | `profile_media.sha256` strong |
| B — фото перекодировано | `pHash` strong |
| C — описание отредактировано | `name+age+city` + (sha256 ИЛИ pHash) → 1 strong + 1 weak ИЛИ строго `profile_descriptions` |
| D — всё переписано, тот же человек | Только weak `(name, age, city)` — не сработает (как и хочется: «всё переписано» ≈ новая презентация) |
| E — чужой со stock-фото | `(name, age, city)` НЕ совпадает → не объявим дубликатом из-за одного фото-хеша. Сейчас бы false-positive — теперь нет |
| F — видео | pHash первого кадра как минимум; если не хватает — pHash превью |
| G — две разные Маши | `(name, age, city)` weak alone, фото разные, текст разный → не дубликат, верно |

Важный бонус: **с N-of-M исчезает FP-кейс E**, который у нас сейчас потенциально есть (любой совпадающий хеш — дубликат).

---

## 4. Стратегия лукапа (на масштаб >50k анкет)

Сейчас всё в RAM. Если БД вырастет:

- **SHA256:** индекс по `profile_media.sha256` — SQL `IN (...)`.
- **pHash:** в RAM `dict[int, list[profile_id]]` для exact и BK-tree для NN. Строится при старте за `O(n)`, обновляется на INSERT.
- **(name, age, city):** SQL composite index — `O(log n)`.
- **canonical description:** SQL index — `O(log n)`.
- **`profile_descriptions`:** SQL index — `O(log n)`.

Один lookup = ~5 SQL запросов + 1 BK-tree query. Меньше 10 ms даже на 100k записей. Допустимо в hot-path `_process()`.

---

## 5. Migration vs sidecar DB — ответ на главный вопрос

### Кратко: **миграция основной БД**.

### Детально

#### Если делать sidecar (новый файл `dedup.db`)

**Что это даст:**
- Прод-БД (`data.db`) не трогается → ноль риска порушить продакшен данные.
- Откатить эксперимент = удалить файл sidecar.
- Можно держать «жирные» структуры (pHash, embeddings) отдельно от core-таблицы.
- Можно отключить sidecar флагом и фильтр откатится к старому поведению.

**Чем это плохо:**
- **Двойная запись на каждый INSERT.** Сейчас вставка нового профиля = одна транзакция в `data.db`. С sidecar — две (или одна с `ATTACH DATABASE`, но это всё равно две отдельные транзакции в каждой). Если процесс упадёт между ними → drift: профиль есть в `data.db`, нет в `dedup.db`. Нужна compensating logic.
- **Два источника правды.** `profiles.first_media_hash` и `profile_media.sha256` для idx=0 должны быть одинаковыми → drift возможен → нужно периодически валидировать.
- **Чтения становятся cross-DB.** Через `ATTACH` SQLite это умеет, но запросы становятся уродливее. Транзакционная семантика между двумя attached БД нетривиальна.
- **Бэкапы и git.** Сейчас один файл — один бэкап. Станет два, синхронизировать вручную.
- **Психологически** разделение «основная и dedup-БД» подталкивает воспринимать sidecar как «грязные эксперименты» — там накапливается tech-debt.

#### Если делать миграцию основной

**Что это даст:**
- Один источник правды. `INSERT` всех связанных данных — одна транзакция, либо всё, либо ничего.
- Простые `JOIN` между `profiles` и новыми таблицами.
- Стандартный pattern (любой Python/Django/SQLAlchemy проект так живёт).
- Бэкап = `cp data.db backup.db`. Один файл — одна цельная картинка.
- Можно делать новые колонки `NULLABLE` → старые ряды просто `NULL`, и signal по ним не работает; backfill — отдельный optional скрипт.

**Чем это «плохо»:**
- Миграция необратима без бэкапа. Лекарство: перед миграцией `cp data.db data_pre_dedup_migration.db` (уже есть `data_backup.db` рядом — паттерн в проекте).
- Любой код, который читает `profiles`, должен пережить новые колонки. Раз код у нас под контролем — это просто.

### Для этого конкретного проекта

Размер БД маленький (~3 MB, 5k анкет), код полностью под контролем, нет внешних потребителей. **Хранить связанные данные раздельно нет ни одного аргумента**, кроме «боюсь сломать прод» — но прод тут локальный, и backup-стратегия уже есть (см. `data_backup.db`).

Sidecar имел бы смысл, если:
- БД была бы shared с другим приложением, в чью схему мы не имеем права лезть.
- Данные dedup были бы **сильно** больше core-таблицы (embeddings по фото, петабайтные индексы) — тогда отделение помогает с housekeeping.
- Мы экспериментировали бы с несколькими версиями dedup параллельно — sidecar как «слот эксперимента».

Ни одно из этих условий не выполняется.

---

## 6. Если делать миграцию — пошагово (мини-план)

1. Залить `data.db` → `data_pre_dedup.db` (страховка).
2. Скрипт `scripts/migrate_dedup.py`:
   - `ALTER TABLE profiles ADD COLUMN description_canonical TEXT;`
   - `ALTER TABLE profiles ADD COLUMN name_canonical TEXT;`
   - `ALTER TABLE profiles ADD COLUMN age INTEGER;`
   - `ALTER TABLE profiles ADD COLUMN city_canonical TEXT;`
   - `CREATE TABLE profile_media (...);`
   - `CREATE INDEX idx_pm_sha256 ON profile_media(sha256);`
   - `CREATE INDEX idx_p_desc_canon ON profiles(description_canonical);`
   - `CREATE INDEX idx_p_nac ON profiles(name_canonical, age, city_canonical);`
   - Опционально: `CREATE TABLE profile_descriptions (...);`
3. Backfill для существующих 5k строк:
   - `description_canonical` = `canonicalize(description)` — мгновенно.
   - `(name, age, city)` = парсинг — мгновенно.
   - `profile_media`: на каждый существующий ряд кладём **одну** запись с `idx=0, sha256=first_media_hash, phash=NULL`. Бэкфил pHash невозможен — медиа удалены (см. недавнюю архитектуру с эфемерным `media/`). Это нормально: новые поступления будут иметь pHash, старые — нет, signal просто не сработает на них.
4. `app/db.py`: добавить вспомогательные функции (`insert_profile_media`, `find_by_sha256`, `find_by_phash`, `find_by_canonical_desc`, `find_by_nac`).
5. `app/dedup.py`: переписать под SQL-индексы вместо in-memory `dict`'ов. `init_indexes()` уже не нужна — БД сама индексирует.
6. `app/profile.py`: на новой анкете считаем хеши всех фото + pHash каждого; парсим (name, age, city); INSERT в `profiles` + INSERT'ы в `profile_media` — одной транзакцией.
7. `requirements.txt`: добавить `Pillow` и `imagehash` (для pHash).

Тестирование — на копии `data.db`. Откат — `cp data_pre_dedup.db data.db`.

---

## 7. Итог в одно предложение

Без свободы менять схему мы уже выжали почти максимум; со свободой — добавлять `profile_media` (multi-hash + pHash) и парсинг `(name, age, city)` в колонки `profiles`, всё в **одной БД через миграцию**, без sidecar.
