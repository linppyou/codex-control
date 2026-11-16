from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
import threading
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, events, pam, pricing, webauth
from ..context import Context
from ..gateway import api as gateway_api
from ..gateway import storage
from ..gateway.upstream import Gateway
from .charts import fmt_tokens
from ..devicelogin import DeviceLoginManager
from ..sysusers import UserError
from ..webauth import (
    MAX_LOGIN_PASSWORD_LEN,
    MAX_LOGIN_USERNAME_LEN,
    SESSION_TTL,
    LoginRateLimiter,
)

SESSION_COOKIE = "codexctl_session"
PRELOGIN_COOKIE = "codexctl_prelogin"
_LOCAL_HOSTS = {"127.0.0.1", "::1"}
log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
}


def clean_text(value: str, limit: int = 64) -> str:
    return "".join(
        "?" if (unicodedata.category(c)[0] in "CZ" and c != " ") else c for c in value[:limit]
    )


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
    username, _ = session_csrf(request, csrf)
    user_task_post = request.method == "POST" and (
        path == "/tasks/preview" or re.fullmatch(r"/tasks/[0-9a-f]{32}/confirm", path)
    )
    if path != "/logout" and username != "admin" and not user_task_post:
        raise HTTPException(status_code=403, detail="仅管理员可以修改")


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.decode().lower() not in _HEADER_NAMES]
                headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


_HEADER_NAMES = {k.lower() for k in SECURITY_HEADERS}


def create_app(
    ctx: Context,
    *,
    devlogins: DeviceLoginManager | None = None,
    limiter: LoginRateLimiter | None = None,
    gateway: Gateway | None = None,
) -> FastAPI:
    stored_admin = webauth.get_hash(ctx.conn, "admin")
    if ctx.settings.gateway_enabled or not ctx.settings.manages_local_codex:
        if not webauth.has_configured_admin(ctx.conn):
            raise webauth.AuthError("启用网关前，请通过 codexctl admin create 或 passwd admin 配置管理员密码")
    elif stored_admin is None:
        try:
            webauth.create_admin(ctx.conn, "admin", webauth.DEFAULT_ADMIN_PASSWORD, now=ctx.now(), configured=False)
        except webauth.AuthError:
            if webauth.get_hash(ctx.conn, "admin") is None:
                raise
    manager = devlogins if devlogins is not None else DeviceLoginManager(ctx.settings, clock=ctx.clock)
    gateway = (gateway or Gateway(ctx)) if ctx.settings.gateway_enabled else None
    if gateway:
        users = (u.name for u in ctx.list_users(ctx.settings.min_uid)) if ctx.settings.manages_local_codex else (
            name for name in webauth.list_admins(ctx.conn) if name != "admin"
        )
        with db.transaction(ctx.conn):
            for username in users:
                storage.ensure_user_key(ctx.conn, ctx.vault, username, "system", ctx.now())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if gateway:
                await gateway.start()
            yield
        finally:
            manager.close()  # 关闭时取消所有进行中的 device-auth 登录
            if gateway:
                await gateway.close()

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(csrf_guard)], lifespan=lifespan
    )
    base = Path(__file__).parent
    app.state.ctx = ctx
    app.state.lock = threading.Lock()
    app.state.devlogins = manager
    app.state.limiter = limiter if limiter is not None else LoginRateLimiter()
    app.state.user_limiter = LoginRateLimiter(max_failures=30, window=900)
    app.state.templates = Jinja2Templates(directory=str(base / "templates"))
    app.state.templates.env.filters["tokens"] = fmt_tokens
    app.state.templates.env.filters["usd"] = pricing.fmt_usd
    app.state.templates.env.filters["rate"] = pricing.fmt_rate
    app.state.templates.env.filters["rate_value"] = pricing.fmt_rate_value
    app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")
    if gateway:
        app.mount("/v1", gateway_api.create_api(gateway), name="api")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=301)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error: %s %s", request.method, request.url.path, exc_info=exc)
        return PlainTextResponse("内部错误", status_code=500, headers=dict(SECURITY_HEADERS))

    def render(request: Request, name: str, status: int = 200, flash: bool = True, **context) -> HTMLResponse:
        context.setdefault("admin", None)
        context.setdefault("csrf", "")
        context.setdefault("is_admin", context["admin"] == "admin")
        context.setdefault("gateway_enabled", bool(gateway))
        context.setdefault("managed", ctx.settings.manages_local_codex)
        context.setdefault("msg", request.query_params.get("msg", "") if flash else "")
        context.setdefault("err", request.query_params.get("err", "") if flash else "")
        return app.state.templates.TemplateResponse(request, name, context, status_code=status)

    app.state.render = render

    def cookie_attrs() -> dict:
        return {"httponly": True, "samesite": "strict", "secure": ctx.settings.web_cookie_secure, "path": "/"}

    def login_page(request: Request, status: int = 200, err: str = "") -> HTMLResponse:
        token = secrets.token_urlsafe(32)
        resp = render(request, "login.html", status, False, prelogin=token, login_err=err)
        resp.set_cookie(PRELOGIN_COOKIE, token, max_age=3600, **cookie_attrs())
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        return login_page(request)

    @app.post("/login")
    def login_post(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        csrf: str = Form(""),
    ):
        pre = request.cookies.get(PRELOGIN_COOKIE, "")
        if not pre or not csrf or not hmac.compare_digest(pre.encode(), csrf.encode()):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        ip = client_ip(request)
        ukey = "user:" + username.lower()[:MAX_LOGIN_USERNAME_LEN]
        lim: LoginRateLimiter = app.state.limiter
        ulim: LoginRateLimiter = app.state.user_limiter
        with app.state.lock:
            now = ctx.now()
            if not (lim.allowed(ip, now) and ulim.allowed(ukey, now)):
                return login_page(request, 429, "尝试过多，请 15 分钟后再试")
            # 先占用尝试次数，成功后清除，避免并发请求绕过限速。
            lim.record_failure(ip, now)
            ulim.record_failure(ukey, now)
            system_login, enrolled_uid = _system_registration(ctx, username)
            stored = None if system_login else webauth.get_hash(ctx.conn, username)
        # 密码验证放在全局锁之外。
        system_uid = None
        if system_login:
            system_uid = _system_uid(ctx, username, enrolled_uid)
            ok = (
                system_uid is not None and len(password) <= MAX_LOGIN_PASSWORD_LEN
                and pam.verify_password(username, password)
            )
        else:
            ok = webauth.check_password(stored, password)
        with app.state.lock:
            now = ctx.now()
            if not ok:
                events.audit(ctx.conn, "web", "web.login_failed", ip, clean_text(username, 32), now=now)
                return login_page(request, 200, "用户名或密码错误")
            if stored is not None:
                webauth.rehash_if_needed(ctx.conn, username, stored, password)
            lim.reset(ip)
            ulim.reset(ukey)
            old = request.cookies.get(SESSION_COOKIE)
            if old:
                webauth.delete_session(ctx.conn, old)
            token, _ = webauth.create_session(ctx.conn, username, now=now, system_uid=system_uid)
            events.audit(ctx.conn, username, "web.login", ip, now=now)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, **cookie_attrs())
        resp.delete_cookie(PRELOGIN_COOKIE, **cookie_attrs())
        return resp

    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE, "")
        with app.state.lock:
            webauth.delete_session(ctx.conn, token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, **cookie_attrs())
        return resp

    from . import dispatch_views, server_views, views

    views.register(app)
    server_views.register(app)
    dispatch_views.register(app)
    if gateway:
        from . import gateway as gateway_views

        gateway_views.register(app, gateway)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
