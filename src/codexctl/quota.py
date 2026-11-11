from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from . import accounts, events, repo
from .context import Context

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaWindow:
    win: str  # "primary" | "secondary"；时长看 window_minutes，不要假设哪个是 5 小时
    used_percent: float
    window_minutes: int | None
    resets_at: int | None


def parse_usage(body: dict, now: int) -> list[QuotaWindow]:
    """解析 ChatGPT 后端 /wham/usage 响应（非官方接口，格式见 docs/server-validation.md）。"""
    rate_limit = body.get("rate_limit") if isinstance(body, dict) else None
    if not isinstance(rate_limit, dict):
        return []
    out = []
    for win, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = rate_limit.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        seconds = w.get("limit_window_seconds")
        resets = w.get("reset_at")
        if resets is None and w.get("reset_after_seconds") is not None:
            resets = now + int(w["reset_after_seconds"])
        out.append(
            QuotaWindow(
                win,
                float(w["used_percent"]),
                int(seconds) // 60 if seconds else None,
                int(resets) if resets is not None else None,
            )
        )
    return out


def windows_from_rate_limits(rate_limits: dict) -> list[QuotaWindow]:
    """解析会话日志 token_count 事件里的 rate_limits（降级来源）。"""
    out = []
    for win in ("primary", "secondary"):
        w = rate_limits.get(win)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        minutes = w.get("window_minutes")
        resets = w.get("resets_at")
        out.append(
            QuotaWindow(
                win,
                float(w["used_percent"]),
                int(minutes) if minutes is not None else None,
                int(resets) if resets is not None else None,
            )
        )
    return out


def record(ctx: Context, account_pk: int, ts: int, windows: list[QuotaWindow], source: str) -> None:
    for w in windows:
        repo.insert_quota_sample(ctx.conn, account_pk, ts, w.win, w.used_percent, w.window_minutes, w.resets_at, source)


def evaluate_thresholds(ctx: Context, acc: repo.Account) -> None:
    rows = repo.latest_quota(ctx.conn, acc.id)
    if not rows:
        return
    now = ctx.now()
    worst = max(r["used_percent"] for r in rows)
    if worst >= ctx.settings.quota_crit_percent:
        events.raise_alert(ctx.conn, "critical", "quota_high", acc.label, f"额度已用 {worst:.0f}%", now=now)
    elif worst >= ctx.settings.quota_warn_percent:
        events.raise_alert(ctx.conn, "warning", "quota_high", acc.label, f"额度已用 {worst:.0f}%", now=now)
    else:
        events.resolve_alert(ctx.conn, "quota_high", acc.label, now=now)


def fetch_usage(ctx: Context, acc: repo.Account) -> list[QuotaWindow]:
    bundle = accounts.load_bundle(ctx, acc)
    resp = ctx.http.get(
        ctx.settings.usage_url,
        headers={"Authorization": f"Bearer {bundle.access_token}", "ChatGPT-Account-Id": bundle.account_id},
    )
    resp.raise_for_status()
    return parse_usage(resp.json(), ctx.now())


def poll_quotas(ctx: Context) -> None:
    for acc in repo.list_accounts(ctx.conn):
        if acc.status != "active":
            continue
        try:
            windows = fetch_usage(ctx, acc)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("额度接口请求失败 %s：%s", acc.label, e)
            continue
        record(ctx, acc.id, ctx.now(), windows, "api")
        evaluate_thresholds(ctx, acc)
