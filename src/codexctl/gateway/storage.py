from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import db, events, sysusers
from ..context import Context

TOKEN = re.compile(r"cctl_([0-9a-f]{24})\.[A-Za-z0-9_-]{43}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
USAGE_RETENTION_DAYS = 90


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Key:
    id: int
    public_id: str
    account_pk: int
    allow_fast: bool
    models: dict[str, bool]  # ID -> verified Fast support


def authorize(conn: sqlite3.Connection, authorization: str, now: int) -> Key:
    scheme, _, token = authorization.partition(" ")
    match = TOKEN.fullmatch(token) if scheme.lower() == "bearer" else None
    row = None
    if match:
        row = conn.execute(
            """SELECT k.*, a.status AS account_status, s.version AS credential_version FROM api_keys k
               JOIN accounts a ON a.id = k.account_pk
               LEFT JOIN account_secrets s ON s.account_pk = a.id WHERE k.public_id = ?""",
            (match[1],),
        ).fetchone()
    if (row is None or not hmac.compare_digest(row["secret_hash"], _digest(token))
            or row["revoked_at"] is not None or row["deleted_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now)):
        raise GatewayError(401, "invalid_api_key", "API Key is missing, invalid, expired or revoked.")
    if row["account_status"] != "active" or row["credential_version"] is None:
        raise unavailable()
    models = conn.execute(
        """SELECT m.model_id, m.fast_verified_at FROM api_key_models km
           JOIN account_models m ON (m.account_pk, m.model_id) = (km.account_pk, km.model_id)
           WHERE km.key_pk = ? AND m.upstream_available = 1 AND m.enabled = 1 AND m.selectable = 1
           ORDER BY m.model_id""",
        (row["id"],),
    )
    return Key(row["id"], row["public_id"], row["account_pk"], bool(row["allow_fast"]),
               {m["model_id"]: m["fast_verified_at"] is not None for m in models})


def unavailable() -> GatewayError:
    return GatewayError(503, "account_unavailable", "The bound ChatGPT account is unavailable.")


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _secret(public_id: str) -> str:
    return f"cctl_{public_id}.{secrets.token_urlsafe(32)}"


def get_key(conn: sqlite3.Connection, public_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM api_keys WHERE public_id = ? AND deleted_at IS NULL", (public_id,),
    ).fetchone()
    if row is None:
        raise GatewayError(404, "key_not_found", "API Key 不存在")
    return row


def key_counts_by_user(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute(
        """SELECT COALESCE(username, '') AS owner, COUNT(*) AS total, SUM(revoked_at IS NULL) AS unrevoked
           FROM api_keys WHERE deleted_at IS NULL GROUP BY owner"""
    )
    return {row["owner"]: (row["total"], row["unrevoked"]) for row in rows}


def list_keys(conn: sqlite3.Connection, username: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT k.id, k.public_id, k.secret_version, k.name, k.username, k.account_pk, k.default_model_id,
                  k.allow_fast, k.expires_at, k.revoked_at, k.last_used_at, a.label, a.display_name,
                  a.status AS account_status FROM api_keys k JOIN accounts a ON a.id = k.account_pk
           WHERE k.deleted_at IS NULL AND (? IS NULL OR COALESCE(k.username, '') = ?)
           ORDER BY k.id DESC""", (username, username),
    )
    selected = {}
    for row in conn.execute(
        """SELECT km.key_pk, km.model_id, m.enabled, m.upstream_available, m.selectable
           FROM api_key_models km JOIN account_models m
           ON (m.account_pk, m.model_id) = (km.account_pk, km.model_id)
           JOIN api_keys k ON k.id = km.key_pk
           WHERE k.deleted_at IS NULL AND (? IS NULL OR COALESCE(k.username, '') = ?)
           ORDER BY km.model_id""", (username, username),
    ):
        selected.setdefault(row["key_pk"], []).append(dict(row))
    return [dict(row, models=selected.get(row["id"], [])) for row in rows]


def save_key(
    conn: sqlite3.Connection, *, actor: str, now: int, name: str, account_pk: int, model_ids: list[str],
    default_model: str, allow_fast: bool, username: str, public_id: str | None = None,
) -> tuple[str, str | None]:
    name = " ".join(name.split())
    selected = set(model_ids)
    if not name or len(name) > 80:
        raise GatewayError(400, "invalid_name", "名称长度须为 1–80 字符")
    if default_model not in selected or any(not MODEL_ID.fullmatch(m) for m in selected):
        raise GatewayError(400, "invalid_models", "请勾选可用模型，并从中选择默认模型")
    with db.transaction(conn):
        current = get_key(conn, public_id) if public_id else None
        if current and current["username"]:
            if username and username != current["username"]:
                raise GatewayError(409, "key_user_locked", "该 Key 已绑定用户")
            username = current["username"]
        if not sysusers.valid_name(username):
            raise GatewayError(400, "invalid_key_user", "请选择或输入有效的用户名")
        account_pk = current["account_pk"] if current else account_pk
        account = conn.execute("SELECT status FROM accounts WHERE id = ?", (account_pk,)).fetchone()
        if account is None or (current is None and account["status"] != "active"):
            raise GatewayError(400, "invalid_account", "请选择一个可用的账号")
        allowed = {row["model_id"] for row in enabled_models(conn, account_pk)}
        if current:
            allowed.update(row["model_id"] for row in conn.execute(
                "SELECT model_id FROM api_key_models WHERE key_pk = ?", (current["id"],),
            ))
        if not selected <= allowed:
            raise GatewayError(400, "invalid_models", "所选模型在绑定账号上不可用")
        token = None
        if current is None:
            public_id = secrets.token_hex(12)
            token = _secret(public_id)
            pk = conn.execute(
                """INSERT INTO api_keys (public_id, secret_hash, name, username, account_pk, default_model_id,
                                         allow_fast, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (public_id, _digest(token), name, username, account_pk, default_model, allow_fast, actor, now),
            ).lastrowid
        else:
            if current["revoked_at"] is not None:
                raise GatewayError(409, "key_revoked", "该 Key 已撤销")
            pk = current["id"]
            conn.execute(
                """UPDATE api_keys SET name = ?, username = ?, default_model_id = ?, allow_fast = ?
                   WHERE id = ?""", (name, username, default_model, allow_fast, pk),
            )
            conn.execute("DELETE FROM api_key_models WHERE key_pk = ?", (pk,))
        conn.executemany("INSERT INTO api_key_models VALUES (?, ?, ?)",
                         [(pk, account_pk, model) for model in sorted(selected)])
        events.audit(conn, actor, "api_key.create" if token else "api_key.update", public_id,
                     f"user={username} account={account_pk} models={','.join(sorted(selected))} default={default_model} "
                     f"fast={bool(allow_fast)}", now=now)
    return public_id, token


def rotate_key(conn: sqlite3.Connection, public_id: str, version: int, actor: str, now: int) -> str:
    token = _secret(public_id)
    with db.transaction(conn):
        updated = conn.execute(
            """UPDATE api_keys SET secret_hash = ?, secret_version = secret_version + 1
               WHERE public_id = ? AND secret_version = ? AND revoked_at IS NULL
                     AND deleted_at IS NULL
                     AND (expires_at IS NULL OR expires_at > ?)""", (_digest(token), public_id, version, now),
        )
        if not updated.rowcount:
            raise GatewayError(409, "stale_key", "Key 已变更、到期或撤销，请刷新页面")
        events.audit(conn, actor, "api_key.rotate", public_id, now=now)
    return token


def revoke_key(conn: sqlite3.Connection, public_id: str, actor: str, now: int) -> str:
    with db.transaction(conn):
        key = get_key(conn, public_id)
        changed = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE public_id = ? AND revoked_at IS NULL",
                               (now, public_id))
        if changed.rowcount:
            events.audit(conn, actor, "api_key.revoke", public_id, now=now)
        return key["username"] or ""


def delete_key(conn: sqlite3.Connection, public_id: str, actor: str, now: int) -> str:
    with db.transaction(conn):
        key = get_key(conn, public_id)
        # Keep the row for usage attribution, including responses already in flight.
        conn.execute("UPDATE api_keys SET deleted_at = ?, revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                     (now, now, key["id"]))
        owner = key["username"] or ""
        events.audit(conn, actor, "api_key.delete", public_id, f"user={owner or '(unassigned)'}", now=now)
        return owner


def account_models(conn: sqlite3.Connection, account_pk: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM account_models WHERE account_pk = ? ORDER BY model_id",
                        (account_pk,)).fetchall()


def enabled_models(conn: sqlite3.Connection, account_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM account_models WHERE account_pk = ? AND upstream_available = 1
           AND enabled = 1 AND selectable = 1 ORDER BY model_id""", (account_pk,),
    ).fetchall()


def get_catalog(conn: sqlite3.Connection, account_pk: int, version: str) -> tuple[dict, int] | None:
    row = conn.execute("SELECT catalog, synced_at FROM account_model_catalogs WHERE account_pk = ? AND client_version = ?",
                       (account_pk, version)).fetchone()
    return (json.loads(row["catalog"]), row["synced_at"]) if row else None


def store_catalog(conn: sqlite3.Connection, account_pk: int, version: str, canonical: bool, catalog: dict, now: int):
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO account_model_catalogs VALUES (?, ?, ?, ?)
               ON CONFLICT(account_pk, client_version) DO UPDATE SET
               catalog = excluded.catalog, synced_at = excluded.synced_at""",
            (account_pk, version, json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), now),
        )
        if canonical:
            conn.execute("UPDATE account_models SET upstream_available = 0 WHERE account_pk = ?", (account_pk,))
            conn.executemany(
                """INSERT INTO account_models(account_pk, model_id, discovered_at, enabled, selectable)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(account_pk, model_id) DO UPDATE SET upstream_available = 1,
                       discovered_at = excluded.discovered_at, selectable = excluded.selectable""",
                [(account_pk, m["slug"], now,
                  m.get("visibility") not in ("hide", "hidden") and m.get("supported_in_api") is not False)
                 for m in catalog["models"]],
            )


def verify_model(conn: sqlite3.Connection, pk: int, model: str, fast: bool, ok: bool, actor: str, now: int):
    with db.transaction(conn):
        if fast:
            conn.execute("UPDATE account_models SET fast_verified_at = ? WHERE account_pk = ? AND model_id = ?",
                         (now if ok else None, pk, model))
        else:
            conn.execute(
                """UPDATE account_models SET verified_at = ?,
                   fast_verified_at = CASE WHEN ? THEN fast_verified_at ELSE NULL END
                   WHERE account_pk = ? AND model_id = ?""", (now if ok else None, ok, pk, model),
            )
        events.audit(conn, actor, "api_model.test", f"{pk}/{model}", f"fast={fast} passed={ok}", now=now)


def enable_model(conn: sqlite3.Connection, pk: int, model: str, enabled: bool, actor: str, now: int):
    with db.transaction(conn):
        changed = conn.execute(
            """UPDATE account_models SET enabled = ? WHERE account_pk = ? AND model_id = ?
               AND (NOT ? OR (upstream_available = 1 AND selectable = 1))""",
            (enabled, pk, model, enabled),
        )
        if not changed.rowcount:
            raise GatewayError(400, "invalid_model", "该模型当前不在账号可用目录")
        events.audit(conn, actor, "api_model.enable" if enabled else "api_model.disable", f"{pk}/{model}", now=now)


def _token_count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 2**60 else None


@dataclass
class Usage:
    request_id: str
    key: Key
    model: str
    ts: int
    requested_tier: str | None = None
    status: str = "incomplete"
    elapsed_ms: int = 0
    first_output_ms: int | None = None
    result: dict = field(default_factory=dict)

    def save(self, conn: sqlite3.Connection):
        usage = self.result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_details = usage.get("input_tokens_details")
        output_details = usage.get("output_tokens_details")
        input_tokens = _token_count(usage.get("input_tokens"))
        output_tokens = _token_count(usage.get("output_tokens"))
        total = _token_count(usage.get("total_tokens"))
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        tier = self.result.get("service_tier")
        tier = tier if isinstance(tier, str) and re.fullmatch(r"[a-z_]{1,30}", tier) else None
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO api_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO NOTHING""",
                (self.request_id, self.key.id, self.key.account_pk, self.model, self.ts, self.status, self.elapsed_ms,
                 self.first_output_ms, input_tokens, output_tokens,
                 _token_count(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None,
                 _token_count(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None,
                 total, self.requested_tier, tier),
            )
            conn.execute("UPDATE api_keys SET last_used_at = MAX(COALESCE(last_used_at, 0), ?) WHERE id = ?",
                         (self.ts, self.key.id))


def usage_summary(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT k.public_id, k.name, k.username, k.deleted_at, a.label, u.model_id, count(*) AS requests,
                  SUM(u.status = 'completed') AS completed, SUM(u.input_tokens) AS input_tokens,
                  SUM(u.cached_input_tokens) AS cached_input_tokens, SUM(u.output_tokens) AS output_tokens,
                  SUM(u.reasoning_output_tokens) AS reasoning_output_tokens, SUM(u.total_tokens) AS total_tokens
           FROM api_usage_events u JOIN api_keys k ON k.id = u.key_pk JOIN accounts a ON a.id = u.account_pk
           WHERE u.ts >= ? GROUP BY u.key_pk, u.account_pk, u.model_id ORDER BY requests DESC""", (since,),
    ).fetchall()


def prune_usage(ctx: Context):
    now = ctx.now()
    ctx.conn.execute("DELETE FROM api_usage_events WHERE ts < ?", (now - USAGE_RETENTION_DAYS * 86400,))
    ctx.conn.execute("DELETE FROM account_model_catalogs WHERE client_version != ? AND synced_at < ?",
                     (ctx.settings.gateway_client_version, now - 7 * 86400))
