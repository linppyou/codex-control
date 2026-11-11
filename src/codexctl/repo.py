from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: int
    label: str
    email: str | None
    account_id: str
    plan_type: str | None
    status: str
    last_refresh: int
    access_expires_at: int
    last_error: str | None
    retry_count: int
    next_retry_at: int | None
    created_at: int


def _account(row: sqlite3.Row | None) -> Account | None:
    return Account(**dict(row)) if row is not None else None


def insert_account(
    conn: sqlite3.Connection,
    *,
    label: str,
    email: str | None,
    account_id: str,
    plan_type: str | None,
    last_refresh: int,
    access_expires_at: int,
    now: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts(label, email, account_id, plan_type, last_refresh, access_expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (label, email, account_id, plan_type, last_refresh, access_expires_at, now),
    )
    return int(cur.lastrowid)


def get_account(conn: sqlite3.Connection, label: str) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone())


def get_account_by_pk(conn: sqlite3.Connection, pk: int) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE id = ?", (pk,)).fetchone())


def find_by_account_id(conn: sqlite3.Connection, account_id: str) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone())


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    return [Account(**dict(r)) for r in conn.execute("SELECT * FROM accounts ORDER BY label")]


def store_secret(conn: sqlite3.Connection, account_pk: int, blob: bytes, now: int) -> None:
    conn.execute(
        """
        INSERT INTO account_secrets(account_pk, blob, version, updated_at) VALUES (?, ?, 1, ?)
        ON CONFLICT(account_pk) DO UPDATE SET
            blob = excluded.blob,
            version = account_secrets.version + 1,
            updated_at = excluded.updated_at
        """,
        (account_pk, blob, now),
    )


def load_secret(conn: sqlite3.Connection, account_pk: int) -> bytes:
    row = conn.execute("SELECT blob FROM account_secrets WHERE account_pk = ?", (account_pk,)).fetchone()
    if row is None:
        raise LookupError(f"账号 {account_pk} 没有保存凭证")
    return bytes(row["blob"])


def update_token_meta(
    conn: sqlite3.Connection,
    pk: int,
    *,
    email: str | None,
    plan_type: str | None,
    last_refresh: int,
    access_expires_at: int,
) -> None:
    conn.execute(
        "UPDATE accounts SET email = ?, plan_type = ?, last_refresh = ?, access_expires_at = ?, "
        "status = CASE WHEN status = 'disabled' THEN 'disabled' ELSE 'active' END, last_error = NULL, retry_count = 0, next_retry_at = NULL WHERE id = ?",
        (email, plan_type, last_refresh, access_expires_at, pk),
    )


def set_status(conn: sqlite3.Connection, pk: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE accounts SET status = ?, last_error = ? WHERE id = ?", (status, error, pk))


def mark_retry(conn: sqlite3.Connection, pk: int, error: str, next_retry_at: int) -> None:
    conn.execute(
        "UPDATE accounts SET retry_count = retry_count + 1, last_error = ?, next_retry_at = ? WHERE id = ?",
        (error, next_retry_at, pk),
    )

@dataclass(frozen=True)
class Assignment:
    id: int
    username: str
    account_pk: int
    started_at: int
    ended_at: int | None


def active_assignment(conn: sqlite3.Connection, username: str) -> Assignment | None:
    row = conn.execute(
        "SELECT * FROM assignments WHERE username = ? AND ended_at IS NULL", (username,)
    ).fetchone()
    return Assignment(**dict(row)) if row is not None else None


def all_active(conn: sqlite3.Connection) -> list[Assignment]:
    return [
        Assignment(**dict(r))
        for r in conn.execute("SELECT * FROM assignments WHERE ended_at IS NULL ORDER BY username")
    ]


def active_usernames(conn: sqlite3.Connection, account_pk: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT username FROM assignments WHERE account_pk = ? AND ended_at IS NULL ORDER BY username",
            (account_pk,),
        )
    ]


def start_assignment(conn: sqlite3.Connection, username: str, account_pk: int, now: int) -> None:
    conn.execute(
        "INSERT INTO assignments(username, account_pk, started_at) VALUES (?, ?, ?)", (username, account_pk, now)
    )


def end_assignment(conn: sqlite3.Connection, username: str, now: int) -> None:
    conn.execute("UPDATE assignments SET ended_at = ? WHERE username = ? AND ended_at IS NULL", (now, username))


def account_at(conn: sqlite3.Connection, username: str, ts: int) -> int | None:
    row = conn.execute(
        "SELECT account_pk FROM assignments WHERE username = ? AND started_at <= ? "
        "AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at DESC LIMIT 1",
        (username, ts, ts),
    ).fetchone()
    return int(row[0]) if row is not None else None


def users_with_recent_assignments(conn: sqlite3.Connection, since: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT username FROM assignments WHERE ended_at IS NULL OR ended_at >= ? ORDER BY username",
            (since,),
        )
    ]
