from __future__ import annotations

import csv
import io

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import codexctl.accounts.refresher as refresher
import codexctl.accounts.repository as account_repo
import codexctl.accounts.service as accounts
import codexctl.events as events
import codexctl.usage.quota as quota
import codexctl.usage.repository as usage_repo
from codexctl.accounts.devicelogin import DeviceLoginError
from codexctl.formatting import fmt_ts, window_name
from codexctl.locks import LockBusy
from codexctl.web.charts import quota_chart
from codexctl.web.dependencies import clean_text, current_admin
from codexctl.web.presenters import (
    _TERMINAL,
    HANDLED,
    _account_table,
    _complete_finished_logins,
    _finish_login,
    _quota_days,
    _quota_view,
    _safe_next,
    _week_window,
)
from codexctl.web.responses import _csv_cell, _flash, _redirect

ACCOUNT_FILTERS = {
    "enabled": "已启用", "all": "全部", "active": "正常",
    "needs_relogin": "需重新登录", "disabled": "已停用",
}


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins

    def actor_of(admin: tuple[str, str]) -> str:
        return f"web:{admin[0]}"


    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, status: str = "enabled", admin: tuple[str, str] = Depends(current_admin)):
        if status not in ACCOUNT_FILTERS:
            status = "enabled"
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            rows = _account_table(ctx, status)
        return render(request, "accounts.html", admin=admin[0], csrf=admin[1], rows=rows,
                      status_filter=status, account_filters=ACCOUNT_FILTERS, **_flash(msg, err))


    def account_action(admin, fn, ok_msg: str, *, back: str = "/accounts") -> RedirectResponse:
        with lock:
            devlogins.sweep()
            try:
                fn(actor_of(admin))
            except LockBusy as e:
                return _redirect(back, err=f"账号 {e} 正在处理其他操作，请稍后再试")
            except HANDLED as e:
                return _redirect(back, err=str(e))
        return _redirect(back, msg=ok_msg)


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
            acc = account_repo.get_account(ctx.conn, label, include_deleted=True)
            if acc is None:
                return None
            now = ctx.now()
            history = usage_repo.quota_history(ctx.conn, acc.id, now - days * 86400)
            latest = [
                _quota_view(q, now)
                for q in usage_repo.latest_quota(ctx.conn, acc.id, api_fresh_since=now - 2 * ctx.settings.quota_poll_seconds)
            ]
        names = {"primary": "主窗口", "secondary": "次窗口"}
        for h in history:
            for key in names:
                if h[key] is not None and h[key]["window_minutes"] is not None:
                    names[key] = window_name(h[key]["window_minutes"])
            if all(n not in ("主窗口", "次窗口") for n in names.values()):
                break
        return acc, history, latest, names


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
        acc, history, latest, names = got
        title = clean_text(acc.name)
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
            rows=rows, latest=latest, names=names, days=days, chart=chart, account_status=acc.status,
        )


    @app.get("/accounts/{label}/quota.csv")
    def quota_csv(label: str, request: Request, admin: tuple[str, str] = Depends(current_admin)):
        days = _quota_days(request)
        got = quota_data(label, days)
        if got is None:
            return _redirect("/accounts", err="账号不存在")
        _acc, history, _latest, names = got
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
                if acc.status == "disabled":
                    raise accounts.AccountError(f"账号 {label} 已停用，不能刷新凭证")
                ok = refresher.refresh_account(ctx, acc, actor_of(admin), blocking=False)
                if not ok:
                    fresh = account_repo.get_account(ctx.conn, label)
                    detail = clean_text((fresh.last_error if fresh else None) or "未知原因", 200)
                    return _redirect("/accounts", err=f"刷新失败：{detail}")
            except LockBusy:
                return _redirect("/accounts", err=f"账号 {label} 正在刷新，请稍后再试")
            except HANDLED as e:
                return _redirect("/accounts", err=str(e))
        return _redirect("/accounts", msg="刷新成功")


    @app.post("/accounts/{label}/disable")
    def account_disable(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(admin, lambda a: accounts.set_disabled(ctx, label, True, a), f"已停用 {label}",
                              back="/accounts?status=disabled")


    @app.post("/accounts/{label}/enable")
    def account_enable(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(admin, lambda a: accounts.set_disabled(ctx, label, False, a), f"已启用 {label}")


    @app.post("/accounts/{label}/delete")
    def account_delete(label: str, confirmation: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        return account_action(admin, lambda a: accounts.delete_account(ctx, label, confirmation, a), f"已删除 {label}",
                              back="/accounts?status=disabled")


    def republish(label: str, actor: str) -> None:
        accounts.republish_account(ctx, label)
        events.audit(ctx.conn, actor, "account.republish", label, now=ctx.now())


    @app.post("/accounts/{label}/republish")
    def account_republish(label: str, admin: tuple[str, str] = Depends(current_admin)):
        return account_action(admin, lambda a: republish(label, a), f"已重新发布 {label}")

