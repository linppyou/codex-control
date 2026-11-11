import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

from codexctl import webauth
from codexctl.web.app import create_app

PW = "correct-horse-battery"


@pytest.fixture
def client(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    return TestClient(create_app(ctx), follow_redirects=False)


def do_login(client, username="root_admin", password=PW, **kw):
    client.get("/login")
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf": client.cookies.get("codexctl_prelogin")},
        **kw,
    )


def audits(ctx):
    return [tuple(r) for r in ctx.conn.execute("SELECT actor, action, target, detail FROM audit_log")]


def test_requires_login(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/logout", data={"csrf": "x"}).status_code == 403


def test_login_success(client, ctx):
    r = do_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200
    assert ("root_admin", "web.login", "testclient", "") in audits(ctx)
    assert 'style="' not in client.get("/").text


def test_login_requires_prelogin_csrf(client):
    client.get("/login")
    assert client.post("/login", data={"username": "root_admin", "password": PW, "csrf": "bad"}).status_code == 403
    assert client.post("/login", data={"username": "root_admin", "password": PW}).status_code == 403


def test_login_failure_audited(client, ctx):
    r = do_login(client, password="wrong-password-xx")
    assert r.status_code == 200 and "用户名或密码错误" in r.text
    r = do_login(client, username="ev\nil\x00")
    assert ("web", "web.login_failed", "testclient", "root_admin") in audits(ctx)
    assert all("\n" not in a[3] and "\x00" not in a[3] for a in audits(ctx))


def test_rate_limit_ip(client):
    for _ in range(5):
        assert do_login(client, username=f"u{_}", password="wrong-password-xx").status_code == 200
    r = do_login(client)
    assert r.status_code == 429 and "尝试过多" in r.text


def test_rate_limit_per_username_across_ips(client):
    for i in range(5):
        r = do_login(client, username="Root_Admin", password="wrong-password-xx", headers={"X-Real-IP": f"9.9.9.{i}"})
        assert r.status_code == 200
    assert do_login(client, username="ROOT_ADMIN").status_code == 429


def test_overlong_credentials_fail(client):
    assert do_login(client, password="x" * 2000).status_code == 200
    assert do_login(client, username="a" * 65).status_code == 200


def test_logout_needs_csrf(client):
    do_login(client)
    assert client.post("/logout", data={}).status_code == 403
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    page = client.get("/").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/logout", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "style=" not in r.text


def test_real_ip_not_trusted_from_remote(client, ctx):
    do_login(client, password="wrong-password-xx", headers={"X-Real-IP": "1.2.3.4"})
    assert ("web", "web.login_failed", "testclient", "root_admin") in audits(ctx)


def test_real_ip_trusted_from_loopback(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    c = TestClient(create_app(ctx), client=("127.0.0.1", 5000), follow_redirects=False)
    do_login(c, password="wrong-password-xx", headers={"X-Real-IP": "1.2.3.4"})
    assert ("web", "web.login_failed", "1.2.3.4", "root_admin") in audits(ctx)


def test_docs_disabled_and_static(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/static/style.css").status_code == 200


def test_flash_escaped(client):
    do_login(client)
    r = client.get("/?msg=<script>alert(1)</script>")
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text
