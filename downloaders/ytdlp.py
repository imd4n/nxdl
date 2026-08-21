import os
import re
import json
import shlex
import asyncio
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Защита от плейлистов/live проверяется и тут
PLAYLIST_RE = re.compile(r"[?&]list=|playlist", re.IGNORECASE)


class YtDlpError(Exception):
    pass


def _build_base_args(url: str, output_template: str, cookies_path: str = None, audio_only: bool = False,
                     quality: str = "best[height<=720]") -> list[str]:
    args = [
        "yt-dlp",
        "--no-playlist",
        "--max-downloads", "1",
        "--no-warnings",
        "--match-filters", "!is_live",
        "--no-live-from-start",
    ]
    if cookies_path and os.path.exists(cookies_path):
        args += ["--cookies", cookies_path]

    if audio_only:
        args += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3"]
    else:
        args += ["-f", quality, "--merge-output-format", "mp4"]

    args += ["-o", output_template, "--print-json", url]
    return args


async def get_formats(url: str, cookies_path: str = None) -> list[dict]:
    """Получить список форматов через yt-dlp -J (json)."""
    # Безопасный вызов: список аргументов, без shell=True
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-J", url]
    if cookies_path and os.path.exists(cookies_path):
        cmd += ["--cookies", cookies_path]

    # проверка на опасные символы
    if any(c in url for c in [";", "&&", "||", "`", "$("]):
        raise YtDlpError("URL содержит запрещённые символы")

    logger.info(f"yt-dlp get_formats: {url[:80]}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore")[:800]
        raise YtDlpError(f"yt-dlp failed: {err}")

    try:
        info = json.loads(stdout.decode())
    except Exception as e:
        raise YtDlpError(f"Failed to parse yt-dlp json: {e}")

    formats = info.get("formats", [])
    # фильтруем видео+аудио
    result = []
    for f in formats:
        result.append({
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
    # также возвращаем title для caption
    return {"formats": result, "title": info.get("title", ""), "extractor": info.get("extractor", ""), "info": info}


async def download(url: str, output_dir: str = "downloads", cookies_path: str = None,
                   audio_only: bool = False, quality: str = "best[height<=720]",
                   max_size_mb: int = 50) -> dict:
    """
    Скачать медиа через yt-dlp.
    Стратегия downgrade_quality по плану §8.3.
    Returns {"file_path": str, "title": str, "ext": str, "filesize_mb": float}
    """
    os.makedirs(output_dir, exist_ok=True)

    if any(c in url for c in [";", "&&", "||", "`", "$("]):
        raise YtDlpError("URL содержит запрещённые символы")

    if PLAYLIST_RE.search(url):
        logger.warning(f"Playlist URL detected: {url[:80]}")

    # проверка что url экранируется корректно (используем список аргументов — уже безопасно)
    quoted = shlex.quote(url)
    logger.debug(f"shlex quoted url: {quoted}")

    qualities_to_try = [quality]
    if not audio_only and quality != "worst[ext=mp4]/worst":
        # стратегия downgrade
        if quality == "best[height<=720]":
            qualities_to_try = ["best[height<=720]", "best[height<=480]", "worst[ext=mp4]/worst"]
        elif "720" in quality:
            qualities_to_try = [quality, "best[height<=480]", "worst[ext=mp4]/worst"]

    last_error = None
    for q in qualities_to_try:
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        cmd = _build_base_args(url, output_template, cookies_path, audio_only=audio_only, quality=q)
        # убираем --print-json для реальной загрузки, заменим на обычный вывод
        # yt-dlp с --print-json выводит json в stdout, файл качается параллельно
        # используем тот же cmd но парсим json
        logger.info(f"yt-dlp download attempt quality={q}: {url[:80]}")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            try:
                info = json.loads(stdout.decode().splitlines()[-1] if stdout else "{}")
            except:
                info = {}
            # находим скачанный файл
            vid_id = info.get("id", "unknown")
            ext = info.get("ext", "mp4" if not audio_only else "mp3")
            # yt-dlp может переименовать — ищем файл
            candidates = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.startswith(vid_id)]
            if candidates:
                # берём самый свежий
                candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                file_path = candidates[0]
            else:
                # fallback: ищем любой mp4/mp3 созданный только что
                all_files = [os.path.join(output_dir, f) for f in os.listdir(output_dir)]
                all_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                file_path = all_files[0] if all_files else None

            if file_path and os.path.exists(file_path):
                size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if size_mb > max_size_mb:
                    logger.warning(f"File {file_path} too large: {size_mb:.1f} MB > {max_size_mb} MB, trying downgrade")
                    # удаляем большой файл
                    try:
                        os.remove(file_path)
                    except:
                        pass
                    last_error = f"File too large: {size_mb:.1f} MB"
                    continue  # пробуем следующее качество
                title = info.get("title", os.path.basename(file_path))
                return {"file_path": file_path, "title": title, "ext": ext, "filesize_mb": size_mb, "info": info}

            # если файла нет, но код 0 — считаем успехом с fallback
            raise YtDlpError("yt-dlp succeeded but file not found")

        else:
            err = stderr.decode(errors="ignore")[:1000]
            logger.warning(f"yt-dlp failed quality {q}: {err[:300]}")
            last_error = err
            # если ошибка не связана с размером — не пробуем дальше, кроме случая "File too large"
            if "is_live" in err.lower() or "live" in err.lower():
                raise YtDlpError("❌ Live-трансляции не поддерживаются")
            continue

    # все попытки исчерпаны — пробуем получить прямую ссылку
    if last_error and "too large" in last_error.lower():
        try:
            direct_url = await get_direct_url(url, cookies_path)
            raise YtDlpError(f"📎 Файл слишком большой для Telegram даже в минимальном качестве. Прямая ссылка: {direct_url}")
        except YtDlpError:
            raise
        except Exception as e:
            raise YtDlpError(f"Файл слишком большой и не удалось получить прямую ссылку: {last_error[:400]}")

    raise YtDlpError(f"Не удалось скачать: {(last_error or 'unknown')[:600]}")


async def get_direct_url(url: str, cookies_path: str = None) -> str:
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--get-url", "-f", "worst[ext=mp4]/worst", url]
    if cookies_path and os.path.exists(cookies_path):
        cmd += ["--cookies", cookies_path]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return stdout.decode().strip().splitlines()[0]
    raise YtDlpError("Не удалось получить прямую ссылку")


async def update_ytdlp() -> tuple[bool, str]:
    """Обновление yt-dlp через pip или yt-dlp -U."""
    for cmd in [["yt-dlp", "-U"], ["pip", "install", "-U", "yt-dlp"]]:
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                return True, f"Updated via {' '.join(cmd)}"
        except Exception as e:
            continue
    return False, "Failed to update yt-dlp"
