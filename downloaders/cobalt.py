import asyncio
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)


class CobaltError(Exception):
    pass


class CobaltClient:
    def __init__(self, base_url: str, api_path: str = "/api/json", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.api_path = api_path
        self.timeout = timeout
        self.endpoint = f"{self.base_url}{self.api_path}"

    async def extract(self, url: str, download_mode: str = "auto") -> dict:
        """
        Запрос к Cobalt API.
        download_mode: auto | audio | mute (по спеце Cobalt)
        Возвращает dict с ключами: cdn_url, filename, picker, status
        """
        payload: dict = {"url": url.strip()}
        # для cobalt 7.x+ downloadMode auto/mute/audio
        # для обратной совместимости добавляем оба варианта
        if download_mode == "audio":
            payload["downloadMode"] = "audio"
            payload["audioFormat"] = "mp3"
        else:
            payload["downloadMode"] = "auto"
            payload["videoQuality"] = "720"

        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        logger.info(f"Cobalt request: {self.endpoint} for {url[:80]}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise CobaltError(f"HTTP {resp.status}: {text[:500]}")

                    data = await resp.json()
                    status = data.get("status")
                    logger.info(f"Cobalt response status: {status}")

                    if status == "error":
                        raise CobaltError(f"Cobalt error: {data.get('error', data.get('text', 'unknown'))}")

                    # redirect/tunnel — прямая ссылка
                    if status in ("redirect", "tunnel"):
                        cdn_url = data.get("url")
                        if not cdn_url:
                            raise CobaltError("Cobalt returned no url for redirect/tunnel")
                        return {
                            "cdn_url": cdn_url,
                            "filename": data.get("filename", "media"),
                            "status": status,
                        }

                    # picker — несколько вариантов (carousel)
                    if status == "picker":
                        picker = data.get("picker") or data.get("picking")
                        if picker and isinstance(picker, list) and len(picker) > 0:
                            # берём первый
                            first = picker[0]
                            return {
                                "cdn_url": first.get("url"),
                                "filename": first.get("filename", "media"),
                                "picker": picker,
                                "status": status,
                            }
                        # аудио в picker
                        if data.get("audio"):
                            return {"cdn_url": data["audio"], "filename": "audio", "status": status}

                    # fallback: если есть url без status (старые инстансы)
                    if "url" in data:
                        return {"cdn_url": data["url"], "filename": data.get("filename", "media"), "status": status or "redirect"}

                    raise CobaltError(f"Unknown cobalt response: {data}")

        except asyncio.TimeoutError:
            raise CobaltError(f"Cobalt timeout after {self.timeout}s")
        except aiohttp.ClientError as e:
            raise CobaltError(f"Cobalt network error: {e}")

    async def head_check(self, cdn_url: str, max_size_mb: int = 50) -> tuple[bool, str]:
        """
        HEAD-запрос для проверки размера файла Cobalt.
        Returns (is_ok_for_telegram, message)
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(cdn_url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as resp:
                    clen = resp.headers.get("Content-Length")
                    if clen:
                        size_mb = int(clen) / (1024 * 1024)
                        if size_mb > max_size_mb:
                            return False, f"📎 Файл слишком большой для Telegram ({size_mb:.1f} MB). Прямая ссылка: {cdn_url}"
                    return True, ""
        except Exception as e:
            logger.warning(f"HEAD check failed for {cdn_url}: {e}")
            return True, ""  # не блокируем, пробуем отправить
