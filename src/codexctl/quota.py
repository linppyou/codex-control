from __future__ import annotations

import logging
import math
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


def _pct(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and 0 <= f <= 100 else None


def _nonneg_int(v: object) -> int | None:
    # 上限与 logscan.MAX_COUNTER 一致，避免超大值写入 SQLite 时 OverflowError 卡住扫描
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2**53 else None


def parse_usage(body: dict, now: int) -> list[QuotaWindow]:
    """解析 ChatGPT 后端 /wham/usage 响应（非官方接口，格式见 docs/server-validation.md）。"""
    rate_limit = body.get("rate_limit") if isinstance(body, dict) else None
    if not isinstance(rate_limit, dict):
        return []
    out = []
    for win, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = rate_limit.get(key)
        if not isinstance(w, dict):
            continue
        pct = _pct(w.get("used_percent"))
        if pct is None:
            continue
        seconds = _nonneg_int(w.get("limit_window_seconds"))
        resets = _nonneg_int(w.get("reset_at"))
        after = _nonneg_int(w.get("reset_after_seconds"))
        if resets is None and after is not None:
            resets = now + after
        out.append(QuotaWindow(win, pct, seconds // 60 if seconds else None, resets))
    return out


def windows_from_rate_limits(rate_limits: dict) -> list[QuotaWindow]:
    """解析会话日志 token_count 事件里的 rate_limits（降级来源）。"""
    out = []
    for win in ("primary", "secondary"):
        w = rate_limits.get(win) if isinstance(rate_limits, dict) else None
        if not isinstance(w, dict):
            continue
        pct = _pct(w.get("used_percent"))
        if pct is None:
            continue
        out.append(QuotaWindow(win, pct, _nonneg_int(w.get("window_minutes")), _nonneg_int(w.get("resets_at"))))
    return out


def record(ctx: Context, account_pk: int, ts: int, windows: list[QuotaWindow], source: str) -> None:
    for w in windows:
        repo.insert_quota_sample(ctx.conn, account_pk, ts, w.win, w.used_percent, w.window_minutes, w.resets_at, source)


def evaluate_thresholds(ctx: Context, acc: repo.Account) -> None:
    now = ctx.now()
    rows = repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
    if not rows:
        return
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


def poll_account(ctx: Context, acc: repo.Account) -> str | None:
    """拉取并记录单个账号的额度；失败返回错误说明，成功返回 None。"""
    try:
        windows = fetch_usage(ctx, acc)
        record(ctx, acc.id, ctx.now(), windows, "api")
        events.resolve_alert(ctx.conn, "auth_rejected", acc.label, now=ctx.now())
        evaluate_thresholds(ctx, acc)
        return None
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            events.raise_alert(
                ctx.conn, "critical", "auth_rejected", acc.label,
                f"额度接口拒绝该账号凭证（HTTP {code}），凭证可能已失效，请执行 account refresh 或 relogin",
                now=ctx.now(),
            )
        log.warning("额度接口请求失败 %s：%s", acc.label, e)
        return f"额度接口返回 HTTP {code}"
    except (httpx.HTTPError, ValueError) as e:
        log.warning("额度接口请求失败 %s：%s", acc.label, e)
        return f"额度接口请求失败：{type(e).__name__}"
    except Exception as e:  # 单个账号出错不能影响其他账号
        log.warning("额度轮询异常 %s：%r", acc.label, e)
        return f"额度轮询异常：{e}"


def poll_quotas(ctx: Context) -> None:
    for acc in repo.list_accounts(ctx.conn):
        if acc.status == "active":
            poll_account(ctx, acc)
    repo.prune_quota_samples(ctx.conn, ctx.now() - 90 * 86400)
