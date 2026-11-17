from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import codexctl.db as db
import codexctl.events as events
import codexctl.users.sysusers as sysusers
from codexctl.context import Context
from codexctl.vault import Vault

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


def authorize(conn: sqlite3.Connection, authorization: str) -> Key:
    scheme, _, token = authorization.partition(" ")
    match = TOKEN.fullmatch(token) if scheme.lower() == "bearer" else None
    row = None
    if match:
        row = conn.execute(
            """SELECT k.id, k.public_id, k.secret_hash, k.allow_fast, x.account_pk,
                      a.status AS account_status, s.version AS credential_version
               FROM api_keys k LEFT JOIN assignments x ON x.username = k.username AND x.ended_at IS NULL
               LEFT JOIN accounts a ON a.id = x.account_pk
               LEFT JOIN account_secrets s ON s.account_pk = a.id
               WHERE k.public_id = ? AND k.revoked_at IS NULL""", (match[1],),
        ).fetchone()
    if row is None or not hmac.compare_digest(row["secret_hash"], _digest(token)):
        raise GatewayError(401, "invalid_api_key", "API Key is missing, invalid or revoked.")
    if row["account_pk"] is None:
        raise GatewayError(403, "account_unassigned", "No Codex account is currently assigned to this user.")
    if row["account_status"] != "active" or row["credential_version"] is None:
        raise unavailable()
    models = enabled_models(conn, row["account_pk"])
    return Key(row["id"], row["public_id"], row["account_pk"], bool(row["allow_fast"]),
               {m["model_id"]: m["fast_verified_at"] is not None for m in models})


def unavailable() -> GatewayError:
    return GatewayError(503, "account_unavailable", "The bound ChatGPT account is unavailable.")


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _secret(public_id: str) -> str:
    return f"cctl_{public_id}.{secrets.token_urlsafe(32)}"


def user_key_names(conn: sqlite3.Connection) -> set[str]:
    return {row["username"] for row in conn.execute("SELECT username FROM api_keys WHERE revoked_at IS NULL")}


def user_key(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM api_keys WHERE username = ? AND revoked_at IS NULL", (username,),
    ).fetchone()


def _key_aad(public_id: str, username: str) -> bytes:
    return f"codexctl-api-key:{public_id}:{username}".encode()


def ensure_user_key(conn: sqlite3.Connection, vault: Vault, username: str, actor: str, now: int) -> None:
    """Issue once inside the caller's write transaction."""
    if not sysusers.valid_name(username) or user_key(conn, username) is not None:
        return
    public_id = secrets.token_hex(12)
    token = _secret(public_id)
    conn.execute(
        """INSERT INTO api_keys(public_id, secret_hash, secret_ciphertext, name, username, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (public_id, _digest(token), vault.encrypt(token.encode("ascii"), _key_aad(public_id, username)),
         username, username, actor, now),
    )
    events.audit(conn, actor, "api_key.create", username, public_id, now=now)


def reveal_user_key(vault: Vault, key: sqlite3.Row) -> str | None:
    blob = key["secret_ciphertext"]
    if blob is None:
        return None
    return vault.decrypt(blob, _key_aad(key["public_id"], key["username"])).decode("ascii")


def rotate_user_key(conn: sqlite3.Connection, vault: Vault, username: str, version: int, actor: str, now: int):
    with db.transaction(conn):
        key = user_key(conn, username)
        if key is None or key["secret_version"] != version:
            raise GatewayError(409, "stale_key", "API Key 已变更，请刷新页面")
        public_id = key["public_id"]
        token = _secret(public_id)
        conn.execute(
            """UPDATE api_keys SET secret_hash = ?, secret_ciphertext = ?, secret_version = secret_version + 1
               WHERE id = ?""",
            (_digest(token), vault.encrypt(token.encode("ascii"), _key_aad(public_id, username)), key["id"]),
        )
        events.audit(conn, actor, "api_key.rotate", username, public_id, now=now)


def set_user_fast(conn: sqlite3.Connection, username: str, enabled: bool, actor: str, now: int):
    with db.transaction(conn):
        result = conn.execute("UPDATE api_keys SET allow_fast = ? WHERE username = ? AND revoked_at IS NULL",
                              (enabled, username))
        if not result.rowcount:
            raise GatewayError(404, "key_not_found", "API Key 不存在")
        events.audit(conn, actor, "api_key.fast", username, f"enabled={enabled}", now=now)


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
        """SELECT k.public_id, k.name, k.username, k.revoked_at, a.label, u.model_id, count(*) AS requests,
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
