import os
import json
import asyncio
import aiohttp
import logging
import time
from typing import Optional, List
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes
from uuid import uuid4
from http.server import BaseHTTPRequestHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
COBALT_DIRECTORY_API = "https://cobalt.directory/api/tests"
COBALT_TIMEOUT = 8
MIN_SCORE = 50
INSTANCE_CACHE_TTL = 300  # 5 minutes

# ─── In-memory caches (warm invocations only) ─────────────────────────────────
_instance_cache: List[str] = []
_last_fetch = 0.0
_bot_username: Optional[str] = None

# ─── Lazy Application ─────────────────────────────────────────────────────────
_application: Optional[Application] = None
_app_initialized = False

def get_application() -> Application:
    global _application
    if _application is None:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN environment variable is not set")
        _application = Application.builder().token(BOT_TOKEN).build()
        _application.add_handler(CommandHandler("start", start))
        _application.add_handler(InlineQueryHandler(inline_query))
        _application.add_error_handler(error_handler)
    return _application

async def ensure_initialized():
    global _app_initialized
    if not _app_initialized:
        app = get_application()
        await app.initialize()
        _app_initialized = True

async def process_update(update_data: dict):
    await ensure_initialized()
    app = get_application()
    update = Update.de_json(update_data, app.bot)
    await app.process_update(update)

# ─── Instance fetcher ─────────────────────────────────────────────────────────

async def fetch_instances() -> List[str]:
    """Fetch working cobalt instances from cobalt.directory API."""
    global _instance_cache, _last_fetch
    now = time.time()
    if _instance_cache and (now - _last_fetch) < INSTANCE_CACHE_TTL:
        return _instance_cache

    instances = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COBALT_DIRECTORY_API,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = []
                    for item in data.get("data", []):
                        if not item.get("online"):
                            continue
                        score = item.get("score", 0)
                        if score < MIN_SCORE:
                            continue
                        protocol = item.get("protocol", "https")
                        api_domain = item.get("api", "")
                        if not api_domain:
                            continue
                        if api_domain.endswith(".imput.net"):
                            continue
                        raw.append({"url": f"{protocol}://{api_domain}", "score": score})
                    raw.sort(key=lambda x: x["score"], reverse=True)
                    instances = [i["url"] for i in raw]
                    _instance_cache = instances
                    _last_fetch = now
                    logger.info(f"Fetched {len(instances)} instances from cobalt.directory")
    except Exception as e:
        logger.error(f"Failed to fetch instances: {e}")
        if _instance_cache:
            return _instance_cache

    return instances

class CobaltClient:
    """Try cobalt instances until one returns a usable media link."""

    def __init__(self, instances: List[str]):
        self.instances = [url.strip().rstrip("/") for url in instances if url]
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def extract_media(self, url: str, audio_only: bool = False) -> Optional[dict]:
        payload = {
            "url": url.strip(),
            "videoQuality": "720",
            "audioFormat": "mp3",
            "downloadMode": "audio" if audio_only else "auto",
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        for instance in self.instances:
            try:
                api_url = f"{instance}/"
                logger.info(f"Trying {api_url} for {url}")
                async with self.session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=COBALT_TIMEOUT),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        status = data.get("status")
                        logger.info(f"Response from {instance}: {status}")

                        if status in ("tunnel", "redirect"):
                            return {
                                "url": data.get("url"),
                                "title": data.get("filename", "Media"),
                                "type": "audio"
                                if audio_only
                                else self._detect_type(data.get("filename", "")),
                            }
                        elif status == "picker":
                            if data.get("audio"):
                                return {
                                    "url": data["audio"],
                                    "title": "Audio",
                                    "type": "audio",
                                }
                            elif data.get("picking") and len(data["picking"]) > 0:
                                pick = data["picking"][0]
                                return {
                                    "url": pick.get("url"),
                                    "title": pick.get("filename", "Media"),
                                    "type": "video",
                                }
                        elif status == "error":
                            logger.warning(f"Cobalt error: {data.get('error', {})}")
                            continue
                    else:
                        logger.warning(f"HTTP {response.status} from {instance}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout from {instance}")
                continue
            except Exception as e:
                logger.error(f"Error with {instance}: {e}")
                continue
        return None

    def _detect_type(self, filename: str) -> str:
        if not filename:
            return "video"
        ext = filename.split(".")[-1].lower()
        audio_exts = ["mp3", "m4a", "opus", "ogg", "wav"]
        return "audio" if ext in audio_exts else "video"

# ─── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a link in inline mode!\n"
        "Type `@YourBotName https://youtube.com/...` in any chat.\n\n"
        "Supported: YouTube, TikTok, Twitter/X, Instagram, Reddit, etc.\n\n"
        "💡 Tip: Add 'audio' before the link for audio-only!"
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()

    if not query or not query.startswith(("http://", "https://")):
        help_result = InlineQueryResultArticle(
            id="help",
            title="📥 Send a link to download media",
            input_message_content=InputTextMessageContent(
                message_text="Usage: @YourBotName \nSupported: YouTube, Instagram, TikTok, Twitter, etc."
            ),
            description="Paste the link here!",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/482/482059.png",
        )
        await update.inline_query.answer([help_result], cache_time=1)
        return

    audio_only = False
    url = query
    if query.lower().startswith("audio "):
        audio_only = True
        url = query[6:].strip()

    if not url.startswith(("http://", "https://")):
        await update.inline_query.answer([], cache_time=0)
        return

    instances = await fetch_instances()
    if not instances:
        error_result = InlineQueryResultArticle(
            id=str(uuid4()),
            title="❌ No working cobalt instances found",
            input_message_content=InputTextMessageContent(
                message_text="❌ Couldn't find any working cobalt instances. Please try again later."
            ),
            description="cobalt.directory unreachable",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/463/463612.png",
        )
        await update.inline_query.answer([error_result], cache_time=0)
        return

    async with CobaltClient(instances) as client:
        result = await client.extract_media(url, audio_only=audio_only)

    if not result:
        error_result = InlineQueryResultArticle(
            id=str(uuid4()),
            title="❌ Failed to extract media",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Couldn't extract media from:\n{url}\n\nThe link might be unsupported or private."
            ),
            description="Try again or check the link",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/463/463612.png",
        )
        await update.inline_query.answer([error_result], cache_time=0)
        return

    media_url = result["url"]
    title = result["title"]
    media_type = result["type"]
    emoji = "🎵" if media_type == "audio" else "📹"

    global _bot_username
    if _bot_username is None:
        me = await context.bot.get_me()
        _bot_username = me.username

    message_text = (
        f"{emoji} {media_type} fetched via @{_bot_username}"
    )

    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title=f"{emoji} {title[:50]}",
            input_message_content=InputTextMessageContent(
                message_text=message_text, parse_mode="HTML"
            ),
            description=f"Click to send {media_type} link",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/724/724933.png"
            if media_type == "video"
            else "https://cdn-icons-png.flaticon.com/512/727/727218.png",
        )
    ]
    await update.inline_query.answer(results, cache_time=0)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ─── Vercel serverless handler ─────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    _webhook_set = False

    def _ensure_webhook(self):
        if not handler._webhook_set and WEBHOOK_URL:
            try:
                asyncio.run(get_application().bot.set_webhook(url=WEBHOOK_URL))
                handler._webhook_set = True
                logger.info(f"Webhook set to {WEBHOOK_URL}")
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")

    def do_POST(self):
        self._ensure_webhook()
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            update_data = json.loads(body)
            asyncio.run(process_update(update_data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            logger.error(f"Error processing update: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_GET(self):
        self._ensure_webhook()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running! Use POST for webhook updates.")