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

import codexctl.db as db
import codexctl.events as events
import codexctl.gateway.api as gateway_api
import codexctl.gateway.storage as storage
import codexctl.usage.pricing as pricing
import codexctl.users.pam as pam
import codexctl.users.webauth as webauth
from codexctl.accounts.devicelogin import DeviceLoginManager
from codexctl.context import Context
from codexctl.formatting import clean_text
from codexctl.gateway.upstream import Gateway
from codexctl.users.sysusers import UserError
from codexctl.users.webauth import (
    MAX_LOGIN_PASSWORD_LEN,
    MAX_LOGIN_USERNAME_LEN,
    SESSION_TTL,
    LoginRateLimiter,
)
from codexctl.web.charts import fmt_tokens
from codexctl.web.dependencies import (
    SESSION_COOKIE,
    _system_registration,
    _system_uid,
    _valid_ip,
    client_ip,
    csrf_guard,
    current_admin,
    current_user,
    session_csrf,
)
from codexctl.web.navigation import navigation

PRELOGIN_COOKIE = "codexctl_prelogin"
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
                security_headers = SECURITY_HEADERS
                path = scope.get("path", "")
                if path.startswith("/servers/") and path.endswith("/wp-center/document"):
                    # The target server supplies executable HTML and is outside the panel's trust boundary.
                    security_headers = {
                        **{k: v for k, v in SECURITY_HEADERS.items() if k != "X-Frame-Options"},
                        "Content-Security-Policy": (
                            "sandbox allow-scripts allow-downloads; default-src 'none'; "
                            "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                            "img-src data: blob: https:; font-src data:; "
                            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
                        ),
                    }
                headers += [(k.lower().encode(), v.encode()) for k, v in security_headers.items()]
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
        if context["admin"]:
            context["navigation"] = navigation(
                request, context["admin"], context["is_admin"], context["managed"], context["gateway_enabled"]
            )
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

    @app.get("/wp-center")
    def wp_center_redirect(admin: tuple[str, str] = Depends(current_admin)) -> RedirectResponse:
        return RedirectResponse("/servers", status_code=303)

    import codexctl.web.routes.dispatch as dispatch_views
    import codexctl.web.routes.servers as server_views
    import codexctl.web.views as views

    views.register(app)
    server_views.register(app)
    dispatch_views.register(app)
    if gateway:
        import codexctl.web.routes.gateway as gateway_views

        gateway_views.register(app, gateway)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
