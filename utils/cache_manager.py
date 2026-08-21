import os
import logging
import aiosqlite

logger = logging.getLogger(__name__)


async def cleanup_cache(downloads_dir: str, db_path: str, limit_gb: float = 4.0):
    """LRU-очистка по плану §9.1"""
    limit_bytes = int(limit_gb * 1024 * 1024 * 1024)
    if not os.path.exists(downloads_dir):
        return

    # считаем размер
    files = []
    total = 0
    for fname in os.listdir(downloads_dir):
        fpath = os.path.join(downloads_dir, fname)
        if os.path.isfile(fpath):
            sz = os.path.getsize(fpath)
            total += sz
            # last_accessed берём из БД если есть, иначе mtime
            files.append((fpath, sz, os.path.getmtime(fpath)))

    if total <= limit_bytes:
        logger.debug(f"Cache OK: {total / 1024 / 1024:.1f} MB / {limit_gb} GB")
        return

    # загружаем last_accessed из БД для приоритизации LRU
    path_to_access = {}
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT file_path, last_accessed FROM downloads WHERE file_path IS NOT NULL")
            rows = await cur.fetchall()
            for r in rows:
                path_to_access[r["file_path"]] = r["last_accessed"]
    except Exception as e:
        logger.warning(f"Failed to load last_accessed: {e}")

    # сортируем по last_accessed (старые первые)
    def sort_key(item):
        fpath, sz, mtime = item
        # если есть в БД — используем её, иначе mtime
        return path_to_access.get(fpath, str(mtime))

    files.sort(key=sort_key)

    removed = 0
    for fpath, sz, _ in files:
        if total <= limit_bytes:
            break
        try:
            os.remove(fpath)
            total -= sz
            removed += 1
            logger.info(f"Cache evicted: {fpath} ({sz / 1024 / 1024:.1f} MB)")
            # обновляем БД
            async with aiosqlite.connect(db_path) as db:
                await db.execute("UPDATE downloads SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE file_path = ?", (fpath,))
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to remove {fpath}: {e}")

    logger.info(f"Cache cleanup done: removed {removed} files, new total {total / 1024 / 1024:.1f} MB")


async def sync_db_fs(downloads_dir: str, db_path: str):
    """Синхронизация БД ↔ ФС по плану §9.2"""
    if not os.path.exists(downloads_dir):
        os.makedirs(downloads_dir, exist_ok=True)
        return

    disk_files = set(os.path.join(downloads_dir, f) for f in os.listdir(downloads_dir) if os.path.isfile(os.path.join(downloads_dir, f)))

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id, file_path FROM downloads WHERE status = 'cached' OR status = 'done'")
            rows = await cur.fetchall()
            db_files = {r["file_path"] for r in rows if r["file_path"]}

            # осиротевшие файлы на диске
            for fpath in disk_files:
                if fpath not in db_files:
                    # файл без записи — удаляем (осиротевший)
                    try:
                        os.remove(fpath)
                        logger.info(f"Removed orphan file: {fpath}")
                    except Exception as e:
                        logger.warning(f"Failed to remove orphan {fpath}: {e}")

            # записи без файлов
            for r in rows:
                fp = r["file_path"]
                if fp and fp not in disk_files:
                    await db.execute("UPDATE downloads SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (r["id"],))
            await db.commit()
    except Exception as e:
        logger.error(f"sync_db_fs error: {e}")


def get_cache_size_mb(downloads_dir: str) -> float:
    if not os.path.exists(downloads_dir):
        return 0.0
    total = 0
    for fname in os.listdir(downloads_dir):
        fpath = os.path.join(downloads_dir, fname)
        if os.path.isfile(fpath):
            total += os.path.getsize(fpath)
    return total / (1024 * 1024)
