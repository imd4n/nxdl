#!/bin/bash
# Watchdog по плану §11.5 — проверять каждые 5 минут через cron/termux-job-scheduler
HEALTH_URL="http://localhost:8080/health"
BOT_DIR="$HOME/nxDLbot"
# если запускается из репозитория — пробуем текущую директорию
if [ ! -d "$BOT_DIR" ]; then
  BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

if ! curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
    echo "$(date): Healthcheck failed, restarting bot..."
    if command -v termux-notification >/dev/null 2>&1; then
        termux-notification --title "nxDLbot" --content "Watchdog: перезапуск бота..." --ongoing 2>/dev/null || true
    fi
    pkill -f "python bot.py" 2>/dev/null || true
    sleep 2
    cd "$BOT_DIR" && nohup python bot.py > logs/bot.log 2>&1 &
    echo "$(date): Bot restarted, pid $!"
else
    echo "$(date): Healthcheck OK"
fi
