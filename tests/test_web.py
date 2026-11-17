import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

import codexctl.repo as repo
import codexctl.users.pam as pam
import codexctl.users.webauth as webauth
from codexctl.web.app import create_app

PW = "correct-horse-battery"


@pytest.fixture
def client(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
    return TestClient(create_app(ctx), follow_redirects=False)


def do_login(client, username="admin", password=PW, **kw):
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


def test_wp_center_links_to_external_service(client):
    response = client.get("/wp-center")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    do_login(client)
    page = client.get("/")
    assert 'href="https://ai.zhituowujie.shop/" target="_blank" rel="noopener noreferrer"' in page.text
    assert 'WP 服务中心 ↗</a>' in page.text
    response = client.get("/wp-center?server_id=old-server")
    assert response.status_code == 303
    assert response.headers["location"] == "https://ai.zhituowujie.shop/"
    assert client.get("/wp-center/old-server/catalog").status_code == 404
    assert client.get("/wp-center/old-server/history").status_code == 404
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    for operation in ("create", "retry-ssl"):
        assert client.post(f"/wp-center/old-server/{operation}", data={"csrf": csrf}).status_code == 404


def test_login_success(client, ctx):
    r = do_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200
    assert ("admin", "web.login", "testclient", "") in audits(ctx)
    assert 'style="' not in client.get("/").text


def test_login_requires_prelogin_csrf(client):
    client.get("/login")
    assert client.post("/login", data={"username": "admin", "password": PW, "csrf": "bad"}).status_code == 403
    assert client.post("/login", data={"username": "admin", "password": PW}).status_code == 403


def test_login_failure_audited(client, ctx):
    r = do_login(client, password="wrong-password-xx")
    assert r.status_code == 200 and "用户名或密码错误" in r.text
    r = do_login(client, username="ev\nil\x00")
    assert ("web", "web.login_failed", "testclient", "admin") in audits(ctx)
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
    assert ("web", "web.login_failed", "testclient", "admin") in audits(ctx)


def test_real_ip_trusted_from_loopback(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
    c = TestClient(create_app(ctx), client=("127.0.0.1", 5000), follow_redirects=False)
    do_login(c, password="wrong-password-xx", headers={"X-Real-IP": "1.2.3.4"})
    assert ("web", "web.login_failed", "1.2.3.4", "admin") in audits(ctx)


def test_docs_disabled_and_static(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/static/style.css").status_code == 200


def test_flash_escaped(client):
    do_login(client)
    r = client.get("/?msg=<script>alert(1)</script>")
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_every_post_route_enforces_csrf(client):
    from fastapi.routing import APIRoute

    do_login(client)
    posts = [r for r in client.app.routes if isinstance(r, APIRoute) and "POST" in r.methods and r.path != "/login"]
    assert posts
    for r in posts:
        assert client.post(r.path, data={}).status_code == 403, r.path
        assert client.post(r.path, data={"csrf": "bad"}).status_code == 403, r.path


def test_unhandled_error_has_headers_and_no_traceback(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    app = create_app(ctx)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-detail")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom")
    assert r.status_code == 500 and r.text == "内部错误"
    assert "secret-detail" not in r.text and "Traceback" not in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_login_page_ignores_query_flash(client):
    r = client.get("/login?msg=hello&err=evil")
    assert "hello" not in r.text and "evil" not in r.text


def test_cookie_attrs_on_delete_and_old_session_removed(client, ctx):
    do_login(client)
    old = client.cookies.get("codexctl_session")
    assert ctx.conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1
    do_login(client)
    assert ctx.conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1
    assert client.cookies.get("codexctl_session") != old
    page = client.get("/").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    sc = client.post("/logout", data={"csrf": csrf}).headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc and "path=/" in sc


def test_client_ip_rules(ctx):
    from codexctl.web.app import clean_text, client_ip

    class Req:
        def __init__(self, host, real=None):
            self.client = None if host is None else type("C", (), {"host": host})()
            self.headers = {"x-real-ip": real} if real else {}

    assert client_ip(Req(None, "1.2.3.4")) == "1.2.3.4"
    assert client_ip(Req("::1", "1.2.3.4")) == "1.2.3.4"
    assert client_ip(Req("127.0.0.1", "garbage")) == "127.0.0.1"
    assert client_ip(Req("8.8.8.8", "1.2.3.4")) == "8.8.8.8"
    assert client_ip(Req(None, "x")) == "unknown"
    assert client_ip(Req("127.0.0.1", "2001:db8::1:2")) == client_ip(Req("127.0.0.1", "2001:db8::9"))
    assert clean_text("a‮b\x00c\td e") == "a?b?c?d e"


def test_user_limiter_threshold_is_30(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
    app = create_app(ctx)
    c = TestClient(app, client=("127.0.0.1", 1), follow_redirects=False)
    for i in range(30):
        r = do_login(c, password="wrong-password-xx", headers={"X-Real-IP": f"10.0.{i // 5}.{i}"})
        assert r.status_code == 200, i
    assert do_login(c, headers={"X-Real-IP": "10.9.9.9"}).status_code == 429


def test_failure_detail_truncated(client, ctx):
    do_login(client, username="a" * 60)
    assert ("web", "web.login_failed", "testclient", "a" * 32) in audits(ctx)


def test_favicon_public(client):
    r = client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/static/favicon.svg"
    svg = client.get("/static/favicon.svg")
    assert svg.status_code == 200 and "image/svg+xml" in svg.headers["content-type"]
    assert 'rel="icon"' in client.get("/login").text


def test_existing_web_user_is_limited_to_own_panel(client, ctx):
    webauth.create_admin(ctx.conn, "linpp", PW, now=ctx.now())
    assert do_login(client, username="linpp").status_code == 303
    assert client.get("/").headers["location"] == "/users/linpp"
    page = client.get("/users/linpp")
    assert page.status_code == 200
    assert "用户面板" in page.text and 'href="/tasks"' in page.text
    assert 'method="post" action="/logout"' in page.text
    assert client.get("/users/alice").status_code == 403
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/logout", data={"csrf": csrf}).status_code == 303


def test_system_users_authenticate_with_pam_and_only_see_their_own_data(ctx, monkeypatch):
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False, web_system_users=("alice", "bob"))
    for name in ("alice", "bob", "linpp"):
        ctx.test_users.add(name)
        delta = dict.fromkeys(repo.USAGE_FIELDS, 17)
        repo.insert_usage(ctx.conn, name, None, ctx.now(), delta, f"{name}-session.jsonl", f"{name}-model")
    system_passwords = {"alice": "alice-pw", "bob": "bob-pw", "linpp": "linpp-pw"}
    monkeypatch.setattr(pam, "verify_password", lambda name, password: system_passwords.get(name) == password)
    webauth.create_admin(ctx.conn, "alice", PW, now=ctx.now())
    app = create_app(ctx)
    with TestClient(app, follow_redirects=False) as client:
        old_token, _ = webauth.create_session(ctx.conn, "alice", now=ctx.now())
        client.cookies.set("codexctl_session", old_token)
        assert client.get("/").headers["location"] == "/login"
        assert do_login(client, "alice", PW).status_code == 200
        assert do_login(client, "linpp", "linpp-pw").status_code == 200
        for name, other in (("alice", "bob"), ("bob", "alice")):
            assert do_login(client, name, system_passwords[name]).status_code == 303
            assert client.get("/").headers["location"] == f"/users/{name}"
            page = client.get(f"/users/{name}")
            assert page.status_code == 200 and f"{name}-model" in page.text
            assert f"{other}-model" not in page.text and f"{other}-session.jsonl" not in page.text
            assert "我的数据" in page.text
            for path in (f"/users/{other}", f"/users/%20{other}", "/accounts", "/accounts/demo/quota",
                         "/accounts/demo/quota.csv", "/accounts/login/example/status", "/users", "/usage",
                         "/usage.csv", "/pricing", "/audit"):
                assert client.get(path).status_code == 403, path
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            for path in (f"/users/{name}/fix", "/users/assign", "/accounts/login", "/pricing/add"):
                assert client.post(path, data={"csrf": csrf}).status_code == 403, path

        system_passwords["bob"] = "new-password"
        assert do_login(client, "bob", "bob-pw").status_code == 200
        assert do_login(client, "bob", "new-password").status_code == 303
        ctx.settings = dataclasses.replace(ctx.settings, web_system_users=("alice",))
        assert client.get("/users/bob").headers["location"] == "/login"


def test_default_admin_created_once(ctx):
    create_app(ctx)
    assert webauth.verify_login(ctx.conn, "admin", "root@root.root")
    webauth.set_password(ctx.conn, "admin", PW, now=ctx.now())
    create_app(ctx)
    assert webauth.verify_login(ctx.conn, "admin", PW)
    assert not webauth.verify_login(ctx.conn, "admin", "root@root.root")
