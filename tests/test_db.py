import os
import sqlite3
import stat

import pytest

from codexctl import db, pricing


def test_schema_created():
    conn = db.connect(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "accounts", "account_secrets", "assignments", "quota_samples",
        "usage_events", "log_offsets", "model_prices", "alerts", "audit_log",
    } <= names


def test_transaction_rolls_back_on_error():
    conn = db.connect(":memory:")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO audit_log(ts, actor, action, target) VALUES (1, 'a', 'b', 'c')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_transaction_commits():
    conn = db.connect(":memory:")
    with db.transaction(conn):
        conn.execute("INSERT INTO audit_log(ts, actor, action, target) VALUES (1, 'a', 'b', 'c')")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_only_one_active_assignment_per_user():
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO accounts(label, account_id, last_refresh, access_expires_at, created_at) "
        "VALUES ('a', 'x', 0, 0, 0)"
    )
    conn.execute("INSERT INTO assignments(username, account_pk, started_at) VALUES ('alice', 1, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO assignments(username, account_pk, started_at) VALUES ('alice', 1, 1)")


def test_file_database_uses_wal(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_db_file_mode_0600_regardless_of_umask(tmp_path):
    old = os.umask(0o022)
    try:
        db.connect(tmp_path / "x.db")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "x.db").stat().st_mode) == 0o600


def test_busy_timeout_set(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_migrates_existing_web_sessions(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE web_sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "csrf TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
        )
        old.execute("INSERT INTO web_sessions VALUES ('hash', 'admin', 'csrf', 0, 123)")

    with db.connect(path) as conn:
        assert tuple(conn.execute("SELECT username, system_uid FROM web_sessions").fetchone()) == ("admin", None)


def test_migrates_dispatch_upload_column(tmp_path):
    path = tmp_path / "tasks.db"
    with db.connect(path) as conn:
        conn.execute("ALTER TABLE dispatch_tasks DROP COLUMN upload")
    for _ in range(2):
        with db.connect(path) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(dispatch_tasks)")}
            assert "upload" in columns


def test_migrates_dispatch_execution_options(tmp_path):
    path = tmp_path / "tasks.db"
    with db.connect(path) as conn:
        conn.execute("ALTER TABLE dispatch_tasks DROP COLUMN model")
        conn.execute("ALTER TABLE dispatch_tasks DROP COLUMN reasoning_effort")
        conn.execute(
            """INSERT INTO dispatch_tasks
               (id, owner, recipient, recipient_uid, target_id, target_snapshot, skills,
                source_url, title, prompt, csv, status, created_at)
               VALUES ('old', 'alice', 'alice', 1001, 'target', '{}', '[]',
                       'https://example.com', 'old task', 'prompt', 'csv', 'queued', 1)"""
        )
    for _ in range(2):
        with db.connect(path) as conn:
            task = conn.execute("SELECT * FROM dispatch_tasks WHERE id='old'").fetchone()
            assert task["model"] is None and task["reasoning_effort"] is None
            assert task["status"] == "queued"


def test_migrates_old_accounts_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE, email TEXT, "
        "account_id TEXT NOT NULL UNIQUE, plan_type TEXT, status TEXT NOT NULL DEFAULT 'active', "
        "last_refresh INTEGER NOT NULL, access_expires_at INTEGER NOT NULL, last_error TEXT, "
        "retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at INTEGER, created_at INTEGER NOT NULL)"
    )
    old.execute("INSERT INTO accounts(label, account_id, last_refresh, access_expires_at, created_at) VALUES ('a', 'x', 1, 2, 3)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT display_name FROM accounts WHERE label = 'a'").fetchone()[0] is None
    db.connect(path)  # 再次连接不应重复加列


def test_migrates_old_usage_tables(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE usage_events (id INTEGER PRIMARY KEY, username TEXT NOT NULL, account_pk INTEGER, "
        "ts INTEGER NOT NULL, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL, "
        "output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL, "
        "session_file TEXT NOT NULL)"
    )
    old.execute("CREATE TABLE log_offsets (path TEXT PRIMARY KEY, inode INTEGER NOT NULL, offset INTEGER NOT NULL, last_totals TEXT)")
    old.execute("INSERT INTO usage_events VALUES (1, 'u', NULL, 1, 1, 0, 0, 0, 1, 'f')")
    old.execute("INSERT INTO log_offsets VALUES ('p', 1, 10, NULL)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT model FROM usage_events").fetchone()[0] is None
    assert conn.execute("SELECT last_model FROM log_offsets").fetchone()[0] is None
    db.connect(path)


def test_seeds_model_prices_once_and_keeps_edits(tmp_path):
    path = tmp_path / "prices.db"
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0] == len(pricing.PRICES)
    conn.execute("UPDATE model_prices SET input_usd = 9 WHERE name = 'GPT-5.2'")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("SELECT input_usd FROM model_prices WHERE name = 'GPT-5.2'").fetchone()[0] == 9
    conn.execute("DELETE FROM model_prices")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0] == 0


def test_migrates_dispatch_runtime_columns(tmp_path):
    path = tmp_path / 'old-runtime.db'
    with db.connect(path) as conn:
        conn.execute('ALTER TABLE dispatch_tasks DROP COLUMN sandbox')
        conn.execute('ALTER TABLE dispatch_tasks DROP COLUMN result_text')
        conn.execute("""INSERT INTO dispatch_tasks
            (id,owner,recipient,recipient_uid,target_id,target_snapshot,skills,source_url,title,prompt,csv,status,created_at)
            VALUES ('old','alice','alice',1001,'t','{}','[]','https://a.com','old','prompt','csv','queued',1)""")
    for _ in range(2):
        with db.connect(path) as conn:
            row=conn.execute('SELECT sandbox,result_text FROM dispatch_tasks WHERE id=\'old\'').fetchone()
            assert tuple(row) == ('workspace-write','')
