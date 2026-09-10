CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  app_version TEXT,
  comment TEXT,
  log TEXT,
  session_id INTEGER,
  event_count INTEGER,
  log_url TEXT
);

-- Migration for existing databases (run once; harmless if the
-- column is already there — it will simply error, so ignore that):
-- ALTER TABLE issues ADD COLUMN log_url TEXT;
