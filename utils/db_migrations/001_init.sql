CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    url TEXT NOT NULL,
    source TEXT,
    quality TEXT,
    file_size_mb REAL,
    file_path TEXT,
    cdn_url TEXT,
    cdn_expires_at TIMESTAMP,
    telegram_file_id TEXT,
    status TEXT DEFAULT 'pending',
    error_msg TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_history ON downloads(user_id, last_accessed);
CREATE INDEX IF NOT EXISTS idx_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_url ON downloads(url);
CREATE INDEX IF NOT EXISTS idx_file_id ON downloads(telegram_file_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    default_downloader TEXT DEFAULT 'auto',
    default_quality TEXT DEFAULT 'best',
    notify_on_finish INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);
