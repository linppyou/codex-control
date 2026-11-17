from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from codexctl.db import transaction

SESSION_TTL = 12 * 3600
MIN_PASSWORD_LEN = 12
MAX_LOGIN_PASSWORD_LEN = 1024
MAX_LOGIN_USERNAME_LEN = 64
MAX_LIMITER_KEYS = 10_000
DEFAULT_ADMIN_PASSWORD = "root@root.root"
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("codexctl-dummy-password")


class AuthError(Exception):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 个字符")


def create_admin(conn: sqlite3.Connection, username: str, password: str, *, now: int, configured: bool = True) -> None:
    if not _NAME_RE.fullmatch(username):
        raise AuthError("管理员用户名只能包含小写字母、数字、_ . -，且以字母或下划线开头，最长 32 位")
    _check_password(password)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM admins WHERE username = ?", (username,)).fetchone():
            raise AuthError(f"管理员已存在：{username}")
        conn.execute(
            "INSERT INTO admins(username, password_hash, created_at, password_configured) VALUES (?, ?, ?, ?)",
            (username, _hasher.hash(password), now, configured),
        )


def set_password(conn: sqlite3.Connection, username: str, password: str, *, now: int) -> None:
    _check_password(password)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE admins SET password_hash = ?, password_configured = 1 WHERE username = ?",
            (_hasher.hash(password), username),
        )
        if cur.rowcount == 0:
            raise AuthError(f"管理员不存在：{username}")
        conn.execute("DELETE FROM web_sessions WHERE username = ?", (username,))


def list_admins(conn: sqlite3.Connection) -> list[str]:
    return [r["username"] for r in conn.execute("SELECT username FROM admins ORDER BY username")]


def check_password(stored_hash: str | None, password: str) -> bool:
    """纯函数：不碰数据库，可在全局锁之外调用。stored_hash 为 None 时用假哈希耗时间。"""
    if len(password) > MAX_LOGIN_PASSWORD_LEN:
        return False
    try:
        _hasher.verify(stored_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return stored_hash is not None


def get_hash(conn: sqlite3.Connection, username: str) -> str | None:
    if len(username) > MAX_LOGIN_USERNAME_LEN:
        return None
    row = conn.execute("SELECT password_hash FROM admins WHERE username = ?", (username,)).fetchone()
    return row["password_hash"] if row else None


def has_configured_admin(conn: sqlite3.Connection) -> bool:
    admin = conn.execute("SELECT password_hash, password_configured FROM admins WHERE username = 'admin'").fetchone()
    return admin is not None and (
        bool(admin["password_configured"]) or not check_password(admin["password_hash"], DEFAULT_ADMIN_PASSWORD)
    )


def rehash_if_needed(conn: sqlite3.Connection, username: str, stored: str, password: str) -> None:
    if _hasher.check_needs_rehash(stored):
        with transaction(conn):
            conn.execute(
                "UPDATE admins SET password_hash = ? WHERE username = ? AND password_hash = ?",
                (_hasher.hash(password), username, stored),
            )


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    stored = get_hash(conn, username)
    if not check_password(stored, password):
        return False
    rehash_if_needed(conn, username, stored, password)
    return True


def enroll_system_user(conn: sqlite3.Connection, username: str, uid: int) -> None:
    conn.execute(
        "INSERT INTO web_system_users(username, uid) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET uid = excluded.uid",
        (username, uid),
    )
    conn.execute("DELETE FROM web_sessions WHERE username = ?", (username,))


def enrolled_system_uid(conn: sqlite3.Connection, username: str) -> int | None:
    row = conn.execute("SELECT uid FROM web_system_users WHERE username = ?", (username,)).fetchone()
    return row["uid"] if row else None


@dataclass(frozen=True)
class WebSession:
    username: str
    csrf: str
    system_uid: int | None


def create_session(
    conn: sqlite3.Connection, username: str, *, now: int, system_uid: int | None = None
) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with transaction(conn):
        conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO web_sessions(token_hash, username, csrf, created_at, expires_at, system_uid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_hash_token(token), username, csrf, now, now + SESSION_TTL, system_uid),
        )
    return token, csrf


def get_session(conn: sqlite3.Connection, token: str, *, now: int) -> WebSession | None:
    row = conn.execute(
        "SELECT username, csrf, system_uid FROM web_sessions WHERE token_hash = ? AND expires_at >= ?",
        (_hash_token(token), now),
    ).fetchone()
    return WebSession(row["username"], row["csrf"], row["system_uid"]) if row else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash_token(token),))


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window: int = 900) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[int]] = {}

    def _recent(self, ip: str, now: int) -> list[int]:
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        if recent:
            self._failures[ip] = recent
        else:
            self._failures.pop(ip, None)
        return recent

    def allowed(self, ip: str, now: int) -> bool:
        return len(self._recent(ip, now)) < self.max_failures

    def record_failure(self, ip: str, now: int) -> None:
        self._recent(ip, now)
        self._failures.setdefault(ip, []).append(now)
        if len(self._failures) > MAX_LIMITER_KEYS:
            self._prune(now)

    def _prune(self, now: int) -> None:
        for key in [k for k, v in self._failures.items() if all(now - t >= self.window for t in v)]:
            del self._failures[key]
        excess = len(self._failures) - MAX_LIMITER_KEYS
        if excess > 0:
            # 先淘汰最旧的非 user: 键，user: 键最后才动
            ordered = sorted(self._failures, key=lambda k: (k.startswith("user:"), max(self._failures[k])))
            for key in ordered[:excess]:
                del self._failures[key]

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
