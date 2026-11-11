import dataclasses
import httpx
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
    assert "acc1" in dash and "a@example****" not in dash
    assert "a@example.com" not in dash and "a****@example.com" in dash
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


def test_refresh_success_failure_and_busy(c, ctx, monkeypatch):
    add_acc(ctx)
    calls = []
    monkeypatch.setattr(
        "codexctl.refresher.refresh_account",
        lambda ctx_, acc, actor, blocking=True: calls.append((actor, blocking)) or True,
    )
    assert qs(post(c, "/accounts/acc1/refresh"))[1] == {"msg": "刷新成功"}
    assert calls == [("web:root_admin", False)]
    monkeypatch.setattr("codexctl.refresher.refresh_account", lambda *a, **k: False)
    repo.set_status(ctx.conn, 1, "needs_relogin", "token reused")
    assert "token reused" in qs(post(c, "/accounts/acc1/refresh"))[1]["err"]
    monkeypatch.undo()
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
    assert path == "/accounts" and "已被取走" in q["err"]
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
    r = c.get("/usage.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "username,model,today_tokens,7d_tokens,total_tokens,turns"
    for bad in ("'=cmd", "'+x", "'-y", "'@z"):
        assert any(l.startswith(bad + ",") for l in lines), bad
    assert any(l.startswith("alice,") for l in lines)
    assert "alice" in c.get("/usage").text


def test_usage_page_periods_and_models(c, ctx):
    def add(ts, total, model):
        d = {f: 0 for f in repo.USAGE_FIELDS}
        d["total_tokens"] = d["input_tokens"] = total
        repo.insert_usage(ctx.conn, "alice", None, ts, d, "f", model)

    now = ctx.now()
    add(now - 10, 1_500_000, "gpt-a")
    add(now - 3 * 86400, 2_000_000_000, "gpt-b")
    add(now - 30 * 86400, 40_000, None)
    add(now - 30 * 86400, 5_000, "<b>m</b>")
    t = c.get("/usage").text
    assert "今天" in t and "近 7 天" in t and "全部" in t
    assert "1.5M" in t and "2B" in t and "2B" in t and "45K" not in t
    assert "gpt-a" in t and "gpt-b" in t and "未知模型" in t
    assert "<b>m</b>" not in t and "&lt;b&gt;m&lt;/b&gt;" in t
    assert "2,001,545,000" in t  # 全部总量的精确值放在 title 里
    csv_lines = sorted(l for l in c.get("/usage.csv").text.splitlines() if l.startswith("alice,"))
    assert csv_lines == [
        "alice,,0,0,40000,1",
        "alice,<b>m</b>,0,0,5000,1",
        "alice,gpt-a,1500000,1500000,1500000,1",
        "alice,gpt-b,0,2000000000,2000000000,1",
    ]


def test_usage_page_per_user_models(c, ctx):
    def add(user, ts, total, model):
        d = {f: 0 for f in repo.USAGE_FIELDS}
        d["total_tokens"] = total
        repo.insert_usage(ctx.conn, user, None, ts, d, "f", model)

    now = ctx.now()
    add("alice", now - 10, 1_000, "gpt-a")
    add("alice", now - 10, 2_000, "gpt-a")
    add("alice", now - 3 * 86400, 7_000_000, "gpt-b")
    add("bob", now - 40 * 86400, 3_300_000_000, "gpt-a")
    t = c.get("/usage").text
    table = t[t.index("按用户"):]
    # alice 近 7 天更多，排在 bob 前面；每个用户下有各自模型的子行
    assert table.index("alice") < table.index("bob")
    alice = table[table.index("alice"):table.index("bob")]
    assert alice.count('class="model-sub"') == 2 and "gpt-a" in alice and "gpt-b" in alice
    assert "3K" in alice and "7M" in alice and "7.0M" not in alice
    bob = table[table.index("bob"):]
    assert bob.count('class="model-sub"') == 1 and "3.3B" in bob


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


def wait_state(mgr, lid, state):
    end = time.time() + 5
    while mgr.status(lid)["state"] != state and time.time() < end:
        time.sleep(0.02)
    assert mgr.status(lid)["state"] == state


@pytest.mark.parametrize("path", ["/accounts", "/"])
def test_finished_login_auto_imported_without_status_page(c, ctx, mgr, path):
    mgr.src.write_bytes(make_auth_json(account_id="acct-7", email="z@example.com"))
    lid = post(c, "/accounts/login", label="auto1", mode="add").headers["location"].rsplit("/", 1)[1]
    mgr.marker.write_text("")
    wait_state(mgr, lid, "succeeded")
    r = c.get(path)
    assert r.status_code == 200 and "auto1 已添加" in r.text
    assert repo.get_account(ctx.conn, "auto1").email == "z@example.com"
    assert "已添加" not in c.get(path).text
    assert mgr.finished_ids() == []


def test_auto_import_error_shown(c, ctx, mgr):
    add_acc(ctx, "acc1", "acct-1")
    mgr.src.write_bytes(make_auth_json(account_id="acct-1"))
    lid = post(c, "/accounts/login", label="dup", mode="add").headers["location"].rsplit("/", 1)[1]
    mgr.marker.write_text("")
    wait_state(mgr, lid, "succeeded")
    assert "已登记为 acc1" in c.get("/accounts").text


def test_quota_reset_passed_and_nonfinite(c, ctx):
    add_acc(ctx)
    now = ctx.now()
    repo.insert_quota_sample(ctx.conn, 1, now, "secondary", 88.0, 10080, now - 5, "api")
    t = c.get("/").text
    assert "已重置（待更新）" in t and 'value="100"' in t and "88%" not in t
    repo.insert_quota_sample(ctx.conn, 1, now + 1, "secondary", float("inf"), 10080, now + 5000, "api")
    t = c.get("/").text
    assert "暂无额度数据" in t and "inf" not in t.lower()


def test_csp_and_no_inline_script(c, mgr):
    r = c.get("/accounts")
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    page = post(c, "/accounts/login", label="zzz", mode="add").headers["location"]
    html = c.get(page).text
    assert 'data-login-id="' in html and '/static/login.js' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert c.get("/static/login.js").status_code == 200


def test_unsafe_url_not_linked(c, ctx, mgr):
    ctx.settings.codex_bin.write_text(
        "#!/bin/sh\nprintf 'https://auth.openai.com.evil.com/\\nABCD-EFGH12\\n'\nsleep 30\n"
    )
    page = post(c, "/accounts/login", label="zzz", mode="add").headers["location"]
    lid = page.rsplit("/", 1)[1]
    end = time.time() + 5
    while not mgr.status(lid)["code"] and time.time() < end:
        time.sleep(0.02)
    html = c.get(page).text
    assert "evil.com" not in html and "ABCD-EFGH12" in html


def test_hidden_user_cannot_be_assigned(c, ctx):
    add_acc(ctx)
    ctx.test_users.add("svc")
    ctx.test_users.add("alice")
    r = c.post("/users/assign", data={"csrf": c.csrf, "users": ["svc", "alice"], "account": "acc1"})
    q = qs(r)[1]
    assert "svc" in q["err"] and "隐藏用户不可分配" in q["err"] and "1" in q["msg"]
    assert repo.active_assignment(ctx.conn, "svc") is None
    assert repo.active_assignment(ctx.conn, "alice") is not None


def test_republish_and_cancel_audited(c, ctx):
    add_acc(ctx)
    post(c, "/accounts/acc1/republish")
    page = post(c, "/accounts/login", label="zzz", mode="add").headers["location"]
    post(c, page + "/cancel")
    got = [tuple(x) for x in ctx.conn.execute("SELECT actor, action, target FROM audit_log")]
    assert ("web:root_admin", "account.republish", "acc1") in got
    assert ("web:root_admin", "account.login_cancel", "zzz") in got


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_BODY = {"rate_limit": {
    "primary_window": {"used_percent": 33, "limit_window_seconds": 18000, "reset_at": 1_800_000_000 + 3000},
    "secondary_window": {"used_percent": 7, "limit_window_seconds": 604800, "reset_at": 1_800_000_000 + 90000},
}}


def seed_history(ctx, pk=1):
    now = ctx.now()
    for i, (a, b) in enumerate([(10.0, 1.0), (20.0, 2.0), (30.0, 3.0)]):
        ts = now - 600 * (3 - i)
        repo.insert_quota_sample(ctx.conn, pk, ts, "primary", a, 300, ts + 3000, "api")
        repo.insert_quota_sample(ctx.conn, pk, ts, "secondary", b, 10080, ts + 90000, "api")
    old = now - 3 * 86400
    repo.insert_quota_sample(ctx.conn, pk, old, "primary", 77.0, 300, old + 100, "api")


def test_quota_page_rows_and_days(c, ctx):
    add_acc(ctx)
    seed_history(ctx)
    r = c.get("/accounts/acc1/quota")
    assert r.status_code == 200 and 'style="' not in r.text
    assert "acc1 额度记录" in r.text and "5小时已用" in r.text and "7天已用" in r.text
    assert "30%" in r.text and "20%" in r.text and "10%" in r.text and "3%" in r.text
    assert "77%" not in r.text and "立即更新" in r.text
    assert "77%" in c.get("/accounts/acc1/quota?days=7").text
    assert "77%" not in c.get("/accounts/acc1/quota?days=999").text


def test_quota_page_empty_and_unknown(c, ctx):
    add_acc(ctx)
    assert "暂无记录" in c.get("/accounts/acc1/quota").text
    path, q = qs(c.get("/accounts/nope/quota"))
    assert path == "/accounts" and q["err"] == "账号不存在"
    path, q = qs(c.get("/accounts/nope/quota.csv"))
    assert path == "/accounts" and q["err"] == "账号不存在"


def test_quota_csv_and_injection_guard(c, ctx):
    add_acc(ctx)
    seed_history(ctx)
    r = c.get("/accounts/acc1/quota.csv?days=1")
    assert r.status_code == 200 and 'filename="acc1-quota.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0].split(",")[0] == "记录时间" and len(lines) == 4
    assert lines[1].split(",")[1] == "30.0" and lines[1].split(",")[3] == "3.0"
    assert len(c.get("/accounts/acc1/quota.csv?days=7").text.splitlines()) == 5
    from codexctl.web.views import _csv_cell
    assert _csv_cell("=1+1") == "'=1+1"


def test_quota_refresh_records_sample_and_requires_csrf(c, ctx):
    add_acc(ctx)
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json=USAGE_BODY)
    assert c.post("/accounts/acc1/quota-refresh", data={}).status_code == 403
    assert repo.quota_history(ctx.conn, 1, 0) == []
    r = post(c, "/accounts/acc1/quota-refresh")
    path, q = qs(r)
    assert path == "/accounts" and q == {"msg": "额度已更新"}
    h = repo.quota_history(ctx.conn, 1, 0)
    assert len(h) == 1 and h[0]["primary"]["used_percent"] == 33.0
    assert ("web:root_admin", "account.quota_refresh", "acc1") in [
        tuple(x) for x in ctx.conn.execute("SELECT actor, action, target FROM audit_log")
    ]
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(500)
    assert "err" in qs(post(c, "/accounts/acc1/quota-refresh"))[1]
    assert "err" in qs(post(c, "/accounts/zzz/quota-refresh"))[1]


def test_quota_refresh_next_validation(c, ctx):
    add_acc(ctx)
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json=USAGE_BODY)
    assert qs(post(c, "/accounts/acc1/quota-refresh", next="/"))[0] == "/"
    assert qs(post(c, "/accounts/acc1/quota-refresh", next="/accounts/acc1/quota"))[0] == "/accounts/acc1/quota"
    for bad in ("https://evil.com/", "//evil.com", "/users", "/accountsx"):
        assert qs(post(c, "/accounts/acc1/quota-refresh", next=bad))[0] == "/accounts", bad


def test_device_login_triggers_immediate_poll(c, ctx, mgr):
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json=USAGE_BODY)
    mgr.src.write_bytes(make_auth_json(account_id="acct-9", email="n@example.com"))
    page = post(c, "/accounts/login", label="newacc", mode="add").headers["location"]
    lid = page.rsplit("/", 1)[1]
    mgr.marker.write_text("")
    wait_state(mgr, lid, "succeeded")
    path, q = qs(c.get(page))
    assert "已添加" in q["msg"]
    acc = repo.get_account(ctx.conn, "newacc")
    assert len(repo.quota_history(ctx.conn, acc.id, 0)) == 1


def test_device_login_poll_failure_appended(c, ctx, mgr):
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(500)
    mgr.src.write_bytes(make_auth_json(account_id="acct-9", email="n@example.com"))
    page = post(c, "/accounts/login", label="newacc", mode="add").headers["location"]
    lid = page.rsplit("/", 1)[1]
    mgr.marker.write_text("")
    wait_state(mgr, lid, "succeeded")
    q = qs(c.get(page))[1]
    assert "已添加" in q["msg"] and "额度获取失败" in q["msg"]


def test_dashboard_and_accounts_links(c, ctx):
    add_acc(ctx)
    seed_history(ctx)
    d = c.get("/").text
    assert "/accounts/acc1/quota" in d and "立即更新" in d and 'name="next" value="/"' in d
    a = c.get("/accounts").text
    assert "/accounts/acc1/quota" in a and "额度" in a


def test_quota_pages_require_login(ctx, mgr):
    webauth.create_admin(ctx.conn, "root_admin", PW, now=ctx.now())
    anon = TestClient(create_app(ctx, devlogins=mgr), follow_redirects=False)
    for p in ("/accounts/acc1/quota", "/accounts/acc1/quota.csv"):
        r = anon.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_quota_page_chart_svg_and_week_remaining(c, ctx):
    add_acc(ctx)
    seed_history(ctx)
    t = c.get("/accounts/acc1/quota").text
    assert '<svg' in t and 'role="img"' in t and "acc1 7 天剩余额度趋势" in t
    assert re.search(r'<path[^>]*class="chart-line"[^>]*d="M [\d.]+ [\d.]+ L', t)
    assert "<title>" in t and "剩余 97%" in t and "7天剩余" in t and "style=" not in t
    assert "本周剩余" in c.get("/").text and "97%" in c.get("/").text
    c2 = c.get("/accounts/acc1/quota?days=30").text
    assert "style=" not in c2


def test_quota_page_no_chart_when_empty(c, ctx):
    add_acc(ctx)
    t = c.get("/accounts/acc1/quota").text
    assert "<svg" not in t and "暂无记录" in t


def test_dashboard_card(c, ctx):
    add_acc(ctx, email="alice.long@example.com")
    now = ctx.now()
    reset = now + 3 * 86400
    for i, used in enumerate((10, 30, 37)):
        repo.insert_quota_sample(ctx.conn, 1, now - (2 - i) * 600, "secondary", used, 10080, reset, "api")
    dash = c.get("/").text
    assert "alic****@example.com" in dash and "alice.long@example.com" not in dash
    assert "本周剩余" in dash and "63%" in dash and "<svg" in dash and 'style=' not in dash
    assert 'class="remain-bar good"' in dash and 'value="63"' in dash
    assert "额度记录" in dash and 'name="next" value="/"' in dash


def test_account_rename(c, ctx):
    add_acc(ctx)
    page = c.get("/accounts").text
    assert "a@example.com" in page and "/accounts/acc1/name" in page
    path, q = qs(post(c, "/accounts/acc1/name", name="主力号"))
    assert path == "/accounts" and "主力号" in q["msg"]
    assert repo.get_account(ctx.conn, "acc1").display_name == "主力号"
    assert "主力号" in c.get("/accounts").text
    assert "主力号" in c.get("/").text
    assert "主力号（acc1）" in c.get("/users").text
    assert "主力号 额度记录" in c.get("/accounts/acc1/quota").text
    assert "err" in qs(post(c, "/accounts/acc1/name", name="x" * 40))[1]
    assert "err" in qs(post(c, "/accounts/nope/name", name="x"))[1]
    assert "已清除" in qs(post(c, "/accounts/acc1/name", name=""))[1]["msg"]
    assert repo.get_account(ctx.conn, "acc1").display_name is None
