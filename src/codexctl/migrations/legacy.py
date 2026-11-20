from __future__ import annotations

import sqlite3

from codexctl.migrations.schema import _API_KEYS, _API_USAGE


def _migrate(conn: sqlite3.Connection) -> None:
    from codexctl.db import transaction

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
        if "upload" not in {r["name"] for r in conn.execute("PRAGMA table_info(dispatch_tasks)")}:
            conn.execute("ALTER TABLE dispatch_tasks ADD COLUMN upload BLOB")
        task_columns = {r["name"] for r in conn.execute("PRAGMA table_info(dispatch_tasks)")}
        for column in ("model", "reasoning_effort", "working_directory"):
            if column not in task_columns:
                conn.execute(f"ALTER TABLE dispatch_tasks ADD COLUMN {column} TEXT")
        if "result_text" not in task_columns:
            conn.execute("ALTER TABLE dispatch_tasks ADD COLUMN result_text TEXT NOT NULL DEFAULT ''")
        if "sandbox" not in task_columns:
            conn.execute("ALTER TABLE dispatch_tasks ADD COLUMN sandbox TEXT NOT NULL DEFAULT 'workspace-write'")
        if "run_immediately" not in task_columns:
            conn.execute("ALTER TABLE dispatch_tasks ADD COLUMN run_immediately INTEGER NOT NULL DEFAULT 0")
        if "system_uid" not in {r["name"] for r in conn.execute("PRAGMA table_info(web_sessions)")}:
            conn.execute("ALTER TABLE web_sessions ADD COLUMN system_uid INTEGER")
        if "password_configured" not in {r["name"] for r in conn.execute("PRAGMA table_info(admins)")}:
            conn.execute("ALTER TABLE admins ADD COLUMN password_configured INTEGER NOT NULL DEFAULT 0")
        if "wp_center_url" in {r["name"] for r in conn.execute("PRAGMA table_info(site_servers)")}:
            conn.execute("ALTER TABLE site_servers DROP COLUMN wp_center_url")
        server_columns = {r["name"] for r in conn.execute("PRAGMA table_info(site_servers)")}
        for column, kind in (("panel_url", "TEXT"), ("panel_username", "TEXT"), ("panel_password", "BLOB")):
            if column not in server_columns:
                conn.execute(f"ALTER TABLE site_servers ADD COLUMN {column} {kind}")
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

