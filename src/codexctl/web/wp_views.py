from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import events, site_probe, site_registry, wp_center
from .app import current_admin

JOB_STATES = {
    "planned": ("预检完成", "info"), "queued": ("排队中", "info"),
    "deploying": ("建站中", "info"), "running": ("建站中", "info"),
    "ssl_pending": ("重试证书中", "info"), "deployed": ("已完成", "ok"),
    "deployed_with_warnings": ("完成，有提醒", "warn"), "failed": ("失败", "bad"),
}
SITE_STATES = {
    "ready": ("待建站", "info"), "deployed": ("已建成", "ok"),
    "deployed_with_warnings": ("已建成，有提醒", "warn"), "failed": ("建站失败", "bad"),
    "skipped": ("已跳过", "warn"), "invalid": ("预检失败", "bad"),
}
SSL_STATES = {
    "installed": ("已安装", "ok"), "failed": ("签发或安装失败", "bad"),
    "pending": ("证书重试中", "info"), "partial": ("部分域名未覆盖", "warn"),
    "disabled": ("未申请", "info"), "retry_failed": ("重试失败", "bad"),
}


def page_url(server_id: str, page: int = 1) -> str:
    return "/wp-center?" + urlencode({"server_id": server_id, **({"page": page} if page > 1 else {})})


def domain_name(value: str) -> str:
    if len(value) > 253 or not (domain := site_probe.hostname(value.strip())):
        raise wp_center.CenterError("请输入有效域名，例如 shop.example.com")
    return domain


def register(app) -> None:
    ctx, lock, render = app.state.ctx, app.state.lock, app.state.render

    def require_server(server_id: str):
        with lock:
            server = site_registry.get(ctx.conn, server_id)
        if not server:
            raise HTTPException(404, "服务器不存在")
        return server

    def call(server, operation, **fields):
        return wp_center.request(server, ctx.settings.wp_ssh_key, operation, **fields)

    @app.get("/wp-center", response_class=HTMLResponse)
    def index(
        request: Request, server_id: str = "", page: int = 1,
        admin: tuple[str, str] = Depends(current_admin),
    ):
        with lock:
            servers = site_registry.list_servers(ctx.conn)
        selected = require_server(server_id) if server_id else next(iter(servers), None)
        return render(request, "wp_center.html", admin=admin[0], csrf=admin[1], servers=servers,
                      server=selected, page=max(1, page), page_url=page_url)

    @app.get("/wp-center/{server_id}/catalog")
    def catalog(server_id: str, admin: tuple[str, str] = Depends(current_admin)):
        server = require_server(server_id)
        try:
            return call(server, "catalog")
        except wp_center.CenterError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/wp-center/{server_id}/template")
    def template(server_id: str, template: str, admin: tuple[str, str] = Depends(current_admin)):
        server = require_server(server_id)
        try:
            return call(server, "inspect", template=template)
        except wp_center.CenterError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/wp-center/{server_id}/history", response_class=HTMLResponse)
    def history(
        request: Request, server_id: str, page: int = 1,
        admin: tuple[str, str] = Depends(current_admin),
    ):
        server = require_server(server_id)
        try:
            data = call(server, "history", page=page)
        except wp_center.CenterError as exc:
            return render(request, "wp_history.html", 502, flash=False, history_error=str(exc))
        return render(
            request, "wp_history.html", flash=False, csrf=admin[1], server=server, jobs=data["jobs"],
            page=page, more=data["more"], page_url=page_url, job_states=JOB_STATES,
            site_states=SITE_STATES, ssl_states=SSL_STATES,
        )

    @app.post("/wp-center/{server_id}/create")
    def create(
        server_id: str, domain: str = Form(""), template: str = Form(""), sql: str = Form(""),
        old_domain: str = Form(""), www: bool = Form(False),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        server = require_server(server_id)
        try:
            domain = domain_name(domain)
            result = call(
                server, "create", domain=domain, template=template, sql=sql,
                old_domain=domain_name(old_domain) if old_domain.strip() else "", www=www,
            )
        except wp_center.CenterError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        with lock:
            events.audit(ctx.conn, f"web:{admin[0]}", "wp.create", domain, result["id"], now=ctx.now())
        return {
            "message": ("已提交建站任务 " if result.get("auto_started") else "已保存预检结果 ") + result["id"],
            "history": page_url(server_id) + "#wp-tasks",
        }

    @app.post("/wp-center/{server_id}/retry-ssl")
    def retry_ssl(
        server_id: str, job: str = Form(""), domain: str = Form(""), page: int = Form(1),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        server = require_server(server_id)
        try:
            domain = domain_name(domain)
            call(server, "retry_ssl", job=job, domain=domain)
        except wp_center.CenterError as exc:
            query = urlencode({"err": str(exc)})
        else:
            with lock:
                events.audit(ctx.conn, f"web:{admin[0]}", "wp.retry_ssl", domain, job, now=ctx.now())
            query = urlencode({"msg": "已提交 SSL 证书重试"})
        return RedirectResponse(f"{page_url(server_id, page)}&{query}#wp-tasks", status_code=303)
