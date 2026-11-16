from __future__ import annotations

from collections import Counter
from datetime import datetime
from itertools import groupby
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import site_registry, sites
from .app import current_admin

PAGE_SIZE = 50
STATUSES = {
    "up": ("可访问", "ok"),
    "http_only": ("仅 HTTP", "warn"),
    "restricted": ("访问受限", "warn"),
    "redirect": ("跳转至其他域名", "warn"),
    "down": ("不可访问", "bad"),
    "pending": ("待检测", "info"),
    "stale": ("结果已过期", "warn"),
}
REASONS = {
    "dns": "DNS 无可用地址",
    "address": "DNS 返回非公网地址",
    "timeout": "请求超时",
    "tls": "TLS 握手或证书错误",
    "connect": "连接失败",
    "redirect_address": "无效的跳转地址",
    "redirect_loop": "重定向循环",
    "redirect_limit": "重定向次数过多",
    "challenge": "访问触发验证",
    "probe_error": "探测程序异常",
}


def _admin_only(admin: tuple[str, str] = Depends(current_admin)) -> tuple[str, str]:
    if admin[0] != "admin":
        raise HTTPException(403, "仅管理员可查看服务器")
    return admin


def _time(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, sites.BEIJING).strftime("%m-%d %H:%M") if ts is not None else ""


def _state(result: dict | None, now: int) -> str:
    if not result:
        return "pending"
    return "stale" if now - result["checked_at"] > sites.STALE_AFTER else result["state"]


def _server_state(server, inv, now: int) -> tuple[str, str]:
    if not server.host_key:
        return "待配置主机公钥", "info"
    if inv and inv.error:
        return "同步失败", "bad"
    if not inv:
        return "等待首次采集", "info"
    if inv.synced_at is not None and now - inv.synced_at > sites.STALE_AFTER:
        return "清单已过期", "warn"
    return "已同步", "ok"


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render

    def require_server(server_id: str):
        server = site_registry.get(ctx.conn, server_id)
        if not server:
            raise HTTPException(404, "服务器不存在")
        return server

    @app.get("/servers", response_class=HTMLResponse)
    def servers_page(
        request: Request, server_id: str = "", date: str = "", state: str = "", q: str = "", page: int = 1,
        admin: tuple[str, str] = Depends(_admin_only),
    ):
        with lock:
            servers = site_registry.list_servers(ctx.conn)
            inventories = sites.snapshots(ctx.conn)
        now = ctx.now()
        server_order = {server.id: index for index, server in enumerate(servers)}
        server_id = server_id if server_id in server_order else ""
        state = state if state in STATUSES else ""
        q = q.strip()[:253]
        summary, rows = [], []
        for server in servers:
            inv = inventories.get(server.id)
            summary.append({
                "server": server, "status": _server_state(server, inv, now),
                "count": len(inv.rows) if inv and inv.synced_at is not None else None,
                "synced_at": _time(inv.synced_at if inv else None), "error": inv.error if inv else None,
            })
            if not inv or (server_id and server_id != server.id):
                continue
            for row in inv.rows:
                status = _state(row["public"], now)
                rows.append({
                    **row, "server": server, "date": sites.deployment_date(row["created_at"]) or "unknown",
                    "domain": row["domains"][0], "status": status,
                    "origin_status": _state(row["origin"], now), "checked_at": _time(row["public"]["checked_at"]),
                })
        available_dates = sorted({row["date"] for row in rows}, key=lambda day: (day != "unknown", day), reverse=True)
        date = date if date in available_dates else ""
        filtered = [
            row for row in rows
            if (not date or row["date"] == date) and (not state or row["status"] == state)
            and (not q or any(q.casefold() in domain for domain in row["domains"]))
        ]
        server_totals = Counter(row["server"].id for row in filtered)
        day_totals = Counter((row["server"].id, row["date"]) for row in filtered)
        filtered.sort(key=lambda row: (row["domain"], row["id"]))
        filtered.sort(key=lambda row: (row["date"] != "unknown", row["date"]), reverse=True)
        filtered.sort(key=lambda row: server_order[row["server"].id])
        pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, pages))
        groups = []
        page_rows = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        for server, server_rows in groupby(page_rows, key=lambda row: row["server"]):
            days = [
                (day, list(day_rows), day_totals[server.id, day])
                for day, day_rows in groupby(server_rows, key=lambda row: row["date"])
            ]
            groups.append((server, days, server_totals[server.id]))
        counts = Counter(row["status"] for row in rows)
        last_check = max((row["public"]["checked_at"] for row in rows), default=None)
        query = {key: val for key, val in {"server_id": server_id, "date": date, "state": state, "q": q}.items() if val}

        def page_url(number: int) -> str:
            return "/servers?" + urlencode({**query, "page": number})

        return render(
            request, "servers.html", admin=admin[0], csrf=admin[1], summary=summary, groups=groups,
            server_id=server_id, date=date, state=state, q=q, available_dates=available_dates, statuses=STATUSES,
            reasons=REASONS, total=len(rows), filtered=len(filtered),
            anomalies=sum(counts[key] for key in ("down", "restricted", "redirect", "http_only")),
            stale=counts["stale"] + counts["pending"], last_check=_time(last_check), page=page, pages=pages,
            previous=page_url(page - 1) if page > 1 else None, next_page=page_url(page + 1) if page < pages else None,
        )

    def editor(request, admin, server=None, fields=None, error=""):
        if fields is None:
            fields = dict(name=server.name, host=server.host, port=server.port, host_key=server.host_key or "") if server else {
                "name": "", "host": "", "port": 22, "host_key": "",
            }
        return render(
            request, "server_form.html", 422 if error else 200, flash=False, admin=admin[0], csrf=admin[1],
            server=server, fields=fields, err=error,
        )

    @app.get("/servers/new", response_class=HTMLResponse)
    def new_server(request: Request, admin: tuple[str, str] = Depends(_admin_only)):
        return editor(request, admin)

    @app.post("/servers")
    def add_server(
        request: Request, name: str = Form(""), host: str = Form(""), port: str = Form(""),
        host_key: str = Form(""), admin: tuple[str, str] = Depends(_admin_only),
    ):
        try:
            with lock:
                server = site_registry.create(ctx, f"web:{admin[0]}", name, host, port, host_key)
        except site_registry.ServerError as exc:
            return editor(request, admin, fields=dict(name=name, host=host, port=port, host_key=host_key), error=str(exc))
        return RedirectResponse(f"/servers/{server.id}", status_code=303)

    @app.get("/servers/{server_id}", response_class=HTMLResponse)
    def server_details(request: Request, server_id: str, admin: tuple[str, str] = Depends(_admin_only)):
        with lock:
            server = require_server(server_id)
            inv = sites.snapshots(ctx.conn).get(server_id)
        return render(
            request, "server_detail.html", admin=admin[0], csrf=admin[1], server=server,
            sync=_server_state(server, inv, ctx.now()), synced_at=_time(inv.synced_at if inv else None),
            sync_error=inv.error if inv else None, count=len(inv.rows) if inv and inv.synced_at is not None else None,
        )

    @app.get("/servers/{server_id}/edit", response_class=HTMLResponse)
    def edit_server(request: Request, server_id: str, admin: tuple[str, str] = Depends(_admin_only)):
        with lock:
            server = require_server(server_id)
        return editor(request, admin, server)

    @app.post("/servers/{server_id}/edit")
    def save_server(
        request: Request, server_id: str, name: str = Form(""), host: str = Form(""), port: str = Form(""),
        host_key: str = Form(""), admin: tuple[str, str] = Depends(_admin_only),
    ):
        with lock:
            server = require_server(server_id)
            try:
                site_registry.update(ctx, f"web:{admin[0]}", server, name, host, port, host_key)
            except site_registry.ServerError as exc:
                return editor(request, admin, server, dict(name=name, host=host, port=port, host_key=host_key), str(exc))
        return RedirectResponse(f"/servers/{server.id}", status_code=303)

    @app.post("/servers/{server_id}/delete")
    def remove_server(server_id: str, admin: tuple[str, str] = Depends(_admin_only)):
        with lock:
            server = require_server(server_id)
            site_registry.delete(ctx, f"web:{admin[0]}", server)
        return RedirectResponse("/servers", status_code=303)
