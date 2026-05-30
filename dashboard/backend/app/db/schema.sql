-- Dashboard backend SQLite schema. Created on first startup; idempotent.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    cache_key       TEXT,                       -- sha256(intent + canonical(args))
    created_ms      INTEGER NOT NULL,
    updated_ms      INTEGER NOT NULL,
    intent          TEXT,
    args_json       TEXT,
    text            TEXT,                        -- original user input
    client_kind     TEXT NOT NULL,               -- 'owner' | 'guest'
    ip              TEXT,
    status          TEXT NOT NULL,               -- 'in_progress' | 'done' | 'error'
    reply_text      TEXT,
    total_cost_usd  REAL DEFAULT 0,
    events_json     TEXT,                        -- JSON array of all emitted events
    cached_from     TEXT,                        -- source session_id if this is a replay
    cached_at_ms    INTEGER,                     -- when the source session originally ran
    expires_at_ms   INTEGER                      -- when this session stops being cache-eligible
);

CREATE INDEX IF NOT EXISTS idx_sessions_cache_lookup
    ON sessions(cache_key, expires_at_ms);

CREATE INDEX IF NOT EXISTS idx_sessions_created
    ON sessions(created_ms DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_ip
    ON sessions(ip, created_ms DESC);


CREATE TABLE IF NOT EXISTS rate_limit (
    ip          TEXT NOT NULL,
    hour_bucket INTEGER NOT NULL,                -- floor(unix_ts / 3600)
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket
    ON rate_limit(hour_bucket);


-- Spent USD aggregated per UTC day. Only guest spend lands here; owner is
-- excluded from the global cap by design.
CREATE TABLE IF NOT EXISTS daily_budget (
    day_bucket  INTEGER PRIMARY KEY,             -- yyyymmdd UTC
    spent_usd   REAL NOT NULL DEFAULT 0
);
