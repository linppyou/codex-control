import dataclasses
import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from codexctl import accounts, events, repo, webauth
from codexctl.devicelogin import DeviceLoginManager
from codexctl.locks import account_lock
from codexctl.web.app import create_app
from fakes import make_auth_json
from test_web import PW, do_login


@pytest.fixture
def mgr(ctx, tmp_path):
    src = tmp_path / "auth_src.json"
    marker = tmp_path / "go"
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False, hidden_users=("svc",))
    body = (
        "#!/bin/sh\n"
        'printf "open\\n  https://auth.openai.com/codex/device\\ncode\\n  ABCD-EFGH12\\n"\n'
        f"while [ ! -e {marker} ]; do sleep 0.02; done\n"
        f'cp {src} "$CODEX_HOME/auth.json"\nexit 0\n'
    )
    ctx.settings.codex_bin.write_text(body)
    ctx.settings.codex_bin.chmod(0o755)
    m = DeviceLoginManager(ctx.settings, clock=time.time)
    m.src, m.marker = src, marker
    yield m
    for lid in list(m._logins):
        m.cancel(lid)
        if m._logins[lid].proc.poll() is None:
            m._logins[lid].proc.kill()
        m._logins[lid].proc.wait()


@pytest.fixture
def c(ctx, mgr):
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    client = TestClient(create_app(ctx, devlogins=mgr), follow_redirects=False)
    assert do_login(client).status_code == 303
    client.csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    return client


def post(c, path, **data):
    return c.post(path, data={"csrf": c.csrf, **data})


def qs(r):
    assert r.status_code == 303
    u = urlparse(r.headers["location"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


def add_acc(ctx, label="acc1", account_id="acct-1", email="a@example.com"):
    return accounts.import_account(ctx, label, make_auth_json(account_id=account_id, email=email), "t")


def test_pages_render(c, ctx):
    add_acc(ctx)
    repo.insert_quota_sample(ctx.conn, 1, ctx.now(), "primary", 42.4, 300, ctx.now() + 8000, "api")
    ctx.test_users.add("alice")
    for p in ("/", "/accounts", "/users", "/usage", "/audit"):
        r = c.get(p)
        assert r.status_code == 200 and 'style="' not in r.text, p
    dash = c.get("/").text
    assert "acc1" in dash and "a@example.com" in dash and "5小时" in dash and 'value="42"' in dash
    assert "2 小时 13 分后重置" in dash
    assert "acc1" in c.get("/accounts").text


def test_pages_require_login(ctx, mgr):
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    anon = TestClient(create_app(ctx, devlogins=mgr), follow_redirects=False)
    for p in ("/", "/accounts", "/users", "/usage", "/usage.csv", "/audit", "/accounts/login/x", "/accounts/login/x/status"):
        r = anon.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_users_hidden_and_all(c, ctx):
    ctx.test_users.add("alice")
    ctx.test_users.add("svc")
    assert "alice" in c.get("/users").text and "svc" not in c.get("/users").text
    assert "svc" in c.get("/users?all=1").text


def test_bulk_assign_and_unassign(c, ctx):
    add_acc(ctx)
    for n in ("alice", "bob"):
        ctx.test_users.add(n)
    r = c.post("/users/assign", data={"csrf": c.csrf, "users": ["alice", "bob"], "account": "acc1"})
    path, q = qs(r)
    assert path == "/users" and "2" in q["msg"] and "err" not in q
    for n in ("alice", "bob"):
        assert (ctx.test_users.get(n).home / ".codex" / "auth.json").is_symlink()
        assert repo.active_assignment(ctx.conn, n).account_pk == 1
    assert any(call[0] == "setfacl" for call in ctx.test_runner.calls)
    assert ("web:root_admin", "assign", "alice", "- -> acc1") in [
        tuple(x) for x in ctx.conn.execute("SELECT actor, action, target, detail FROM audit_log")
    ]
    assert "acc1" in c.get("/users").text
    r = c.post("/users/unassign", data={"csrf": c.csrf, "users": ["alice", "bob"]})
    assert qs(r)[1]["msg"]
    assert repo.active_assignment(ctx.conn, "alice") is None
    assert not (ctx.test_users.get("alice").home / ".codex" / "auth.json").exists()
    r = post(c, "/users/alice/fix")
    assert qs(r)[0] == "/users" and "msg" in qs(r)[1]


def test_assign_disabled_and_unknown_user(c, ctx):
    add_acc(ctx)
    ctx.test_users.add("alice")
    assert "msg" in qs(post(c, "/accounts/acc1/disable"))[1]
    assert "disabled" in c.get("/accounts").text
    assert "acc1" not in c.get("/users").text
    r = c.post("/users/assign", data={"csrf": c.csrf, "users": ["alice", "ghost"], "account": "acc1"})
    assert "已停用" in qs(r)[1]["err"] and "ghost" in qs(r)[1]["err"]
    assert qs(post(c, "/accounts/acc1/enable"))[1]["msg"]
    assert qs(post(c, "/accounts/acc1/republish"))[1]["msg"]
    assert "err" in qs(post(c, "/accounts/nope/republish"))[1]
    assert "err" in qs(post(c, "/users/assign", account="acc1"))[1]


def test_refresh_success_failure_and_busy(c, ctx):
    add_acc(ctx)
    import codexctl.refresher as rf

    calls = []
    orig = rf.refresh_account
    rf.refresh_account = lambda ctx_, acc, actor, blocking=True: calls.append((actor, blocking)) or True
    try:
        assert qs(post(c, "/accounts/acc1/refresh"))[1] == {"msg": "刷新成功"}
        assert calls == [("web:root_admin", False)]
        rf.refresh_account = lambda *a, **k: False
        repo.set_status(ctx.conn, 1, "needs_relogin", "token reused")
        assert "token reused" in qs(post(c, "/accounts/acc1/refresh"))[1]["err"]
    finally:
        rf.refresh_account = orig
    with account_lock(ctx.settings.state_dir, "acc1"):
        assert "正在刷新" in qs(post(c, "/accounts/acc1/refresh"))[1]["err"]
    assert "err" in qs(post(c, "/accounts/zzz/refresh"))[1]


def test_device_login_add_full_flow(c, ctx, mgr):
    mgr.src.write_bytes(make_auth_json(account_id="acct-9", email="n@example.com"))
    r = post(c, "/accounts/login", label="newacc", mode="add")
    assert r.status_code == 303
    page = r.headers["location"]
    assert page.startswith("/accounts/login/")
    lid = page.rsplit("/", 1)[1]
    end = time.time() + 5
    while mgr.status(lid)["state"] != "waiting" and time.time() < end:
        time.sleep(0.02)
    html = c.get(page).text
    assert "ABCD-EFGH12" in html and 'rel="noopener"' in html and "https://auth.openai.com/codex/device" in html
    js = c.get(page + "/status").json()
    assert js["state"] == "waiting" and js["code"] == "ABCD-EFGH12"
    assert "csrf" in html
    mgr.marker.write_text("")
    end = time.time() + 5
    while mgr.status(lid)["state"] != "succeeded" and time.time() < end:
        time.sleep(0.02)
    path, q = qs(c.get(page))
    assert path == "/accounts" and "已添加" in q["msg"]
    assert repo.get_account(ctx.conn, "newacc").email == "n@example.com"
    n = ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='account.add'").fetchone()[0]
    path, q = qs(c.get(page))
    assert path == "/accounts" and ("err" in q or "msg" in q)
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='account.add'").fetchone()[0] == n == 1
    assert ("web:root_admin", "account.add") in [tuple(x) for x in ctx.conn.execute("SELECT actor, action FROM audit_log")]


def test_device_login_relogin_and_errors(c, ctx, mgr):
    add_acc(ctx)
    assert "已存在" in qs(post(c, "/accounts/login", label="acc1", mode="add"))[1]["err"]
    assert "不存在" in qs(post(c, "/accounts/login", label="zz", mode="relogin"))[1]["err"]
    assert "err" in qs(post(c, "/accounts/login", label="Bad Label", mode="add"))[1]
    assert "err" in qs(post(c, "/accounts/login", label="acc1", mode="x"))[1]
    mgr.src.write_bytes(make_auth_json(account_id="other", email="o@example.com"))
    page = post(c, "/accounts/login", label="acc1", mode="relogin").headers["location"]
    lid = page.rsplit("/", 1)[1]
    assert "已有进行中" in qs(post(c, "/accounts/login", label="acc1", mode="relogin"))[1]["err"]
    mgr.marker.write_text("")
    end = time.time() + 5
    while mgr.status(lid)["state"] != "succeeded" and time.time() < end:
        time.sleep(0.02)
    path, q = qs(c.get(page))
    assert path == "/accounts" and "另一个" in q["err"]


def test_device_login_cancel_and_unknown(c, ctx, mgr):
    page = post(c, "/accounts/login", label="zzz", mode="add").headers["location"]
    lid = page.rsplit("/", 1)[1]
    assert qs(post(c, page + "/cancel"))[0] == "/accounts"
    assert mgr.status(lid)["state"] == "cancelled"
    assert "cancelled" in c.get(page).text
    assert "err" in qs(c.get("/accounts/login/nope"))[1]
    assert c.get("/accounts/login/nope/status").status_code == 404


def test_shutdown_cancels_logins(ctx, mgr):
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    with TestClient(create_app(ctx, devlogins=mgr), follow_redirects=False) as cl:
        do_login(cl)
        csrf = re.search(r'name="csrf" value="([^"]+)"', cl.get("/").text).group(1)
        page = cl.post("/accounts/login", data={"csrf": csrf, "label": "zzz", "mode": "add"}).headers["location"]
        lid = page.rsplit("/", 1)[1]
    assert mgr._logins[lid].state == "cancelled"


def test_csv_and_usage(c, ctx):
    ctx.test_users.add("alice")
    d = {f: 1 for f in repo.USAGE_FIELDS}
    d["total_tokens"] = 7
    for name in ("=cmd", "+x", "-y", "@z", "alice"):
        repo.insert_usage(ctx.conn, name, None, ctx.now() - 10, d, "f")
    r = c.get("/usage.csv?days=7")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    for bad in ("'=cmd", "'+x", "'-y", "'@z"):
        assert any(l.startswith(bad + ",") for l in lines), bad
    assert any(l.startswith("alice,") for l in lines)
    assert "alice" in c.get("/usage?days=30").text
    assert "<strong>7 天</strong>" in c.get("/usage?days=5").text


def test_audit_and_xss(c, ctx):
    events.raise_alert(ctx.conn, "critical", "k", "t", "<script>alert(1)</script>", now=ctx.now())
    events.audit(ctx.conn, "web:x", "weird", "<b>t</b>", now=ctx.now())
    for p in ("/", "/audit"):
        t = c.get(p).text
        assert "<script>alert(1)" not in t and "&lt;script&gt;alert(1)" in t
    assert "&lt;b&gt;t" in c.get("/audit").text


def test_writes_require_csrf(c, ctx):
    add_acc(ctx)
    for p in ("/accounts/acc1/refresh", "/accounts/acc1/disable", "/accounts/login", "/users/assign",
              "/users/unassign", "/users/alice/fix", "/accounts/login/x/cancel"):
        assert c.post(p, data={}).status_code == 403, p
        assert c.post(p, data={"csrf": "bad"}).status_code == 403, p
    assert repo.get_account(ctx.conn, "acc1").status == "active"
