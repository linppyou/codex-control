from __future__ import annotations

import sqlite3


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str, detail: str = "", *, now: int) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
        (now, actor, action, target, detail),
    )


def raise_alert(conn: sqlite3.Connection, level: str, kind: str, target: str, message: str, *, now: int) -> None:
    conn.execute(
        """
        INSERT INTO alerts(ts, level, kind, target, message) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind, target) WHERE resolved_at IS NULL
        DO UPDATE SET ts = excluded.ts, level = excluded.level, message = excluded.message
        """,
        (now, level, kind, target, message),
    )


def resolve_alert(conn: sqlite3.Connection, kind: str, target: str, *, now: int) -> None:
    conn.execute(
        "UPDATE alerts SET resolved_at = ? WHERE kind = ? AND target = ? AND resolved_at IS NULL",
        (now, kind, target),
    )


def open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY ts DESC, id DESC").fetchall()


def recent_audit(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()


def recent_alerts(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
