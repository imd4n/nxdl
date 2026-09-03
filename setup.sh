#!/bin/bash
# nxDLbot v1.2 — one-shot установка в Termux.
# Запуск: bash setup.sh   (из корня репозитория ~/nxDLbot)
set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

echo "=== [1/7] Пакеты Termux ==="
pkg update -y
pkg install -y python python-pip git ffmpeg termux-api termux-boot

echo "=== [2/7] Python-зависимости ==="
pip install --upgrade pip
pip install -r requirements.txt
pip install -U yt-dlp

echo "=== [3/7] Папки ==="
mkdir -p downloads logs cookies

echo "=== [4/7] config.json ==="
if [ ! -f config.json ]; then
  cp config.json.example config.json
  echo "Создан config.json из примера."
fi
if grep -q "123456:ABC-DEF" config.json; then
  echo ""
  read -r -p "Вставь токен от @BotFather: " TOKEN
  read -r -p "Вставь admin_id (узнать можно у @userinfobot): " ADMIN_ID
  TOKEN="$TOKEN" ADMIN_ID="$ADMIN_ID" python -c "
import json, os
p = 'config.json'
cfg = json.load(open(p, encoding='utf-8'))
t = os.environ.get('TOKEN', '').strip()
a = os.environ.get('ADMIN_ID', '').strip()
if t: cfg['bot_token'] = t
if a.isdigit(): cfg['admin_id'] = int(a)
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=4)
print('config.json обновлён.')
"
else
  echo "config.json уже заполнен — пропускаю."
fi

echo "=== [5/7] Заглушки для инлайна ==="
if [ ! -f placeholder.mp4 ]; then
  ffmpeg -y -f lavfi -i color=c=black:s=320x240:d=1 \
    -vf "drawtext=text='Loading...':fontsize=30:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -t 1 -pix_fmt yuv420p placeholder.mp4 2>/dev/null \
    && echo "placeholder.mp4 создан." || echo "WARN: не вышло создать mp4 (ffmpeg?)"
else
  echo "placeholder.mp4 уже есть."
fi
if [ ! -f placeholder.mp3 ]; then
  ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t 3 -c:a libmp3lame placeholder.mp3 2>/dev/null \
    && echo "placeholder.mp3 создан." || echo "WARN: не вышло создать mp3 (ffmpeg?)"
else
  echo "placeholder.mp3 уже есть."
fi

echo "=== [6/7] Автозапуск и watchdog ==="
chmod +x start-bot.sh watchdog.sh adb-setup.sh setup.sh 2>/dev/null || true
mkdir -p ~/.termux/boot
cp start-bot.sh ~/.termux/boot/start-bot.sh
chmod +x ~/.termux/boot/start-bot.sh
echo "Boot-скрипт установлен в ~/.termux/boot/start-bot.sh"
if command -v termux-job-scheduler >/dev/null 2>&1; then
  termux-job-scheduler --job-id 1 --period-ms 300000 \
    --script "$BOT_DIR/watchdog.sh" --persisted true \
    && echo "Watchdog запланирован (каждые 5 мин)." || echo "WARN: не вышло запланировать watchdog."
else
  echo "SKIP: termux-job-scheduler недоступен — watchdog можно поднять вручную (см. README)."
fi

echo "=== [7/7] Проверка ==="
python -m py_compile bot.py && echo "Python-код компилируется."
curl -sf http://localhost:8080/health >/dev/null 2>&1 \
  && echo "Похоже, бот уже запущен (health OK)." \
  || echo "Бот пока не запущен — это нормально."

echo ""
echo "Готово! Дальше:"
echo "  1. BotFather: /setinline и /setinlinefeedback (Enable) для твоего бота."
echo "  2. Первый запуск:  cd $BOT_DIR && python bot.py"
echo "  3. Отправь боту в личку placeholder.mp4 и placeholder.mp3,"
echo "     забери file_id из лога:  grep PLACEHOLDER logs/bot.log"
echo "     впиши их в config.json и перезапусти:  pkill -f 'python bot.py'; python bot.py"
echo "  4. Cookies (позже): положи cookies.txt в $BOT_DIR/cookies/"
