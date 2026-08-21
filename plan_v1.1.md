# nxDLbot — План проекта v1.1

> Актуализировано: август 2026. Учтён фидбек по v1.0, устранены критические пробелы в безопасности, стабильности и Android-специфике.

---

## 1. Обзор

Telegram-бот для загрузки медиа из ссылок, хостящийся на Android-смартфоне через Termux. Поддерживает инлайн-запросы и автоматический ответ в чате. Использует гибридную стратегию: Cobalt API (приоритетный) с fallback на yt-dlp.

**Ключевые изменения v1.1:**
- Убран Redis (не используется в архитектуре).
- Исправлена логика сжатия: `ffmpeg -fs` заменён на разумную стратегию обхода лимита Telegram.
- Добавлена SSRF-защита, rate limiting, валидация URL и экранирование subprocess.
- Добавлен watchdog/healthcheck для выживания в фоне на Android.
- Добавлены миграции БД, индексы, graceful shutdown.
- Уточнена интеграция Cobalt (конфигурируемый endpoint, оговорены ограничения self-hosted на Termux).
- Добавлена поддержка `cookies.txt` для Instagram и приватного контента.

---

## 2. Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│  Android (LineageOS) + Termux                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  aiogram 3.x                                          │  │
│  │  ├─ bot.py           — хендлеры, логика               │  │
│  │  ├─ config.json      — токен, настройки (nano)        │  │
│  │  ├─ bot.db           — SQLite (aiosqlite)             │  │
│  │  ├─ downloads/       — LRU-кэш (max 4 GB)             │  │
│  │  ├─ logs/            — ротированные логи              │  │
│  │  ├─ downloaders/                                      │  │
│  │  │   ├─ cobalt.py  — HTTP API (конфигурируемый)     │  │
│  │  │   └─ ytdlp.py   — subprocess yt-dlp               │  │
│  │  └─ utils/                                            │  │
│  │      ├─ cache_manager.py — LRU + автоочистка        │  │
│  │      ├─ db.py            — миграции, схема БД        │  │
│  │      ├─ security.py      — URL whitelist, SSRF       │  │
│  │      ├─ rate_limiter.py   — per-user семафор + RL    │  │
│  │      └─ helpers.py       — парсинг URL/аргументов   │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Cobalt API (self-hosted, localhost:PORT)               │  │
│  │  — Node.js, endpoint конфигурируется в config.json    │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  yt-dlp + ffmpeg (Termux packages)                    │  │
│  │  — cookies.txt в ~/nxDLbot/cookies/                   │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Структура проекта

```
~/nxDLbot/
├── bot.py                 # Точка входа, хендлеры, graceful shutdown
├── config.json            # Конфигурация
├── requirements.txt       # Python-зависимости
├── bot.db                 # SQLite
├── downloads/             # LRU-кэш медиафайлов
├── logs/                  # Ротированные логи (logrotate или ручная ротация)
├── cookies/               # cookies.txt для yt-dlp (Instagram и др.)
├── downloaders/
│   ├── __init__.py
│   ├── cobalt.py          # Обёртка над Cobalt API
│   └── ytdlp.py           # Обёртка над yt-dlp (subprocess, shlex)
└── utils/
    ├── __init__.py
    ├── cache_manager.py   # LRU + автоочистка
    ├── db.py              # Миграции, схема, инициализация
    ├── security.py        # URL whitelist, SSRF-фильтры
    ├── rate_limiter.py    # Rate limiting + per-user concurrent limit
    └── helpers.py         # extract_url, parse_args, семафор
```

---

## 4. Конфигурация (config.json)

```json
{
    "bot_token": "123456:ABC-DEF...",
    "admin_id": 123456789,
    "max_concurrent_global": 2,
    "max_concurrent_per_user": 1,
    "cobalt_url": "http://localhost:9000",
    "cobalt_api_path": "/api/json",
    "cache_limit_gb": 4,
    "default_downloader": "auto",
    "cleanup_interval_sec": 3600,
    "max_telegram_size_mb": 50,
    "auto_compress": true,
    "compress_strategy": "downgrade_quality",
    "max_telegram_caption_length": 1024,
    "url_whitelist": ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "x.com", "reddit.com", "v.redd.it"],
    "block_private_ips": true,
    "yt_dlp_auto_update": true,
    "yt_dlp_update_interval_hours": 48,
    "healthcheck_port": 8080,
    "log_max_bytes": 10485760,
    "log_backup_count": 5,
    "placeholder_video_file_id": "abc..."  // file_id заглушки для inline
}
```

| Поле | Описание |
|------|----------|
| `bot_token` | Токен от @BotFather |
| `admin_id` | Telegram ID администратора |
| `max_concurrent_global` | Глобальный семафор загрузок |
| `max_concurrent_per_user` | Семафор на пользователя (антиспам) |
| `cobalt_url` / `cobalt_api_path` | URL self-hosted Cobalt и путь API |
| `cache_limit_gb` | Лимит LRU-кэша |
| `default_downloader` | `auto` (Cobalt → fallback yt-dlp) |
| `compress_strategy` | `downgrade_quality` (yt-dlp перекачка в худшем качестве) или `skip` (отправка ссылки) |
| `url_whitelist` | Разрешённые домены (регулярки/список) |
| `block_private_ips` | Блокировать `localhost`, `192.168.x.x`, `file://` |
| `yt_dlp_auto_update` | Автообновление yt-dlp каждые N часов |
| `healthcheck_port` | Порт для HTTP healthcheck (watchdog) |

---

## 5. Безопасность

### 5.1 SSRF-защита (`utils/security.py`)

```python
BLOCKED_SCHEMES = {"file", "ftp", "sftp", "data"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
BLOCKED_PREFIXES = ("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "169.254.")
```

- Проверка scheme URL (`http`/`https` only).
- Разрешение hostname → проверка на private IP через `socket.getaddrinfo`.
- Whitelist доменов: только известные видеохостинги.
- **Важно:** проверка выполняется **до** передачи URL в yt-dlp/Cobalt.

### 5.2 Subprocess безопасность

- Все URL передаются в yt-dlp через `shlex.quote()`.
- Запрет спецсимволов: `;`, `&&`, `||`, `` ` ``, `$()`.
- Использование списка аргументов (`subprocess.run([...])`) вместо `shell=True`.

### 5.3 Rate Limiting

| Лимит | Значение | Действие |
|-------|----------|----------|
| Глобальный семафор | `max_concurrent_global` | Макс. параллельных загрузок |
| Per-user семафор | `max_concurrent_per_user` | Антиспам (по умолчанию 1) |
| Сообщения в минуту | 10 запросов / 60 сек | Игнорирование + предупреждение |
| Банлист | Таблица `banned_users` | Полный игнор пользователя |

---

## 6. База данных (SQLite + миграции)

### 6.1 Миграции

```
utils/db_migrations/
├── 001_init.sql
├── 002_add_banned_users.sql
├── 003_add_url_index.sql
└── 004_add_cdn_expires.sql
```

При старте бот проверяет `PRAGMA user_version` и применяет недостающие миграции.

### 6.2 Таблица `downloads`

```sql
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    url TEXT NOT NULL,
    source TEXT,                    -- 'cobalt', 'ytdlp'
    quality TEXT,                   -- 'best', '720p', 'audio'
    file_size_mb REAL,
    file_path TEXT,                 -- локальный путь (yt-dlp)
    cdn_url TEXT,                   -- прямая ссылка (Cobalt)
    cdn_expires_at TIMESTAMP,       -- срок жизни CDN-ссылки
    telegram_file_id TEXT,          -- file_id для повторной отправки
    status TEXT DEFAULT 'pending',  -- pending, processing, done, cached, deleted, failed, cancelled
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_history ON downloads(user_id, last_accessed);
CREATE INDEX idx_status ON downloads(status);
CREATE INDEX idx_url ON downloads(url);          -- поиск дубликатов
CREATE INDEX idx_file_id ON downloads(telegram_file_id);
```

### 6.3 Таблица `user_settings`

```sql
CREATE TABLE user_settings (
    user_id INTEGER PRIMARY KEY,
    default_downloader TEXT DEFAULT 'auto',
    default_quality TEXT DEFAULT 'best',
    notify_on_finish INTEGER DEFAULT 1
);
```

### 6.4 Таблица `banned_users`

```sql
CREATE TABLE banned_users (
    user_id INTEGER PRIMARY KEY,
    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);
```

### 6.5 Graceful Shutdown

При получении `SIGTERM`/`SIGINT`:
1. Отклонять новые запросы (reply "Бот останавливается...").
2. Дождаться завершения активных загрузок (таймаут 60 сек).
3. Завершить задачи в статусе `pending` → `cancelled`.
4. Закрыть соединения с БД и Telegram.

---

## 7. Режимы работы

### 7.1 Инлайн-режим

**Триггер:** `@имябота <ссылка> [флаг]`

**Флаги:**
| Флаг | Действие |
|------|----------|
| (нет) | `auto` — Cobalt, fallback yt-dlp |
| `-c` | Принудительно Cobalt |
| `-y` | Принудительно yt-dlp |
| `-a` | Только аудио (yt-dlp) |

**Пустой запрос (`@имябота `):** показывает историю загрузок пользователя (последние 10).

**Механика загрузки:**
1. Пользователь вводит запрос → валидация URL (`security.py`).
2. Бот отвечает `answer_inline_query` с одним `InlineQueryResultArticle` — заглушка "⏳ Загружаю...".
3. Пользователь тапает результат.
4. Бот получает `ChosenInlineResult` (требуется `/setinlinefeedback` в BotFather).
5. Бот качает медиа (с глобальным + per-user семафором).
6. Бот редактирует сообщение через `edit_message_media` по `inline_message_id`.
7. Результат сохраняется в БД + кэш.

**Заглушка и ChosenInlineResult:**
- Бот отвечает `answer_inline_query` с `InlineQueryResultCachedVideo`, используя 
  заранее загруженный `placeholder_video_file_id` (1-секундное видео, хранится в config.json).
- Пользователь тапает → `ChosenInlineResult` (требуется `/setinlinefeedback` в BotFather).
- Бот редактирует сообщение через `edit_message_media(inline_message_id, InputMediaVideo(...))`.
- **Важно:** тип сообщения не меняется. Article → Video невозможен, поэтому заглушка 
  обязана быть видео с самого начала.

**Прогресс в инлайне:**
- Заглушка обновляется текстом: "⏳ Загружаю... (N%)" или "⏳ В очереди (позиция: 2)".
- Обновление каждые 5 секунд через `edit_message_text` (если inline-заглушка — article, обновляем через `edit_message_media` после готовности).

**Ограничение:** автоматический выбор качества. Кнопки качества в инлайне недоступны.

### 7.2 Чат-режим (Reply Mode)

**Триггер:** бот видит сообщение с URL из `url_whitelist`.

**Поведение:**
1. Валидация URL → реакция ⏳ или "Качаю...".
2. Проверка дубликата: если `url` уже есть в БД со статусом `done`/`cached` и `telegram_file_id` — отправить сразу.
3. Для yt-dlp: получить список форматов (`-F`), показать InlineKeyboard с вариантами качества.
4. Пользователь выбирает качество → начинается загрузка.
5. Для Cobalt: сразу загружает (CDN-ссылка).
6. Отправляет медиа, обновляет статус.

**Кнопки качества (чат):**
```
[720p | mp4 | ~12 MB] [1080p | mp4 | ~45 MB]
[480p | mp4 | ~8 MB]  [Только аудио | mp3]
```

**Обработка caption:**
- Название видео обрезается до 1024 символов (лимит Telegram для caption).
- Добавление источника: `via @botname` (если влезает).

**Media Group (Instagram Carousel):**
- Если yt-dlp/Cobalt возвращает несколько файлов (album):
  - До 10 файлов: отправка через `send_media_group`.
  - Более 10: разбиение на несколько групп или отправка первых 10 + ссылка на остальные.

### 7.3 Админка (Telegram)

Доступна только `admin_id`.

| Команда | Описание |
|---------|----------|
| `/stats` | Загрузки, размер кэша, активные задачи, uptime |
| `/cleanup` | Принудительная очистка кэша |
| `/setdownloader <user_id> <auto/cobalt/ytdlp>` | Смена дефолтного загрузчика |
| `/ban <user_id> [причина]` | Заблокировать пользователя |
| `/unban <user_id>` | Разблокировать |
| `/logs [N]` | Последние N строк логов (по умолчанию 20) |
| `/restart` | Graceful restart бота |
| `/ytupdate` | Принудительное обновление yt-dlp |

---

## 8. Загрузка медиа (гибридная стратегия)

```
Валидация URL (security.py)
    → Если не валиден: "❌ Неподдерживаемый или опасный URL"

Проверка дубликата (БД, idx_url)
    → Если telegram_file_id есть: отправить file_id
    → Если cdn_url есть и не протух: отправить по ссылке

Если флаг -c:
    → Cobalt API
    Если ошибка/таймаут (>15 сек):
        → Сообщение об ошибке (fallback отключён для -c)

Если флаг -y или -a:
    → yt-dlp
    → Если файл > 50 MB:
        → Стратегия compress_strategy:
            "downgrade_quality": перекачать с -f "best[height<=480]" или -f "worst"
            "skip": отправить прямую ссылку на файл
        Если после downgrade всё ещё > 50 MB:
            → Отправка как ссылка

Если флаг auto (или нет):
    → Попытка Cobalt API (таймаут 15 сек)
    Если ошибка/таймаут:
        → Fallback на yt-dlp
        → Если файл > 50 MB → downgrade_quality → ссылка
```

### 8.1 Cobalt (self-hosted)

> **Важно:** Cobalt API эволюционирует. Endpoint и поля запроса могли измениться к 2026. В `config.json` задаётся `cobalt_api_path` (по умолчанию `/api/json`).

```python
POST {cobalt_url}{cobalt_api_path}
Headers: {"Accept": "application/json", "Content-Type": "application/json"}
Body: {"url": "...", "downloadMode": "auto"}

Ответ (ожидаемый формат, адаптировать под актуальную версию):
- {"url": "...", "filename": "..."} → прямая ссылка
- {"picker": [{"url": "...", "type": "..."}]} → выбор первого видео
- {"status": "error", "text": "..."} → исключение
```
**Обработка больших файлов от Cobalt:**
- После получения `cdn_url` бот выполняет `HEAD`-запрос.
- Если `Content-Length` отсутствует или > 50 MB → отправка пользователю текстового 
  сообщения с прямой ссылкой: «📎 Файл слишком большой для Telegram. [ссылка]»
- Если `Content-Length` ≤ 50 MB → файл скачивается и отправляется через Telegram API.

**Ограничения self-hosted Cobalt на Termux:**
- «Простой режим без браузера» не работает для Instagram и частично TikTok.
- Headless Chrome/Puppeteer на ARM/Termux — ресурсоёмко и нестабильно.
- **Рекомендация:** держать Cobalt как опциональный компонент. Если не запущен — yt-dlp fallback.

### 8.2 yt-dlp

**Базовая команда:**
```bash
yt-dlp     --no-playlist     --max-downloads 1     --match-filters "!is_live"     --max-filesize 50M     --cookies ~/nxDLbot/cookies/cookies.txt     -o "downloads/%(id)s.%(ext)s"     <URL>
```

**Форматы:**
- Видео: `-f "best[height<=720]"` или `-f "best[filesize_approx<50M]"` (с fallback).
- Аудио: `-f "bestaudio/best" -x --audio-format mp3`.

**Защита от плейлистов:**
- `--no-playlist` — обязательно.
- Дополнительная проверка URL: если содержит `&list=` или `playlist`, выдать предупреждение.

**Защита от live:**
- `--match-filters "!is_live"` — отклонить livestream.
- `--no-live-from-start` — не начинать бесконечную загрузку.

**Обновление:**
- `yt-dlp -U` или `pip install -U yt-dlp` каждые 48 часов (фоновая задача).
- Команда `/ytupdate` для принудительного обновления.

### 8.3 Обработка > 50 MB (стратегия `downgrade_quality`)

Вместо `ffmpeg -fs` (который обрезает файл):

1. yt-dlp скачивает с `-f "best[height<=720]"`.
2. Если > 50 MB:
   - Попытка 2: `-f "best[height<=480]"`.
   - Попытка 3: `-f "worst[ext=mp4]/worst"`.
3. Если всё ещё > 50 MB:
   - Получить прямую ссылку через yt-dlp `--get-url`.
   - Отправить пользователю сообщение: "📎 Файл слишком большой для Telegram: [ссылка]".

**ffmpeg используется только для:**
- Мержа видео + аудио (`--merge-output-format mp4`).
- Конвертации аудио (`-x --audio-format mp3`).
- **Не используется для принудительного ужатия видео** (слишком долго на ARM).

---

## 9. LRU-кэш (4 GB)

### 9.1 Алгоритм

1. Все файлы yt-dlp сохраняются в `downloads/`.
2. При каждом обращении обновляется `last_accessed` в БД.
3. **При повторной отправке по `telegram_file_id`:** обновлять `last_accessed` в БД (чтобы файл не был удалён, пока file_id актуален).
4. После каждой загрузки вызывается `cleanup_cache()`:
   - Считает суммарный размер `downloads/`.
   - Если > 4 GB → удаляет файлы с самым старым `last_accessed`.
   - Обновляет статус в БД: `cached` → `deleted`.
5. Периодическая очистка (asyncio task) каждые `cleanup_interval_sec`.

### 9.2 Рассинхронизация БД ↔ ФС

При старте бота:
```python
# Проверка: файлы на диске = записи в БД
for file_path in downloads_dir:
    if file_path not in db_files:
        os.remove(file_path)  # осиротевший файл

for row in db.query("SELECT file_path FROM downloads WHERE status = 'cached'"):
    if not os.path.exists(row.file_path):
        db.execute("UPDATE downloads SET status = 'deleted' WHERE id = ?", row.id)
```

### 9.3 Повторное использование

- **Cobalt:** CDN-ссылка хранится в `cdn_url` + `cdn_expires_at`. Повторная отправка через `InlineQueryResultVideo`, пока ссылка жива.
- **yt-dlp:** `telegram_file_id` хранится после первой отправки. Повторная отправка через `InputMediaVideo(media=file_id)` без повторной загрузки.
- **История в инлайне:** пустой запрос `@бот ` показывает последние 10 загрузок с `telegram_file_id` или `cdn_url`.

---

## 10. Семафор и очередь

```python
import asyncio
download_semaphore_global = asyncio.Semaphore(max_concurrent_global)
user_semaphores = {}  # defaultdict(lambda: asyncio.Semaphore(max_concurrent_per_user))
```

- Максимум 2 глобальные параллельные загрузки.
- Максимум 1 загрузка на пользователя (антиспам).
- Остальные ждут в очереди.
- В чат-режиме: статусное сообщение обновляется с позицией в очереди.
- В инлайн-режиме: заглушка висит до освобождения семафора.

---

## 11. Установка и запуск

### 11.1 Termux

```bash
pkg update && pkg upgrade
pkg install python python-pip git ffmpeg nodejs
pip install aiogram aiohttp aiosqlite
```

**yt-dlp:**
```bash
pip install yt-dlp
# или
pkg install yt-dlp
```

### 11.2 Cobalt (self-hosted, опционально)

```bash
git clone https://github.com/imputnet/cobalt.git
cd cobalt
npm install
npm run build
# Проверить актуальный endpoint в документации проекта
# Запуск в фоне
nohup node api/src/index.js &
```

> **Примечание:** Если Cobalt не запускается или не работает для Instagram — бот должен корректно fallback на yt-dlp.

### 11.3 Cookies для Instagram

```bash
# Экспорт cookies из браузера (на ПК) через Get cookies.txt LOCALLY
# Скопировать на телефон в ~/nxDLbot/cookies/cookies.txt
mkdir -p ~/nxDLbot/cookies
```

### 11.4 Бот

```bash
cd ~/nxDLbot
nano config.json  # вставить токен
python bot.py
```

### 11.5 Автозапуск и выживание в фоне (termux-boot + watchdog)

```bash
pkg install termux-api termux-boot
mkdir -p ~/.termux/boot
```

**`~/.termux/boot/start-bot.sh`:**
```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
termux-notification --title "nxDLbot" --content "Бот запускается..." --ongoing
cd ~/nxDLbot && nohup python bot.py > logs/bot.log 2>&1 &
```

**Watchdog-скрипт (`~/nxDLbot/watchdog.sh`):**
```bash
#!/bin/bash
# Запускать через cron каждые 5 минут или termux-job-scheduler
HEALTH_URL="http://localhost:8080/health"
if ! curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
    termux-notification --title "nxDLbot" --content "Watchdog: перезапуск бота..."
    pkill -f "python bot.py"
    cd ~/nxDLbot && nohup python bot.py > logs/bot.log 2>&1 &
fi
```

**Android UI:** Settings → Apps → Termux → Battery → Unrestricted

### 11.6 ADB-настройка (LineageOS)

```bash
# Whitelist от Doze Mode
adb shell dumpsys deviceidle whitelist +com.termux

# Фоновая активность
adb shell appops set com.termux RUN_IN_BACKGROUND allow
adb shell appops set com.termux WAKE_LOCK allow
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow

# Отключение cached_apps_freezer (Android 12+)
adb shell settings put global cached_apps_freezer enabled 0

# Внутри Termux
termux-wake-lock
```

---

## 12. Обработка ошибок

| Сценарий | Действие |
|----------|----------|
| Cobalt таймаут (>15 сек) | Fallback на yt-dlp (если auto), иначе ошибка |
| yt-dlp не нашёл форматы | "Не удалось извлечь медиа. Возможно, требуется авторизация (Instagram)." |
| Файл > 50 MB после downgrade | Отправка прямой ссылки |
| Telegram API ошибка (429) | Экспоненциальный backoff (aiogram throttling) |
| FloodWait | Автоматический retry с удвоением задержки |
| OOM / нехватка памяти | Семафор ограничивает нагрузку, cleanup чистит кэш |
| Cobalt не запущен | yt-dlp fallback для auto, ошибка для `-c` |
| Invalid URL / private IP | "❌ URL не поддерживается или недоступен" |
| Livestream | Отклонение с сообщением "❌ Live-трансляции не поддерживаются" |
| Playlist URL | "❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео." |
| Caption > 1024 | Автообрезка до 1024 символов + многоточие |
| Instagram 403 (login required) | "❌ Instagram требует авторизации. Админ может загрузить cookies.txt" |

---

## 13. Логирование и мониторинг

```python
import logging
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler(
    "logs/bot.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
```

- Ротация по 10 MB, 5 бэкапов.
- Уровень `INFO` для продакшена, `DEBUG` для разработки.
- Логи включают: user_id, url (обрезанный), source, duration, error.
- Команда `/logs [N]` отдаёт последние N строк из `logs/bot.log`.

**Healthcheck endpoint:**
```python
from aiohttp import web

async def health(request):
    return web.Response(text="ok", status=200)

app = web.Application()
app.router.add_get("/health", health)
# Запуск на localhost:8080 (порт из config.json)
```

---

## 14. Этапы разработки

### Этап 1: Скелет и безопасность
- [ ] Структура проекта
- [ ] config.json + загрузка
- [ ] Подключение aiogram 3.x, базовый эхо-бот
- [ ] Миграции SQLite
- [ ] `security.py` — URL whitelist, SSRF, private IP block
- [ ] `rate_limiter.py` — per-user лимиты

### Этап 2: Чат-режим
- [ ] Распознавание ссылок в сообщениях (whitelist only)
- [ ] yt-dlp интеграция (subprocess, shlex, cookies)
- [ ] Проверка дубликатов (idx_url)
- [ ] Отправка видео + caption truncate
- [ ] Кнопки выбора качества
- [ ] Семафор + очередь с позицией
- [ ] Graceful shutdown

### Этап 3: Инлайн
- [ ] InlineQuery handler + валидация
- [ ] Заглушка + ChosenInlineResult
- [ ] edit_message_media
- [ ] Парсинг флагов (-c, -y, -a)
- [ ] История в пустом инлайн-запросе

### Этап 4: Кэш и история
- [ ] LRU-кэш менеджер
- [ ] Сохранение telegram_file_id + cdn_url
- [ ] Обновление last_accessed при file_id-использовании
- [ ] Автоочистка + синхронизация БД↔ФС
- [ ] Ротация логов

### Этап 5: Cobalt и гибрид
- [ ] Установка Cobalt в Termux (опционально)
- [ ] Интеграция API (конфигурируемый endpoint)
- [ ] Гибридная стратегия (auto)
- [ ] Fallback логика

### Этап 6: Админка и полировка
- [ ] Команды /stats, /cleanup, /logs, /ban, /ytupdate
- [ ] Healthcheck endpoint
- [ ] Watchdog-скрипт
- [ ] Обработка ошибок и edge cases
- [ ] Автозапуск через termux-boot
- [ ] ADB-скрипт

---

## 15. Зависимости (requirements.txt)

```
aiogram>=3.0.0
aiohttp>=3.8.0
aiosqlite>=0.19.0
yt-dlp>=2026.01.01
```

**Системные:** `python`, `ffmpeg`, `nodejs` (для Cobalt), `termux-api` (для уведомлений).

---

*Plan v1.1 — nxDLbot (август 2026)*
