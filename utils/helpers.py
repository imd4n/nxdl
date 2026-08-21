import re
import logging

logger = logging.getLogger(__name__)


def parse_inline_query(query: str) -> tuple[str, str, bool]:
    """
    Парсит инлайн-запрос.
    Returns (url, flag, audio_only)
    flag: 'auto' | 'cobalt' | 'ytdlp'
    """
    query = query.strip()
    if not query:
        return "", "auto", False

    # флаги перед URL: -c, -y, -a
    flag = "auto"
    audio_only = False
    url = query

    # поддерживаем префикс "audio " как в старом боте
    if query.lower().startswith("audio "):
        audio_only = True
        url = query[6:].strip()
        return url, "ytdlp", True  # audio = принудительно yt-dlp

    parts = query.split(maxsplit=1)
    if len(parts) == 2 and parts[0] in ("-c", "-y", "-a"):
        flag_map = {"-c": "cobalt", "-y": "ytdlp", "-a": "ytdlp"}
        flag = flag_map[parts[0]]
        if parts[0] == "-a":
            audio_only = True
        url = parts[1].strip()
    elif len(parts) == 1 and parts[0] in ("-c", "-y", "-a"):
        # только флаг без URL
        return "", flag, audio_only

    return url, flag, audio_only


def truncate_caption(text: str, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def extract_url_from_text(text: str) -> str | None:
    """Находит первый валидный http(s) URL в тексте."""
    if not text:
        return None
    pattern = r"https?://[^\s<>\"]+"
    m = re.search(pattern, text)
    if not m:
        return None
    url = m.group(0).rstrip(".,!);]\"'")
    return url
