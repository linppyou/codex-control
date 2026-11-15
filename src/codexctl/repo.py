from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import pricing


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
    display_name: str | None = None

    @property
    def name(self) -> str:
        """页面上展示的名称：设置了备注名用备注名，否则用标签。"""
        return self.display_name or self.label


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


def set_display_name(conn: sqlite3.Connection, pk: int, name: str | None) -> None:
    conn.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (name, pk))


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
    """优先使用最近一次新鲜的 API 响应；没有时按窗口回退到最新样本。"""
    return conn.execute(
        """
        WITH fresh_api AS (
            SELECT MAX(ts) AS ts FROM quota_samples
            WHERE account_pk = ? AND source = 'api' AND ts >= ?
        )
        SELECT * FROM (
            SELECT q.*, ROW_NUMBER() OVER (
                PARTITION BY q.win
                ORDER BY q.ts DESC, q.id DESC
            ) AS rn
            FROM quota_samples q CROSS JOIN fresh_api f
            WHERE q.account_pk = ? AND
                (f.ts IS NULL OR (q.source = 'api' AND q.ts = f.ts))
        ) WHERE rn = 1 ORDER BY win
        """,
        (account_pk, api_fresh_since, account_pk),
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
    conn: sqlite3.Connection, username: str, account_pk: int | None, ts: int, delta: dict[str, int], session_file: str,
    model: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO usage_events(username, account_pk, ts, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, session_file, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, account_pk, ts, *(delta[f] for f in USAGE_FIELDS), session_file, model),
    )


def get_offset(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM log_offsets WHERE path = ?", (path,)).fetchone()


def set_offset(
    conn: sqlite3.Connection, path: str, inode: int, offset: int, last_totals: str | None, last_model: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO log_offsets(path, inode, offset, last_totals, last_model) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, offset = excluded.offset,
            last_totals = excluded.last_totals, last_model = excluded.last_model
        """,
        (path, inode, offset, last_totals, last_model),
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


def usage_summary_by_model(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    """按用户、账号、模型汇总 since 之后的用量；命令行的费用估算需要模型明细。"""
    return conn.execute(
        """
        SELECT u.username, a.label, u.model,
               SUM(u.total_tokens) AS total, SUM(u.input_tokens) AS input,
               SUM(u.cached_input_tokens) AS cached, SUM(u.output_tokens) AS output, COUNT(*) AS turns
        FROM usage_events u LEFT JOIN accounts a ON a.id = u.account_pk
        WHERE u.ts >= ?
        GROUP BY u.username, a.label, u.model
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


def usage_files_missing_model(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """有模型为空的用量记录的 (用户, 会话文件)。"""
    return [
        (r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT username, session_file FROM usage_events WHERE model IS NULL ORDER BY username, session_file"
        )
    ]


def fill_usage_models(conn: sqlite3.Connection, username: str, session_file: str, pairs: list[tuple[str, int]]) -> int:
    """按 (模型, 时间戳) 给该会话文件中模型为空的用量补上模型名，返回更新条数。"""
    before = conn.total_changes
    conn.executemany(
        "UPDATE usage_events SET model = ? WHERE username = ? AND session_file = ? AND ts = ? AND model IS NULL",
        [(m, username, session_file, ts) for m, ts in pairs],
    )
    return conn.total_changes - before


def set_offset_model(conn: sqlite3.Connection, path: str, model: str) -> None:
    conn.execute("UPDATE log_offsets SET last_model = ? WHERE path = ?", (model, path))


def usage_by_model(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    """按模型汇总 since 之后的用量，未知模型归为 NULL，按总量降序。"""
    return conn.execute(
        """
        SELECT model, SUM(total_tokens) AS total, SUM(input_tokens) AS input,
               SUM(cached_input_tokens) AS cached, SUM(output_tokens) AS output, COUNT(*) AS turns
        FROM usage_events WHERE ts >= ?
        GROUP BY model ORDER BY total DESC
        """,
        (since,),
    ).fetchall()


def usage_by_user_model_windows(conn: sqlite3.Connection, today: int, week: int) -> list[sqlite3.Row]:
    """按用户+模型汇总：今天、近 7 天与全部的 tokens 及输入/缓存/输出明细，以及次数；未知模型为 NULL。"""
    return conn.execute(
        """
        SELECT username, model,
               SUM(CASE WHEN ts >= :today THEN total_tokens ELSE 0 END) AS today,
               SUM(CASE WHEN ts >= :week THEN total_tokens ELSE 0 END) AS week,
               SUM(total_tokens) AS total, COUNT(*) AS turns,
               SUM(CASE WHEN ts >= :today THEN input_tokens ELSE 0 END) AS today_input,
               SUM(CASE WHEN ts >= :today THEN cached_input_tokens ELSE 0 END) AS today_cached,
               SUM(CASE WHEN ts >= :today THEN output_tokens ELSE 0 END) AS today_output,
               SUM(CASE WHEN ts >= :week THEN input_tokens ELSE 0 END) AS week_input,
               SUM(CASE WHEN ts >= :week THEN cached_input_tokens ELSE 0 END) AS week_cached,
               SUM(CASE WHEN ts >= :week THEN output_tokens ELSE 0 END) AS week_output,
               SUM(input_tokens) AS total_input,
               SUM(cached_input_tokens) AS total_cached,
               SUM(output_tokens) AS total_output
        FROM usage_events
        GROUP BY username, model
        ORDER BY username, week DESC, total DESC
        """,
        {"today": today, "week": week},
    ).fetchall()


def usage_user_accounts(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """每个用户产生过用量的账号标签（按标签排序，不含未关联账号的用量）。"""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT DISTINCT u.username, a.label FROM usage_events u JOIN accounts a ON a.id = u.account_pk "
        "ORDER BY u.username, a.label"
    ):
        out.setdefault(r[0], []).append(r[1])
    return out


def user_recent_sessions(conn: sqlite3.Connection, username: str, limit: int = 50, offset: int = 0, model: str = "", account: str = "") -> list[sqlite3.Row]:
    """查询用户的最近会话（按最后活跃时间倒序），包含 token 分项和所用账号与模型列表。"""
    return conn.execute(
        """
        SELECT
            u.session_file,
            MIN(u.ts) AS started_at,
            MAX(u.ts) AS last_active_at,
            COUNT(*) AS turns,
            SUM(u.total_tokens) AS total_tokens,
            SUM(u.input_tokens) AS input_tokens,
            SUM(u.cached_input_tokens) AS cached_input_tokens,
            SUM(u.output_tokens) AS output_tokens,
            SUM(u.reasoning_output_tokens) AS reasoning_output_tokens,
            GROUP_CONCAT(DISTINCT u.model) AS models,
            GROUP_CONCAT(DISTINCT a.label) AS accounts
        FROM usage_events u
        LEFT JOIN accounts a ON a.id = u.account_pk
        WHERE u.username = ?
          AND (? = '' OR EXISTS (SELECT 1 FROM usage_events m WHERE m.username = u.username AND m.session_file = u.session_file AND m.model = ?))
          AND (? = '' OR EXISTS (SELECT 1 FROM usage_events e JOIN accounts a2 ON a2.id = e.account_pk WHERE e.username = u.username AND e.session_file = u.session_file AND a2.label = ?))
        GROUP BY u.session_file
        ORDER BY last_active_at DESC, u.session_file DESC
        LIMIT ? OFFSET ?
        """,
        (username, model, model, account, account, limit, offset),
    ).fetchall()


def user_session_count(conn: sqlite3.Connection, username: str, model: str = "", account: str = "") -> int:
    """查询指定用户的会话总数。"""
    row = conn.execute(
        """SELECT COUNT(DISTINCT u.session_file) FROM usage_events u WHERE u.username = ?
        AND (? = '' OR EXISTS (SELECT 1 FROM usage_events m WHERE m.username = u.username AND m.session_file = u.session_file AND m.model = ?))
        AND (? = '' OR EXISTS (SELECT 1 FROM usage_events e JOIN accounts a ON a.id = e.account_pk WHERE e.username = u.username AND e.session_file = u.session_file AND a.label = ?))""",
        (username, model, model, account, account),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def user_daily_usage(conn: sqlite3.Connection, username: str, since: int, until: int) -> list[sqlite3.Row]:
    """Aggregate by server-local calendar day, within a half-open time range."""
    return conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS turns,
               SUM(total_tokens) AS total, SUM(input_tokens) AS input,
               SUM(cached_input_tokens) AS cached, SUM(output_tokens) AS output
        FROM usage_events WHERE username = ? AND ts >= ? AND ts < ?
        GROUP BY day ORDER BY day
    """, (username, since, until)).fetchall()


def usage_daily_by_user_model(conn: sqlite3.Connection, since: int, until: int) -> list[sqlite3.Row]:
    """Summarize a bounded range by local calendar day, user and model."""
    return conn.execute("""
        SELECT date(ts, 'unixepoch', 'localtime') AS day, username, model,
               SUM(total_tokens) AS total, SUM(input_tokens) AS input,
               SUM(cached_input_tokens) AS cached, SUM(output_tokens) AS output,
               COUNT(*) AS turns
        FROM usage_events WHERE ts >= ? AND ts < ?
        GROUP BY day, username, model ORDER BY day, username
    """, (since, until)).fetchall()


def user_session_filters(conn: sqlite3.Connection, username: str) -> tuple[list[str], list[str]]:
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM usage_events WHERE username = ? AND model IS NOT NULL AND model != '' ORDER BY model", (username,))]
    accounts = [r[0] for r in conn.execute("SELECT DISTINCT a.label FROM usage_events u JOIN accounts a ON a.id = u.account_pk WHERE u.username = ? ORDER BY a.label", (username,))]
    return models, accounts


def user_sessions_model_breakdown(
    conn: sqlite3.Connection, username: str, session_files: list[str]
) -> list[sqlite3.Row]:
    """按会话和模型统计 token 分项，用于精确估算各会话的费用。"""
    if not session_files:
        return []
    placeholders = ",".join("?" * len(session_files))
    return conn.execute(
        f"""
        SELECT
            session_file,
            model,
            SUM(input_tokens) AS input,
            SUM(cached_input_tokens) AS cached,
            SUM(output_tokens) AS output,
            SUM(total_tokens) AS total
        FROM usage_events
        WHERE username = ? AND session_file IN ({placeholders})
        GROUP BY session_file, model
        """,
        (username, *session_files),
    ).fetchall()


def usage_by_model_for_user(
    conn: sqlite3.Connection, username: str, today: int, week: int
) -> list[sqlite3.Row]:
    """查询指定用户按模型汇总的用量（今天、近 7 天与全部）。"""
    return conn.execute(
        """
        SELECT model,
               SUM(CASE WHEN ts >= :today THEN total_tokens ELSE 0 END) AS today,
               SUM(CASE WHEN ts >= :week THEN total_tokens ELSE 0 END) AS week,
               SUM(total_tokens) AS total, COUNT(*) AS turns,
               SUM(CASE WHEN ts >= :today THEN input_tokens ELSE 0 END) AS today_input,
               SUM(CASE WHEN ts >= :today THEN cached_input_tokens ELSE 0 END) AS today_cached,
               SUM(CASE WHEN ts >= :today THEN output_tokens ELSE 0 END) AS today_output,
               SUM(CASE WHEN ts >= :week THEN input_tokens ELSE 0 END) AS week_input,
               SUM(CASE WHEN ts >= :week THEN cached_input_tokens ELSE 0 END) AS week_cached,
               SUM(CASE WHEN ts >= :week THEN output_tokens ELSE 0 END) AS week_output,
               SUM(input_tokens) AS total_input,
               SUM(cached_input_tokens) AS total_cached,
               SUM(output_tokens) AS total_output
        FROM usage_events
        WHERE username = :username
        GROUP BY model
        ORDER BY week DESC, total DESC
        """,
        {"username": username, "today": today, "week": week},
    ).fetchall()


def _price(row: sqlite3.Row) -> pricing.Price:
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except (TypeError, ValueError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    return pricing.Price(
        name=row["name"], input_usd=row["input_usd"], cached_usd=row["cached_usd"],
        output_usd=row["output_usd"], aliases=tuple(str(a) for a in aliases), id=row["id"],
    )


def list_prices(conn: sqlite3.Connection) -> list[pricing.Price]:
    return [
        _price(r) for r in conn.execute(
            "SELECT id, name, input_usd, cached_usd, output_usd, aliases FROM model_prices ORDER BY id"
        )
    ]


def get_price(conn: sqlite3.Connection, price_id: int) -> pricing.Price | None:
    row = conn.execute(
        "SELECT id, name, input_usd, cached_usd, output_usd, aliases FROM model_prices WHERE id = ?", (price_id,)
    ).fetchone()
    return _price(row) if row is not None else None


def _aliases_json(aliases: tuple[str, ...]) -> str:
    return json.dumps(list(aliases), ensure_ascii=False)


def insert_price(conn: sqlite3.Connection, price: pricing.Price) -> int:
    cur = conn.execute(
        "INSERT INTO model_prices(name, input_usd, cached_usd, output_usd, aliases) VALUES (?, ?, ?, ?, ?)",
        (price.name, price.input_usd, price.cached_usd, price.output_usd, _aliases_json(price.aliases)),
    )
    return int(cur.lastrowid)


def update_price(conn: sqlite3.Connection, price_id: int, price: pricing.Price) -> bool:
    before = conn.total_changes
    conn.execute(
        "UPDATE model_prices SET name = ?, input_usd = ?, cached_usd = ?, output_usd = ?, aliases = ? WHERE id = ?",
        (price.name, price.input_usd, price.cached_usd, price.output_usd, _aliases_json(price.aliases), price_id),
    )
    return conn.total_changes > before


def delete_price(conn: sqlite3.Connection, price_id: int) -> bool:
    before = conn.total_changes
    conn.execute("DELETE FROM model_prices WHERE id = ?", (price_id,))
    return conn.total_changes > before


def recent_audit(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()


def recent_alerts(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
