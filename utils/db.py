import os
import glob
import logging
import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "db_migrations")


async def init_db(db_path: str = DB_PATH):
    """Инициализация БД, применение миграций по user_version."""
    from .helpers import normalize_url  # lazy: избежать цикла импорта
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        cur = await db.execute("PRAGMA user_version;")
        row = await cur.fetchone()
        current_version = row[0] if row else 0

        migration_files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
        # нумерация по порядку файлов
        for idx, fpath in enumerate(migration_files, start=1):
            if idx <= current_version:
                continue
            logger.info(f"Applying migration {os.path.basename(fpath)} (version {idx})")
            with open(fpath, "r", encoding="utf-8") as f:
                sql = f.read()
            if sql.strip():
                try:
                    await db.executescript(sql)
                except Exception as e:
                    # для 004/005 может быть duplicate column — игнорируем
                    logger.warning(f"Migration {fpath} warning: {e}")
            await db.execute(f"PRAGMA user_version = {idx};")
        await db.commit()

        # backfill url_norm для старых строк (v1.2)
        try:
            cols = set()
            cur = await db.execute("PRAGMA table_info(downloads);")
            for r in await cur.fetchall():
                cols.add(r[1])
            if "url_norm" in cols:
                cur = await db.execute(
                    "SELECT id, url FROM downloads WHERE url_norm IS NULL LIMIT 500")
                rows = await cur.fetchall()
                for rid, url in rows:
                    try:
                        await db.execute("UPDATE downloads SET url_norm = ? WHERE id = ?",
                                         (normalize_url(url or ""), rid))
                    except Exception:
                        pass
                await db.commit()
        except Exception as e:
            logger.warning(f"url_norm backfill failed: {e}")
    logger.info(f"DB initialized at {db_path}, version {len(migration_files)}")


# ─── helpers ───────────────────────────────────────────────────────────────

async def is_banned(db_path: str, user_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row is not None


async def get_user_history(db_path: str, user_id: int, limit: int = 10):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM downloads WHERE user_id = ? AND telegram_file_id IS NOT NULL AND status IN ('done','cached') ORDER BY last_accessed DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def find_cached_by_url(db_path: str, url: str, is_audio: bool = False):
    """Поиск дубликата по нормализованному URL + is_audio (v1.2)."""
    from .helpers import normalize_url
    norm = normalize_url(url)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # сначала по url_norm (новый формат), затем fallback по точному url
        cur = await db.execute(
            """SELECT * FROM downloads WHERE url_norm = ? AND is_audio = ?
               AND status IN ('done','cached')
               AND telegram_file_id IS NOT NULL
               ORDER BY last_accessed DESC LIMIT 1""",
            (norm, 1 if is_audio else 0),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        cur = await db.execute(
            "SELECT * FROM downloads WHERE url = ? AND status IN ('done','cached') ORDER BY last_accessed DESC LIMIT 1",
            (url,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def insert_download(db_path: str, user_id: int, username: str, url: str,
                          source: str = "ytdlp", quality: str | None = None,
                          is_audio: bool = False) -> int:
    from .helpers import normalize_url
    norm = normalize_url(url)
    async with aiosqlite.connect(db_path) as db:
        # url_norm может отсутствовать в очень старых БД до миграции — пробуем с ним, иначе без
        try:
            cur = await db.execute(
                "INSERT INTO downloads (user_id, username, url, url_norm, source, quality, is_audio, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (user_id, username, url, norm, source, quality, 1 if is_audio else 0),
            )
        except Exception:
            cur = await db.execute(
                "INSERT INTO downloads (user_id, username, url, source, quality, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (user_id, username, url, source, quality),
            )
            try:
                await db.execute("UPDATE downloads SET is_audio = ? WHERE id = ?",
                                 (1 if is_audio else 0, cur.lastrowid))
            except Exception:
                pass
        await db.commit()
        return cur.lastrowid


async def update_download(db_path: str, download_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    # last_accessed обновляем явно если есть
    set_clause = ", ".join(f"{k} = ?" if v != "CURRENT_TIMESTAMP" else f"{k} = CURRENT_TIMESTAMP" for k, v in fields.items())
    values = [v for v in fields.values() if v != "CURRENT_TIMESTAMP"]
    values.append(download_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE downloads SET {set_clause} WHERE id = ?", values)
        await db.commit()


async def touch_accessed(db_path: str, download_id: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE downloads SET last_accessed = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (download_id,))
        await db.commit()
