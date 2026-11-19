from __future__ import annotations

import hmac
import ipaddress
import re

from fastapi import Depends, Form, HTTPException, Request

import codexctl.users.webauth as webauth
from codexctl.context import Context
from codexctl.formatting import clean_text
from codexctl.users.sysusers import UserError

SESSION_COOKIE = "codexctl_session"
_LOCAL_HOSTS = {"127.0.0.1", "::1"}


def _valid_ip(value: str) -> str | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip(request: Request) -> str:
    client = request.client
    host = client.host if client else None
    if client is None or host in _LOCAL_HOSTS:
        real = _valid_ip(request.headers.get("x-real-ip", ""))
        if real:
            return real
    if host is None:
        return "unknown"
    return _valid_ip(host) or clean_text(host)


def _system_registration(ctx: Context, username: str) -> tuple[bool, int | None]:
    if not ctx.settings.manages_local_codex or username in {"admin", "root"}:
        return False, None
    uid = webauth.enrolled_system_uid(ctx.conn, username)
    return uid is not None or username in ctx.settings.web_system_users, uid


def _system_uid(ctx: Context, username: str, enrolled_uid: int | None) -> int | None:
    try:
        uid = ctx.lookup_user(username, max(1, ctx.settings.min_uid)).uid
    except (UserError, OSError):
        return None
    return uid if enrolled_uid is None or uid == enrolled_uid else None


def current_user(request: Request) -> tuple[str, str]:
    app = request.app
    token = request.cookies.get(SESSION_COOKIE)
    ctx: Context = app.state.ctx
    found = None
    if token:
        with app.state.lock:
            found = webauth.get_session(ctx.conn, token, now=ctx.now())
            if found is not None:
                system_login, enrolled_uid = _system_registration(ctx, found.username)
    if found is not None:
        if system_login:
            if found.system_uid is not None and found.system_uid == _system_uid(ctx, found.username, enrolled_uid):
                return found.username, found.csrf
        elif found.system_uid is None:
            return found.username, found.csrf
    if request.method == "GET":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    raise HTTPException(status_code=403, detail="未登录")


def current_admin(user: tuple[str, str] = Depends(current_user)) -> tuple[str, str]:
    if user[0] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以访问")
    return user


def session_csrf(request: Request, csrf: str = Form("")) -> tuple[str, str]:
    """POST 路由依赖：要求已登录且表单 csrf 与会话一致，返回 (username, csrf)。"""
    username, expected = current_user(request)
    if not csrf or not hmac.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return username, expected


def csrf_guard(request: Request, csrf: str = Form("")) -> None:
    """全局依赖：除 /login 外的所有写请求都必须带有效会话和 csrf。"""
    path = request.url.path
    settings = request.app.state.ctx.settings
    users_path = path == "/users" or path.startswith("/users/")
    if not settings.manages_local_codex and (
        users_path and (not settings.gateway_enabled or path.endswith(("/fix", "/restart-codex")))
        or path.startswith(("/accounts/login/", "/accounts/login"))
        or path.startswith("/accounts/") and path.endswith("/republish")
    ):
        raise HTTPException(status_code=404)
    if request.method in ("GET", "HEAD", "OPTIONS") or request.url.path == "/login":
        return
    if request.method == "POST" and path in ("/site-enrollment/install", "/site-enrollment/complete"):
        return  # These endpoints authenticate using a short-lived enrollment bearer token.
    csrf = csrf or request.headers.get("X-CSRFToken", "")
    username, _ = session_csrf(request, csrf)
    user_task_post = request.method == "POST" and (
        path == "/tasks/import" or re.fullmatch(r"/tasks/[0-9a-f]{32}/(?:confirm|cancel|prompt)", path)
    )
    user_site_post = request.method == "POST" and (
        re.fullmatch(r"/servers/[0-9a-f]{32}/sites/[^/]+/(?:probe|waf|state|remark)", path)
        or re.fullmatch(r"/servers/[0-9a-f]{32}/wp-center/proxy/(?:plugin|config)", path)
    )
    if path != "/logout" and username != "admin" and not user_task_post and not user_site_post:
        raise HTTPException(status_code=403, detail="仅管理员可以修改")

