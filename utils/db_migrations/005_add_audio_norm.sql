-- v1.2: флаг аудио + нормализованный URL для дедупликации
-- idempotent: выполняется один раз через PRAGMA user_version
ALTER TABLE downloads ADD COLUMN is_audio INTEGER DEFAULT 0;
ALTER TABLE downloads ADD COLUMN url_norm TEXT;
CREATE INDEX IF NOT EXISTS idx_audio ON downloads(is_audio);
CREATE INDEX IF NOT EXISTS idx_url_norm ON downloads(url_norm);
