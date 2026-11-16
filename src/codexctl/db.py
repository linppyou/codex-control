from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import pricing

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    email TEXT,
    account_id TEXT NOT NULL UNIQUE,
    plan_type TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_refresh INTEGER NOT NULL,
    access_expires_at INTEGER NOT NULL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    created_at INTEGER NOT NULL,
    display_name TEXT
);
CREATE TABLE IF NOT EXISTS account_secrets (
    account_pk INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    blob BLOB NOT NULL,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS assignments_one_active
    ON assignments(username) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS quota_samples (
    id INTEGER PRIMARY KEY,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    win TEXT NOT NULL,
    used_percent REAL NOT NULL,
    window_minutes INTEGER,
    resets_at INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_samples_latest ON quota_samples(account_pk, win, ts);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    session_file TEXT NOT NULL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS usage_events_user_ts ON usage_events(username, ts);
CREATE INDEX IF NOT EXISTS usage_events_user_session ON usage_events(username, session_file);
CREATE TABLE IF NOT EXISTS log_offsets (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    last_totals TEXT,
    last_model TEXT
);
CREATE TABLE IF NOT EXISTS model_prices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    input_usd REAL NOT NULL,
    cached_usd REAL NOT NULL,
    output_usd REAL NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open
    ON alerts(kind, target) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admins (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    system_uid INTEGER
);
CREATE TABLE IF NOT EXISTS web_system_users (
    username TEXT PRIMARY KEY,
    uid INTEGER NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    has_prices = _table_exists(conn, "model_prices")
    conn.executescript(SCHEMA)
    _migrate(conn)
    if not has_prices:
        _seed_prices(conn)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _seed_prices(conn: sqlite3.Connection) -> None:
    """只在首次创建 model_prices 表时写入默认价目表；用户删除后不再自动补回。"""
    rows = [
        (p.name, p.input_usd, p.cached_usd, p.output_usd, json.dumps(list(p.aliases), ensure_ascii=False))
        for p in pricing.PRICES
    ]
    with transaction(conn):
        conn.executemany(
            "INSERT INTO model_prices(name, input_usd, cached_usd, output_usd, aliases) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧数据库补齐后来新增的列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "display_name" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_events)")}
    if "model" not in cols:
        conn.execute("ALTER TABLE usage_events ADD COLUMN model TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(log_offsets)")}
    if "last_model" not in cols:
        conn.execute("ALTER TABLE log_offsets ADD COLUMN last_model TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS usage_events_user_session ON usage_events(username, session_file)")
    with transaction(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(web_sessions)")}
        if "system_uid" not in cols:
            conn.execute("ALTER TABLE web_sessions ADD COLUMN system_uid INTEGER")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
