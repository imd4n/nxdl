"""yt-dlp wrapper по плану v1.2 §8: анализ форматов ДО скачивания, автовыбор под 50MB."""
import os
import re
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

PLAYLIST_RE = re.compile(r"[?&]list=|playlist", re.IGNORECASE)

# инъекции сюда не нужны: вызов всегда списком без shell=True.
# Оставляем только грубую проверку на управляющие символы.
_CTRL_RE = re.compile(r"[\n\r\t\x00-\x1f`$<>\\]")


class YtDlpError(Exception):
    pass


def _check_url(url: str):
    if _CTRL_RE.search(url) or "$(" in url:
        raise YtDlpError("URL содержит запрещённые символы")


def _cookies_args(cookies_path: str | None) -> list[str]:
    if cookies_path and os.path.exists(cookies_path):
        return ["--cookies", cookies_path]
    return []


async def _run(cmd: list[str], timeout: int = 120) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise YtDlpError("yt-dlp завис (timeout)")
    return proc.returncode or 0, stdout, stderr


async def get_info(url: str, cookies_path: str | None = None) -> dict:
    """yt-dlp -J --no-playlist → info_dict. NOTE: без -x (при дампе он игнорируется)."""
    _check_url(url)
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-J",
           "--no-live-from-start", *cookies_args(cookies_path), url]
    logger.info(f"yt-dlp get_info: {url[:80]}")
    rc, stdout, stderr = await _run(cmd, timeout=60)
    if rc != 0:
        err = stderr.decode(errors="ignore")[:800]
        raise YtDlpError(_humanize(err))
    try:
        return json.loads(stdout.decode())
    except Exception as e:
        raise YtDlpError(f"Не удалось разобрать ответ yt-dlp: {e}")


def cookies_args(cookies_path):
    return _cookies_args(cookies_path)


# backward-compat для старого bot.py v1.1
async def get_formats(url: str, cookies_path: str | None = None) -> dict:
    info = await get_info(url, cookies_path)
    formats = []
    for f in info.get("formats", []):
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "width": f.get("width"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "tbr": f.get("tbr"),
            "format_note": f.get("format_note"),
        })
    return {"formats": formats, "title": info.get("title", ""),
            "extractor": info.get("extractor", ""), "info": info}


def _fmt_size(f: dict) -> float | None:
    sz = f.get("filesize")
    if sz:
        return float(sz)
    sz = f.get("filesize_approx")
    if sz:
        return float(sz)
    return None


def choose_video_format(info: dict, max_size_mb: int = 50,
                        max_height: int = 1080) -> str:
    """Выбор format_id лучшего качества, влезающего в лимит.

    Учитываем довесок аудио (~10%) при комбинировании video-only + bestaudio.
    Возвращает готовую -f строку.
    """
    limit = max_size_mb * 1024 * 1024 * 0.9  # запас 10% на аудио-дорожку
    formats = [f for f in info.get("formats", [])
               if not f.get("is_live") and (f.get("height") or 0) <= max_height]
    if info.get("is_live"):
        raise YtDlpError("❌ Live-трансляции не поддерживаются")

    # только прогрессивные или video-only с известной высотой
    cands = [f for f in formats
             if f.get("vcodec") not in (None, "none") and f.get("height")]
    # TikTok/Instagram: один progressive формат без height — берём как есть
    if not cands:
        prot = [f for f in formats if f.get("vcodec") not in (None, "none")]
        if prot:
            best = max(prot, key=lambda f: (_fmt_size(f) or 0))
            fid = best.get("format_id")
            if fid:
                return f"{fid}+bestaudio/best"
        return "best"

    # сортировка: высота desc, затем размер desc
    cands.sort(key=lambda f: ((f.get("height") or 0), (_fmt_size(f) or 0)), reverse=True)

    for max_h in (max_height, 720, 480, 360):
        for f in cands:
            if (f.get("height") or 0) > max_h:
                continue
            sz = _fmt_size(f)
            if sz is None:
                # размер неизвестен: 720p обычно влезает — пробуем
                if (f.get("height") or 0) <= 720:
                    fid = f.get("format_id")
                    return f"{fid}+bestaudio/best" if fid else "best[height<=720]"
                continue
            if sz < limit:
                fid = f.get("format_id")
                return f"{fid}+bestaudio/best" if fid else "best[height<=720]"
    return "worst[ext=mp4]/worst"


def choose_audio_format(info: dict, max_size_mb: int = 50) -> str:
    limit = max_size_mb * 1024 * 1024
    audios = [f for f in info.get("formats", [])
              if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    if not audios:
        return "bestaudio/best"
    audios.sort(key=lambda f: (_fmt_size(f) or 0, f.get("abr") or 0), reverse=True)
    for f in audios:
        sz = _fmt_size(f)
        if sz is None or sz < limit:
            fid = f.get("format_id")
            if fid and sz is not None:
                return fid
            break
    # downgrade по битрейту
    return "bestaudio[abr<=128]/bestaudio/best"


def _humanize(err: str) -> str:
    low = err.lower()
    if "live" in low:
        return "❌ Live-трансляции не поддерживаются"
    if "login required" in low or "cookies" in low or "403" in low:
        return "❌ Источник требует авторизации. Админ может загрузить cookies.txt. " + err[:300]
    if "unsupported url" in low or "no video formats" in low or "not available" in low:
        return "❌ Не удалось извлечь медиа. Возможно, ссылка недействительна или нужна авторизация. " + err[:300]
    if "playlist" in low:
        return "❌ Плейлисты не поддерживаются. Отправьте ссылку на конкретное видео."
    return f"❌ Ошибка загрузки: {err[:400]}"


async def download(url: str, output_dir: str = "downloads",
                   cookies_path: str | None = None, audio_only: bool = False,
                   quality: str | None = None, max_size_mb: int = 50,
                   max_height: int = 1080) -> dict:
    """Скачать медиа. Returns {file_path, title, ext, filesize_mb}."""
    os.makedirs(output_dir, exist_ok=True)
    _check_url(url)
    if PLAYLIST_RE.search(url):
        logger.warning(f"Playlist URL detected: {url[:80]}")

    # 1. анализ форматов (best-effort: если упал — качаем вслепую с downgrade)
    info_title = ""
    format_str = quality
    if not format_str:
        try:
            info = await get_info(url, cookies_path)
            info_title = info.get("title", "")
            format_str = choose_audio_format(info, max_size_mb) if audio_only \
                else choose_video_format(info, max_size_mb, max_height)
        except YtDlpError as e:
            if "Live" in str(e):
                raise
            logger.warning(f"get_info failed, blind download: {e}")
            format_str = "bestaudio/best" if audio_only else "best[height<=720]"
    logger.info(f"yt-dlp download format={format_str} audio={audio_only}: {url[:80]}")

    # downgrade-лестница для видео
    attempts = [format_str]
    if not audio_only and format_str not in ("worst[ext=mp4]/worst",):
        for step in ("best[height<=480]", "best[height<=360]", "worst[ext=mp4]/worst"):
            if step not in attempts:
                attempts.append(step)

    before = set(os.listdir(output_dir))
    last_error = ""
    for q in attempts:
        # уникальный шаблон: экстрактор + id, чтобы не коллизить между площадками
        template = os.path.join(output_dir, "%(extractor)s-%(id)s.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "--max-downloads", "1", "--no-warnings",
               "--match-filters", "!is_live", "--no-live-from-start",
               *cookies_args(cookies_path),
               "-f", q]
        if audio_only:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            cmd += ["--merge-output-format", "mp4"]
        cmd += ["-o", template, "--print-json", url]

        rc, stdout, stderr = await _run(cmd, timeout=600)
        if rc != 0:
            err = stderr.decode(errors="ignore")[:1000]
            logger.warning(f"yt-dlp failed q={q}: {err[:200]}")
            last_error = err
            if "is_live" in err.lower() or "live" in err.lower():
                raise YtDlpError("❌ Live-трансляции не поддерживаются")
            continue

        try:
            lines = (stdout.decode(errors="ignore") or "").strip().splitlines()
            meta = json.loads(lines[-1]) if lines else {}
        except Exception:
            meta = {}
        # ищем новый файл
        new_files = [f for f in os.listdir(output_dir) if f not in before]
        if not new_files:
            vid = meta.get("id", "")
            ext = meta.get("extractor", "")
            prefix = f"{ext}-{vid}" if vid else ""
            cands = [os.path.join(output_dir, f) for f in os.listdir(output_dir)
                     if prefix and f.startswith(prefix)]
            file_path = max(cands, key=os.path.getmtime) if cands else None
        else:
            new_files.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
            file_path = os.path.join(output_dir, new_files[0])

        if not file_path or not os.path.exists(file_path):
            last_error = "yt-dlp succeeded but file not found"
            continue
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > max_size_mb:
            logger.warning(f"Too large {size_mb:.1f}MB, downgrade (q={q})")
            try:
                os.remove(file_path)
            except Exception:
                pass
            before = set(os.listdir(output_dir))
            last_error = f"File too large: {size_mb:.1f} MB"
            continue
        title = meta.get("title") or info_title or os.path.basename(file_path)
        return {"file_path": file_path, "title": title,
                "ext": os.path.splitext(file_path)[1].lstrip("."),
                "filesize_mb": size_mb, "info": meta}

    if last_error and "too large" in last_error.lower():
        try:
            direct = await get_direct_url(url, cookies_path)
            raise YtDlpError(f"📎 Файл слишком большой для Telegram даже в минимальном качестве. Прямая ссылка: {direct}")
        except YtDlpError:
            raise
        except Exception:
            pass
        raise YtDlpError(f"📎 Файл слишком большой для Telegram (> {max_size_mb} MB). {last_error[:200]}")
    raise YtDlpError(_humanize(last_error or "unknown error"))


async def get_direct_url(url: str, cookies_path: str | None = None) -> str:
    _check_url(url)
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--get-url",
           "-f", "worst[ext=mp4]/worst", *cookies_args(cookies_path), url]
    rc, stdout, stderr = await _run(cmd, timeout=60)
    if rc == 0 and stdout.decode().strip():
        return stdout.decode().strip().splitlines()[0]
    raise YtDlpError("Не удалось получить прямую ссылку")


async def update_ytdlp() -> tuple[bool, str]:
    for cmd in (["yt-dlp", "-U"], ["pip", "install", "-U", "yt-dlp"]):
        try:
            rc, out, err = await _run(cmd, timeout=180)
            if rc == 0:
                return True, f"Updated via {' '.join(cmd)}"
        except Exception:
            continue
    return False, "Failed to update yt-dlp"
