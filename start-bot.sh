#!/data/data/com.termux/files/usr/bin/sh
# Автозапуск по плану §11.5 — положить в ~/.termux/boot/start-bot.sh
termux-wake-lock
if command -v termux-notification >/dev/null 2>&1; then
    termux-notification --title "nxDLbot" --content "Бот запускается..." --ongoing
fi
cd ~/nxDLbot && nohup python bot.py > logs/bot.log 2>&1 &
