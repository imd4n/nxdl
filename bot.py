#!/usr/bin/env python3
"""
nxDLbot — Telegram bot (Termux hosting) по плану v1.1
aiogram 3.x + Cobalt (self-hosted) + yt-dlp fallback
"""
import os
import sys
import json
import asyncio
import logging
import signal
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from aiohttp import web
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineQuery, ChosenInlineResult, CallbackQuery,
    InlineQueryResultArticle, InlineQueryResultCachedVideo,
    InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, InputMediaVideo
)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter

# ─── Paths & Config ─────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR = BASE_DIR / "logs"
COOKIES_DIR = BASE_DIR / "cookies"
COOKIES_PATH = COOKIES_DIR / "cookies.txt"
DB_PATH = BASE_DIR / "bot.db"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

BOT_TOKEN = CONFIG.get("bot_token", "")
ADMIN_ID = CONFIG.get("admin_id")
MAX_CONCURRENT_GLOBAL = CONFIG.get("max_concurrent_global", 2)
MAX_CONCURRENT_PER_USER = CONFIG.get("max_concurrent_per_user", 1)
COBALT_URL = CONFIG.get("cobalt_url", "http://localhost:9000")
COBALT_API_PATH = CONFIG.get("cobalt_api_path", "/api/json")
CACHE_LIMIT_GB = CONFIG.get("cache_limit_gb", 4)
HEALTHCHECK_PORT = CONFIG.get("healthcheck_port", 8080)
LOG_MAX_BYTES = CONFIG.get("log_max_bytes", 10 * 1024 * 1024)
LOG_BACKUP_COUNT = CONFIG.get("log_backup_count", 5)
MAX_TELEGRAM_SIZE_MB = CONFIG.get("max_telegram_size_mb", 50)
MAX_CAPTION_LENGTH = CONFIG.get("max_telegram_caption_length", 1024)
URL_WHITELIST = CONFIG.get("url_whitelist", [])
BLOCK_PRIVATE_IPS = CONFIG.get("block_private_ips", True)
YT_DLP_AUTO_UPDATE = CONFIG.get("yt_dlp_auto_update", True)
YT_DLP_UPDATE_INTERVAL_HOURS = CONFIG.get("yt_dlp_update_interval_hours", 48)
PLACEHOLDER_FILE_ID = CONFIG.get("placeholder_video_file_id", "")
COMPRESS_STRATEGY = CONFIG.get("compress_strategy", "downgrade_quality")

# ─── Logging ────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler = RotatingFileHandler(
    LOGS_DIR / "bot.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
)
file_handler.setFormatter(log_formatter)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
logger = logging.getLogger("nxDLbot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# Validation (после logger)
if not BOT_TOKEN or BOT_TOKEN == "123456:ABC-DEF...":
    logger.warning("bot_token not set in config.json — set token from @BotFather")
    _TOKEN_MISSING = True
else:
    _TOKEN_MISSING = False

# ─── Imports after logging ──────────────────────────────────────────────────
from utils.db import init_db, is_banned, find_cached_by_url, insert_download, update_download, touch_accessed, get_user_history
from utils.security import validate_url, extract_urls, is_playlist_url
from utils.rate_limiter import RateLimiter
from utils.helpers import parse_inline_query, truncate_caption, extract_url_from_text
from utils.cache_manager import cleanup_cache, sync_db_fs, get_cache_size_mb
from downloaders.cobalt import CobaltClient, CobaltError
from downloaders.ytdlp import download as ytdlp_download, get_formats, YtDlpError, update_ytdlp

# ─── Global state ───────────────────────────────────────────────────────────
bot: Bot = None
dp: Dispatcher = None
rate_limiter = RateLimiter(
    max_concurrent_global=MAX_CONCURRENT_GLOBAL,
    max_concurrent_per_user=MAX_CONCURRENT_PER_USER,
)
shutting_down = False
start_time = time.time()
active_downloads: set[int] = set()

# ─── Helpers ────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


async def hybrid_download(url: str, flag: str = "auto", audio_only: bool = False) -> dict:
    """
    Гибридная стратегия по плану §8.
    Returns {"type": "file"|"cdn_url", "file_path": str, "cdn_url": str, "filename": str, "source": str, "filesize_mb": float}
    """
    if flag == "cobalt":
        client = CobaltClient(COBALT_URL, COBALT_API_PATH)
        data = await client.extract(url, download_mode="audio" if audio_only else "auto")
        # HEAD check
        ok, msg = await client.head_check(data["cdn_url"], MAX_TELEGRAM_SIZE_MB)
        if not ok:
            raise CobaltError(msg)
        return {"type": "cdn_url", "cdn_url": data["cdn_url"], "filename": data["filename"], "source": "cobalt"}

    if flag == "ytdlp":
        result = await ytdlp_download(
            url, output_dir=str(DOWNLOADS_DIR), cookies_path=str(COOKIES_PATH),
            audio_only=audio_only, max_size_mb=MAX_TELEGRAM_SIZE_MB
        )
        return {"type": "file", "file_path": result["file_path"], "filename": result["title"], "source": "ytdlp", "filesize_mb": result["filesize_mb"]}

    # auto: cobalt -> fallback ytdlp
    if flag == "auto":
        try:
            client = CobaltClient(COBALT_URL, COBALT_API_PATH)
            data = await client.extract(url, download_mode="audio" if audio_only else "auto")
            ok, msg = await client.head_check(data["cdn_url"], MAX_TELEGRAM_SIZE_MB)
            if not ok:
                raise CobaltError(msg)
            return {"type": "cdn_url", "cdn_url": data["cdn_url"], "filename": data["filename"], "source": "cobalt"}
        except Exception as e:
            logger.warning(f"Cobalt failed for auto mode, fallback to yt-dlp: {e}")
            # если cobalt не запущен — fallback
            if "too large" in str(e).lower() or "прямая ссылка" in str(e).lower():
                raise  # прокидываем сообщение о большом файле
            try:
                result = await ytdlp_download(
                    url, output_dir=str(DOWNLOADS_DIR), cookies_path=str(COOKIES_PATH),
                    audio_only=audio_only, max_size_mb=MAX_TELEGRAM_SIZE_MB
                )
                return {"type": "file", "file_path": result["file_path"], "filename": result["title"], "source": "ytdlp", "filesize_mb": result["filesize_mb"]}
            except YtDlpError:
                raise
            except Exception as e2:
                raise YtDlpError(f"Cobalt failed ({e}) и yt-dlp тоже: {e2}")

    raise ValueError(f"Unknown flag: {flag}")


# ─── Bot handlers ───────────────────────────────────────────────────────────

async def cmd_start(message: Message):
    if await is_banned(str(DB_PATH), message.from_user.id):
        return
    await message.answer(
        "👋 <b>nxDLbot</b> — загрузчик медиа\n\n"
        "• Отправь ссылку в чат — я скачаю видео\n"
        "• Или используй инлайн: <code>@вашбот https://...</code>\n\n"
        "Флаги инлайна: <code>-c</code> (cobalt), <code>-y</code> (yt-dlp), <code>-a</code> (аудио)\n"
        "Поддержка: YouTube, TikTok, Instagram, Twitter/X, Reddit",
        parse_mode=ParseMode.HTML
    )


async def cmd_help(message: Message):
    await cmd_start(message)


# ─── Admin handlers ─────────────────────────────────────────────────────────

async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для админа")
        return
    uptime = time.time() - start_time
    hours = int(uptime // 3600)
    mins = int((uptime % 3600) // 60)
    cache_mb = get_cache_size_mb(str(DOWNLOADS_DIR))

    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur = await db.execute("SELECT COUNT(*) FROM downloads")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM downloads WHERE status IN ('done','cached')")
        done = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM banned_users")
        banned = (await cur.fetchone())[0]

    await message.answer(
        f"📊 <b>Статистика</b>\n"
        f"Uptime: {hours}ч {mins}м\n"
        f"Всего загрузок: {total}\n"
        f"Успешных: {done}\n"
        f"Активных: {len(active_downloads)}\n"
        f"Кэш: {cache_mb:.1f} MB / {CACHE_LIMIT_GB * 1024} MB\n"
        f"Забанено: {banned}",
        parse_mode=ParseMode.HTML
    )


async def cmd_cleanup(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🧹 Очищаю кэш...")
    await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)
    cache_mb = get_cache_size_mb(str(DOWNLOADS_DIR))
    await message.answer(f"✅ Кэш после очистки: {cache_mb:.1f} MB")


async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /ban <user_id> [причина]")
        return
    try:
        uid = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("INSERT OR REPLACE INTO banned_users (user_id, reason) VALUES (?, ?)", (uid, reason))
            await db.commit()
        await message.answer(f"✅ Пользователь {uid} забанен")
    except ValueError:
        await message.answer("❌ Неверный user_id")


async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unban <user_id>")
        return
    try:
        uid = int(parts[1])
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
            await db.commit()
        await message.answer(f"✅ Пользователь {uid} разбанен")
    except ValueError:
        await message.answer("❌ Неверный user_id")


async def cmd_logs(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    n = 20
    if len(parts) > 1:
        try:
            n = int(parts[1])
            n = max(1, min(n, 100))
        except:
            pass
    log_file = LOGS_DIR / "bot.log"
    if not log_file.exists():
        await message.answer("Лог пуст")
        return
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    tail = "\n".join(lines[-n:])
    if len(tail) > 4000:
        tail = tail[-4000:]
    await message.answer(f"<pre>{tail}</pre>", parse_mode=ParseMode.HTML)


async def cmd_restart(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🔄 Перезапуск...")
    # graceful shutdown
    asyncio.create_task(shutdown())
    # через 2 сек рестарт процесса (Termux watchdog поднимет)
    await asyncio.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def cmd_ytupdate(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Обновляю yt-dlp...")
    ok, msg = await update_ytdlp()
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


async def cmd_setdownloader(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or parts[2] not in ("auto", "cobalt", "ytdlp"):
        await message.answer("Использование: /setdownloader <user_id> <auto/cobalt/ytdlp>")
        return
    try:
        uid = int(parts[1])
        val = parts[2]
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT INTO user_settings (user_id, default_downloader) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET default_downloader = ?",
                (uid, val, val)
            )
            await db.commit()
        await message.answer(f"✅ Для {uid} установлен {val}")
    except ValueError:
        await message.answer("❌ Неверный user_id")


# ─── Chat mode ──────────────────────────────────────────────────────────────

async def handle_chat_message(message: Message):
    global shutting_down
    if shutting_down:
        await message.reply("⏳ Бот останавливается, попробуйте позже")
        return
    if not message.text:
        return
    user_id = message.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        return

    # rate limit check
    ok, msg = rate_limiter.check_rate(user_id)
    if not ok:
        await message.reply(msg)
        return

    url = extract_url_from_text(message.text)
    if not url:
        return

    # SSRF + whitelist validation
    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        await message.reply(err)
        return

    if is_playlist_url(url):
        await message.reply("❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео.")
        return

    # duplicate check
    cached = await find_cached_by_url(str(DB_PATH), url)
    if cached and cached.get("telegram_file_id"):
        try:
            await bot.send_video(
                chat_id=message.chat.id,
                video=cached["telegram_file_id"],
                caption=truncate_caption(cached.get("url", "") + f"\nvia @{ (await bot.get_me()).username }", MAX_CAPTION_LENGTH),
                reply_to_message_id=message.message_id
            )
            await touch_accessed(str(DB_PATH), cached["id"])
            return
        except Exception as e:
            logger.warning(f"Failed to send cached file_id: {e}")

    # check if ytdlp can list formats -> show quality keyboard
    # иначе сразу качаем
    status_msg = await message.reply("⏳ Качаю...")

    download_id = await insert_download(str(DB_PATH), user_id, message.from_user.username or "", url, source="auto")
    active_downloads.add(download_id)

    try:
        await rate_limiter.acquire(user_id)
        await update_download(str(DB_PATH), download_id, status="processing")

        # пытаемся получить форматы для кнопок качества (только если не audio-only и URL не из cobalt-only доменов)
        # покажем кнопки если yt-dlp доступен
        try:
            info = await get_formats(url, str(COOKIES_PATH) if COOKIES_PATH.exists() else None)
            formats = info.get("formats", [])
            # соберём кнопки качества (уникальные height)
            heights = sorted(set(f["height"] for f in formats if f.get("height") and f.get("vcodec") != "none"), reverse=True)
            if heights and len(heights) > 1:
                # показываем выбор
                kb_rows = []
                for h in heights[:4]:
                    # оценим размер
                    f_for_h = next((f for f in formats if f["height"] == h), None)
                    size_str = ""
                    if f_for_h and f_for_h.get("filesize"):
                        size_str = f"~{f_for_h['filesize'] / 1024 / 1024:.0f}MB"
                    label = f"{h}p {size_str}".strip()
                    kb_rows.append(InlineKeyboardButton(text=label, callback_data=f"dl:{download_id}:{h}:{url[:80]}"))
                # аудио
                kb_rows.append(InlineKeyboardButton(text="🎵 Только аудио", callback_data=f"dl:{download_id}:audio:{url[:80]}"))
                # группируем по 2
                keyboard = InlineKeyboardMarkup(inline_keyboard=[kb_rows[i:i+2] for i in range(0, len(kb_rows), 2)])
                await status_msg.edit_text(f"📹 <b>{truncate_caption(info.get('title',''), 100)}</b>\nВыберите качество:", reply_markup=keyboard, parse_mode=ParseMode.HTML)
                await update_download(str(DB_PATH), download_id, status="pending")
                return  # ждём callback
        except Exception as e:
            logger.debug(f"get_formats failed, direct download: {e}")

        # прямой hybrid download (auto)
        result = await hybrid_download(url, flag="auto", audio_only=False)
        await send_result(message, status_msg, result, download_id, url)

    except (CobaltError, YtDlpError) as e:
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        await status_msg.edit_text(str(e)[:800])
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await status_msg.edit_text(f"⏳ Telegram лимит, повторите через {e.retry_after}с")
    except Exception as e:
        logger.exception(f"Chat download error: {e}")
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:500]}")
    finally:
        rate_limiter.release(user_id)
        active_downloads.discard(download_id)
        await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)


async def handle_quality_callback(callback: CallbackQuery):
    global shutting_down
    if shutting_down:
        await callback.answer("Бот останавливается", show_alert=True)
        return
    user_id = callback.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        await callback.answer("Вы забанены", show_alert=True)
        return

    ok, msg = rate_limiter.check_rate(user_id)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    # callback_data: dl:{download_id}:{height|audio}:{url_prefix}
    try:
        _, dl_id_str, quality, *_ = callback.data.split(":", 3)
        download_id = int(dl_id_str)
    except:
        await callback.answer("❌ Некорректные данные", show_alert=True)
        return

    # получаем url из БД
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT url FROM downloads WHERE id = ?", (download_id,))
        row = await cur.fetchone()
        if not row:
            await callback.answer("❌ Загрузка не найдена", show_alert=True)
            return
        url = row["url"]

    await callback.answer("⏳ Начинаю загрузку...")
    try:
        await callback.message.edit_text("⏳ Качаю...")
    except:
        pass

    active_downloads.add(download_id)
    try:
        await rate_limiter.acquire(user_id)
        await update_download(str(DB_PATH), download_id, status="processing")

        audio_only = quality == "audio"
        if audio_only:
            # yt-dlp audio
            result = await ytdlp_download(url, output_dir=str(DOWNLOADS_DIR), cookies_path=str(COOKIES_PATH) if COOKIES_PATH.exists() else None,
                                          audio_only=True, max_size_mb=MAX_TELEGRAM_SIZE_MB)
            result_wrapped = {"type": "file", "file_path": result["file_path"], "filename": result["title"], "source": "ytdlp", "filesize_mb": result["filesize_mb"]}
        else:
            # quality like 720 -> маппим в yt-dlp формат
            q_map = {"720": "best[height<=720]", "1080": "best[height<=1080]", "480": "best[height<=480]", "360": "best[height<=360]"}
            q = q_map.get(quality, f"best[height<={quality}]")
            # для чата используем напрямую ytdlp с выбранным качеством (без cobalt чтобы не игнорировать выбор)
            try:
                result = await ytdlp_download(url, output_dir=str(DOWNLOADS_DIR), cookies_path=str(COOKIES_PATH) if COOKIES_PATH.exists() else None,
                                              audio_only=False, quality=q, max_size_mb=MAX_TELEGRAM_SIZE_MB)
                result_wrapped = {"type": "file", "file_path": result["file_path"], "filename": result["title"], "source": "ytdlp", "filesize_mb": result["filesize_mb"]}
            except YtDlpError as e:
                # если файл слишком большой — отправим ссылку
                await callback.message.edit_text(str(e)[:800])
                await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
                return

        # отправка
        # callback.message — это статусное сообщение, а исходное сообщение юзера — reply_to
        # отправим в тот же чат
        dummy_msg = callback.message
        # создаём объект Message-подобный для send_result (нужен chat.id)
        await send_result(dummy_msg, dummy_msg, result_wrapped, download_id, url, is_callback=True)

    except Exception as e:
        logger.exception(f"Callback download error: {e}")
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        try:
            await callback.message.edit_text(f"❌ Ошибка: {str(e)[:500]}")
        except:
            pass
    finally:
        rate_limiter.release(user_id)
        active_downloads.discard(download_id)
        await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)


async def send_result(trigger_msg: Message, status_msg: Message, result: dict, download_id: int, url: str, is_callback: bool = False):
    """Отправка результата пользователю и обновление БД."""
    bot_username = (await bot.get_me()).username
    caption = truncate_caption(result.get("filename", url) + f"\nvia @{bot_username}", MAX_CAPTION_LENGTH)

    if result["type"] == "cdn_url":
        cdn_url = result["cdn_url"]
        # пробуем скачать и отправить через Telegram если <=50MB, иначе ссылка
        # пока отправляем как текст с ссылкой (Cobalt отдаёт CDN которая может быть > лимита но HEAD уже проверен)
        # Скачиваем и отправляем как видео если можем
        try:
            # HEAD уже проверен — можно скачать
            import aiohttp
            tmp_path = DOWNLOADS_DIR / f"cobalt_{download_id}.mp4"
            async with aiohttp.ClientSession() as session:
                async with session.get(cdn_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        with open(tmp_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 256):
                                f.write(chunk)
                        size_mb = tmp_path.stat().st_size / (1024 * 1024)
                        if size_mb <= MAX_TELEGRAM_SIZE_MB:
                            sent = await bot.send_video(
                                chat_id=trigger_msg.chat.id,
                                video=FSInputFile(str(tmp_path)),
                                caption=caption,
                            )
                            file_id = sent.video.file_id if sent.video else None
                            await update_download(str(DB_PATH), download_id, status="done", cdn_url=cdn_url, telegram_file_id=file_id, file_size_mb=size_mb, source="cobalt")
                            await touch_accessed(str(DB_PATH), download_id)
                            try:
                                await status_msg.delete()
                            except:
                                pass
                            # чистим tmp
                            try:
                                tmp_path.unlink()
                            except:
                                pass
                            await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)
                            return
                        else:
                            tmp_path.unlink(missing_ok=True)
                            raise ValueError("too_large")
        except Exception as e:
            logger.debug(f"Cobalt download fallback to link: {e}")

        # fallback: отправляем ссылкой
        await status_msg.edit_text(f"📎 <a href='{cdn_url}'>Скачать медиа</a> ({caption[:60]})", parse_mode=ParseMode.HTML)
        await update_download(str(DB_PATH), download_id, status="done", cdn_url=cdn_url, source="cobalt")
        return

    if result["type"] == "file":
        fpath = result["file_path"]
        ext = Path(fpath).suffix.lower()
        is_audio = ext in (".mp3", ".m4a", ".opus", ".ogg", ".wav") or result.get("source") == "ytdlp" and "audio" in str(fpath).lower()
        # также проверяем если audio_only
        if ext == ".mp3" or ext == ".m4a":
            is_audio = True

        try:
            if is_audio:
                sent = await bot.send_audio(
                    chat_id=trigger_msg.chat.id,
                    audio=FSInputFile(fpath),
                    caption=caption,
                )
                file_id = sent.audio.file_id if sent.audio else (sent.voice.file_id if sent.voice else None)
            else:
                sent = await bot.send_video(
                    chat_id=trigger_msg.chat.id,
                    video=FSInputFile(fpath),
                    caption=caption,
                    supports_streaming=True,
                )
                file_id = sent.video.file_id if sent.video else None

            await update_download(str(DB_PATH), download_id, status="cached", file_path=fpath, telegram_file_id=file_id,
                                  file_size_mb=result.get("filesize_mb", 0), source=result.get("source", "ytdlp"))
            await touch_accessed(str(DB_PATH), download_id)
            try:
                await status_msg.delete()
            except:
                pass
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await status_msg.edit_text(f"⏳ FloodWait {e.retry_after}с, повторите позже")
        except Exception as e:
            logger.exception(f"send file error: {e}")
            await status_msg.edit_text(f"❌ Ошибка отправки: {e}")


# ─── Inline mode ────────────────────────────────────────────────────────────

async def handle_inline_query(inline_query: InlineQuery):
    user_id = inline_query.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        return

    query = inline_query.query.strip()

    # пустой запрос — история
    if not query:
        history = await get_user_history(str(DB_PATH), user_id, limit=10)
        if not history:
            result = InlineQueryResultArticle(
                id="help_empty",
                title="📥 Отправьте ссылку для загрузки",
                input_message_content=InputTextMessageContent(
                    message_text="Использование: @бот https://...  (YouTube, TikTok, Instagram...)\nФлаги: -c (cobalt), -y (yt-dlp), -a (аудио)"
                ),
                description="Вставьте ссылку",
            )
            await inline_query.answer([result], cache_time=1, is_personal=True)
            return

        results = []
        for row in history:
            title = (row.get("url") or "")[:50]
            # если есть file_id — можно отдать как cached video
            # но Input требует file_id валидный, поэтому отдаём article с ссылкой
            results.append(InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"🕘 {title}",
                input_message_content=InputTextMessageContent(
                    message_text=f"📹 {row.get('url')}\nvia @{ (await bot.get_me()).username }",
                    disable_web_page_preview=False,
                ),
                description="Нажми чтобы отправить снова",
            ))
        await inline_query.answer(results, cache_time=1, is_personal=True)
        return

    url, flag, audio_only = parse_inline_query(query)

    if not url or not url.startswith(("http://", "https://")):
        result = InlineQueryResultArticle(
            id="help",
            title="📥 Вставьте ссылку (http/https)",
            input_message_content=InputTextMessageContent(message_text="❌ Вставьте корректную ссылку: https://..."),
            description="Поддержка: YouTube, TikTok, Instagram...",
        )
        await inline_query.answer([result], cache_time=1)
        return

    # валидация
    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        result = InlineQueryResultArticle(
            id=str(uuid4()),
            title=err,
            input_message_content=InputTextMessageContent(message_text=err),
            description="Ошибка валидации",
        )
        await inline_query.answer([result], cache_time=1)
        return

    if is_playlist_url(url):
        result = InlineQueryResultArticle(
            id=str(uuid4()),
            title="❌ Плейлисты не поддерживаются",
            input_message_content=InputTextMessageContent(message_text="❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео."),
            description="",
        )
        await inline_query.answer([result], cache_time=1)
        return

    # duplicate check — если есть file_id, сразу отдаём видео
    cached = await find_cached_by_url(str(DB_PATH), url)
    if cached and cached.get("telegram_file_id") and PLACEHOLDER_FILE_ID:
        # можно отдать cached video? Но нужен правильный file_id — пробуем
        try:
            result = InlineQueryResultCachedVideo(
                id=str(uuid4()),
                video_file_id=cached["telegram_file_id"],
                title="✅ Из кэша (мгновенно)",
                description="Нажми чтобы отправить",
            )
            await inline_query.answer([result], cache_time=10, is_personal=True)
            await touch_accessed(str(DB_PATH), cached["id"])
            return
        except Exception as e:
            logger.warning(f"Cached inline failed: {e}")

    # основная механика: заглушка + ChosenInlineResult
    # По плану §7.1 заглушка обязана быть видео
    if PLACEHOLDER_FILE_ID:
        # используем CachedVideo как заглушку
        result_text = f"⏳ Загружаю... {url[:40]}"
        # query.encode в chosen_inline_result.result_id мы используем для передачи url
        # id должен быть уникальным, но result_id в chosen_inline_result — это id результата
        inline_id = str(uuid4())
        # сохраняем маппинг inline_id -> url в БД временно (через downloads pending)
        await insert_download(str(DB_PATH), user_id, inline_query.from_user.username or "", url, source=flag)
        result = InlineQueryResultCachedVideo(
            id=inline_id,
            video_file_id=PLACEHOLDER_FILE_ID,
            title="⏳ Загружаю... нажми чтобы получить",
            description=f"{flag} | {url[:40]}",
            input_message_content=InputTextMessageContent(message_text=result_text) if False else None,
        )
        # Для CachedVideo input_message_content не нужен, но если хотим — можно
        await inline_query.answer([result], cache_time=0, is_personal=True)
    else:
        # fallback: Article заглушка (если нет placeholder)
        inline_id = str(uuid4())
        await insert_download(str(DB_PATH), user_id, inline_query.from_user.username or "", url, source=flag)
        result = InlineQueryResultArticle(
            id=inline_id,
            title="⏳ Загружаю... нажми чтобы получить",
            input_message_content=InputTextMessageContent(message_text=f"⏳ Загружаю: {url}"),
            description=f"Режим: {flag} | {url[:40]}",
        )
        await inline_query.answer([result], cache_time=0, is_personal=True)


async def handle_chosen_inline(chosen: ChosenInlineResult):
    """Срабатывает после тапа на заглушку (требует /setinlinefeedback)."""
    user_id = chosen.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        return
    # query содержит исходный запрос (может включать флаг)
    query = chosen.query or ""
    url, flag, audio_only = parse_inline_query(query)
    if not url:
        # пробуем взять последний pending из БД
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT url, source FROM downloads WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            )
            row = await cur.fetchone()
            if row:
                url = row["url"]
                flag = row["source"] or "auto"
            else:
                return

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logger.warning("ChosenInlineResult without inline_message_id")
        return

    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        try:
            await bot.edit_message_text(text=err, inline_message_id=inline_message_id)
        except:
            pass
        return

    # rate limit
    ok, msg = rate_limiter.check_rate(user_id)
    if not ok:
        try:
            await bot.edit_message_text(text=msg, inline_message_id=inline_message_id)
        except:
            pass
        return

    download_id = await insert_download(str(DB_PATH), user_id, chosen.from_user.username or "", url, source=flag)
    active_downloads.add(download_id)

    try:
        await rate_limiter.acquire(user_id)
        await update_download(str(DB_PATH), download_id, status="processing")

        # редактируем заглушку — прогресс (если article — text, если video — caption)
        try:
            await bot.edit_message_text(text="⏳ Загружаю... (в очереди)", inline_message_id=inline_message_id)
        except:
            pass

        result = await hybrid_download(url, flag=flag, audio_only=audio_only)

        if result["type"] == "cdn_url":
            # для инлайна: редактируем на Text с ссылкой (т.к. inline_message_id от article)
            # если заглушка была CachedVideo — нужно edit_message_media
            cdn_url = result["cdn_url"]
            caption = truncate_caption(result.get("filename", url) + f" via @{(await bot.get_me()).username}", MAX_CAPTION_LENGTH)
            # пробуем скачать и заменить на видео
            try:
                import aiohttp
                tmp_path = DOWNLOADS_DIR / f"inline_{download_id}.mp4"
                async with aiohttp.ClientSession() as session:
                    async with session.get(cdn_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status == 200:
                            with open(tmp_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(256 * 1024):
                                    f.write(chunk)
                            if tmp_path.stat().st_size / (1024 * 1024) <= MAX_TELEGRAM_SIZE_MB:
                                # edit media
                                media = InputMediaVideo(media=FSInputFile(str(tmp_path)), caption=caption)
                                await bot.edit_message_media(media=media, inline_message_id=inline_message_id)
                                await update_download(str(DB_PATH), download_id, status="done", cdn_url=cdn_url, source="cobalt")
                                tmp_path.unlink(missing_ok=True)
                                await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)
                                return
                            tmp_path.unlink(missing_ok=True)
                            raise ValueError("too_large")
                # fallback ссылка
                await bot.edit_message_text(text=f"📎 <a href='{cdn_url}'>{caption[:60]}</a>", inline_message_id=inline_message_id, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"Inline cdn edit failed: {e}")
                # если не удалось скачать — шлём ссылку текстом
                try:
                    # если cdn слишком большой — отправим сообщение о ссылке
                    if "прямая ссылка" in str(e) or "слишком большой" in str(e):
                        await bot.edit_message_text(text=str(e)[:800], inline_message_id=inline_message_id)
                    else:
                        await bot.edit_message_text(text=f"📎 <a href='{cdn_url}'>{caption[:60]}</a>", inline_message_id=inline_message_id, parse_mode=ParseMode.HTML)
                except:
                    pass
                await update_download(str(DB_PATH), download_id, status="done", cdn_url=cdn_url, source="cobalt")
            return

        if result["type"] == "file":
            fpath = result["file_path"]
            caption = truncate_caption(result.get("filename", url) + f" via @{(await bot.get_me()).username}", MAX_CAPTION_LENGTH)
            ext = Path(fpath).suffix.lower()
            if ext == ".mp3" or ext == ".m4a":
                # аудио в инлайне — отправляем как видео? Telegram inline не поддерживает аудио edit легко — отправим текстом с файлом?
                # Для простоты: редактируем на текст с указанием что аудио, пользователь получит файл в чат через fallback
                media = InputMediaVideo(media=FSInputFile(fpath), caption=caption)
            else:
                media = InputMediaVideo(media=FSInputFile(fpath), caption=caption)
            try:
                await bot.edit_message_media(media=media, inline_message_id=inline_message_id)
            except Exception as e:
                # если edit_message_media падает (заглушка была article) — пробуем edit_text + send
                logger.warning(f"edit_message_media failed: {e}, fallback to edit_text")
                await bot.edit_message_text(text=f"📹 {caption}\n{url}", inline_message_id=inline_message_id)
                # и отправим файл отдельным сообщением пользователю (если можем определить чат — нельзя, inline)
                # оставим как есть
            await update_download(str(DB_PATH), download_id, status="cached", file_path=fpath, source=result.get("source", "ytdlp"),
                                  file_size_mb=result.get("filesize_mb", 0))
            await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)

    except Exception as e:
        logger.exception(f"Chosen inline error: {e}")
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        try:
            await bot.edit_message_text(text=f"❌ Ошибка: {str(e)[:500]}", inline_message_id=inline_message_id)
        except:
            pass
    finally:
        rate_limiter.release(user_id)
        active_downloads.discard(download_id)


# ─── Healthcheck ────────────────────────────────────────────────────────────

async def health_handler(request):
    return web.Response(text="ok", status=200)

async def start_healthcheck():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HEALTHCHECK_PORT)
    await site.start()
    logger.info(f"Healthcheck listening on 127.0.0.1:{HEALTHCHECK_PORT}")
    return runner


# ─── Background tasks ───────────────────────────────────────────────────────

async def periodic_cleanup():
    while True:
        await asyncio.sleep(CONFIG.get("cleanup_interval_sec", 3600))
        try:
            await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)
        except Exception as e:
            logger.warning(f"Periodic cleanup error: {e}")

async def periodic_ytdlp_update():
    if not YT_DLP_AUTO_UPDATE:
        return
    interval = YT_DLP_UPDATE_INTERVAL_HOURS * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            ok, msg = await update_ytdlp()
            logger.info(f"Auto yt-dlp update: {msg}")
        except Exception as e:
            logger.warning(f"Auto update failed: {e}")

async def shutdown():
    global shutting_down
    shutting_down = True
    logger.info("Graceful shutdown started")
    # отклонять новые запросы уже через флаг
    # ждать активных 60 сек
    for i in range(60):
        if not active_downloads:
            break
        await asyncio.sleep(1)
    # pending -> cancelled
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("UPDATE downloads SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE status = 'pending'")
            await db.commit()
    except Exception as e:
        logger.warning(f"Shutdown DB update failed: {e}")
    logger.info("Graceful shutdown done")


def setup_signals(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda s, f: asyncio.create_task(shutdown()))


# ─── Main ───────────────────────────────────────────────────────────────────

async def main():
    global bot, dp

    if _TOKEN_MISSING:
        print("bot_token не задан в config.json — укажите токен от @BotFather")
        sys.exit(1)

    # init DB
    await init_db(str(DB_PATH))
    await sync_db_fs(str(DOWNLOADS_DIR), str(DB_PATH))

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # commands
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_cleanup, Command("cleanup"))
    dp.message.register(cmd_ban, Command("ban"))
    dp.message.register(cmd_unban, Command("unban"))
    dp.message.register(cmd_logs, Command("logs"))
    dp.message.register(cmd_restart, Command("restart"))
    dp.message.register(cmd_ytupdate, Command("ytupdate"))
    dp.message.register(cmd_setdownloader, Command("setdownloader"))

    # quality callback
    dp.callback_query.register(handle_quality_callback, F.data.startswith("dl:"))

    # inline
    dp.inline_query.register(handle_inline_query)
    dp.chosen_inline_result.register(handle_chosen_inline)

    # chat message (must be last — catches URLs)
    dp.message.register(handle_chat_message, F.text)

    # healthcheck
    runner = await start_healthcheck()

    # background tasks
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(periodic_ytdlp_update())

    logger.info("nxDLbot started (polling)")
    # polling
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    setup_signals(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(shutdown())
    finally:
        loop.close()
