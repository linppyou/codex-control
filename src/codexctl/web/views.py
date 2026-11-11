from __future__ import annotations

import csv
import io
import math
import sqlite3
import time
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import accounts, assignments, events, quota, refresher, repo
from ..authfile import AuthFileError
from ..devicelogin import DeviceLoginError
from ..cli import fmt_ts, window_name
from ..locks import LockBusy
from ..publisher import PublishError
from ..runner import CommandError
from ..sysusers import UserError
from .app import clean_text, current_admin
from .charts import fmt_md, fmt_md_hm, quota_chart, week_trend_chart

HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, LockBusy, CommandError, DeviceLoginError,
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
        "label": clean_text(acc.label), "email": _mask_email(clean_text(acc.email or "", 128)),
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


def _quota_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "1"))
    except ValueError:
        return 1
    return days if days in (1, 7, 30) else 1


def _safe_next(value: str) -> str:
    ok = value == "/" or (value.startswith("/accounts/") and "\\" not in value and value.isprintable())
    return value if ok else "/accounts"


def _usage_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "7"))
    except ValueError:
        return 7
    return days if days in (1, 7, 30) else 7


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
    def index(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            cards = _account_rows(ctx)
            alerts = [
                {
                    "level": clean_text(a["level"], 16), "kind": clean_text(a["kind"]),
                    "target": clean_text(a["target"]), "message": clean_text(a["message"], 300),
                    "ts": fmt_ts(a["ts"]),
                }
                for a in events.open_alerts(ctx.conn)
            ]
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1], cards=cards, alerts=alerts, **_flash(msg, err))

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            rows = _account_rows(ctx)
        return render(request, "accounts.html", admin=admin[0], csrf=admin[1], rows=rows, **_flash(msg, err))

    def account_action(label: str, admin, fn, ok_msg: str) -> RedirectResponse:
        with lock:
            devlogins.sweep()
            try:
                fn(actor_of(admin))
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg=ok_msg)

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
        chart = quota_chart(points, gap=3 * ctx.settings.quota_poll_seconds)
        return render(
            request, "account_quota.html", admin=admin[0], csrf=admin[1], label=clean_text(label),
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
            devlogins.sweep()
            now = ctx.now()
            hidden = set(ctx.settings.hidden_users)
            u1 = repo.usage_by_user(ctx.conn, now - 86400)
            u7 = repo.usage_by_user(ctx.conn, now - 7 * 86400)
            labels = {a.id: a.label for a in repo.list_accounts(ctx.conn)}
            assigned = {a.username: labels.get(a.account_pk, "?") for a in repo.all_active(ctx.conn)}
            rows = []
            for u in ctx.list_users(ctx.settings.min_uid):
                if not show_all and u.name in hidden:
                    continue
                link = ctx.publisher.read_link(u)
                rows.append({
                    "name": u.name,
                    "account": assigned.get(u.name, ""),
                    "link": clean_text(link.kind),
                    "target": clean_text(link.target or "", 200),
                    "t24": u1.get(u.name, 0),
                    "t7": u7.get(u.name, 0),
                })
            choices = [a.label for a in repo.list_accounts(ctx.conn) if a.status != "disabled"]
        return render(
            request, "users.html", admin=admin[0], csrf=admin[1], rows=rows, choices=choices, show_all=show_all
        )

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
        return _redirect("/users", msg=msg, err="；".join(bad))

    @app.post("/users/unassign")
    def users_unassign(users: list[str] = Form([]), admin: tuple[str, str] = Depends(current_admin)):
        if not users:
            return _redirect("/users", err="请先选择用户")
        ok, bad = 0, []
        with lock:
            devlogins.sweep()
            for name in users:
                try:
                    if assignments.unassign(ctx, name, actor_of(admin)):
                        ok += 1
                except HANDLED as e:
                    bad.append(f"{clean_text(name, 32)}：{e}")
        return _redirect("/users", msg=f"已回收 {ok} 个用户" if ok else "", err="；".join(bad))

    @app.post("/users/{name}/fix")
    def user_fix(name: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            try:
                assignments.reconcile_user(ctx, name, actor_of(admin))
            except HANDLED as e:
                return _redirect("/users", err=str(e))
        return _redirect("/users", msg=f"已修复 {clean_text(name, 32)}")

    def usage_rows(days: int) -> list[sqlite3.Row]:
        with lock:
            devlogins.sweep()
            return repo.usage_summary(ctx.conn, ctx.now() - days * 86400)

    @app.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        days = _usage_days(request)
        rows = [
            {
                "username": clean_text(r["username"]), "label": clean_text(r["label"] or "-"),
                "total": r["total"], "input": r["input"], "output": r["output"], "turns": r["turns"],
            }
            for r in usage_rows(days)
        ]
        return render(request, "usage.html", admin=admin[0], csrf=admin[1], rows=rows, days=days)

    @app.get("/usage.csv")
    def usage_csv(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        days = _usage_days(request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["username", "account", "total_tokens", "input_tokens", "output_tokens", "turns"])
        for r in usage_rows(days):
            w.writerow([
                _csv_cell(r["username"]), _csv_cell(r["label"] or ""), r["total"], r["input"], r["output"], r["turns"],
            ])
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="usage-{days}d.csv"'},
        )

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
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
