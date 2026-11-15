from __future__ import annotations

import csv
from datetime import datetime, timedelta
import io
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import accounts, assignments, events, pricing, procs, quota, refresher, repo, useradmin
from ..authfile import AuthFileError
from ..codexinstall import CodexInstallError
from ..devicelogin import DeviceLoginError
from ..cli import fmt_ts, window_name
from ..locks import LockBusy
from ..publisher import PublishError
from ..runner import CommandError
from ..sysusers import UserError
from ..useradmin import UserAdminError
from .app import clean_text, current_admin
from .charts import fmt_md, fmt_md_hm, quota_chart, week_trend_chart

HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, LockBusy, CommandError, DeviceLoginError, UserAdminError, CodexInstallError,
)
_TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def _redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg[:500]
    if err:
        params["err"] = err[:500]
    return RedirectResponse(path + ("?" + urlencode(params) if params else ""), status_code=303)


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


def _flash(msg: str, err: str) -> dict:
    out = {}
    if msg:
        out["msg"] = msg
    if err:
        out["err"] = err
    return out


def _csv_cell(v: object) -> object:
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


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
    """首页账号卡片的纯计算：latest 为 repo.latest_quota 行，history 为 repo.quota_history（新到旧）。"""
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
    rows = []
    for acc in repo.list_accounts(ctx.conn):
        latest = repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
        history = repo.quota_history(ctx.conn, acc.id, now - 7 * 86400)
        rows.append(_card(acc, history, latest, now, ctx.settings))
    return rows


def _account_table(ctx) -> list[dict]:
    """账号管理页的表格行：比首页卡片多出邮箱、用户数、错误与 token 到期等管理信息。"""
    now = ctx.now()
    counts: dict[int, int] = {}
    for a in repo.all_active(ctx.conn):
        counts[a.account_pk] = counts.get(a.account_pk, 0) + 1
    rows = []
    for acc in repo.list_accounts(ctx.conn):
        quotas = [
            _quota_view(q, now)
            for q in repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
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
        acc = repo.get_account(ctx.conn, label)
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


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins

    def actor_of(admin: tuple[str, str]) -> str:
        return f"web:{admin[0]}"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, days: int = 7, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            msg, err = "", ""
            if admin[0] == "admin":
                devlogins.sweep()
                msg, err = _complete_finished_logins(app, admin)
            cards = _account_rows(ctx)
            analytics = user_comparison(days)
            alerts = [
                {
                    "level": clean_text(a["level"], 16), "kind": clean_text(a["kind"]),
                    "target": clean_text(a["target"]), "message": clean_text(a["message"], 300),
                    "ts": fmt_ts(a["ts"]),
                }
                for a in events.open_alerts(ctx.conn)
            ]
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1], cards=cards, alerts=alerts, **analytics, **_flash(msg, err))

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            msg, err = "", ""
            if admin[0] == "admin":
                devlogins.sweep()
                msg, err = _complete_finished_logins(app, admin)
            rows = _account_table(ctx)
        return render(request, "accounts.html", admin=admin[0], csrf=admin[1], rows=rows, **_flash(msg, err))

    def account_action(label: str, admin, fn, ok_msg: str) -> RedirectResponse:
        with lock:
            devlogins.sweep()
            try:
                fn(actor_of(admin))
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg=ok_msg)

    @app.post("/accounts/{label}/name")
    def account_name(label: str, name: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            try:
                value = accounts.set_display_name(ctx, label, name, actor_of(admin))
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg=f"{label} 的名称已改为 {value}" if value else f"{label} 的名称已清除")

    @app.post("/accounts/{label}/quota-refresh")
    def quota_refresh(
        label: str, next: str = Form(""), admin: tuple[str, str] = Depends(current_admin)
    ):
        back = _safe_next(next)
        with lock:
            devlogins.sweep()
            try:
                acc = accounts.require_account(ctx, label)
            except HANDLED as e:
                return _redirect(back, err=str(e))
            failure = quota.poll_account(ctx, acc)
            events.audit(
                ctx.conn, actor_of(admin), "account.quota_refresh", label,
                clean_text(failure, 200) if failure else "", now=ctx.now(),
            )
        if failure:
            return _redirect(back, err=f"额度获取失败：{clean_text(failure, 200)}")
        return _redirect(back, msg="额度已更新")

    def quota_data(label: str, days: int):
        with lock:
            devlogins.sweep()
            acc = repo.get_account(ctx.conn, label)
            if acc is None:
                return None
            now = ctx.now()
            history = repo.quota_history(ctx.conn, acc.id, now - days * 86400)
            latest = [
                _quota_view(q, now)
                for q in repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
            ]
        names = {"primary": "主窗口", "secondary": "次窗口"}
        for h in history:
            for key in names:
                if h[key] is not None and h[key]["window_minutes"] is not None:
                    names[key] = window_name(h[key]["window_minutes"])
            if all(n not in ("主窗口", "次窗口") for n in names.values()):
                break
        return history, latest, names

    def _cell(w: dict | None) -> tuple[float | None, str]:
        if w is None:
            return None, "-"
        return w["used_percent"], fmt_ts(w["resets_at"])

    @app.get("/accounts/{label}/quota", response_class=HTMLResponse)
    def quota_page(label: str, request: Request, admin: tuple[str, str] = Depends(current_admin)):
        days = _quota_days(request)
        got = quota_data(label, days)
        if got is None:
            return _redirect("/accounts", err="账号不存在")
        history, latest, names = got
        with lock:
            acc = repo.get_account(ctx.conn, label)
        title = clean_text(acc.name if acc else label)
        rows = []
        for h in history:
            p, s = _cell(h["primary"]), _cell(h["secondary"])
            wk = _week_window(h)
            rows.append({
                "week_left": "-" if wk is None else f"{max(0, 100 - round(wk['used_percent']))}%",
                "ts": fmt_ts(h["ts"]),
                "p_used": "-" if p[0] is None else f"{p[0]:.0f}%", "p_reset": p[1],
                "s_used": "-" if s[0] is None else f"{s[0]:.0f}%", "s_reset": s[1],
            })
        points = [
            (h["ts"], 100 - _week_window(h)["used_percent"]) for h in history if _week_window(h) is not None
        ]
        chart = quota_chart(points, width=1100, height=280, gap=3 * ctx.settings.quota_poll_seconds)
        return render(
            request, "account_quota.html", admin=admin[0], csrf=admin[1], label=clean_text(label), title=title,
            rows=rows, latest=latest, names=names, days=days, chart=chart,
        )

    @app.get("/accounts/{label}/quota.csv")
    def quota_csv(label: str, request: Request, admin: tuple[str, str] = Depends(current_admin)):
        days = _quota_days(request)
        got = quota_data(label, days)
        if got is None:
            return _redirect("/accounts", err="账号不存在")
        history, _latest, names = got
        buf = io.StringIO()
        w = csv.writer(buf)
        p, s = names["primary"], names["secondary"]
        w.writerow(["记录时间", f"{p}已用", f"{p}重置时间", f"{s}已用", f"{s}重置时间"])
        for h in history:
            pu, pr = _cell(h["primary"])
            su, sr = _cell(h["secondary"])
            w.writerow([_csv_cell(fmt_ts(h["ts"])), "" if pu is None else pu, pr, "" if su is None else su, sr])
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{label}-quota.csv"'},
        )

    @app.post("/accounts/login")
    def login_start(
        label: str = Form(""), mode: str = Form("add"), admin: tuple[str, str] = Depends(current_admin)
    ):
        with lock:
            devlogins.sweep()
            try:
                if mode == "add":
                    accounts.check_new_label(ctx, label)
                elif mode == "relogin":
                    accounts.require_account(ctx, label)
                else:
                    raise DeviceLoginError("mode 只能是 add 或 relogin")
                login_id = devlogins.start(label, mode)
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
            events.audit(ctx.conn, actor_of(admin), f"account.login_{mode}_start", label, now=ctx.now())
        return RedirectResponse(f"/accounts/login/{login_id}", status_code=303)

    @app.get("/accounts/login/{login_id}", response_class=HTMLResponse)
    def login_page(request: Request, login_id: str, admin: tuple[str, str] = Depends(current_admin)):
        if admin[0] != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可以查看设备登录")
        with lock:
            devlogins.sweep()
            st = devlogins.status(login_id)
            if st is None:
                return _redirect("/accounts", err="登录不存在或已过期")
            if st["state"] == "succeeded":
                msg, err = _finish_login(ctx, devlogins, login_id, st["label"], st["mode"], actor_of(admin))
                return _redirect("/accounts", msg=msg, err=err)
        view = {
            "id": login_id,
            "label": clean_text(st["label"]),
            "mode": st["mode"],
            "state": st["state"],
            "url": clean_text(st["url"] or "", 512),
            "code": clean_text(st["code"] or "", 32),
            "error": clean_text(st["error"] or "", 200),
            "active": st["state"] not in _TERMINAL,
        }
        return render(request, "account_login.html", admin=admin[0], csrf=admin[1], login=view)

    @app.get("/accounts/login/{login_id}/status")
    def login_status(login_id: str, admin: tuple[str, str] = Depends(current_admin)):
        if admin[0] != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可以查看设备登录")
        with lock:
            devlogins.sweep()
            st = devlogins.status(login_id)
        if st is None:
            raise HTTPException(status_code=404, detail="登录不存在")
        return JSONResponse({
            "state": st["state"],
            "url": clean_text(st["url"] or "", 512),
            "code": clean_text(st["code"] or "", 32),
            "error": clean_text(st["error"] or "", 200),
        })

    @app.post("/accounts/login/{login_id}/cancel")
    def login_cancel(login_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            st = devlogins.status(login_id)
            devlogins.cancel(login_id)
            if st is not None and st["state"] in ("pending", "waiting"):
                events.audit(ctx.conn, actor_of(admin), "account.login_cancel", st["label"], now=ctx.now())
        return _redirect("/accounts", msg="已取消登录")

    @app.post("/accounts/{label}/refresh")
    def account_refresh(label: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            try:
                acc = accounts.require_account(ctx, label)
                ok = refresher.refresh_account(ctx, acc, actor_of(admin), blocking=False)
                if not ok:
                    fresh = repo.get_account(ctx.conn, label)
                    detail = clean_text((fresh.last_error if fresh else None) or "未知原因", 200)
                    return _redirect("/accounts", err=f"刷新失败：{detail}")
            except LockBusy:
                return _redirect("/accounts", err=f"账号 {label} 正在刷新，请稍后再试")
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg="刷新成功")

    @app.post("/accounts/{label}/disable")
    def account_disable(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(label, admin, lambda a: accounts.set_disabled(ctx, label, True, a), f"已停用 {label}")

    @app.post("/accounts/{label}/enable")
    def account_enable(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(label, admin, lambda a: accounts.set_disabled(ctx, label, False, a), f"已启用 {label}")

    def republish(label: str, actor: str) -> None:
        accounts.republish_account(ctx, label)
        events.audit(ctx.conn, actor, "account.republish", label, now=ctx.now())

    @app.post("/accounts/{label}/republish")
    def account_republish(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(label, admin, lambda a: republish(label, a), f"已重新发布 {label}")

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        show_all = request.query_params.get("all") == "1"
        with lock:
            if admin[0] == "admin":
                devlogins.sweep()
            now = ctx.now()
            hidden = set(ctx.settings.hidden_users)
            book = pricing.PriceBook(repo.list_prices(ctx.conn))
            usage = {
                u["raw"]: u
                for u in group_users(
                    repo.usage_by_user_model_windows(ctx.conn, now - 86400, now - 7 * 86400), {}, book
                )
            }
            accounts_by_id = {a.id: a for a in repo.list_accounts(ctx.conn)}
            assigned = {a.username: accounts_by_id.get(a.account_pk) for a in repo.all_active(ctx.conn)}
            rows = []
            for u in ctx.list_users(ctx.settings.min_uid):
                if not show_all and u.name in hidden:
                    continue
                link = ctx.publisher.read_link(u)
                stale = procs.stale_procs(
                    procs.list_codex_procs(u.uid, ctx.settings.codex_bin.parent, home=u.home),
                    repo.last_assignment_change(ctx.conn, u.name),
                )
                used = usage.get(u.name, {})
                account = assigned.get(u.name)
                rows.append({
                    "name": u.name,
                    "account": clean_text(account.name) if account else "",
                    "account_label": clean_text(account.label) if account and account.display_name else "",
                    "link": clean_text(link.kind),
                    "target": clean_text(link.target or "", 200),
                    "t24": used.get("today", 0),
                    "t7": used.get("week", 0),
                    "c24": used.get("cost_today"),
                    "c7": used.get("cost_week"),
                    "u24": used.get("unpriced_today", 0),
                    "u7": used.get("unpriced_week", 0),
                    "stale": stale,
                    "stale_title": "；".join(
                        f"pid {p.pid} {clean_text(p.exe, 80)} 于 {fmt_ts(p.started_at)} 启动" for p in stale
                    ),
                })
            choices = [
                {"label": a.label, "name": clean_text(a.name)}
                for a in repo.list_accounts(ctx.conn) if a.status != "disabled"
            ]
        return render(
            request, "users.html", admin=admin[0], csrf=admin[1], rows=rows, choices=choices, show_all=show_all
        )

    @app.post("/users/create")
    def users_create(
        username: str = Form(""), password: str = Form(""), password2: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        name = username.strip()
        with lock:
            devlogins.sweep()
        try:
            useradmin.validate_new_user(ctx, name, password, password2)
        except HANDLED as e:
            return _redirect("/users", err=str(e))
        # codex 首次安装可能耗时较长，放在全局锁之外，避免阻塞面板其它请求
        try:
            codex_note = useradmin.ensure_codex(ctx)
        except HANDLED as e:
            return _redirect("/users", err=str(e))
        with lock:
            try:
                user, note = useradmin.create(
                    ctx, name, password, password2, actor_of(admin), codex_note=codex_note
                )
            except HANDLED as e:
                return _redirect("/users", err=str(e))
        msg = f"用户 {user.name} 已创建（uid {user.uid}，家目录 {user.home}）"
        if note:
            msg += f"，{note}"
        msg += "；请在列表勾选后分配账号"
        return _redirect("/users", msg=msg)

    @app.post("/users/assign")
    def users_assign(
        users: list[str] = Form([]), account: str = Form(""), admin: tuple[str, str] = Depends(current_admin)
    ):
        if not users:
            return _redirect("/users", err="请先选择用户")
        if not account:
            return _redirect("/users", err="请选择账号")
        ok, bad, backups = [], [], 0
        with lock:
            devlogins.sweep()
            hidden = set(ctx.settings.hidden_users)
            for name in users:
                if name in hidden:
                    bad.append(f"{clean_text(name, 32)}：隐藏用户不可分配")
                    continue
                try:
                    if assignments.assign(ctx, name, account, actor_of(admin)) is not None:
                        backups += 1
                    ok.append(name)
                except HANDLED as e:
                    bad.append(f"{clean_text(name, 32)}：{e}")
        msg = f"已分配 {len(ok)} 个用户到 {account}" if ok else ""
        if backups:
            msg += f"（{backups} 个用户的原 auth.json 已备份）"
        for name in ok:
            n = len(assignments.stale_codex_procs(ctx, name))
            if n:
                msg += (
                    f"。{clean_text(name, 32)} 有 {n} 个旧 codex 进程仍在使用旧账号，"
                    "通知对方后点『重启 codex』"
                )
        return _redirect("/users", msg=msg, err="；".join(bad))

    @app.post("/users/unassign")
    def users_unassign(users: list[str] = Form([]), admin: tuple[str, str] = Depends(current_admin)):
        if not users:
            return _redirect("/users", err="请先选择用户")
        ok, bad = 0, []
        done: list[str] = []
        with lock:
            devlogins.sweep()
            for name in users:
                try:
                    if assignments.unassign(ctx, name, actor_of(admin)):
                        ok += 1
                        done.append(name)
                except HANDLED as e:
                    bad.append(f"{clean_text(name, 32)}：{e}")
        msg = f"已回收 {ok} 个用户" if ok else ""
        for name in done:
            n = len(assignments.stale_codex_procs(ctx, name))
            if n:
                msg += (
                    f"。{clean_text(name, 32)} 有 {n} 个旧 codex 进程仍在使用旧账号，"
                    "通知对方后点『重启 codex』"
                )
        return _redirect("/users", msg=msg, err="；".join(bad))

    def _safe_user_next(value: str) -> str:
        ok = value == "/users" or (value.startswith("/users/") and "\\" not in value and value.isprintable())
        return value if ok else "/users"

    def fmt_duration(seconds: int) -> str:
        if seconds <= 0:
            return "< 1 秒"
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, rem_secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} 分钟" if rem_secs == 0 else f"{minutes} 分 {rem_secs} 秒"
        hours, rem_mins = divmod(minutes, 60)
        return f"{hours} 小时" if rem_mins == 0 else f"{hours} 小时 {rem_mins} 分钟"

    def _format_session_id(session_file: str) -> str:
        base = os.path.basename(session_file)
        if base.startswith("rollout-") and base.endswith(".jsonl"):
            return base[len("rollout-") : -len(".jsonl")]
        if base.endswith(".jsonl"):
            return base[: -len(".jsonl")]
        return base

    def _session_titles(home: Path, uid: int, session_files: list[str]) -> dict[str, str]:
        """Read the most recent title updates from the user's Codex session index."""
        wanted = {}
        for path in session_files:
            match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$", os.path.basename(path))
            if match:
                wanted[match.group(1).lower()] = path
        if not wanted:
            return {}
        try:
            directory = os.open(home / ".codex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                st = os.fstat(directory)
                if st.st_uid != uid:
                    return {}
                fd = os.open("session_index.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                    return {}
                # Bound memory and work for a user-controlled file; the index is append-only.
                offset = max(0, st.st_size - 8 * 1024 * 1024)
                f.seek(offset)
                if offset:
                    f.readline(8 * 1024 * 1024)
                lines = f.read(8 * 1024 * 1024).splitlines()
        except OSError:
            return {}
        titles = {}
        for line in reversed(lines):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                thread_id, title = row.get("id"), row.get("thread_name")
                if not isinstance(thread_id, str) or not isinstance(title, str):
                    continue
                path = wanted.get(thread_id.lower())
                if path and path not in titles and title.strip():
                    titles[path] = " ".join(title.split())[:200]
            except (ValueError, UnicodeError):
                continue
        return titles

    def _shorten_cwd(cwd: str | None, home: Path | None) -> str | None:
        if not cwd:
            return None
        if home:
            home_str = str(home)
            if cwd == home_str:
                return "~"
            if cwd.startswith(home_str + "/"):
                return "~" + cwd[len(home_str) :]
        return cwd

    def extract_session_meta(path_str: str, user_uid: int | None = None) -> dict[str, str | None]:
        meta: dict[str, str | None] = {"cwd": None, "first_prompt": None}
        try:
            fd = os.open(path_str, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError:
            return meta
        try:
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or (user_uid is not None and st.st_uid != user_uid):
                    return meta
                chunk = f.read(16 * 1024)
        except OSError:
            return meta

        for raw in chunk.splitlines()[:20]:
            try:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            payload = d.get("payload")
            if isinstance(payload, dict):
                if not meta["cwd"]:
                    cwd = payload.get("cwd") or payload.get("working_directory") or payload.get("project_dir")
                    if isinstance(cwd, str) and cwd.strip():
                        meta["cwd"] = cwd.strip()
                if not meta["first_prompt"]:
                    p = None
                    if isinstance(payload.get("prompt"), str):
                        p = payload["prompt"]
                    elif isinstance(payload.get("text"), str) and payload.get("type") in ("user_input", "user_message"):
                        p = payload["text"]
                    elif payload.get("type") == "message" and payload.get("role") == "user":
                        content = payload.get("content")
                        if isinstance(content, str):
                            p = content
                        elif isinstance(content, list) and content and isinstance(content[0], dict):
                            p = content[0].get("text")
                    if isinstance(p, str) and p.strip():
                        clean_p = " ".join(p.strip().split())
                        meta["first_prompt"] = clean_p[:80] + ("…" if len(clean_p) > 80 else "")
            if meta["cwd"] and meta["first_prompt"]:
                break
        return meta

    @app.post("/users/{name}/fix")
    def user_fix(name: str, next: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        back = _safe_user_next(next)
        with lock:
            devlogins.sweep()
            try:
                assignments.reconcile_user(ctx, name, actor_of(admin))
            except HANDLED as e:
                return _redirect(back, err=str(e))
        return _redirect(back, msg=f"已修复 {clean_text(name, 32)}")

    @app.post("/users/{name}/restart-codex")
    def user_restart_codex(name: str, next: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        back = _safe_user_next(next)
        with lock:
            devlogins.sweep()
            try:
                ended, unable = assignments.restart_codex(ctx, name, actor_of(admin))
            except HANDLED as e:
                return _redirect(back, err=str(e))
        label = clean_text(name, 32)
        if not ended and not unable:
            return _redirect(back, msg=f"{label} 没有旧 codex 进程")
        msg = f"已结束 {label} 的 {len(ended)} 个 codex 进程"
        err = f"部分进程无法结束：{', '.join(str(p) for p in unable)}" if unable else ""
        return _redirect(back, msg=msg, err=err)

    @app.get("/users/{name}", response_class=HTMLResponse)
    def user_detail_page(name: str, request: Request, days: int = 7, page: int = 1, model: str = "", account_filter: str = "", admin: tuple[str, str] = Depends(current_admin)):
        clean_name = clean_text(name.strip(), 32)
        today, week = usage_windows()
        days = days if days in (7, 30, 90) else 7
        page = max(1, page)
        model, account_filter = model[:200], account_filter[:200]
        local_today = datetime.fromtimestamp(ctx.now()).date()
        start_day = local_today - timedelta(days=days - 1)
        start_ts = int(time.mktime(start_day.timetuple()))
        end_ts = int(time.mktime((local_today + timedelta(days=1)).timetuple()))
        with lock:
            if admin[0] == "admin":
                devlogins.sweep()
            user = None
            try:
                user = ctx.lookup_user(clean_name, ctx.settings.min_uid)
            except UserError:
                pass
            total_sessions = repo.user_session_count(ctx.conn, clean_name)
            if user is None and total_sessions == 0:
                return _redirect("/users", err=f"用户 {clean_name} 不存在")

            book = pricing.PriceBook(repo.list_prices(ctx.conn))

            active = repo.active_assignment(ctx.conn, clean_name)
            account = repo.get_account_by_pk(ctx.conn, active.account_pk) if active else None
            link = ctx.publisher.read_link(user) if user else None
            stale = procs.stale_procs(
                procs.list_codex_procs(user.uid, ctx.settings.codex_bin.parent, home=user.home),
                repo.last_assignment_change(ctx.conn, clean_name),
            ) if user else []

            model_rows = repo.usage_by_model_for_user(ctx.conn, clean_name, today, week)
            models = []
            summary = {
                "today": 0, "week": 0, "total": 0, "turns": 0,
                "cost_today": 0.0, "cost_week": 0.0, "cost_total": 0.0,
                "unpriced_today": 0, "unpriced_week": 0, "unpriced_total": 0,
                "today_input": 0, "today_output": 0, "week_input": 0, "week_output": 0,
            }
            for r in model_rows:
                m = {
                    "name": clean_text(r["model"]) if r["model"] else "未知模型",
                    "known": bool(r["model"]),
                    "today": r["today"] or 0, "week": r["week"] or 0, "total": r["total"] or 0,
                    "turns": r["turns"],
                }
                for k in ("today", "week", "total"):
                    cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
                    m[f"cost_{k}"] = cost
                    if m[k]:
                        if cost is None:
                            summary[f"unpriced_{k}"] += 1
                        else:
                            summary[f"cost_{k}"] += cost
                summary["today_input"] += r["today_input"] or 0
                summary["today_output"] += r["today_output"] or 0
                summary["week_input"] += r["week_input"] or 0
                summary["week_output"] += r["week_output"] or 0
                summary["today"] += m["today"]
                summary["week"] += m["week"]
                summary["total"] += m["total"]
                summary["turns"] += m["turns"]
                models.append(m)

            for k in ("today", "week", "total"):
                if summary[f"unpriced_{k}"] > 0 and summary[k] > 0 and summary[f"cost_{k}"] == 0.0:
                    summary[f"cost_{k}"] = None

            daily_rows = {r["day"]: r for r in repo.user_daily_usage(ctx.conn, clean_name, start_ts, end_ts)}
            daily = []
            for i in range(days):
                day = start_day + timedelta(days=i)
                row = daily_rows.get(day.isoformat())
                daily.append({"day": day.isoformat(), "label": day.strftime("%m-%d"),
                              **{k: (row[k] or 0) if row else 0 for k in ("total", "input", "cached", "output", "turns")}})
            daily_max = max((d["total"] for d in daily), default=0)
            for i, d in enumerate(daily):
                d["x"] = round(45 + i * 900 / days, 2)
                d["width"] = round(max(2, 900 / days - 2), 2)
                d["height"] = round(max(0, d["total"]) / (daily_max or 1) * 150, 2)
                d["y"] = round(165 - d["height"], 2)
                d["show_label"] = i == 0 or i == days - 1 or (days <= 7 or (days <= 30 and i % 5 == 0) or (days > 30 and i % 15 == 0))
            filter_models, filter_accounts = repo.user_session_filters(ctx.conn, clean_name)
            filtered_sessions = repo.user_session_count(ctx.conn, clean_name, model, account_filter)
            pages = max(1, math.ceil(filtered_sessions / 20))
            page = min(page, pages)
            session_rows = repo.user_recent_sessions(ctx.conn, clean_name, limit=20, offset=(page - 1) * 20, model=model, account=account_filter)
            def page_url(n: int) -> str:
                return "?" + urlencode({"days": days, "page": n, "model": model, "account_filter": account_filter}) + "#sessions"

            session_files = [r["session_file"] for r in session_rows]
            session_titles = _session_titles(user.home, user.uid, session_files) if user else {}
            breakdown = repo.user_sessions_model_breakdown(ctx.conn, clean_name, session_files)
            costs: dict[str, tuple[float | None, int]] = {}
            for b in breakdown:
                c = book.cost(b["model"], b["input"], b["cached"], b["output"])
                curr_cost, curr_unpriced = costs.setdefault(b["session_file"], (0.0, 0))
                if c is None:
                    costs[b["session_file"]] = (curr_cost, curr_unpriced + 1)
                else:
                    costs[b["session_file"]] = ((curr_cost or 0.0) + c, curr_unpriced)

            sessions = []
            user_home = user.home if user else None
            user_uid = user.uid if user else None
            for r in session_rows:
                sf = r["session_file"]
                cost, unpriced = costs.get(sf, (0.0, 0))
                if unpriced > 0 and (r["total_tokens"] or 0) > 0 and cost == 0.0:
                    cost = None
                meta = extract_session_meta(sf, user_uid)
                started_at = r["started_at"]
                last_active_at = r["last_active_at"]
                duration = max(0, last_active_at - started_at)
                models_list = [clean_text(m) for m in (r["models"] or "").split(",") if m]
                accounts_list = [clean_text(a) for a in (r["accounts"] or "").split(",") if a]
                cwd = meta.get("cwd")
                sessions.append({
                    "session_file": sf,
                    "session_id": _format_session_id(sf),
                    "started_at": started_at,
                    "started_at_fmt": fmt_ts(started_at),
                    "last_active_at": last_active_at,
                    "last_active_at_fmt": fmt_ts(last_active_at),
                    "duration": duration,
                    "duration_fmt": fmt_duration(duration),
                    "turns": r["turns"],
                    "total_tokens": r["total_tokens"],
                    "input_tokens": r["input_tokens"],
                    "cached_input_tokens": r["cached_input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "reasoning_output_tokens": r["reasoning_output_tokens"],
                    "models": models_list,
                    "accounts": accounts_list,
                    "cost": cost,
                    "unpriced": unpriced > 0,
                    "cwd": cwd,
                    "cwd_short": _shorten_cwd(cwd, user_home),
                    "first_prompt": meta.get("first_prompt"),
                    "title": session_titles.get(sf) or meta.get("first_prompt") or "未命名会话",
                })

        return render(
            request, "user_detail.html", admin=admin[0], csrf=admin[1],
            username=clean_name, user=user, account=account, link=link, stale=stale,
            summary=summary, models=models, sessions=sessions, total_sessions=total_sessions,
            daily=daily, daily_max=daily_max, days=days, page=page, pages=pages,
            filtered_sessions=filtered_sessions, filter_models=filter_models, filter_accounts=filter_accounts,
            selected_model=model, selected_account=account_filter, page_url=page_url,
        )

    def usage_windows() -> tuple[int, int]:
        """返回 (今天零点, 7 天前) 的时间戳；今天按服务器本地时区计算。"""
        now = ctx.now()
        t = time.localtime(now)
        today = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
        return today, now - 7 * 86400

    def usage_user_rows() -> tuple[list[sqlite3.Row], pricing.PriceBook]:
        today, week = usage_windows()
        with lock:
            rows = repo.usage_by_user_model_windows(ctx.conn, today, week)
            return rows, pricing.PriceBook(repo.list_prices(ctx.conn))

    def group_users(rows: list[sqlite3.Row], labels: dict[str, list[str]], book: pricing.PriceBook) -> list[dict]:
        """把 (用户, 模型) 行聚合成每个用户一项，内含按近 7 天降序的模型明细与各时段估算费用。"""
        users: dict[str, dict] = {}
        for r in rows:
            u = users.setdefault(r["username"], {
                "username": clean_text(r["username"]), "raw": r["username"],
                "labels": [clean_text(x) for x in labels.get(r["username"], [])],
                "today": 0, "week": 0, "total": 0, "turns": 0, "models": [],
                "cost_today": None, "cost_week": None, "cost_total": None,
                "unpriced_today": 0, "unpriced_week": 0, "unpriced_total": 0,
            })
            m = {
                "name": clean_text(r["model"]) if r["model"] else "未知模型", "known": bool(r["model"]),
                "today": r["today"] or 0, "week": r["week"] or 0, "total": r["total"] or 0, "turns": r["turns"],
            }
            for k in ("today", "week", "total"):
                cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
                m[f"cost_{k}"] = cost
                if m[k]:
                    if cost is None:
                        u[f"unpriced_{k}"] += 1
                    else:
                        u[f"cost_{k}"] = (u[f"cost_{k}"] or 0) + cost
            u["models"].append(m)
            for k in ("today", "week", "total", "turns"):
                u[k] += m[k]
        return sorted(users.values(), key=lambda u: (-u["week"], -u["total"], u["username"]))

    def user_comparison(days: int) -> dict:
        """Build the per-user daily chart for the dashboard; caller holds the lock."""
        days = days if days in (7, 30, 90) else 7
        local_today = datetime.fromtimestamp(ctx.now()).date()
        start_day = local_today - timedelta(days=days - 1)
        start_ts = int(time.mktime(start_day.timetuple()))
        end_ts = int(time.mktime((local_today + timedelta(days=1)).timetuple()))
        book = pricing.PriceBook(repo.list_prices(ctx.conn))
        daily_rows = repo.usage_daily_by_user_model(ctx.conn, start_ts, end_ts)
        comparison = {}
        daily_totals = {}
        for r in daily_rows:
            u = comparison.setdefault(r["username"], {"name": clean_text(r["username"]), "raw": r["username"],
                "total": 0, "input": 0, "cached": 0, "output": 0, "turns": 0, "cost": 0.0,
                "priced": False, "unpriced": False})
            for key in ("total", "input", "cached", "output", "turns"):
                u[key] += r[key] or 0
            cost = book.cost(r["model"], r["input"], r["cached"], r["output"])
            if cost is None and (r["total"] or 0):
                u["unpriced"] = True
            elif cost is not None:
                u["cost"] += cost
                u["priced"] = True
            key = (r["day"], r["username"])
            daily_totals[key] = daily_totals.get(key, 0) + (r["total"] or 0)
        ranking = sorted(comparison.values(), key=lambda u: (-u["total"], u["raw"]))
        daily = []
        for i in range(days):
            day = start_day + timedelta(days=i)
            key = day.isoformat()
            values = [daily_totals.get((key, u["raw"]), 0) for u in ranking]
            daily.append({"day": key, "label": day.strftime("%m-%d"), "values": values, "total": sum(values),
                          "show_label": i == 0 or i == days - 1 or (days <= 7 or (days <= 30 and i % 5 == 0) or (days > 30 and i % 15 == 0))})
        series_names = [u["name"] for u in ranking]
        daily_max = max((value for d in daily for value in d["values"]), default=0)
        series = [{"name": name, "index": i, "points": []} for i, name in enumerate(series_names)]
        for i, d in enumerate(daily):
            d["x"] = round(70 + i * 850 / max(1, days - 1), 2)
            for index, item in enumerate(series):
                value = d["values"][index]
                y = round(175 - max(0, value) / (daily_max or 1) * 140, 2)
                item["points"].append({"x": d["x"], "y": y, "value": value, "day": d["day"]})
        for item in series:
            item["path"] = " ".join(("M" if i == 0 else "L") + f"{p['x']},{p['y']}" for i, p in enumerate(item["points"]))
        total_tokens = sum(u["total"] for u in ranking)
        total_turns = sum(u["turns"] for u in ranking)
        total_cost = sum(u["cost"] for u in ranking)
        has_price = any(u["priced"] for u in ranking)
        has_unpriced = any(u["unpriced"] for u in ranking)
        total_input = sum(u["input"] for u in ranking)
        total_cached = sum(u["cached"] for u in ranking)
        return dict(days=days, ranking=ranking, daily=daily, daily_max=daily_max, series=series,
                    total_tokens=total_tokens, total_turns=total_turns, total_cost=total_cost,
                    has_price=has_price, has_unpriced=has_unpriced, total_input=total_input, total_cached=total_cached)

    @app.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        today, week = usage_windows()
        with lock:
            if admin[0] == "admin":
                devlogins.sweep()
            book = pricing.PriceBook(repo.list_prices(ctx.conn))
            periods = []
            for key, title, since in (("today", "今天", today), ("week", "近 7 天", week), ("all", "全部", 0)):
                models = []
                for r in repo.usage_by_model(ctx.conn, since):
                    inp, cached, out = r["input"] or 0, r["cached"] or 0, r["output"] or 0
                    cost = book.cost(r["model"], inp, cached, out)
                    models.append({
                        "name": clean_text(r["model"]) if r["model"] else "未知模型", "known": bool(r["model"]),
                        "total": r["total"] or 0, "input": inp, "cached": cached, "output": out,
                        "turns": r["turns"], "cost": cost,
                    })
                total = sum(m["total"] for m in models)
                for m in models:
                    m["pct"] = round(m["total"] * 100 / total, 1) if total else 0
                priced = [m for m in models if m["cost"] is not None]
                periods.append({
                    "key": key, "title": title, "models": models, "total": total,
                    "input": sum(m["input"] for m in models), "cached": sum(m["cached"] for m in models),
                    "output": sum(m["output"] for m in models), "turns": sum(m["turns"] for m in models),
                    "cost": sum(m["cost"] for m in priced) if priced else None,
                    "unpriced": sum(1 for m in models if m["cost"] is None),
                })
            users = group_users(
                repo.usage_by_user_model_windows(ctx.conn, today, week), repo.usage_user_accounts(ctx.conn), book
            )
        return render(
            request, "usage.html", admin=admin[0], csrf=admin[1], periods=periods, users=users,
            prices=book.prices,
        )

    @app.get("/usage.csv")
    def usage_csv(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "username", "model", "today_tokens", "7d_tokens", "total_tokens",
            "today_usd", "7d_usd", "total_usd", "turns",
        ])
        rows, book = usage_user_rows()
        for r in rows:
            costs = []
            for k in ("today", "week", "total"):
                cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
                costs.append("" if cost is None else f"{cost:.6f}".rstrip("0").rstrip(".") or "0")
            w.writerow([
                _csv_cell(r["username"]), _csv_cell(r["model"] or ""),
                r["today"] or 0, r["week"] or 0, r["total"] or 0, *costs, r["turns"],
            ])
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
        )

    def _price_from_form(
        name: str, input_usd: str, cached_usd: str, output_usd: str, aliases: str
    ) -> pricing.Price:
        label = name.strip()
        if not label:
            raise ValueError("模型名不能为空")
        if len(label) > pricing.MAX_NAME:
            raise ValueError(f"模型名最多 {pricing.MAX_NAME} 个字符")
        label = clean_text(label, pricing.MAX_NAME)

        def rate(field: str, raw: str) -> float:
            try:
                return pricing.parse_usd(raw)
            except ValueError as e:
                raise ValueError(f"{field}：{e}") from None

        inp = rate("输入价格", input_usd)
        cached = rate("缓存输入价格", cached_usd)
        out = rate("输出价格", output_usd)
        try:
            alias_tuple = pricing.parse_aliases(clean_text(aliases, pricing.MAX_NAME * pricing.MAX_ALIASES + 40))
        except ValueError as e:
            raise ValueError(f"别名：{e}") from None
        return pricing.Price(label, inp, cached, out, alias_tuple)

    def _price_detail(price: pricing.Price) -> str:
        return f"输入 {price.input_usd:g} / 缓存 {price.cached_usd:g} / 输出 {price.output_usd:g}"

    def _price_diff(old: pricing.Price, new: pricing.Price) -> str:
        parts = []
        if old.name != new.name:
            parts.append(f"名称 {old.name}→{new.name}")
        for label, before, after in (
            ("输入", old.input_usd, new.input_usd),
            ("缓存", old.cached_usd, new.cached_usd),
            ("输出", old.output_usd, new.output_usd),
        ):
            if before != after:
                parts.append(f"{label} {before:g}→{after:g}")
        if old.aliases != new.aliases:
            parts.append("别名已更新")
        return "；".join(parts)

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            if admin[0] == "admin":
                devlogins.sweep()
            prices = repo.list_prices(ctx.conn)
        return render(request, "pricing.html", admin=admin[0], csrf=admin[1], prices=prices)

    @app.post("/pricing/add")
    def pricing_add(
        name: str = Form(""), input_usd: str = Form(""), cached_usd: str = Form(""),
        output_usd: str = Form(""), aliases: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        with lock:
            devlogins.sweep()
            try:
                price = _price_from_form(name, input_usd, cached_usd, output_usd, aliases)
                error = pricing.validate_prices([*repo.list_prices(ctx.conn), price])
                if error:
                    raise ValueError(error)
                repo.insert_price(ctx.conn, price)
            except ValueError as e:
                return _redirect("/pricing", err=str(e))
            events.audit(
                ctx.conn, actor_of(admin), "pricing.add", price.name, _price_detail(price), now=ctx.now()
            )
        return _redirect("/pricing", msg=f"已添加 {price.name}")

    @app.post("/pricing/{price_id}")
    def pricing_update(
        price_id: int, name: str = Form(""), input_usd: str = Form(""), cached_usd: str = Form(""),
        output_usd: str = Form(""), aliases: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        with lock:
            devlogins.sweep()
            old = repo.get_price(ctx.conn, price_id)
            if old is None:
                return _redirect("/pricing", err="模型不存在或已被删除")
            try:
                price = _price_from_form(name, input_usd, cached_usd, output_usd, aliases)
                prices = [price if p.id == price_id else p for p in repo.list_prices(ctx.conn)]
                error = pricing.validate_prices(prices)
                if error:
                    raise ValueError(error)
                repo.update_price(ctx.conn, price_id, price)
            except ValueError as e:
                return _redirect("/pricing", err=str(e))
            events.audit(
                ctx.conn, actor_of(admin), "pricing.update", price.name,
                _price_diff(old, price) or "未改变", now=ctx.now(),
            )
        return _redirect("/pricing", msg=f"{price.name} 的价格已保存")

    @app.post("/pricing/{price_id}/delete")
    def pricing_delete(price_id: int, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            old = repo.get_price(ctx.conn, price_id)
            if old is None:
                return _redirect("/pricing", err="模型不存在或已被删除")
            repo.delete_price(ctx.conn, price_id)
            events.audit(ctx.conn, actor_of(admin), "pricing.delete", old.name, now=ctx.now())
        return _redirect("/pricing", msg=f"已删除 {old.name}")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            if admin[0] == "admin":
                devlogins.sweep()
            audit_rows = [
                {
                    "ts": fmt_ts(r["ts"]), "actor": clean_text(r["actor"]), "action": clean_text(r["action"]),
                    "target": clean_text(r["target"]), "detail": clean_text(r["detail"], 200),
                }
                for r in repo.recent_audit(ctx.conn, 200)
            ]
            alert_rows = [
                {
                    "ts": fmt_ts(r["ts"]), "level": clean_text(r["level"], 16), "kind": clean_text(r["kind"]),
                    "target": clean_text(r["target"]), "message": clean_text(r["message"], 300),
                    "resolved": fmt_ts(r["resolved_at"]) if r["resolved_at"] else "",
                }
                for r in repo.recent_alerts(ctx.conn, 100)
            ]
        return render(request, "audit.html", admin=admin[0], csrf=admin[1], audit=audit_rows, alerts=alert_rows)
