from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import pricing

_API_KEYS = """
CREATE TABLE IF NOT EXISTS {keys} (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    secret_hash BLOB NOT NULL,
    secret_ciphertext BLOB,
    secret_version INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    username TEXT,
    allow_fast INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    last_used_at INTEGER,
    CHECK (revoked_at IS NOT NULL OR (username IS NOT NULL AND username <> ''))
)
"""
_API_USAGE = """
CREATE TABLE IF NOT EXISTS {usage} (
    request_id TEXT PRIMARY KEY,
    key_pk INTEGER NOT NULL REFERENCES {keys}(id),
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    model_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    first_output_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    requested_tier TEXT,
    actual_tier TEXT
)
"""

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
    created_at INTEGER NOT NULL,
    password_configured INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS site_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    host_key TEXT,
    public_key TEXT NOT NULL,
    private_key BLOB NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE (host, port)
);
CREATE TABLE IF NOT EXISTS site_inventories (
    server_id TEXT PRIMARY KEY REFERENCES site_servers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    attempted_at INTEGER NOT NULL,
    synced_at INTEGER,
    error TEXT,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_skills (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('dispatcher', 'migration')),
    name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    label TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    archive BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_targets (
    id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL REFERENCES site_servers(id) ON DELETE CASCADE,
    server_revision INTEGER NOT NULL,
    domain TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    credential_env TEXT NOT NULL,
    project TEXT NOT NULL,
    users TEXT NOT NULL,
    UNIQUE (server_id, domain)
);
CREATE TABLE IF NOT EXISTS dispatch_tasks (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    recipient TEXT NOT NULL,
    recipient_uid INTEGER NOT NULL,
    target_id TEXT NOT NULL,
    target_snapshot TEXT NOT NULL,
    skills TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    csv TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    account_label TEXT,
    thread_id TEXT,
    turn_id TEXT,
    turn_status TEXT,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dispatch_tasks_owner ON dispatch_tasks(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS dispatch_tasks_queue ON dispatch_tasks(status, confirmed_at);
CREATE TABLE IF NOT EXISTS account_models (
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    model_id TEXT NOT NULL,
    upstream_available INTEGER NOT NULL DEFAULT 1,
    selectable INTEGER NOT NULL DEFAULT 1,
    discovered_at INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_at INTEGER,
    fast_verified_at INTEGER,
    PRIMARY KEY (account_pk, model_id)
);
CREATE TABLE IF NOT EXISTS account_model_catalogs (
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    client_version TEXT NOT NULL,
    catalog TEXT NOT NULL,
    synced_at INTEGER NOT NULL,
    PRIMARY KEY (account_pk, client_version)
);
""" + _API_KEYS.format(keys="api_keys") + ";" + _API_USAGE.format(usage="api_usage_events", keys="api_keys") + ";"


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


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a migrated database for a single unit of work."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


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
        if "system_uid" not in {r["name"] for r in conn.execute("PRAGMA table_info(web_sessions)")}:
            conn.execute("ALTER TABLE web_sessions ADD COLUMN system_uid INTEGER")
        if "password_configured" not in {r["name"] for r in conn.execute("PRAGMA table_info(admins)")}:
            conn.execute("ALTER TABLE admins ADD COLUMN password_configured INTEGER NOT NULL DEFAULT 0")
        key_columns = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)")}
        if "account_pk" in key_columns:
            _migrate_user_keys(conn, key_columns)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS api_keys_one_active ON api_keys(username) WHERE revoked_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS api_usage_events_key_ts ON api_usage_events(key_pk, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS api_usage_events_account_ts ON api_usage_events(account_pk, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS api_usage_events_ts ON api_usage_events(ts)")
        if "selectable" not in {r["name"] for r in conn.execute("PRAGMA table_info(account_models)")}:
            conn.execute("ALTER TABLE account_models ADD COLUMN selectable INTEGER NOT NULL DEFAULT 1")
            # Older rows started disabled until tested. Preserve explicit administrator disables.
            conn.execute("""
                UPDATE account_models SET enabled = 1 WHERE enabled = 0 AND NOT EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.action = 'api_model.disable' AND a.target = account_pk || '/' || model_id
                )
            """)
            conn.execute("""
                UPDATE account_models AS m SET selectable = 0 WHERE EXISTS (
                    SELECT 1 FROM account_model_catalogs c, json_each(c.catalog, '$.models') model
                    WHERE c.account_pk = m.account_pk AND c.synced_at = m.discovered_at
                      AND json_extract(model.value, '$.slug') = m.model_id
                      AND (json_extract(model.value, '$.visibility') IN ('hide', 'hidden')
                           OR json_type(model.value, '$.supported_in_api') = 'false')
                )
            """)


def _migrate_user_keys(conn: sqlite3.Connection, columns: set[str]) -> None:
    if "username" not in columns:
        conn.execute("ALTER TABLE api_keys ADD COLUMN username TEXT")
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE api_keys ADD COLUMN deleted_at INTEGER")
    conn.execute(_API_KEYS.format(keys="api_keys_new"))
    conn.execute(_API_USAGE.format(usage="api_usage_events_new", keys="api_keys_new"))
    # Prefer the credential already used with the current assignment.
    conn.execute("""
        WITH ranked AS (
            SELECT k.id, ROW_NUMBER() OVER (PARTITION BY k.username
                ORDER BY (a.account_pk IS k.account_pk) DESC, k.last_used_at DESC, k.id DESC) AS rank
            FROM api_keys k LEFT JOIN assignments a ON a.username = k.username AND a.ended_at IS NULL
            WHERE k.username IS NOT NULL AND k.username <> '' AND k.revoked_at IS NULL AND k.deleted_at IS NULL
                AND (k.expires_at IS NULL OR k.expires_at > unixepoch())
        )
        INSERT INTO api_keys_new (id, public_id, secret_hash, secret_version, name, username, allow_fast,
                                  created_by, created_at, revoked_at, last_used_at)
        SELECT k.id, k.public_id, k.secret_hash, k.secret_version, k.name, k.username, k.allow_fast,
               k.created_by, k.created_at,
               CASE WHEN r.rank = 1 THEN NULL ELSE COALESCE(k.revoked_at, k.deleted_at, unixepoch()) END,
               k.last_used_at
        FROM api_keys k LEFT JOIN ranked r ON r.id = k.id
    """)
    conn.execute("INSERT INTO api_usage_events_new SELECT * FROM api_usage_events")
    conn.execute("DROP TABLE api_usage_events")
    # Clear the old deferred default-model reference before dropping both tables.
    conn.execute("DELETE FROM api_key_models")
    conn.execute("DELETE FROM api_keys")
    conn.execute("DROP TABLE api_key_models")
    conn.execute("DROP TABLE api_keys")
    conn.execute("ALTER TABLE api_keys_new RENAME TO api_keys")
    conn.execute("ALTER TABLE api_usage_events_new RENAME TO api_usage_events")


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
