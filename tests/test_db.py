import os
import sqlite3
import stat

import pytest

from codexctl import db


def test_schema_created():
    conn = db.connect(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "accounts", "account_secrets", "assignments", "quota_samples",
        "usage_events", "log_offsets", "alerts", "audit_log",
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
