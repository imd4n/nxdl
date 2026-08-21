#!/bin/bash
# ADB-настройка по плану §11.6 (LineageOS)
set -e
echo "Whitelist Termux от Doze..."
adb shell dumpsys deviceidle whitelist +com.termux
echo "Фоновая активность..."
adb shell appops set com.termux RUN_IN_BACKGROUND allow
adb shell appops set com.termux WAKE_LOCK allow
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
echo "Отключение cached_apps_freezer (Android 12+)..."
adb shell settings put global cached_apps_freezer enabled 0 || echo "cached_apps_freezer not available"
echo "Готово. Внутри Termux выполни: termux-wake-lock"
