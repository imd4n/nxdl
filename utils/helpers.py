import re
import logging
import urllib.parse

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"]+")


def truncate_caption(text: str, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def extract_url_from_text(text: str) -> str | None:
    """Находит первый валидный http(s) URL в тексте."""
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,!);]\"'")
    return url


def extract_urls(text: str) -> list[str]:
    raw = URL_RE.findall(text or "")
    return [u.rstrip(".,!);]\"'") for u in raw]


def parse_args(text: str) -> tuple[str | None, bool]:
    """Парсинг чат-сообщения по плану v1.2 §7.1. Returns (url, is_mp3)."""
    if not text:
        return None, False
    text = text.strip()
    # флаг -mp3: в конце или отдельным токеном
    is_mp3 = False
    # убираем все вхождения отдельного токена -mp3 (регистронезависимо)
    tokens = text.split()
    filtered = [t for t in tokens if t.lower() != "-mp3"]
    if len(filtered) != len(tokens):
        is_mp3 = True
    text = " ".join(filtered).strip()
    # хвост вида "...-mp3" без пробела (на случай "url-mp3")
    if text.lower().endswith("-mp3"):
        is_mp3 = True
        text = text[: -len("-mp3")].strip()
    url = extract_url_from_text(text)
    return url, is_mp3


def parse_inline_query(query: str) -> tuple[str | None, bool]:
    """Парсинг инлайн-запроса '@бот <url> [-mp3]'. Returns (url, is_audio)."""
    if not query or not query.strip():
        return None, False
    # переиспользуем parse_args: флаги -c/-y/-a из v1.1 больше не поддерживаются
    return parse_args(query.strip())


# ─── Нормализация URL для дедупликации ────────────────────────────────────────
# youtu.be/XXX == youtube.com/watch?v=XXX&t=10  → одно и то же видео

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "si", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "vero_id",
    "t", "s", "t",  # youtube time param — отбрасываем для dedup
}

# legacy alias для старого bot.py v1.1
def parse_inline_query_legacy(query: str) -> tuple[str, str, bool]:
    url, audio = parse_inline_query(query)
    return url or "", "ytdlp", audio


def normalize_url(url: str) -> str:
    """Нормализованный ключ для поиска дубликатов в БД."""
    try:
        p = urllib.parse.urlparse(url.strip())
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path or ""
        # youtu.be/<id> → youtube.com/watch?v=<id>
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
            return f"youtube.com/watch?v={vid.lower()}" if vid else "youtu.be"
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if host in ("youtube.com", "m.youtube.com", "music.youtube.com",
                    "youtube-nocookie.com", "www.youtube-nocookie.com"):
            v = ""
            for k, val in qs:
                if k == "v":
                    v = val
                    break
            if v:
                return f"youtube.com/watch?v={v.lower()}"
        # общий случай: чистим трекинг, сортируем query, убираем фрагмент и trailing slash
        kept = sorted((k, val) for k, val in qs if k.lower() not in TRACKING_PARAMS)
        q = urllib.parse.urlencode(kept)
        norm_path = path.rstrip("/") or "/"
        out = f"{host}{norm_path}"
        if q:
            out += f"?{q}"
        return out.lower()
    except Exception:
        return url.strip().lower()
