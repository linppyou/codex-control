from __future__ import annotations

import sqlite3
from dataclasses import dataclass


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


def last_assignment_change(conn: sqlite3.Connection, username: str) -> int | None:
    """该用户最近一次「账号真正变化」的时间；从未分配过则为 None。

    回收后再分配同一个账号不算变化：用户始终持有同一份凭证，期间启动的
    codex 进程仍然有效。
    """
    rows = conn.execute(
        "SELECT account_pk, started_at, ended_at FROM assignments WHERE username = ? ORDER BY started_at, id",
        (username,),
    ).fetchall()
    boundary: int | None = None
    previous: int | None = None
    for index, row in enumerate(rows):
        account_pk, started_at, ended_at = int(row[0]), int(row[1]), row[2]
        if account_pk != previous:
            boundary = started_at
        if ended_at is not None:
            next_account = int(rows[index + 1][0]) if index + 1 < len(rows) else None
            if next_account != account_pk:
                boundary = int(ended_at)
        previous = account_pk
    return boundary


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

