# nxDLbot — Telegram Media Downloader (Termux)

Гибридный бот для загрузки медиа: **Cobalt API (приоритет) + yt-dlp fallback**, хостинг в **Termux** на Android (polling, aiogram 3.x).

Реализация по плану [plan_v1.1.md](plan_v1.1.md). Также сохранён legacy Vercel-вариант в `api/index.py`.

## Структура (v1.1)

```
~/nxDLbot/  (или корень репозитория)
├── bot.py                 # Точка входа, хендлеры, graceful shutdown
├── config.json            # Конфигурация (см. config.json.example)
├── requirements.txt
├── bot.db                 # SQLite (создаётся автоматически)
├── downloads/             # LRU-кэш (4 GB)
├── logs/                  # Ротированные логи
├── cookies/               # cookies.txt для Instagram/приватного контента
├── downloaders/
│   ├── cobalt.py          # Cobalt API (конфигурируемый endpoint)
│   └── ytdlp.py           # yt-dlp subprocess + downgrade_quality
└── utils/
    ├── db.py              # Миграции SQLite
    ├── security.py        # SSRF, whitelist, private IP
    ├── rate_limiter.py    # Семафор + rate limit
    ├── helpers.py         # Парсинг URL/флагов
    └── cache_manager.py   # LRU + синхронизация БД↔ФС
```

## Быстрый старт (Termux)

```bash
pkg update && pkg upgrade
pkg install python python-pip git ffmpeg nodejs
pip install -r requirements.txt
# опционально cobalt self-hosted:
# git clone https://github.com/imputnet/cobalt && cd cobalt && npm install && npm run build

cp config.json.example config.json
nano config.json  # вставь bot_token от @BotFather, admin_id

python bot.py
```

## Конфиг

См. `config.json.example`. Ключевые поля:
- `bot_token`, `admin_id`
- `cobalt_url` + `cobalt_api_path` (по умолчанию `http://localhost:9000/api/json`)
- `cache_limit_gb`, `max_telegram_size_mb`, `compress_strategy`
- `url_whitelist`, `block_private_ips`
- `healthcheck_port` (для watchdog)

## Режимы

- **Чат**: кинь ссылку в чат — бот покажет кнопки качества (720/480/аудио) или сразу скачает. Дубликаты отдаются по `telegram_file_id`.
- **Инлайн**: `@бот https://...` с флагами `-c` (cobalt), `-y` (yt-dlp), `-a` (аудио). Пустой запрос `@бот ` — история (10 последних).

## Админ-команды

`/stats /cleanup /ban /unban /logs [N] /restart /ytupdate /setdownloader`

## Watchdog & автозапуск

```bash
# ручной watchdog
chmod +x watchdog.sh && ./watchdog.sh

# автозапуск (Termux:Boot)
mkdir -p ~/.termux/boot
cp start-bot.sh ~/.termux/boot/start-bot.sh
chmod +x ~/.termux/boot/start-bot.sh

# ADB (LineageOS)
chmod +x adb-setup.sh && ./adb-setup.sh
termux-wake-lock
```

## Cookies (Instagram)

Экспортируй `cookies.txt` (Get cookies.txt LOCALLY) на ПК и скопируй в `cookies/cookies.txt`.

## Vercel (legacy)

Старый webhook-бот в `api/index.py` остался для совместимости. Для Termux-режима используй `bot.py` (polling).
Для Vercel установи `python-telegram-bot` дополнительно: `pip install python-telegram-bot`.

## Зависимости

```
aiogram>=3.0.0
aiohttp>=3.8.0
aiosqlite>=0.19.0
yt-dlp>=2024.1.0
```
Системные: `ffmpeg`, `nodejs` (для self-hosted cobalt).
