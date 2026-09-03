# nxDLbot — План проекта v1.2

> Август 2026. Упрощённая версия: убран Cobalt, убраны кнопки качества, добавлен предварительный анализ форматов yt-dlp. Только yt-dlp, только хардкор.

---

## 1. Обзор

Telegram-бот для загрузки медиа из ссылок, хостящийся на Android-смартфоне через Termux. 
Поддерживает инлайн-запросы и автоматический ответ в чате. 
**Единственный загрузчик — yt-dlp.**

**Ключевые изменения v1.2:**
- Полностью убран Cobalt и вся гибридная логика.
- Убраны кнопки выбора качества. Бот сам выбирает лучшее качество, влезающее в 50 MB.
- Постфикс `-mp3` работает в чате и в инлайне.
- Добавлен предварительный анализ форматов yt-dlp (`-j`) для выбора оптимального качества ДО скачивания.
- Упрощена схема БД: убраны `cdn_url`, `cdn_expires_at`.
- Упрощён конфиг.

---

## 2. Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│  Android (LineageOS) + Termux                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  aiogram 3.x                                          │  │
│  │  ├─ bot.py           — хендлеры, логика               │  │
│  │  ├─ config.json      — токен, настройки               │  │
│  │  ├─ bot.db           — SQLite (aiosqlite)             │  │
│  │  ├─ downloads/       — LRU-кэш (max 4 GB)             │  │
│  │  ├─ logs/            — ротированные логи              │  │
│  │  ├─ cookies/         — cookies.txt для yt-dlp         │  │
│  │  ├─ downloaders/                                    │  │
│  │  │   └─ ytdlp.py   — subprocess yt-dlp               │  │
│  │  └─ utils/                                          │  │
│  │      ├─ cache_manager.py — LRU + автоочистка        │  │
│  │      ├─ db.py            — миграции, схема БД        │  │
│  │      ├─ security.py      — URL whitelist, SSRF       │  │
│  │      ├─ rate_limiter.py   — per-user семафор + RL    │  │
│  │      └─ helpers.py       — парсинг URL/аргументов   │  │
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
├── logs/                  # Ротированные логи
├── cookies/               # cookies.txt для yt-dlp (Instagram и др.)
├── downloaders/
│   ├── __init__.py
│   └── ytdlp.py           # Обёртка над yt-dlp (subprocess, shlex, анализ форматов)
└── utils/
    ├── __init__.py
    ├── cache_manager.py   # LRU + автоочистка
    ├── db.py              # Миграции, схема, инициализация
    ├── security.py        # URL whitelist, SSRF-фильтры
    ├── rate_limiter.py    # Rate limiting + per-user concurrent limit
    └── helpers.py         # extract_url, parse_args (-mp3), семафор
```

---

## 4. Конфигурация (config.json)

```json
{
    "bot_token": "123456:ABC-DEF...",
    "admin_id": 123456789,
    "max_concurrent_global": 2,
    "max_concurrent_per_user": 1,
    "cache_limit_gb": 4,
    "cleanup_interval_sec": 3600,
    "max_telegram_size_mb": 50,
    "compress_strategy": "downgrade_quality",
    "max_telegram_caption_length": 1024,
    "url_whitelist": ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "x.com", "reddit.com", "v.redd.it", "soundcloud.com", "bandcamp.com"],
    "block_private_ips": true,
    "yt_dlp_auto_update": true,
    "yt_dlp_update_interval_hours": 48,
    "healthcheck_port": 8080,
    "log_max_bytes": 10485760,
    "log_backup_count": 5,
    "placeholder_video_file_id": "abc...",
    "default_video_quality": "best",
    "max_video_height": 1080
}
```

| Поле | Описание |
|------|----------|
| `bot_token` | Токен от @BotFather |
| `admin_id` | Telegram ID администратора |
| `max_concurrent_global` | Глобальный семафор загрузок |
| `max_concurrent_per_user` | Семафор на пользователя (антиспам) |
| `cache_limit_gb` | Лимит LRU-кэша |
| `cleanup_interval_sec` | Интервал автоочистки кэша |
| `max_telegram_size_mb` | Лимит Telegram на файл (50 MB) |
| `compress_strategy` | `downgrade_quality` — перекачать в меньшем качестве, если не влезает |
| `url_whitelist` | Разрешённые домены |
| `block_private_ips` | Блокировать `localhost`, `192.168.x.x`, `file://` |
| `yt_dlp_auto_update` | Автообновление yt-dlp |
| `yt_dlp_update_interval_hours` | Интервал автообновления |
| `healthcheck_port` | Порт для HTTP healthcheck |
| `placeholder_video_file_id` | `file_id` заглушки для inline (1-сек видео, загруженное вручную) |
| `default_video_quality` | Начальное качество (`best`) |
| `max_video_height` | Максимальная высота видео (1080) |

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
- Проверка выполняется **до** передачи URL в yt-dlp.

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
└── 004_add_audio_flag.sql
```

При старте бот проверяет `PRAGMA user_version` и применяет недостающие миграции.

### 6.2 Таблица `downloads`

```sql
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    url TEXT NOT NULL,
    source TEXT DEFAULT 'ytdlp',
    quality TEXT,                   -- 'best', '720p', '480p', '360p', 'audio'
    is_audio INTEGER DEFAULT 0,     -- 1 если -mp3
    file_size_mb REAL,
    file_path TEXT,                 -- локальный путь
    telegram_file_id TEXT,          -- file_id для повторной отправки
    status TEXT DEFAULT 'pending',  -- pending, processing, done, cached, deleted, failed, cancelled
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_user_history ON downloads(user_id, last_accessed);
CREATE INDEX idx_status ON downloads(status);
CREATE INDEX idx_url ON downloads(url);
CREATE INDEX idx_file_id ON downloads(telegram_file_id);
CREATE INDEX idx_audio ON downloads(is_audio);
```

### 6.3 Таблица `user_settings`

```sql
CREATE TABLE user_settings (
    user_id INTEGER PRIMARY KEY,
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

### 7.1 Постфикс `-mp3`

Работает **везде**: в чате и в инлайне.

**Чат:**
- Пользователь отправляет: `https://youtube.com/watch?v=... -mp3`
- Бот парсит URL и флаг `-mp3`.
- Если флаг установлен — `is_audio=1`, yt-dlp запускается с `-x --audio-format mp3`.

**Инлайн:**
- `@bot <url> -mp3`
- Аналогично, флаг передаётся в логику загрузки.

**Парсинг:**
```python
def parse_args(text: str) -> tuple[str, bool]:
    text = text.strip()
    is_mp3 = text.endswith("-mp3") or " -mp3" in text
    if is_mp3:
        text = text.replace(" -mp3", "").replace("-mp3", "").strip()
    url = extract_url(text)
    return url, is_mp3
```

### 7.2 Чат-режим (Reply Mode)

**Триггер:** бот видит сообщение с URL из `url_whitelist`.

**Поведение:**
1. Валидация URL → реакция ⏳ или ответ "⏳ Качаю...".
2. Проверка дубликата: если `url` + `is_audio` уже есть в БД со статусом `done`/`cached` и `telegram_file_id` — отправить сразу по `file_id`.
3. Предварительный анализ форматов (см. раздел 8.1) — выбор оптимального формата.
4. Загрузка через yt-dlp с выбранным форматом.
5. Отправка медиа, обновление статуса.

**Обработка caption:**
- Название видео обрезается до 1024 символов (лимит Telegram для caption).
- Добавление источника: `via @botname` (если влезает).

**Media Group (Instagram Carousel):**
- Если yt-dlp возвращает несколько файлов (album):
  - До 10 файлов: отправка через `send_media_group`.
  - Более 10: разбиение на несколько групп или отправка первых 10 + ссылка на остальные.

### 7.3 Инлайн-режим

**Триггер:** `@имябота <ссылка> [-mp3]`

**Пустой запрос (`@имябота `):** показывает историю загрузок пользователя (последние 10).

**Механика загрузки:**
1. Пользователь вводит запрос → валидация URL (`security.py`).
2. Бот отвечает `answer_inline_query` с одним `InlineQueryResultCachedVideo` — заглушка (используется `placeholder_video_file_id` из конфига).
3. Пользователь тапает результат.
4. Бот получает `ChosenInlineResult` (требуется `/setinlinefeedback` в BotFather).
5. Бот качает медиа (с глобальным + per-user семафором).
6. Бот редактирует сообщение через `edit_message_media` по `inline_message_id`.
7. Результат сохраняется в БД + кэш.

**Заглушка — подробно:**
- Ты один раз (вручную) загружаешь в любой чат короткое видео (1 сек, чёрный экран с текстом "⏳ Загрузка...").
- Telegram возвращает `file_id` этого видео.
- Записываешь `file_id` в `config.json` → `placeholder_video_file_id`.
- Бот использует `InlineQueryResultCachedVideo` с этим `file_id` — сообщение вставляется мгновенно.
- После готовности реального видео — `edit_message_media(inline_message_id=..., media=InputMediaVideo(...))`.
- **Важно:** тип не меняется (видео → видео), поэтому заглушка обязана быть видео.

**Прогресс в инлайне:**
- Заглушка обновляется текстом: "⏳ Загружаю... (N%)" или "⏳ В очереди (позиция: 2)".
- Обновление каждые 5 секунд через `edit_message_caption` (или `edit_message_media` с тем же видео, но новым caption).

### 7.4 Админка (Telegram)

Доступна только `admin_id`.

| Команда | Описание |
|---------|----------|
| `/stats` | Загрузки, размер кэша, активные задачи, uptime |
| `/cleanup` | Принудительная очистка кэша |
| `/ban <user_id> [причина]` | Заблокировать пользователя |
| `/unban <user_id>` | Разблокировать |
| `/logs [N]` | Последние N строк логов (по умолчанию 20) |
| `/restart` | Graceful restart бота |
| `/ytupdate` | Принудительное обновление yt-dlp |
| `/setplaceholder` | Инструкция по загрузке заглушки (отправляет подсказку) |

---

## 8. Загрузка медиа (yt-dlp only)

### 8.1 Предварительный анализ форматов (оптимизация)

Вместо слепой загрузки и последующей проверки размера:

```bash
yt-dlp -j --no-playlist --cookies ~/nxDLbot/cookies/cookies.txt <URL>
```

Это возвращает JSON (info_dict) со всеми доступными форматами. Каждый формат содержит:
- `format_id`
- `ext`
- `height`, `width`
- `filesize` (точный, если известен)
- `filesize_approx` (приблизительный)
- `vcodec`, `acodec`
- `abr`, `vbr`

**Алгоритм выбора формата:**

```
1. Получить info_dict через yt-dlp -j
2. Отфильтровать форматы:
   - Только видео (или аудио если -mp3)
   - height <= max_video_height (из конфига, по умолчанию 1080)
   - Не live (!is_live)
3. Сортировать по качеству (height desc, затем filesize_approx desc)
4. Выбрать лучший формат, у которого:
   - filesize < 50MB (если известен)
   - ИЛИ filesize_approx < 50MB
   - ИЛИ height <= 720 (если размер неизвестен — предполагаем, что 720p обычно влезает)
5. Если ничего не подошло под 50MB:
   - Попытка 2: фильтр height <= 480
   - Попытка 3: фильтр height <= 360
   - Попытка 4: worst
6. Если всё ещё >50MB после скачивания:
   - Получить прямую ссылку через yt-dlp --get-url
   - Отправить текст: "📎 Файл слишком большой для Telegram: [ссылка]"
```

**Для аудио (-mp3):**
```bash
yt-dlp -j -x --audio-format mp3 <URL>
```
- Выбираем `bestaudio`.
- Проверяем `filesize`/`filesize_approx`.
- Если >50MB — `bestaudio[abr<=128]` или аналогичный downgrade.

**Форматная строка для скачивания:**
```python
# После анализа форматов, выбираем конкретный format_id
format_str = f"{best_format['format_id']}+bestaudio/best"
# Или для аудио:
format_str = "bestaudio/best"
```

### 8.2 yt-dlp команда

**Базовая:**
```bash
yt-dlp \
    --no-playlist \
    --max-downloads 1 \
    --match-filters "!is_live" \
    --cookies ~/nxDLbot/cookies/cookies.txt \
    -f "{format_id}+bestaudio/best" \
    --merge-output-format mp4 \
    -o "downloads/%(id)s.%(ext)s" \
    <URL>
```

**Аудио (-mp3):**
```bash
yt-dlp \
    --no-playlist \
    --max-downloads 1 \
    --cookies ~/nxDLbot/cookies/cookies.txt \
    -f "bestaudio/best" \
    -x --audio-format mp3 \
    --audio-quality 0 \
    -o "downloads/%(id)s.%(ext)s" \
    <URL>
```

**Защита от плейлистов:**
- `--no-playlist` — обязательно.
- Дополнительная проверка URL: если содержит `&list=` или `playlist`, выдать предупреждение.

**Защита от live:**
- `--match-filters "!is_live"` — отклонить livestream.
- `--no-live-from-start` — не начинать бесконечную загрузку.

### 8.3 Обработка > 50 MB

Если предварительный анализ ошибся (размер неизвестен или приблизителен):

1. После скачивания проверить `os.path.getsize()`.
2. Если > 50 MB:
   - Попытка 2: перекачать с `-f "best[height<=480]"`.
   - Попытка 3: `-f "worst[ext=mp4]/worst"`.
3. Если всё ещё > 50 MB:
   - `yt-dlp --get-url -f best <URL>` → получить прямую ссылку.
   - Отправить пользователю: "📎 Файл слишком большой для Telegram. [ссылка]"
   - Статус в БД: `failed` (причина: too_large) или `done` (с `error_msg`).

**ffmpeg используется только для:**
- Мержа видео + аудио (`--merge-output-format mp4`).
- Конвертации аудио (`-x --audio-format mp3`).

### 8.4 Обновление yt-dlp

- `yt-dlp -U` или `pip install -U yt-dlp` каждые 48 часов (фоновая задача).
- Команда `/ytupdate` для принудительного обновления.

---

## 9. LRU-кэш (4 GB)

### 9.1 Алгоритм

1. Все файлы yt-dlp сохраняются в `downloads/`.
2. При каждом обращении обновляется `last_accessed` в БД.
3. **При повторной отправке по `telegram_file_id`:** обновлять `last_accessed` в БД (чтобы файл не был удалён, пока `file_id` актуален).
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

- **yt-dlp:** `telegram_file_id` хранится после первой отправки. Повторная отправка через `InputMediaVideo(media=file_id)` без повторной загрузки.
- **История в инлайне:** пустой запрос `@бот ` показывает последние 10 загрузок с `telegram_file_id`.

---

## 10. Семафор и очередь

```python
import asyncio
from collections import defaultdict

download_semaphore_global = asyncio.Semaphore(max_concurrent_global)
user_semaphores = defaultdict(lambda: asyncio.Semaphore(max_concurrent_per_user))
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
pkg install python python-pip git ffmpeg
pip install aiogram aiohttp aiosqlite yt-dlp
```

### 11.2 Cookies для Instagram

```bash
mkdir -p ~/nxDLbot/cookies
# Экспорт cookies из браузера (на ПК) через Get cookies.txt LOCALLY
# Скопировать на телефон в ~/nxDLbot/cookies/cookies.txt
```

### 11.3 Заглушка для инлайна

```bash
# 1. Создать 1-секундное видео (чёрный экран с текстом)
# Можно через ffmpeg:
ffmpeg -f lavfi -i color=c=black:s=320x240:d=1 -vf "drawtext=text='Loading...':fontsize=30:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" -c:v libx264 -t 1 -pix_fmt yuv420p placeholder.mp4

# 2. Загрузить placeholder.mp4 в любой чат через бота
# 3. Бот логирует file_id (или ты получаешь его через API/логи)
# 4. Записать file_id в config.json → placeholder_video_file_id
```

### 11.4 Бот

```bash
cd ~/nxDLbot
nano config.json  # вставить токен и placeholder_video_file_id
python bot.py
```

### 11.5 Автозапуск и выживание в фоне

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
adb shell dumpsys deviceidle whitelist +com.termux
adb shell appops set com.termux RUN_IN_BACKGROUND allow
adb shell appops set com.termux WAKE_LOCK allow
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
adb shell settings put global cached_apps_freezer enabled 0
```

---

## 12. Обработка ошибок

| Сценарий | Действие |
|----------|----------|
| yt-dlp не нашёл форматы | "Не удалось извлечь медиа. Возможно, требуется авторизация (Instagram) или ссылка недействительна." |
| Файл > 50 MB после downgrade | Отправка прямой ссылки |
| Telegram API ошибка (429) | Экспоненциальный backoff (aiogram throttling) |
| FloodWait | Автоматический retry с удвоением задержки |
| OOM / нехватка памяти | Семафор ограничивает нагрузку, cleanup чистит кэш |
| Invalid URL / private IP | "❌ URL не поддерживается или недоступен" |
| Livestream | Отклонение с сообщением "❌ Live-трансляции не поддерживаются" |
| Playlist URL | "❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео." |
| Caption > 1024 | Автообрезка до 1024 символов + многоточие |
| Instagram 403 (login required) | "❌ Instagram требует авторизации. Админ может загрузить cookies.txt" |
| Заглушка не настроена | "⚠️ Заглушка для инлайна не настроена. Используйте /setplaceholder" |

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
# Запуск на localhost:8080
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

### Этап 2: yt-dlp ядро
- [ ] `ytdlp.py` — предварительный анализ форматов (`-j`)
- [ ] Логика выбора качества (best под 50MB)
- [ ] Скачивание видео и аудио (-mp3)
- [ ] Обработка >50MB (downgrade → ссылка)
- [ ] Graceful shutdown

### Этап 3: Чат-режим
- [ ] Распознавание ссылок + парсинг `-mp3`
- [ ] Проверка дубликатов (idx_url + is_audio)
- [ ] Отправка видео/аудио + caption truncate
- [ ] Семафор + очередь с позицией
- [ ] Media group (Instagram carousel)

### Этап 4: Инлайн
- [ ] InlineQuery handler + валидация
- [ ] Заглушка (`InlineQueryResultCachedVideo`)
- [ ] `ChosenInlineResult` + скачивание
- [ ] `edit_message_media` по `inline_message_id`
- [ ] Парсинг `-mp3` в инлайне
- [ ] История в пустом инлайн-запросе

### Этап 5: Кэш и полировка
- [ ] LRU-кэш менеджер
- [ ] Сохранение `telegram_file_id`
- [ ] Обновление `last_accessed` при повторной отправке
- [ ] Автоочистка + синхронизация БД↔ФС
- [ ] Ротация логов

### Этап 6: Админка и деплой
- [ ] Команды /stats, /cleanup, /logs, /ban, /ytupdate, /setplaceholder
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

**Системные:** `python`, `ffmpeg`, `termux-api` (для уведомлений).

---

*Plan v1.2 — nxDLbot (август 2026)*
