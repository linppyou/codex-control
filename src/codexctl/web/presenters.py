from __future__ import annotations

import math
import time
from datetime import datetime

from fastapi import Request

import codexctl.accounts.repository as account_repo
import codexctl.accounts.service as accounts
import codexctl.sites.inventory as sites
import codexctl.usage.quota as quota
import codexctl.usage.repository as usage_repo
import codexctl.users.assignments as assignments
import codexctl.users.repository as user_repo
import codexctl.users.webauth as webauth
from codexctl.accounts.authfile import AuthFileError
from codexctl.accounts.devicelogin import DeviceLoginError
from codexctl.accounts.publisher import PublishError
from codexctl.formatting import fmt_ts, window_name
from codexctl.locks import LockBusy
from codexctl.runner import CommandError
from codexctl.users.codexinstall import CodexInstallError
from codexctl.users.service import UserAdminError
from codexctl.users.sysusers import UserError
from codexctl.web.charts import fmt_md_hm, week_trend_chart
from codexctl.web.dependencies import clean_text

HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, LockBusy, CommandError, DeviceLoginError, UserAdminError, CodexInstallError, webauth.AuthError,
)


_TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def _countdown(resets_at: int | None, now: int) -> str:
    if resets_at is None:
        return "重置时间未知"
    left = resets_at - now
    if left <= 0:
        return "已重置"
    days, rem = divmod(left, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时后重置"
    if hours:
        return f"{hours} 小时 {minutes} 分后重置"
    return f"{minutes} 分钟后重置"


def _quota_view(q, now: int) -> dict:
    pct = q["used_percent"]
    expired = q["resets_at"] is not None and q["resets_at"] <= now
    if expired:
        percent, reset = 0, "已重置（待更新）"
    else:
        reset = _countdown(q["resets_at"], now)
        percent = None
        if isinstance(pct, (int, float)) and math.isfinite(pct):
            percent = max(0, min(100, round(pct)))
    return {"name": window_name(q["window_minutes"]), "minutes": q["window_minutes"], "percent": percent, "reset": reset,
            "source": clean_text(q["source"])}


def _current_account_quota(ctx, account) -> dict | None:
    if account is None:
        return None
    now = ctx.now()
    rows = usage_repo.latest_quota(ctx.conn, account.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
    windows = []
    for row in rows:
        view = _quota_view(row, now)
        expired = row["resets_at"] is not None and row["resets_at"] <= now
        remaining = 100 - view["percent"] if view["percent"] is not None and not expired else None
        windows.append(dict(
            view, win=row["win"], has_quota=remaining is not None, remaining=remaining,
            used=view["percent"] if not expired else None,
            tone=_tone(remaining) if remaining is not None else "warn",
            expired=expired, stale=expired or now - row["ts"] > 2 * ctx.settings.quota_poll_seconds,
            sampled_at=datetime.fromtimestamp(row["ts"], sites.BEIJING).strftime("%m-%d %H:%M"),
        ))
    selected = next((window for row, window in zip(rows, windows) if row["win"] == "primary"),
                    windows[0] if windows else dict(has_quota=False, remaining=None, used=None, name="",
                                                   reset="", tone="warn", stale=False, expired=False,
                                                   sampled_at="", source=""))
    return dict(selected, windows=windows, other_windows=[window for window in windows if window is not selected], status_text={"needs_relogin": "需重新登录", "disabled": "已停用"}.get(account.status, ""))


def _week_window(h: dict) -> dict | None:
    for key in ("primary", "secondary"):
        if h[key] is not None and h[key]["window_minutes"] == 10080:
            return h[key]
    return h["secondary"]


def _mask_email(email: str | None) -> str:
    if not email:
        return "-"
    local, sep, domain = email.partition("@")
    return local[:4] + "****" + (sep + domain if sep else "")


def _tone(remaining: int) -> str:
    return "good" if remaining >= 30 else "warn" if remaining >= 10 else "bad"


def _used(w: dict) -> float | None:
    v = w["used_percent"]
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _projection(used: float, window_start: int, resets_at: int, now: int) -> str:
    if used <= 0:
        return "本周尚未使用"
    elapsed = now - window_start
    if elapsed <= 0:
        return ""
    exhaust = window_start + elapsed * 100 / used
    if exhaust >= resets_at:
        return "按此速度，重置前不会用完"
    return f"按此速度，预计 {fmt_md_hm(int(exhaust))} 用完"


def _card(acc, history: list[dict], latest, now: int, settings) -> dict:
    """首页账号卡片的纯计算：latest 为 usage_repo.latest_quota 行，history 为 usage_repo.quota_history（新到旧）。"""
    rows = [dict(q) for q in latest]
    week = next((q for q in rows if q["window_minutes"] == 10080), None)
    if week is None:
        week = next((q for q in rows if q["win"] == "secondary"), rows[-1] if rows else None)
    other = next((q for q in rows if q is not week), None)
    api_ts = [q["ts"] for q in rows if q["source"] == "api"]
    card = {
        "label": clean_text(acc.label), "name": clean_text(getattr(acc, "display_name", None) or acc.label),
        "renamed": bool(getattr(acc, "display_name", None)), "email": _mask_email(clean_text(acc.email or "", 128)),
        "plan": clean_text(acc.plan_type or "-").upper(), "status": acc.status,
        "status_text": {"needs_relogin": "需重新登录", "disabled": "已停用"}.get(acc.status, "") if acc.status != "active" else "",
        "has_quota": False, "synced": fmt_md_hm(max(api_ts)) if api_ts else "-",
    }
    used = _used(week) if week else None
    if used is None:
        return card
    resets = week["resets_at"]
    expired = resets is not None and resets <= now
    if expired:
        used = 0.0
    remaining = max(0, min(100, 100 - round(used)))
    card.update(
        has_quota=True, remaining=remaining, used=100 - remaining, tone=_tone(remaining),
        reset="已重置（待更新）" if expired else ("-" if resets is None else fmt_md_hm(resets)),
        week_name=window_name(week["window_minutes"]), chart=None, projection="", other=None,
    )
    if other is not None and _used(other) is not None:
        ot = other["resets_at"]
        card["other"] = {
            "name": window_name(other["window_minutes"]), "used": max(0, min(100, round(_used(other)))),
            "reset": "-" if ot is None else time.strftime("%H:%M", time.localtime(ot)),
        }
    if resets is None or expired:
        return card
    start = resets - 7 * 86400
    card["projection"] = _projection(min(100.0, max(0.0, used)), start, resets, now)
    samples = []
    for h in history:
        w = _week_window(h)
        if w is None or w["resets_at"] is None or abs(w["resets_at"] - resets) > 3600 or _used(w) is None:
            continue
        samples.append((h["ts"], 100 - _used(w)))
    card["chart"] = week_trend_chart(
        samples, window_start=start, window_end=resets, now=now, gap=3 * settings.quota_poll_seconds,
        width=360, height=150, pad_left=34, pad_bottom=20, pad_top=10, pad_right=10,
    )
    card["current"] = remaining
    return card


def _account_rows(ctx) -> list[dict]:
    now = ctx.now()
    users_by_account: dict[int, list[str]] = {}
    for assignment in user_repo.all_active(ctx.conn):
        users_by_account.setdefault(assignment.account_pk, []).append(assignment.username)
    rows = []
    for acc in account_repo.list_accounts(ctx.conn):
        latest = usage_repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
        history = usage_repo.quota_history(ctx.conn, acc.id, now - 7 * 86400)
        card = _card(acc, history, latest, now, ctx.settings)
        card["users"] = users_by_account.get(acc.id, [])
        rows.append(card)
    return rows


def _account_table(ctx) -> list[dict]:
    """账号管理页的表格行：比首页卡片多出邮箱、用户数、错误与 token 到期等管理信息。"""
    now = ctx.now()
    counts: dict[int, int] = {}
    for a in user_repo.all_active(ctx.conn):
        counts[a.account_pk] = counts.get(a.account_pk, 0) + 1
    rows = []
    for acc in account_repo.list_accounts(ctx.conn):
        quotas = [
            _quota_view(q, now)
            for q in usage_repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
        ]
        rows.append({
            "label": acc.label,
            "name": clean_text(acc.name),
            "display_name": clean_text(acc.display_name or ""),
            "email": clean_text(acc.email or "-", 128),
            "plan": clean_text(acc.plan_type or "-"),
            "status": acc.status,
            "last_error": clean_text(acc.last_error or "", 200),
            "users": counts.get(acc.id, 0),
            "last_refresh": fmt_ts(acc.last_refresh),
            "expires": fmt_ts(acc.access_expires_at),
            "quota": quotas,
        })
    return rows


def _quota_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "1"))
    except ValueError:
        return 1
    return days if days in (1, 7, 30) else 1


def _safe_next(value: str) -> str:
    ok = value == "/" or (value.startswith("/accounts/") and "\\" not in value and value.isprintable())
    return value if ok else "/accounts"


def _finish_login_import(ctx, devlogins, login_id: str, label: str, mode: str, actor: str) -> tuple[str, str]:
    """取走一次登录结果并导入；返回 (msg, err)。调用方须持有全局锁。"""
    try:
        data = devlogins.take_result(login_id)
    except DeviceLoginError:
        return "", "登录结果已被取走或不可用"
    try:
        if mode == "relogin":
            accounts.relogin_account(ctx, label, data, actor)
            return f"账号 {label} 已重新登录", ""
        accounts.import_account(ctx, label, data, actor)
        return f"账号 {label} 已添加", ""
    except HANDLED as e:
        return "", str(e)


def _finish_login(ctx, devlogins, login_id: str, label: str, mode: str, actor: str) -> tuple[str, str]:
    msg, err = _finish_login_import(ctx, devlogins, login_id, label, mode, actor)
    if msg:
        acc = account_repo.get_account(ctx.conn, label)
        if acc is not None:
            failure = quota.poll_account(ctx, acc)
            if failure:
                msg += f"（额度获取失败：{clean_text(failure, 200)}）"
    return msg, err


def _complete_finished_logins(app, admin: tuple[str, str]) -> tuple[str, str]:
    """导入所有已成功但尚未取走的登录（管理员关掉状态页也不会丢结果）。调用方须持有全局锁。"""
    ctx, devlogins = app.state.ctx, app.state.devlogins
    msgs, errs = [], []
    for lid in devlogins.finished_ids():
        st = devlogins.status(lid)
        if st is None:
            continue
        m, e = _finish_login(ctx, devlogins, lid, st["label"], st["mode"], f"web:{admin[0]}")
        if m:
            msgs.append(m)
        if e:
            errs.append(f"{clean_text(st['label'], 32)}：{e}")
    return "；".join(msgs), "；".join(errs)

