from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import codexctl.usage.pricing as pricing
from codexctl.migrations.legacy import _migrate, _migrate_user_keys
from codexctl.migrations.schema import _API_KEYS, _API_USAGE, SCHEMA


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


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
