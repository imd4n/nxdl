# nxDLbot — Telegram Media Downloader (Termux)

Гибридный бот для загрузки медиа: **Cobalt API (приоритет) + yt-dlp fallback**, хостинг в **Termux** на Android (polling, aiogram 3.x).

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
│   ├── cobalt.py          # Cobalt API (конфигурируемый endpoint, обязателен)
│   └── ytdlp.py           # yt-dlp subprocess + downgrade_quality
└── utils/
    ├── db.py              # Миграции SQLite
    ├── security.py        # SSRF, whitelist, private IP
    ├── rate_limiter.py    # Семафор + rate limit
    ├── helpers.py         # Парсинг URL/флагов
    └── cache_manager.py   # LRU + синхронизация БД↔ФС
```

## 1. Установка в Termux

> Ставь Termux только с **F-Droid** (версия с Play Store заброшена и без `termux-api`).

```bash
pkg update && pkg upgrade -y
pkg install python python-pip git ffmpeg nodejs termux-api termux-tools
pip install -r requirements.txt

# обязательно: self-hosted Cobalt
git clone https://github.com/imputnet/cobalt.git ~/cobalt
cd ~/cobalt
npm install
npm run build
# проверка endpoint (может меняться — смотри доку cobalt)
# запуск:
nohup node api/src/index.js > /tmp/cobalt.log 2>&1 &
curl -sf http://localhost:9000/api/json -X POST -H "Content-Type: application/json" -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}' | head

# бот
cd ~/nxDLbot
cp config.json.example config.json
nano config.json  # вставь bot_token от @BotFather, admin_id, cobalt_url
python bot.py
```

В `config.json`:
```json
{
  "cobalt_url": "http://localhost:9000",
  "cobalt_api_path": "/api/json"
}
```
`cobalt_url` должен указывать на твой локальный инстанс. Если меняешь порт/host — меняй оба поля.

## 2. Дать Termux все разрешения и снять ограничения (обязательно!)

Без этого Android убьёт бота через 5–30 минут в фоне. Делай **все** пункты.

### 2.1 Android UI (вручную)

1. **Настройки → Приложения → Termux → Батарея → Без ограничений / Unrestricted** (на Xiaomi: *Экономия заряда → Без ограничений*, на Samsung: *Не оптимизировать*)
2. **Настройки → Приложения → Termux → Уведомления → Разрешить** (нужно для `termux-notification` и foreground)
3. **Настройки → Приложения → Termux:API → Батарея → Без ограничений** (если установлен)
4. **Настройки → Приложения → Termux:Boot → Автозапуск → Разрешить** (если установлен)
5. На LineageOS / AOSP: **Настройки → Система → Для разработчиков → Отключить оптимизацию Doze для Termux** (если есть)
6. Закрепи Termux в меню недавних приложений (иконка замка), чтобы свайп не убивал процесс.

### 2.2 Внутри Termux

```bash
pkg install termux-api termux-boot

# запретить системе усыплять Termux (держит wakelock)
termux-wake-lock
termux-wake-unlock # проверка: должен показать статус, затем снова termux-wake-lock

# проверка что уведомления работают
termux-notification --title "nxDLbot test" --content "если видишь — разрешения ок" --id 1

# доступ к хранилищу (для cookies/downloads если нужно)
termux-setup-storage

# проверка батареи/оптимизации
termux-battery-status | grep -i optim
```

`termux-wake-lock` нужно выполнять при каждом запуске. Он уже встроен в `start-bot.sh` и `~/.termux/boot/start-bot.sh`.

### 2.3 ADB — снять системные ограничения (LineageOS / любой с adb)

Подключи телефон по USB, включи **Отладка по USB**, затем с ПК:

```bash
# 1. Whitelist от Doze (бот не засыпает)
adb shell dumpsys deviceidle whitelist +com.termux
adb shell dumpsys deviceidle whitelist +com.termux.api
adb shell dumpsys deviceidle whitelist +com.termux.boot

# проверка:
adb shell dumpsys deviceidle whitelist | grep termux

# 2. Разрешить фоновую работу и wakelock
adb shell appops set com.termux RUN_IN_BACKGROUND allow
adb shell appops set com.termux WAKE_LOCK allow
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
adb shell appops set com.termux.api RUN_IN_BACKGROUND allow

# 3. Отключить заморозку кэшированных приложений (Android 12+ убивает Termux)
adb shell settings put global cached_apps_freezer enabled 0

# 4. Отключить адаптивную батарею для Termux (если есть)
adb shell settings put global adaptive_battery_management_enabled 0

# проверка:
adb shell cmd appops get com.termux RUN_IN_BACKGROUND
adb shell settings get global cached_apps_freezer
```

Скрипт уже в репо: `chmod +x adb-setup.sh && ./adb-setup.sh`

> Для MIUI/HyperOS/OneUI дополнительно выключи *Оптимизацию MIUI*, включи *Автозапуск* для Termux в фирменном менеджере.

## 3. Автозапуск и watchdog

```bash
# автозапуск при перезагрузке (требует Termux:Boot)
mkdir -p ~/.termux/boot
cp ~/nxDLbot/start-bot.sh ~/.termux/boot/start-bot.sh
chmod +x ~/.termux/boot/start-bot.sh
# содержимое start-bot.sh уже делает termux-wake-lock + nohup python bot.py

# watchdog — проверка каждые 5 минут
chmod +x ~/nxDLbot/watchdog.sh

# вариант A: через cron (termux-job-scheduler в новых Android надёжнее cron)
pkg install cronie termux-services
crond
echo "*/5 * * * * $HOME/nxDLbot/watchdog.sh >> $HOME/nxDLbot/logs/watchdog.log 2>&1" | crontab -

# вариант B: через Termux:JobScheduler (рекомендуется)
termux-job-scheduler --job-id 1 --period-ms 300000 --script $HOME/nxDLbot/watchdog.sh --persisted true

# healthcheck вручную:
curl -sf http://localhost:8080/health && echo "bot alive" || echo "bot down"
```

`watchdog.sh` уже настроен на `http://localhost:8080/health` (порт из `config.json:healthcheck_port`). Если бот упал — шлёт `termux-notification` и рестартит.

## 4. Cobalt self-host — обязательно

Без него бот в режиме `auto` всегда будет уходить в `yt-dlp` (медленнее, хуже с Instagram/TikTok, нет instant CDN-ссылок).

```bash
# зависимости уже установлены выше
cd ~/cobalt
git pull
npm install
npm run build

# запуск в фоне (вариант 1 — nohup)
nohup node api/src/index.js > /tmp/cobalt.log 2>&1 &
# вариант 2 — pm2 (авторестарт)
npm i -g pm2
pm2 start api/src/index.js --name cobalt
pm2 save
pm2 startup

# проверка:
curl -s http://localhost:9000/api/json -X POST -H "Content-Type: application/json" -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","downloadMode":"auto"}' | jq
```

**Ограничения self-host на Termux (ARM, без браузера):**
- «Простой режим без браузера» не работает для Instagram и частично TikTok — для них нужен `yt-dlp fallback` или headless Chrome (ресурсоёмко на телефоне, не рекомендуется).
- Держи `cookies.txt` для Instagram (см. ниже) — тогда `yt-dlp` подхватит приватный контент.
- Если Cobalt упал — бот вернёт ошибку для `-c` и сделает fallback для `auto`. Следи за логами: `tail -f /tmp/cobalt.log`.

## 5. Конфиг

См. `config.json.example`. Ключевые поля:
- `bot_token`, `admin_id`
- `cobalt_url` + `cobalt_api_path` (обязательно локальный, по умолчанию `http://localhost:9000/api/json`)
- `cache_limit_gb`, `max_telegram_size_mb`, `compress_strategy: downgrade_quality|skip`
- `url_whitelist`, `block_private_ips`
- `healthcheck_port` (для watchdog)

## 6. Режимы

- **Чат**: кинь ссылку в чат — бот покажет кнопки качества (720/480/аудио) или сразу скачает. Дубликаты отдаются по `telegram_file_id`.
- **Инлайн**: `@бот https://...` с флагами `-c` (cobalt обязателен), `-y` (yt-dlp), `-a` (аудио). Пустой запрос `@бот ` — история (10 последних).

## 7. Админ-команды

`/stats /cleanup /ban /unban /logs [N] /restart /ytupdate /setdownloader`

## 8. Cookies (Instagram)

Экспортируй `cookies.txt` расширением **Get cookies.txt LOCALLY** на ПК и скопируй в `cookies/cookies.txt` (в Termux: `cp /sdcard/Download/cookies.txt ~/nxDLbot/cookies/`).

## 9. Vercel (legacy)

Старый webhook-бот в `api/index.py` остался для совместимости. Для Termux-режима используй `bot.py` (polling).
Для Vercel установи `python-telegram-bot` дополнительно: `pip install python-telegram-bot`.

## 10. Зависимости

```
aiogram>=3.0.0
aiohttp>=3.8.0
aiosqlite>=0.19.0
yt-dlp>=2024.1.0
```
Системные: `ffmpeg`, `nodejs` (для обязательного self-hosted cobalt), `termux-api`, `termux-boot`.
