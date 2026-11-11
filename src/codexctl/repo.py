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


def mark_needs_relogin(conn: sqlite3.Connection, pk: int, error: str) -> None:
    conn.execute(
        "UPDATE accounts SET status = CASE WHEN status = 'disabled' THEN 'disabled' ELSE 'needs_relogin' END, "
        "last_error = ? WHERE id = ?",
        (error, pk),
    )


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


def insert_quota_sample(
    conn: sqlite3.Connection,
    account_pk: int,
    ts: int,
    win: str,
    used_percent: float,
    window_minutes: int | None,
    resets_at: int | None,
    source: str,
) -> None:
    conn.execute(
        "INSERT INTO quota_samples(account_pk, ts, win, used_percent, window_minutes, resets_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_pk, ts, win, used_percent, window_minutes, resets_at, source),
    )


def latest_quota(conn: sqlite3.Connection, account_pk: int, *, api_fresh_since: int = 0) -> list[sqlite3.Row]:
    """每个窗口取一条：优先 ts >= api_fresh_since 的最新 api 样本，否则取任意来源的最新样本。"""
    return conn.execute(
        """
        SELECT * FROM (
            SELECT q.*, ROW_NUMBER() OVER (
                PARTITION BY q.win
                ORDER BY (q.source = 'api' AND q.ts >= ?) DESC, q.ts DESC, q.id DESC
            ) AS rn
            FROM quota_samples q WHERE q.account_pk = ?
        ) WHERE rn = 1 ORDER BY win
        """,
        (api_fresh_since, account_pk),
    ).fetchall()


def prune_quota_samples(conn: sqlite3.Connection, before: int) -> int:
    return conn.execute("DELETE FROM quota_samples WHERE ts < ?", (before,)).rowcount


def quota_history(conn: sqlite3.Connection, account_pk: int, since: int, source: str = "api") -> list[dict]:
    """按采样时间分组（新到旧），每个时间点含 primary/secondary 两个窗口（缺失为 None）。"""
    rows = conn.execute(
        "SELECT ts, win, used_percent, window_minutes, resets_at FROM quota_samples "
        "WHERE account_pk = ? AND source = ? AND ts >= ? ORDER BY ts DESC, id",
        (account_pk, source, since),
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        entry = out.setdefault(r["ts"], {"ts": r["ts"], "primary": None, "secondary": None})
        if r["win"] in ("primary", "secondary"):
            entry[r["win"]] = {
                "used_percent": r["used_percent"], "window_minutes": r["window_minutes"], "resets_at": r["resets_at"],
            }
    return list(out.values())


USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def insert_usage(
    conn: sqlite3.Connection, username: str, account_pk: int | None, ts: int, delta: dict[str, int], session_file: str
) -> None:
    conn.execute(
        "INSERT INTO usage_events(username, account_pk, ts, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, session_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, account_pk, ts, *(delta[f] for f in USAGE_FIELDS), session_file),
    )


def get_offset(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM log_offsets WHERE path = ?", (path,)).fetchone()


def set_offset(conn: sqlite3.Connection, path: str, inode: int, offset: int, last_totals: str | None) -> None:
    conn.execute(
        """
        INSERT INTO log_offsets(path, inode, offset, last_totals) VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, offset = excluded.offset,
            last_totals = excluded.last_totals
        """,
        (path, inode, offset, last_totals),
    )


def usage_summary(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT u.username, a.label, SUM(u.total_tokens) AS total, SUM(u.input_tokens) AS input,
               SUM(u.output_tokens) AS output, COUNT(*) AS turns
        FROM usage_events u LEFT JOIN accounts a ON a.id = u.account_pk
        WHERE u.ts >= ?
        GROUP BY u.username, a.label
        ORDER BY total DESC
        """,
        (since,),
    ).fetchall()


def usage_by_user(conn: sqlite3.Connection, since: int) -> dict[str, int]:
    return {
        r[0]: int(r[1])
        for r in conn.execute(
            "SELECT username, SUM(total_tokens) FROM usage_events WHERE ts >= ? GROUP BY username", (since,)
        )
    }


def recent_audit(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()


def recent_alerts(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
