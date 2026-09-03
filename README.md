# nxDLbot v1.2 — Telegram Media Downloader (Termux)

Только **yt-dlp**, автовыбор лучшего качества до 50 МБ. Хостинг в **Termux** на Android (polling, aiogram 3.x).

## Структура

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
│   └── ytdlp.py           # yt-dlp: анализ форматов -J + автовыбор под 50MB
└── utils/
    ├── db.py              # Миграции SQLite
    ├── db_migrations/     # 001..005 (005: is_audio + url_norm)
    ├── security.py        # SSRF, whitelist, private IP
    ├── rate_limiter.py    # Семафор + rate limit
    ├── helpers.py         # Парсинг -mp3, нормализация URL
    └── cache_manager.py   # LRU + синхронизация БД↔ФС
```

## 1. Установка в Termux (с нуля)

> Ставь Termux только с **F-Droid** (версия с Play Store заброшена).

**Быстрый путь — один скрипт:**
```bash
cd ~/nxDLbot
bash setup.sh
```
Он поставит пакеты, зависимости, создаст папки и `config.json` (спросит токен и admin_id),
сгенерирует заглушки `placeholder.mp4`/`placeholder.mp3`, установит автозапуск и watchdog.

**Вручную (если скрипту не доверяешь):**

```bash
# 1. База (Termux:API и Termux:Boot — отдельные приложения с F-Droid, ставятся вручную)
pkg update && pkg upgrade -y
pkg install python python-pip git ffmpeg termux-api -y

# 2. Код
git clone <твой-репо> ~/nxDLbot
# или скопируй файлы вручную в ~/nxDLbot
cd ~/nxDLbot
cp config.json.example config.json
nano config.json   # вставь bot_token от @BotFather и admin_id

# 3. Зависимости (строго из папки бота!)
pip install --upgrade pip
pip install -r requirements.txt
# Пины pydantic + TUR-индекс уже зашиты в requirements.txt: у pydantic-core
# нет колёс под Android на PyPI, и голый pip падает на сборке через Rust.
# В requirements лежат pydantic==2.12.4 + pydantic-core==2.41.5 — под них
# в TUR есть готовые колёса (проверено для Python 3.14/aarch64).
# ВАЖНО: вводи URL индекса буква в букву — .../pypi/, не .../pupi/ :)

# 4. Настройки BotFather (обязательно для инлайна!)
# @BotFather → /mybots → твой бот:
#   /setinline       — включить inline mode (placeholder, например: "🔍 вставь ссылку")
#   /setinlinefeedback — Enable (иначе бот не узнает о тапе на заглушку!)
#   /setprivacy      — Disable НЕ нужен (бот читает только ссылки/команды)

# 5. Первый запуск (проверочный, на переднем плане)
python bot.py
# в другом сеансе Termux: curl -sf http://localhost:8080/health && echo ALIVE
# останови: Ctrl+C
```

Узнать свой `admin_id`: напиши боту `@userinfobot` в Telegram, он ответит твоим ID.

## 2. Заглушки для инлайна (2 шт: видео + аудио)

Без них инлайн отвечает ошибкой «заглушка не настроена» (чат-режим работает и без них).

```bash
# видео-заглушка 1 сек
ffmpeg -f lavfi -i color=c=black:s=320x240:d=1 \
  -vf "drawtext=text='Loading...':fontsize=30:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" \
  -c:v libx264 -t 1 -pix_fmt yuv420p placeholder.mp4
# аудио-заглушка: любой короткий mp3 (3 сек тишины)
ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 3 -c:a libmp3lame placeholder.mp3
```

1. Отправь оба файла боту **в личку**.
2. В логе появятся строки `PLACEHOLDER_CANDIDATE video file_id=...` / `audio file_id=...`:
   `tail ~/nxDLbot/logs/bot.log | grep PLACEHOLDER`
3. Впиши оба `file_id` в `config.json` → `placeholder_video_file_id`, `placeholder_audio_file_id`.
4. Перезапусти: `/restart` в чате с ботом (админ) или рестарт процесса.

## 3. Cookies (Instagram и др., позже)

```bash
mkdir -p ~/nxDLbot/cookies
# На ПК: расширение "Get cookies.txt LOCALLY" → экспорт для instagram.com
cp /sdcard/Download/cookies.txt ~/nxDLbot/cookies/cookies.txt
```

## 4. Фон, автозапуск, watchdog

```bash
# Termux:Boot — приложение с F-Droid (ставится вручную, пакета в pkg нет).
# Без него автозапуск после перезагрузки не работает, но сам бот — работает.
pkg install termux-api
mkdir -p ~/.termux/boot
cp ~/nxDLbot/start-bot.sh ~/.termux/boot/start-bot.sh
chmod +x ~/.termux/boot/start-bot.sh ~/nxDLbot/watchdog.sh

# watchdog каждые 5 мин через JobScheduler (рекомендуется)
termux-job-scheduler --job-id 1 --period-ms 300000 \
  --script $HOME/nxDLbot/watchdog.sh --persisted true
# или через cron:
pkg install cronie termux-services && crond
echo "*/5 * * * * $HOME/nxDLbot/watchdog.sh >> $HOME/nxDLbot/logs/watchdog.log 2>&1" | crontab -
```

Android UI (иначе система убьёт бота за 5–30 мин):
1. Настройки → Приложения → Termux → Батарея → **Без ограничений**
2. Уведомления Termux → разрешить; закрепи Termux в недавних (замок)
3. ADB с ПК: `chmod +x adb-setup.sh && ./adb-setup.sh`

## 5. Режимы

- **Чат**: кинь ссылку — бот сам выберет лучшее качество ≤50 МБ. Суффикс `-mp3` → аудио.
- **Инлайн**: `@бот https://...` (видео) или `@бот https://... -mp3` (аудио). Пустой `@бот ` — история (10 последних).
- Дубликаты отдаются мгновенно: по `telegram_file_id`, иначе с диска без повторного скачивания.
- Файл >50 МБ даже в 360p → бот пришлёт прямую ссылку (`--get-url`).

## 6. Админ-команды

`/stats /cleanup /ban /unban /logs [N] /restart /ytupdate /setplaceholder`

## 7. Конфиг

См. `config.json.example`. Ключевое: `bot_token`, `admin_id`, `cache_limit_gb`,
`max_telegram_size_mb` (не поднимай выше 50 — лимит Bot API), `url_whitelist`
(можно править без перезапуска логики — применяется при рестарте),
`max_video_height` (потолок качества), `placeholder_video/audio_file_id`.

## 8. Обновление

```bash
cd ~/nxDLbot && git pull
pip install -U -r requirements.txt   # или /ytupdate из чата для yt-dlp
```

## 9. Зависимости

```
aiogram>=3.0.0
aiohttp>=3.8.0
aiosqlite>=0.19.0
yt-dlp>=2026.01.01
```

Системные: `python`, `ffmpeg`, `termux-api`, приложение `Termux:Boot` (F-Droid, для автозапуска).

## 10. Если aiogram не ставится (Termux)

Симптом: `pip install` падает на `pydantic-core` / `maturin` / `can't find Rust compiler`.
Причина: на PyPI нет колёс под Android (bionic libc), pip пытается собрать Rust-пакет из исходников.

1. Штатный путь: `pip install -r requirements.txt` из папки бота — пины
   (`pydantic==2.12.4`, `pydantic-core==2.41.5`) и TUR-индекс уже внутри файла.
   Проверь две вещи:
   - команда запущена именно из `~/nxDLbot` (где лежит requirements.txt);
   - в логе pip строка `Looking in indexes` содержит `...github.io/pypi/` (именно **pypi**, не `pupi` — такая опечатка уже была :)).
   Успех выглядит так: `Downloading pydantic_core-2.41.5-...-android_24_arm64_v8a.whl` — сразу `.whl`, без `Building wheel`.
2. Запасной путь (долго, ~10–20 мин, телефон греется): собрать из исходников:
   ```bash
   pkg install rust
   pip install -r requirements.txt   # не закрывай Termux, держи wakelock: termux-wake-lock
   ```
3. Если ошибка НЕ про pydantic-core — скинь хвост лога (`pip install -r requirements.txt` последние 30 строк), разберём.
