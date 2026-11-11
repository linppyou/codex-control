from __future__ import annotations

import csv
import io
import sqlite3
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import accounts, assignments, events, refresher, repo
from ..authfile import AuthFileError
from ..cli import fmt_ts, window_name
from ..locks import LockBusy
from ..publisher import PublishError
from ..runner import CommandError
from ..sysusers import UserError
from .app import clean_text, current_admin

HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, LockBusy, CommandError, ValueError,
)
FRESH_API_SECONDS = 600
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


def _csv_cell(v: object) -> object:
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def _account_rows(ctx) -> list[dict]:
    now = ctx.now()
    counts = {}
    for a in repo.all_active(ctx.conn):
        counts[a.account_pk] = counts.get(a.account_pk, 0) + 1
    rows = []
    for acc in repo.list_accounts(ctx.conn):
        rows.append({
            "label": acc.label,
            "email": clean_text(acc.email or "-", 128),
            "plan": clean_text(acc.plan_type or "-"),
            "status": acc.status,
            "last_error": clean_text(acc.last_error or "", 200),
            "users": counts.get(acc.id, 0),
            "last_refresh": fmt_ts(acc.last_refresh),
            "expires": fmt_ts(acc.access_expires_at),
            "quota": [
                {
                    "name": window_name(q["window_minutes"]),
                    "percent": max(0, min(100, round(q["used_percent"]))),
                    "reset": _countdown(q["resets_at"], now),
                    "source": clean_text(q["source"]),
                }
                for q in repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - FRESH_API_SECONDS)
            ],
        })
    return rows


def _usage_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "7"))
    except ValueError:
        return 7
    return days if days in (1, 7, 30) else 7


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
            cards = _account_rows(ctx)
            alerts = [
                {
                    "level": clean_text(a["level"], 16), "kind": clean_text(a["kind"]),
                    "target": clean_text(a["target"]), "message": clean_text(a["message"], 300),
                    "ts": fmt_ts(a["ts"]),
                }
                for a in events.open_alerts(ctx.conn)
            ]
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1], cards=cards, alerts=alerts)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            rows = _account_rows(ctx)
        return render(request, "accounts.html", admin=admin[0], csrf=admin[1], rows=rows)

    def account_action(label: str, admin, fn, ok_msg: str) -> RedirectResponse:
        with lock:
            devlogins.sweep()
            try:
                fn(actor_of(admin))
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg=ok_msg)

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
                    raise ValueError("mode 只能是 add 或 relogin")
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
                try:
                    data = devlogins.take_result(login_id)
                except ValueError:
                    return _redirect("/accounts", err="登录结果已被取走或不可用")
                try:
                    if st["mode"] == "relogin":
                        accounts.relogin_account(ctx, st["label"], data, actor_of(admin))
                        msg = f"账号 {st['label']} 已重新登录"
                    else:
                        accounts.import_account(ctx, st["label"], data, actor_of(admin))
                        msg = f"账号 {st['label']} 已添加"
                except HANDLED as e:
                    return _redirect("/accounts", err=str(e))
                return _redirect("/accounts", msg=msg)
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
            devlogins.cancel(login_id)
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

    @app.post("/accounts/{label}/republish")
    def account_republish(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(label, admin, lambda a: accounts.republish_account(ctx, label), f"已重新发布 {label}")

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
            rows = []
            for u in ctx.list_users(ctx.settings.min_uid):
                if not show_all and u.name in hidden:
                    continue
                cur = repo.active_assignment(ctx.conn, u.name)
                link = ctx.publisher.read_link(u)
                rows.append({
                    "name": u.name,
                    "account": labels.get(cur.account_pk, "?") if cur else "",
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
            for name in users:
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
