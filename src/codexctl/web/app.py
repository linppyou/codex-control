from __future__ import annotations

import hmac
import re
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import events, webauth
from ..context import Context
from ..devicelogin import DeviceLoginManager
from ..webauth import (
    MAX_LOGIN_PASSWORD_LEN,
    MAX_LOGIN_USERNAME_LEN,
    SESSION_TTL,
    LoginRateLimiter,
)

SESSION_COOKIE = "codexctl_session"
PRELOGIN_COOKIE = "codexctl_prelogin"
_LOCAL_HOSTS = {"127.0.0.1", "::1"}
_NONPRINTABLE = re.compile(r"[^\x20-\x7e -\U0010ffff]|[  ​-‏‪-‮]")

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; "
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
    return _NONPRINTABLE.sub("?", value)[:limit]


def client_ip(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    if host in _LOCAL_HOSTS:
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return clean_text(real, 64)
    return host


def current_admin(request: Request) -> tuple[str, str]:
    app = request.app
    token = request.cookies.get(SESSION_COOKIE)
    ctx: Context = app.state.ctx
    found = None
    if token:
        with app.state.lock:
            found = webauth.get_session(ctx.conn, token, now=ctx.now())
    if found is None:
        if request.method == "GET":
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        raise HTTPException(status_code=403, detail="未登录")
    return found


def admin_csrf(request: Request, csrf: str = Form("")) -> tuple[str, str]:
    """POST 路由依赖：要求已登录且表单 csrf 与会话一致，返回 (username, csrf)。"""
    username, expected = current_admin(request)
    if not csrf or not hmac.compare_digest(csrf.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return username, expected


def create_app(
    ctx: Context,
    *,
    devlogins: DeviceLoginManager | None = None,
    limiter: LoginRateLimiter | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    base = Path(__file__).parent
    app.state.ctx = ctx
    app.state.lock = threading.Lock()
    app.state.devlogins = devlogins if devlogins is not None else DeviceLoginManager(ctx.settings, clock=ctx.clock)
    app.state.limiter = limiter if limiter is not None else LoginRateLimiter()
    app.state.templates = Jinja2Templates(directory=str(base / "templates"))
    app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    def render(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        context.setdefault("admin", None)
        context.setdefault("csrf", "")
        context["msg"] = request.query_params.get("msg", "")
        context["err"] = request.query_params.get("err", "")
        return app.state.templates.TemplateResponse(request, name, context, status_code=status)

    app.state.render = render

    def login_page(request: Request, status: int = 200, err: str = "") -> HTMLResponse:
        token = secrets.token_urlsafe(32)
        resp = render(request, "login.html", status, prelogin=token, login_err=err)
        resp.set_cookie(
            PRELOGIN_COOKIE, token, httponly=True, samesite="strict",
            secure=ctx.settings.web_cookie_secure, path="/", max_age=3600,
        )
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
        with app.state.lock:
            now = ctx.now()
            lim: LoginRateLimiter = app.state.limiter
            if not (lim.allowed(ip, now) and lim.allowed(ukey, now)):
                return login_page(request, 429, "尝试过多，请 15 分钟后再试")
            ok = (
                len(password) <= MAX_LOGIN_PASSWORD_LEN
                and len(username) <= MAX_LOGIN_USERNAME_LEN
                and webauth.verify_login(ctx.conn, username, password)
            )
            if not ok:
                lim.record_failure(ip, now)
                lim.record_failure(ukey, now)
                events.audit(ctx.conn, "web", "web.login_failed", ip, clean_text(username), now=now)
                return login_page(request, 200, "用户名或密码错误")
            lim.reset(ip)
            lim.reset(ukey)
            token, _ = webauth.create_session(ctx.conn, username, now=now)
            events.audit(ctx.conn, username, "web.login", ip, now=now)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="strict",
            secure=ctx.settings.web_cookie_secure, path="/", max_age=SESSION_TTL,
        )
        resp.delete_cookie(PRELOGIN_COOKIE, path="/")
        return resp

    @app.post("/logout")
    def logout(request: Request, _admin: tuple[str, str] = Depends(admin_csrf)):
        token = request.cookies.get(SESSION_COOKIE, "")
        with app.state.lock:
            webauth.delete_session(ctx.conn, token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1])

    return app
