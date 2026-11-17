from __future__ import annotations

import sqlite3

_FIELDS = "id, kind, name, version, label, sha256"


def list_skills(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT {_FIELDS} FROM dispatch_skills ORDER BY kind, label, name").fetchall()


def get(conn: sqlite3.Connection, skill_id: str, *, with_archive: bool = False) -> sqlite3.Row | None:
    fields = "*" if with_archive else _FIELDS
    return conn.execute(f"SELECT {fields} FROM dispatch_skills WHERE id = ?", (skill_id,)).fetchone()

