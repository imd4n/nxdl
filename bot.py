import os
import asyncio
import aiohttp
import logging
from typing import Optional, List
from dotenv import load_dotenv
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes
from uuid import uuid4

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
COBALT_INSTANCES = os.getenv("COBALT_INSTANCES", "").split(",")
COBALT_TIMEOUT = 8  # seconds

if not BOT_TOKEN or not COBALT_INSTANCES:
    raise ValueError("Missing BOT_TOKEN or COBALT_INSTANCES")

class CobaltClient:
    def __init__(self, instances: List[str]):
        self.instances = [url.strip().rstrip('/') for url in instances if url]
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def extract_media(self, url: str, audio_only: bool = False) -> Optional[dict]:
        """Try each Cobalt instance until one works"""
        payload = {
            "url": url.strip(),
            "videoQuality": "720",
            "audioFormat": "mp3",
            "downloadMode": "audio" if audio_only else "auto"
        }
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        for instance in self.instances:
            try:
                api_url = f"{instance}/"
                logger.info(f"Trying {api_url} for {url}")
                
                async with self.session.post(
                    api_url, 
                    json=payload, 
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=COBALT_TIMEOUT)
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        status = data.get("status")
                        logger.info(f"Response from {instance}: {status}")
                        
                        if status in ("tunnel", "redirect"):
                            return {
                                "url": data.get("url"),
                                "title": data.get("filename", "Media"),
                                "type": "audio" if audio_only else self._detect_type(data.get("filename", ""))
                            }
                        elif status == "picker":
                            if data.get("audio"):
                                return {
                                    "url": data["audio"],
                                    "title": "Audio",
                                    "type": "audio"
                                }
                            elif data.get("picking") and len(data["picking"]) > 0:
                                pick = data["picking"][0]
                                return {
                                    "url": pick.get("url"),
                                    "title": pick.get("filename", "Media"),
                                    "type": "video"
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
        ext = filename.split('.')[-1].lower()
        audio_exts = ['mp3', 'm4a', 'opus', 'ogg', 'wav']
        return "audio" if ext in audio_exts else "video"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a link in inline mode!\n"
        "Type `@YourBotName https://youtube.com/...` in any chat.\n\n"
        "Supported: YouTube, TikTok, Twitter/X, Instagram, Reddit, etc.\n\n"
        "💡 Tip: Add 'audio' before the link for audio-only!"
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline queries"""
    query = update.inline_query.query.strip()
    
    if not query or not query.startswith(('http://', 'https://')):
        help_result = InlineQueryResultArticle(
            id="help",
            title="📥 Send a link to download media",
            input_message_content=InputTextMessageContent(
                message_text="Usage: @YourBotName <url>\nSupported: YouTube, Instagram, TikTok, Twitter, etc."
            ),
            description="Paste the link here!",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/482/482059.png"  # Fixed: was thumb_url
        )
        await update.inline_query.answer([help_result], cache_time=1)
        return
    
    # Check for audio prefix
    audio_only = False
    url = query
    if query.lower().startswith('audio '):
        audio_only = True
        url = query[6:].strip()
    
    if not url.startswith(('http://', 'https://')):
        await update.inline_query.answer([], cache_time=0)
        return
    
    # Extract with Cobalt
    async with CobaltClient(COBALT_INSTANCES) as client:
        result = await client.extract_media(url, audio_only=audio_only)
    
    if not result:
        error_result = InlineQueryResultArticle(
            id=str(uuid4()),
            title="❌ Failed to extract media",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Couldn't extract media from:\n{url}\n\nThe link might be unsupported or private."
            ),
            description="Try again or check the link",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/463/463612.png"  # Fixed: was thumb_url
        )
        await update.inline_query.answer([error_result], cache_time=0)
        return
    
    media_url = result["url"]
    title = result["title"]
    bot_username = (await context.bot.get_me()).username
    
    media_type = result["type"]  # "video" or "audio"
    emoji = "🎵" if media_type == "audio" else "📹"
    
    # Format: "video fetched via @bot" with clickable link on "video"
    message_text = (
        f"{emoji} <a href='{media_url}'>{media_type}</a> fetched via @{bot_username}"
    )
    
    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title=f"{emoji} {title[:50]}",
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode='HTML'
            ),
            description=f"Click to send {media_type} link",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/724/724933.png" if media_type == "video" else "https://cdn-icons-png.flaticon.com/512/727/727218.png"
        )
    ]
    
    await update.inline_query.answer(results, cache_time=0)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_error_handler(error_handler)
    
    print("🤖 Bot is running...")
    
    application.run_polling()

if __name__ == "__main__":
    main()