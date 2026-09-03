#!/usr/bin/env python3
"""
nxDLbot v1.2 — Telegram bot (Termux hosting).
Только yt-dlp, автовыбор качества под 50MB, без Cobalt и кнопок качества.
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
    Message, InlineQuery, ChosenInlineResult,
    InlineQueryResultArticle, InlineQueryResultCachedVideo,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    FSInputFile, InputMediaVideo, InputMediaAudio
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
PLACEHOLDER_VIDEO_ID = CONFIG.get("placeholder_video_file_id", "")
PLACEHOLDER_AUDIO_ID = CONFIG.get("placeholder_audio_file_id", "")
MAX_VIDEO_HEIGHT = CONFIG.get("max_video_height", 1080)

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

if not BOT_TOKEN or BOT_TOKEN == "123456:ABC-DEF...":
    logger.warning("bot_token not set in config.json — set token from @BotFather")
    _TOKEN_MISSING = True
else:
    _TOKEN_MISSING = False

# ─── Imports after logging ──────────────────────────────────────────────────
from utils.db import init_db, is_banned, find_cached_by_url, insert_download, update_download, touch_accessed, get_user_history
from utils.security import validate_url, is_playlist_url
from utils.rate_limiter import RateLimiter
from utils.helpers import parse_args, parse_inline_query, truncate_caption
from utils.cache_manager import cleanup_cache, sync_db_fs, get_cache_size_mb
from downloaders.ytdlp import download as ytdlp_download, YtDlpError, update_ytdlp

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


def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def _cookies() -> str | None:
    return str(COOKIES_PATH) if COOKIES_PATH.exists() else None


async def _bot_username() -> str:
    try:
        me = await bot.get_me()
        return me.username or "bot"
    except Exception:
        return "bot"


# ─── Start / help ───────────────────────────────────────────────────────────

async def cmd_start(message: Message):
    if await is_banned(str(DB_PATH), message.from_user.id):
        return
    await message.answer(
        "👋 <b>nxDLbot</b> — загрузчик медиа (yt-dlp)\n\n"
        "• Отправь ссылку в чат — скачаю лучшее качество до 50 МБ\n"
        "• Добавь <code>-mp3</code> после ссылки — пришлю аудио\n"
        "• Или используй инлайн: <code>@вашбот https://... [-mp3]</code>",
        parse_mode=ParseMode.HTML
    )


async def cmd_help(message: Message):
    await cmd_start(message)


# ─── Admin ──────────────────────────────────────────────────────────────────

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
            n = max(1, min(int(parts[1]), 100))
        except Exception:
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
    asyncio.create_task(shutdown())
    await asyncio.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def cmd_ytupdate(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Обновляю yt-dlp...")
    ok, msg = await update_ytdlp()
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


async def cmd_setplaceholder(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🎬 <b>Заглушки для инлайна</b>\n\n"
        "Нужно 2 файла:\n"
        "1. <b>Видео</b> (1 сек, чёрный экран «⏳ Загрузка...»):\n"
        "<code>ffmpeg -f lavfi -i color=c=black:s=320x240:d=1 -vf \"drawtext=text='Loading...':fontsize=30:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2\" -c:v libx264 -t 1 -pix_fmt yuv420p placeholder.mp4</code>\n"
        "2. <b>Аудио</b> (любой короткий mp3, 1-3 сек тишины)\n\n"
        "Отправь оба файла мне в личку — я залогирую их <code>file_id</code> в лог.\n"
        "Затем впиши их в <code>config.json</code>:\n"
        "<code>placeholder_video_file_id</code> и <code>placeholder_audio_file_id</code>\n"
        "и перезапусти бота.",
        parse_mode=ParseMode.HTML
    )


async def log_incoming_file_id(message: Message):
    """Логирует file_id входящих видео/аудио — для настройки заглушек."""
    try:
        if message.video:
            logger.info(f"PLACEHOLDER_CANDIDATE video file_id={message.video.file_id}")
        if message.audio:
            logger.info(f"PLACEHOLDER_CANDIDATE audio file_id={message.audio.file_id}")
    except Exception:
        pass


# ─── Chat mode (v1.2: без кнопок, автовыбор) ────────────────────────────────

async def _send_cached(message: Message, cached: dict, username: str) -> bool:
    """Попытка мгновенной отправки дубликата. Returns True если отправлено."""
    fid = cached.get("telegram_file_id")
    if fid:
        try:
            is_audio = cached.get("is_audio") == 1
            caption = truncate_caption(f"{cached.get('url', '')}\nvia @{username}", MAX_CAPTION_LENGTH)
            if is_audio:
                await bot.send_audio(chat_id=message.chat.id, audio=fid,
                                     caption=caption, reply_to_message_id=message.message_id)
            else:
                await bot.send_video(chat_id=message.chat.id, video=fid,
                                     caption=caption, reply_to_message_id=message.message_id,
                                     supports_streaming=True)
            await touch_accessed(str(DB_PATH), cached["id"])
            return True
        except Exception as e:
            logger.warning(f"Failed to send cached file_id: {e}")
    # fallback: файл ещё на диске — шлём без повторного скачивания
    fpath = cached.get("file_path")
    if fpath and os.path.exists(fpath):
        try:
            sent_id = await _send_file(message.chat.id, fpath, bool(cached.get("is_audio")),
                                        truncate_caption(f"{cached.get('url', '')}\nvia @{username}", MAX_CAPTION_LENGTH))
            await update_download(str(DB_PATH), cached["id"], telegram_file_id=sent_id)
            await touch_accessed(str(DB_PATH), cached["id"])
            return True
        except Exception as e:
            logger.warning(f"Failed to resend cached file: {e}")
    return False


async def _send_file(chat_id: int, fpath: str, is_audio: bool, caption: str) -> str | None:
    ext = Path(fpath).suffix.lower()
    if is_audio or ext in (".mp3", ".m4a", ".opus", ".ogg", ".wav"):
        sent = await bot.send_audio(chat_id=chat_id, audio=FSInputFile(fpath), caption=caption)
        a = sent.audio or sent.voice
        return a.file_id if a else None
    sent = await bot.send_video(chat_id=chat_id, video=FSInputFile(fpath),
                                caption=caption, supports_streaming=True)
    return sent.video.file_id if sent.video else None


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

    ok, msg = rate_limiter.check_rate(user_id)
    if not ok:
        await message.reply(msg)
        return

    url, is_mp3 = parse_args(message.text)
    if not url:
        return

    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        await message.reply(err)
        return
    if is_playlist_url(url):
        await message.reply("❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео.")
        return

    username = (await _bot_username())
    cached = await find_cached_by_url(str(DB_PATH), url, is_audio=is_mp3)
    if cached and await _send_cached(message, cached, username):
        return

    status_msg = await message.reply("⏳ Качаю..." + (" (аудио)" if is_mp3 else ""))
    download_id = await insert_download(str(DB_PATH), user_id,
                                        message.from_user.username or "",
                                        url, source="ytdlp", is_audio=is_mp3)
    active_downloads.add(download_id)
    try:
        await rate_limiter.acquire(user_id)
        await update_download(str(DB_PATH), download_id, status="processing")
        try:
            await status_msg.edit_text("⏳ Качаю..." + (" (аудио, в работе)" if is_mp3 else " (в работе)"))
        except Exception:
            pass

        result = await ytdlp_download(url, output_dir=str(DOWNLOADS_DIR),
                                      cookies_path=_cookies(), audio_only=is_mp3,
                                      max_size_mb=MAX_TELEGRAM_SIZE_MB,
                                      max_height=MAX_VIDEO_HEIGHT)
        caption = truncate_caption(f"{result.get('title', url)}\nvia @{username}", MAX_CAPTION_LENGTH)
        try:
            file_id = await _send_file(message.chat.id, result["file_path"], is_mp3, caption)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await status_msg.edit_text(f"⏳ Telegram лимит, повторите через {e.retry_after}с")
            await update_download(str(DB_PATH), download_id, status="failed",
                                  error_msg=f"FloodWait {e.retry_after}")
            return
        await update_download(str(DB_PATH), download_id, status="cached",
                              file_path=result["file_path"], telegram_file_id=file_id,
                              file_size_mb=result.get("filesize_mb", 0), source="ytdlp")
        await touch_accessed(str(DB_PATH), download_id)
        try:
            await status_msg.delete()
        except Exception:
            pass
    except YtDlpError as e:
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        try:
            await status_msg.edit_text(str(e)[:800])
        except Exception:
            pass
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await status_msg.edit_text(f"⏳ Telegram лимит, повторите через {e.retry_after}с")
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"Chat download error: {e}")
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        try:
            await status_msg.edit_text(f"❌ Ошибка: {str(e)[:500]}")
        except Exception:
            pass
    finally:
        rate_limiter.release(user_id)
        active_downloads.discard(download_id)
        await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)


# ─── Inline mode (v1.2: две заглушки — видео и аудио) ───────────────────────

async def handle_inline_query(inline_query: InlineQuery):
    user_id = inline_query.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        return
    query = (inline_query.query or "").strip()

    if not query:
        history = await get_user_history(str(DB_PATH), user_id, limit=10)
        if not history:
            result = InlineQueryResultArticle(
                id="help_empty",
                title="📥 Отправьте ссылку для загрузки",
                input_message_content=InputTextMessageContent(
                    message_text="Использование: @бот https://... [-mp3]"
                ),
                description="Вставьте ссылку, опционально добавьте -mp3",
            )
            await inline_query.answer([result], cache_time=1, is_personal=True)
            return
        results = []
        uname = await _bot_username()
        for row in history:
            title = (row.get("url") or "")[:50]
            results.append(InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"🕘 {title}",
                input_message_content=InputTextMessageContent(
                    message_text=f"📹 {row.get('url')}\nvia @{uname}",
                    disable_web_page_preview=False,
                ),
                description="Нажми чтобы отправить снова",
            ))
        await inline_query.answer(results, cache_time=1, is_personal=True)
        return

    url, is_audio = parse_inline_query(query)
    if not url or not url.startswith(("http://", "https://")):
        result = InlineQueryResultArticle(
            id="help",
            title="📥 Вставьте ссылку (http/https) [+ -mp3 для аудио]",
            input_message_content=InputTextMessageContent(message_text="❌ Вставьте корректную ссылку: https://..."),
            description="",
        )
        await inline_query.answer([result], cache_time=1)
        return

    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        result = InlineQueryResultArticle(
            id=str(uuid4()), title=err,
            input_message_content=InputTextMessageContent(message_text=err),
            description="Ошибка валидации",
        )
        await inline_query.answer([result], cache_time=1)
        return
    if is_playlist_url(url):
        result = InlineQueryResultArticle(
            id=str(uuid4()), title="❌ Плейлисты не поддерживаются",
            input_message_content=InputTextMessageContent(
                message_text="❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео."),
            description="",
        )
        await inline_query.answer([result], cache_time=1)
        return

    # дубликат с file_id — мгновенная отдача правильным типом
    cached = await find_cached_by_url(str(DB_PATH), url, is_audio=is_audio)
    if cached and cached.get("telegram_file_id"):
        try:
            if is_audio:
                result = InlineQueryResultCachedAudio(
                    id=str(uuid4()), audio_file_id=cached["telegram_file_id"],
                    title="✅ Из кэша: аудио (мгновенно)",
                )
            else:
                result = InlineQueryResultCachedVideo(
                    id=str(uuid4()), video_file_id=cached["telegram_file_id"],
                    title="✅ Из кэша: видео (мгновенно)",
                )
            await inline_query.answer([result], cache_time=10, is_personal=True)
            await touch_accessed(str(DB_PATH), cached["id"])
            return
        except Exception as e:
            logger.warning(f"Cached inline failed: {e}")

    placeholder = PLACEHOLDER_AUDIO_ID if is_audio else PLACEHOLDER_VIDEO_ID
    kind = "аудио" if is_audio else "видео"
    if not placeholder:
        result = InlineQueryResultArticle(
            id=str(uuid4()),
            title=f"⚠️ Заглушка ({kind}) не настроена",
            input_message_content=InputTextMessageContent(
                message_text="⚠️ Админ ещё не загрузил заглушку для инлайна. Используй /setplaceholder"),
            description="Обратитесь к админу",
        )
        await inline_query.answer([result], cache_time=1, is_personal=True)
        return

    inline_id = str(uuid4())
    await insert_download(str(DB_PATH), user_id, inline_query.from_user.username or "",
                          url, source="ytdlp", is_audio=is_audio)
    if is_audio:
        result = InlineQueryResultCachedAudio(
            id=inline_id, audio_file_id=placeholder,
            title="⏳ Аудио загружается — нажми чтобы получить",
            description=url[:60],
        )
    else:
        result = InlineQueryResultCachedVideo(
            id=inline_id, video_file_id=placeholder,
            title="⏳ Видео загружается — нажми чтобы получить",
            description=url[:60],
        )
    await inline_query.answer([result], cache_time=0, is_personal=True)


async def _edit_progress(inline_message_id: str, text: str):
    """Прогресс после тапа: пробуем caption (видео/аудио), иначе текст."""
    try:
        await bot.edit_message_caption(caption=text, inline_message_id=inline_message_id)
        return
    except Exception:
        pass
    try:
        await bot.edit_message_text(text=text, inline_message_id=inline_message_id)
    except Exception as e:
        logger.debug(f"progress edit failed: {e}")


async def handle_chosen_inline(chosen: ChosenInlineResult):
    user_id = chosen.from_user.id
    if await is_banned(str(DB_PATH), user_id):
        return
    url, is_audio = parse_inline_query(chosen.query or "")
    if not url:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT url, is_audio FROM downloads WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (user_id,))
            row = await cur.fetchone()
            if row:
                url = row["url"]
                is_audio = bool(row["is_audio"])
            else:
                return

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logger.warning("ChosenInlineResult without inline_message_id")
        return

    valid, err = validate_url(url, whitelist=URL_WHITELIST, block_private_ips=BLOCK_PRIVATE_IPS)
    if not valid:
        await _edit_progress(inline_message_id, err)
        return

    ok, msg = rate_limiter.check_rate(user_id)
    if not ok:
        await _edit_progress(inline_message_id, msg)
        return

    # дубликат с файлом на диске — правим заглушку без скачивания
    cached = await find_cached_by_url(str(DB_PATH), url, is_audio=is_audio)
    if cached and cached.get("file_path") and os.path.exists(cached["file_path"]):
        try:
            uname = await _bot_username()
            caption = truncate_caption(f"{cached.get('url', url)}\nvia @{uname}", MAX_CAPTION_LENGTH)
            if is_audio:
                media = InputMediaAudio(media=FSInputFile(cached["file_path"]), caption=caption)
            else:
                media = InputMediaVideo(media=FSInputFile(cached["file_path"]), caption=caption,
                                        supports_streaming=True)
            await bot.edit_message_media(media=media, inline_message_id=inline_message_id)
            await touch_accessed(str(DB_PATH), cached["id"])
            return
        except Exception as e:
            logger.warning(f"Cached file inline edit failed: {e}")

    download_id = await insert_download(str(DB_PATH), user_id,
                                        chosen.from_user.username or "",
                                        url, source="ytdlp", is_audio=is_audio)
    active_downloads.add(download_id)
    last_progress = 0.0
    try:
        await rate_limiter.acquire(user_id)
        await update_download(str(DB_PATH), download_id, status="processing")
        await _edit_progress(inline_message_id, "⏳ Загружаю...")
        last_progress = time.monotonic()

        result = await ytdlp_download(url, output_dir=str(DOWNLOADS_DIR),
                                      cookies_path=_cookies(), audio_only=is_audio,
                                      max_size_mb=MAX_TELEGRAM_SIZE_MB,
                                      max_height=MAX_VIDEO_HEIGHT)
        # троттлинг прогресса не нужен — одно финальное редактирование
        if time.monotonic() - last_progress < 0:
            pass

        uname = await _bot_username()
        caption = truncate_caption(f"{result.get('title', url)}\nvia @{uname}", MAX_CAPTION_LENGTH)
        if is_audio:
            media = InputMediaAudio(media=FSInputFile(result["file_path"]), caption=caption)
        else:
            media = InputMediaVideo(media=FSInputFile(result["file_path"]), caption=caption,
                                    supports_streaming=True)
        try:
            await bot.edit_message_media(media=media, inline_message_id=inline_message_id)
        except Exception as e:
            logger.warning(f"edit_message_media failed: {e}")
            await _edit_progress(inline_message_id, f"📹 {caption}\n{url}")
        await update_download(str(DB_PATH), download_id, status="cached",
                              file_path=result["file_path"],
                              file_size_mb=result.get("filesize_mb", 0), source="ytdlp")
        await cleanup_cache(str(DOWNLOADS_DIR), str(DB_PATH), CACHE_LIMIT_GB)
    except Exception as e:
        logger.exception(f"Chosen inline error: {e}")
        await update_download(str(DB_PATH), download_id, status="failed", error_msg=str(e)[:500])
        await _edit_progress(inline_message_id, f"❌ Ошибка: {str(e)[:500]}")
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
    for _ in range(60):
        if not active_downloads:
            break
        await asyncio.sleep(1)
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("UPDATE downloads SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE status IN ('pending','processing')")
            await db.commit()
    except Exception as e:
        logger.warning(f"Shutdown DB update failed: {e}")
    logger.info("Graceful shutdown done")


def setup_signals(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            signal.signal(sig, lambda s, f: asyncio.create_task(shutdown()))


# ─── Main ───────────────────────────────────────────────────────────────────

async def main():
    global bot, dp

    if _TOKEN_MISSING:
        print("bot_token не задан в config.json — укажите токен от @BotFather")
        sys.exit(1)

    await init_db(str(DB_PATH))
    await sync_db_fs(str(DOWNLOADS_DIR), str(DB_PATH))

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_cleanup, Command("cleanup"))
    dp.message.register(cmd_ban, Command("ban"))
    dp.message.register(cmd_unban, Command("unban"))
    dp.message.register(cmd_logs, Command("logs"))
    dp.message.register(cmd_restart, Command("restart"))
    dp.message.register(cmd_ytupdate, Command("ytupdate"))
    dp.message.register(cmd_setplaceholder, Command("setplaceholder"))

    dp.inline_query.register(handle_inline_query)
    dp.chosen_inline_result.register(handle_chosen_inline)

    # file_id-логгер для заглушек (видео/аудио в личку)
    dp.message.register(log_incoming_file_id, F.video | F.audio)
    # чат-режим последним — ловит ссылки
    dp.message.register(handle_chat_message, F.text)

    runner = await start_healthcheck()

    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(periodic_ytdlp_update())

    if not PLACEHOLDER_VIDEO_ID:
        logger.warning("placeholder_video_file_id пуст — инлайн-видео покажет ошибку. См. /setplaceholder")
    if not PLACEHOLDER_AUDIO_ID:
        logger.warning("placeholder_audio_file_id пуст — инлайн-аудио покажет ошибку. См. /setplaceholder")

    logger.info("nxDLbot v1.2 started (polling, ytdlp-only)")
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
