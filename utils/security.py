import re
import socket
import ipaddress
import urllib.parse
import logging

logger = logging.getLogger(__name__)

BLOCKED_SCHEMES = {"file", "ftp", "sftp", "data", "javascript", "blob"}
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "0:0:0:0:0:0:0:1"}
BLOCKED_PREFIXES = (
    "192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "169.254.", "127.",
)
# NOTE(v1.2): yt-dlp вызывается списком аргументов без shell=True,
# поэтому shell-метасимволы (& ; | && ||) в query-строке URL легитимны
# (YouTube: watch?v=...&t=...). Блокируем только то, что реально опасно
# вне shell: управляющие символы, бэктики, $(), угловые скобки.
DANGEROUS_CHARS = re.compile(r"[`$<>\n\r\t\\]|\$\(|[\x00-\x1f]")


def is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False


def validate_url(url: str, whitelist: list[str] = None, block_private_ips: bool = True) -> tuple[bool, str]:
    """
    Валидация URL по плану v1.1 §5.
    Returns (ok, error_msg)
    """
    url = url.strip()

    # 1. Проверка опасных символов (injection)
    if DANGEROUS_CHARS.search(url):
        return False, "❌ URL содержит запрещённые символы"

    # 2. Парсинг
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "❌ Некорректный URL"

    scheme = parsed.scheme.lower()
    if scheme in BLOCKED_SCHEMES:
        return False, "❌ Запрещённая схема URL"
    if scheme not in ("http", "https"):
        return False, "❌ Разрешены только http/https ссылки"

    host = parsed.hostname
    if not host:
        return False, "❌ Не удалось определить хост"

    host_lower = host.lower()
    if host_lower in BLOCKED_HOSTS:
        return False, "❌ Запрос к локальному хосту запрещён"
    for prefix in BLOCKED_PREFIXES:
        if host_lower.startswith(prefix):
            return False, "❌ Запрос к приватной сети запрещён"
    if host_lower == "localhost":
        return False, "❌ Запрос к приватной сети запрещён"

    # 3. Whitelist доменов
    if whitelist:
        matched = False
        for domain in whitelist:
            domain = domain.lower().lstrip(".")
            if host_lower == domain or host_lower.endswith("." + domain):
                matched = True
                break
        if not matched:
            return False, f"❌ Домен {host} не в списке поддерживаемых"

    # 4. DNS → проверка private IP (SSRF)
    if block_private_ips:
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in infos:
                ip_str = sockaddr[0]
                if is_private_ip(ip_str):
                    logger.warning(f"SSRF blocked: {host} resolves to private IP {ip_str}")
                    return False, "❌ URL указывает на приватный IP"
                # доп. проверка на 0.0.0.0
                if ip_str in BLOCKED_HOSTS:
                    return False, "❌ URL указывает на запрещённый адрес"
                # проверка префиксов строкой
                for prefix in BLOCKED_PREFIXES:
                    if ip_str.startswith(prefix):
                        return False, "❌ URL указывает на приватную сеть"
        except socket.gaierror as e:
            # не резолвится — не блокируем, но логируем (Cobalt/yt-dlp сами проверят)
            logger.debug(f"DNS lookup failed for {host}: {e}")
        except Exception as e:
            logger.warning(f"SSRF check error for {host}: {e}")

    # 5. playlist / live не блокируем тут, но помечаем — проверка в ytdlp.py

    return True, ""


def is_playlist_url(url: str) -> bool:
    return "list=" in url.lower() or "playlist" in url.lower()


def extract_urls(text: str) -> list[str]:
    """Извлекает http(s) URL из текста."""
    pattern = r"https?://[^\s<>\"]+"
    raw = re.findall(pattern, text)
    # чистим хвостовые знаки препинания
    cleaned = [u.rstrip(".,!);]\"'") for u in raw]
    return cleaned
