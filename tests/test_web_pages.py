import dataclasses
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fakes import make_auth_json
from fastapi.testclient import TestClient
from test_web import PW, do_login

import codexctl.accounts.service as accounts
import codexctl.events as events
import codexctl.repo as repo
import codexctl.users.assignments as assignments
import codexctl.users.pam as pam
import codexctl.users.procs as procs
import codexctl.users.webauth as webauth
from codexctl.accounts.devicelogin import DeviceLoginManager
from codexctl.locks import account_lock
from codexctl.web.app import create_app


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
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
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
    for p in ("/", "/accounts", "/users", "/usage", "/pricing", "/audit"):
        r = c.get(p)
        assert r.status_code == 200 and 'style="' not in r.text, p
    dash = c.get("/").text
    assert "acc1" in dash and "a@example****" not in dash
    assert "a@example.com" not in dash and "a****@example.com" in dash
    assert "acc1" in c.get("/accounts").text


def test_pages_require_login(ctx, mgr):
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
    anon = TestClient(create_app(ctx, devlogins=mgr), follow_redirects=False)
    for p in ("/", "/accounts", "/users", "/usage", "/usage.csv", "/pricing", "/audit", "/accounts/login/x", "/accounts/login/x/status"):
        r = anon.get(p)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_users_hidden_and_all(c, ctx):
    ctx.test_users.add("alice")
    ctx.test_users.add("svc")
    assert "alice" in c.get("/users").text and "svc" not in c.get("/users").text
    assert "svc" in c.get("/users?all=1").text


def test_create_user_form_rendered(c):
    page = c.get("/users").text
    assert 'action="/users/create"' in page
    assert 'name="username"' in page and 'name="password"' in page and 'name="password2"' in page
    assert 'autocomplete="new-password"' in page


def test_create_user_from_users_page(c, ctx, monkeypatch):
    pw = "long-enough-password"
    r = post(c, "/users/create", username="alice", password=pw, password2=pw)
    path, q = qs(r)
    assert path == "/users"
    assert "alice 已创建" in q["msg"] and "已开通面板登录" in q["msg"]
    user = ctx.test_users.get("alice")
    assert (user.home / ".codex").is_dir()
    assert webauth.enrolled_system_uid(ctx.conn, "alice") == user.uid
    assert webauth.get_hash(ctx.conn, "alice") is None
    assert ctx.settings.codex_wrapper.is_file()
    row = ctx.conn.execute(
        "SELECT actor, action, target, detail FROM audit_log WHERE action = 'user.create'"
    ).fetchone()
    assert tuple(row)[:3] == ("web:admin", "user.create", "alice")
    assert pw not in row["detail"] and pw not in q["msg"]
    assert pw not in c.get("/users").text
    monkeypatch.setattr(pam, "verify_password", lambda name, password: (name, password) in ctx.test_users.created)
    with TestClient(create_app(ctx), follow_redirects=False) as new_user:
        assert do_login(new_user, "alice", pw).status_code == 303
        assert new_user.get("/").headers["location"] == "/users/alice"
        assert new_user.get("/users/alice").status_code == 200
        assert new_user.get("/users/bob").status_code == 403


def test_create_user_errors(c, ctx):
    ctx.test_users.add("bob")
    cases = [
        ({"username": "alice", "password": "short", "password2": "short"}, "至少"),
        ({"username": "alice", "password": "long-enough", "password2": "different"}, "不一致"),
        ({"username": "bob", "password": "long-enough", "password2": "long-enough"}, "已存在"),
        ({"username": "admin", "password": "long-enough", "password2": "long-enough"}, "保留"),
        ({"username": "Bad Name", "password": "long-enough", "password2": "long-enough"}, "非法用户名"),
    ]
    for data, expected in cases:
        q = qs(post(c, "/users/create", **data))[1]
        assert "err" in q and expected in q["err"], (data, q)
    assert not ctx.test_users.exists("alice")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.create'").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM web_system_users").fetchone()[0] == 0


def test_create_user_codex_install_failure(c, ctx, tmp_path):
    ctx.settings = dataclasses.replace(
        ctx.settings,
        codex_bin=tmp_path / "missing" / "bin" / "codex",
        codex_wrapper=tmp_path / "missing-wrapper",
        codex_requirements=tmp_path / "missing-requirements",
    )
    q = qs(post(c, "/users/create", username="alice", password="long-enough", password2="long-enough"))[1]
    assert "err" in q and "HTTP 404" in q["err"]
    assert not ctx.test_users.exists("alice")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.create'").fetchone()[0] == 0


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
    assert ("web:admin", "assign", "alice", "- -> acc1") in [
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
        "codexctl.accounts.refresher.refresh_account",
        lambda ctx_, acc, actor, blocking=True: calls.append((actor, blocking)) or True,
    )
    assert qs(post(c, "/accounts/acc1/refresh"))[1] == {"msg": "刷新成功"}
    assert calls == [("web:admin", False)]
    monkeypatch.setattr("codexctl.accounts.refresher.refresh_account", lambda *a, **k: False)
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
    assert ("web:admin", "account.add") in [tuple(x) for x in ctx.conn.execute("SELECT actor, action FROM audit_log")]


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
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
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
    assert lines[0] == "username,model,today_tokens,7d_tokens,total_tokens,today_usd,7d_usd,total_usd,turns"
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
        "alice,,0,0,40000,,,,1",
        "alice,<b>m</b>,0,0,5000,,,,1",
        "alice,gpt-a,1500000,1500000,1500000,,,,1",
        "alice,gpt-b,0,2000000000,2000000000,,,,1",
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
    assert table.count('class="user-usage-toggle" aria-expanded="false"') == 2
    assert alice.count('class="model-sub" hidden') == 2 and "gpt-a" in alice and "gpt-b" in alice
    assert "3K" in alice and "7M" in alice and "7.0M" not in alice
    bob = table[table.index("bob"):]
    assert bob.count('class="model-sub" hidden') == 1 and "3.3B" in bob


def test_usage_page_shows_cost_and_price_list(c, ctx):
    def add(ts, model, inp, cached, out):
        d = {f: 0 for f in repo.USAGE_FIELDS}
        d.update(input_tokens=inp, cached_input_tokens=cached, output_tokens=out, total_tokens=inp + out)
        repo.insert_usage(ctx.conn, "alice", None, ts, d, "f", model)

    now = ctx.now()
    add(now, "gpt-5.2", 1_000_000, 400_000, 100_000)
    add(now, "gpt-a", 1_000, 0, 1_000)
    t = c.get("/usage").text
    assert "价目表" in t and "美元 / 百万 tokens" in t
    assert "GPT-5.2" in t and "GPT-6.1 Sol" in t
    assert "$1.75" in t and "$0.175" in t
    assert "$2.52" in t
    assert "未定价" in t  # gpt-a 不在价目表
    assert "1 个模型未定价，费用未计入" in t

    lines = sorted(l for l in c.get("/usage.csv").text.splitlines() if l.startswith("alice,"))
    assert lines == [
        "alice,gpt-5.2,1100000,1100000,1100000,2.52,2.52,2.52,1",
        "alice,gpt-a,2000,2000,2000,,,,1",
    ]


def test_users_page_shows_cost(c, ctx):
    ctx.test_users.add("alice")
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1_000_000, output_tokens=0, total_tokens=1_000_000)
    repo.insert_usage(ctx.conn, "alice", None, ctx.now(), d, "f", "gpt-5.2")
    t = c.get("/users").text
    assert "今天 tokens / 费用" in t
    assert "$1.75" in t


def _price_id(ctx, name="GPT-5.2"):
    return next(p.id for p in repo.list_prices(ctx.conn) if p.name == name)


def test_pricing_page_and_update_changes_usage_cost(c, ctx):
    pid = _price_id(ctx)
    page = c.get("/pricing").text
    assert "模型价格" in page and "新增模型" in page and "GPT-5.2" in page
    assert f'action="/pricing/{pid}"' in page and 'name="input_usd"' in page

    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1_000_000, total_tokens=1_000_000)
    repo.insert_usage(ctx.conn, "alice", None, ctx.now(), d, "f", "gpt-5.2")
    assert "alice,gpt-5.2,1000000,1000000,1000000,1.75,1.75,1.75,1" in c.get("/usage.csv").text

    r = post(c, f"/pricing/{pid}", name="GPT-5.2", input_usd="3.33", cached_usd="0.20", output_usd="20", aliases="gpt-5.2")
    assert qs(r)[0] == "/pricing" and "msg" in qs(r)[1]
    assert repo.get_price(ctx.conn, pid).input_usd == 3.33
    assert "alice,gpt-5.2,1000000,1000000,1000000,3.33,3.33,3.33,1" in c.get("/usage.csv").text
    assert "$3.33" in c.get("/usage").text


def test_pricing_add_delete_and_validation(c, ctx):
    r = post(
        c, "/pricing/add", name="Custom-X", input_usd="2", cached_usd="0.2", output_usd="4",
        aliases="custom-x, custom x",
    )
    assert "msg" in qs(r)[1]
    price = next(p for p in repo.list_prices(ctx.conn) if p.name == "Custom-X")
    assert price.aliases == ("custom-x",)  # 与 custom x 归一化后相同，自动去重

    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1_000_000, total_tokens=1_000_000)
    repo.insert_usage(ctx.conn, "alice", None, ctx.now(), d, "f", "custom-x")
    assert "alice,custom-x,1000000,1000000,1000000,2,2,2,1" in c.get("/usage.csv").text

    r = post(c, "/pricing/add", name="custom x", input_usd="1", cached_usd="0", output_usd="1", aliases="")
    assert "重复" in qs(r)[1]["err"]

    r = post(c, f"/pricing/{price.id}", name="Custom-X", input_usd="-1", cached_usd="0", output_usd="1", aliases="")
    assert "价格" in qs(r)[1]["err"]
    assert repo.get_price(ctx.conn, price.id).input_usd == 2

    r = post(c, f"/pricing/{price.id}/delete")
    assert "msg" in qs(r)[1]
    assert repo.get_price(ctx.conn, price.id) is None
    assert "alice,custom-x,1000000,1000000,1000000,,,,1" in c.get("/usage.csv").text
    assert "未定价" in c.get("/usage").text


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
              "/users/unassign", "/users/alice/fix", "/users/alice/restart-codex", "/accounts/login/x/cancel",
              "/pricing/1", "/pricing/add", "/pricing/1/delete"):
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
    assert ("web:admin", "account.republish", "acc1") in got
    assert ("web:admin", "account.login_cancel", "zzz") in got


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
    assert ("web:admin", "account.quota_refresh", "acc1") in [
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
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
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



def test_users_page_shows_assigned_account_name_and_label(c, ctx):
    add_acc(ctx)
    ctx.test_users.add("alice")
    assignments.assign(ctx, "alice", "acc1", "test")
    post(c, "/accounts/acc1/name", name="主力号")
    page = c.get("/users").text
    row = re.search(r"<tr>\s*.*?<b>alice</b>.*?</tr>", page, re.S).group()
    assert '<span class="tag">主力号</span>' in row
    assert '<span class="tag">acc1</span>' in row
    post(c, "/accounts/acc1/name", name="")
    page = c.get("/users").text
    row = re.search(r"<tr>\s*.*?<b>alice</b>.*?</tr>", page, re.S).group()
    assert row.count('<span class="tag">acc1</span>') == 1


def test_users_page_flags_stale_codex(c, ctx, monkeypatch):
    add_acc(ctx)
    ctx.test_users.add("alice")
    base = ctx.now()
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_clock.t = base + 60
    stale = procs.CodexProc(pid=4321, exe=str(ctx.settings.codex_bin), started_at=base - 5)
    monkeypatch.setattr(procs, "list_codex_procs", lambda uid, d, **kw: [stale])

    page = c.get("/users").text
    assert "1 个旧进程（仍用旧账号）" in page
    assert "重启 codex" in page and "pid 4321" in page

    monkeypatch.setattr(procs, "terminate", lambda pids, **kw: (list(pids), []))
    path, q = qs(post(c, "/users/alice/restart-codex"))
    assert path == "/users" and "已结束 alice 的 1 个 codex 进程" in q["msg"]
    got = [
        tuple(x)
        for x in ctx.conn.execute("SELECT action, target, detail FROM audit_log WHERE action = 'restart_codex'")
    ]
    assert got == [("restart_codex", "alice", "4321")]


def test_users_page_without_stale_codex(c, ctx, monkeypatch):
    ctx.test_users.add("alice")
    monkeypatch.setattr(procs, "list_codex_procs", lambda uid, d, **kw: [])
    assert "重启 codex" not in c.get("/users").text
    path, q = qs(post(c, "/users/alice/restart-codex"))
    assert path == "/users" and "没有旧 codex 进程" in q["msg"]


def test_assign_mentions_stale_codex(c, ctx, monkeypatch):
    add_acc(ctx)
    ctx.test_users.add("alice")
    base = ctx.now()
    monkeypatch.setattr(
        procs, "list_codex_procs", lambda uid, d, **kw: [procs.CodexProc(pid=101, exe="x", started_at=base - 5)]
    )
    r = c.post("/users/assign", data={"csrf": c.csrf, "users": ["alice"], "account": "acc1"})
    q = qs(r)[1]
    assert "旧 codex 进程" in q["msg"] and "重启 codex" in q["msg"]


def test_user_detail_page_with_sessions(c, ctx):
    acc = add_acc(ctx, label="acc1")
    alice = ctx.test_users.add("alice")
    repo.start_assignment(ctx.conn, "alice", acc.id, ctx.now() - 3600)

    # 创建会话文件及目录
    sess_dir = alice.home / ".codex" / "sessions" / "2026" / "11" / "15"
    sess_dir.mkdir(parents=True, exist_ok=True)
    sess_path = sess_dir / "rollout-task42.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"cwd": str(alice.home / "project_x")}}) + "\n",
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "实现登录接口"}]}}) + "\n",
    ]
    sess_path.write_text("".join(lines), encoding="utf-8")

    # 写入用量
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1_000_000, output_tokens=0, total_tokens=1_000_000)
    repo.insert_usage(ctx.conn, "alice", acc.id, ctx.now(), d, str(sess_path), "gpt-5.2")

    r = c.get("/users/alice")
    assert r.status_code == 200
    assert "style=" not in r.text
    t = r.text
    assert ">用户</a> / alice" in t
    assert "task42" in t
    assert "~/project_x" in t
    assert "实现登录接口" in t
    assert "最近会话" in t
    assert "模型用量明细" in t
    assert "GPT-5.2" in t or "gpt-5.2" in t
    assert "$1.75" in t


def test_user_detail_page_empty_sessions(c, ctx):
    ctx.test_users.add("bob")
    r = c.get("/users/bob")
    assert r.status_code == 200
    assert "style=" not in r.text
    assert ">用户</a> / bob" in r.text
    assert "暂无会话记录" in r.text


def test_user_detail_page_nonexistent_user(c, ctx):
    r = c.get("/users/nonexistent_guy")
    assert r.status_code == 303
    path, q = qs(r)
    assert path == "/users" and "不存在" in q.get("err", "")


def test_user_detail_action_redirects_back(c, ctx):
    ctx.test_users.add("alice")
    r = c.post("/users/alice/fix", data={"csrf": c.csrf, "next": "/users/alice"})
    path, q = qs(r)
    assert path == "/users/alice" and "已修复" in q.get("msg", "")


def test_user_detail_daily_chart_and_session_pagination(c, ctx):
    ctx.test_users.add("alice")
    acc = add_acc(ctx, label="daily-acc")
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=100, cached_input_tokens=10, output_tokens=20, total_tokens=120)
    for i in range(21):
        repo.insert_usage(ctx.conn, "alice", acc.id, ctx.now() - i, d, f"session-{i}.jsonl", "daily-model")
    r = c.get("/users/alice?days=7&model=daily-model&account_filter=daily-acc")
    assert r.status_code == 200
    assert "每日用量" in r.text and "近 7 天" in r.text
    assert "2,520" in r.text and "缓存输入" in r.text
    assert "第 1 / 2 页" in r.text and "下一页" in r.text
    assert "session-20" not in r.text
    r = c.get("/users/alice?days=7&model=daily-model&account_filter=daily-acc&page=2")
    assert r.status_code == 200 and "session-20" in r.text and "上一页" in r.text
    r = c.get("/users/alice?model=missing")
    assert "没有符合条件的会话" in r.text


def test_usage_comparison_panel_renders(ctx):
    from starlette.requests import Request

    ctx.test_users.add("alice")
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1_000_000, cached_input_tokens=250_000, output_tokens=200_000, total_tokens=1_200_000)
    repo.insert_usage(ctx.conn, "alice", None, ctx.now(), d, "alice-session", "unknown-model")
    repo.insert_usage(ctx.conn, "bob", None, ctx.now(), d, "bob-session", "unknown-model")
    app = create_app(ctx)
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [], "app": app, "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http", "root_path": ""})
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/")
    response = endpoint(request, days=7, admin=("admin", "test"))
    html = response.body.decode()
    assert "用户用量对比" in html and "近 7 天" in html
    assert "alice" in html and "bob" in html and "未定价" in html
    assert 'compare-line compare-0' in html and 'compare-line compare-1' in html
    assert 'data-series-toggle="0" aria-pressed="true"' in html
    assert html.index('>Codex</span>') < html.index('>用户用量对比</h2>') < html.index('id="dashboard-alerts"')
    assert '折线图' in html and '查看每日用户用量' in html
    assert 'data-chart-kind="comparison"' in html and 'data-chart-names=' in html
    assert html.count('class="chart-hit"') == 7
    assert 'chart-inspector.js' in html and 'data-values-short=' in html
    assert 'data-total-short=' in html
    assert "25.0%" in html
    assert 'title="1,200,000 tokens">1.2M</td>' in html
    assert 'title="2,400,000 tokens">2.4M</td>' in html


def test_user_detail_uses_latest_session_title_and_fallback(ctx):
    from starlette.requests import Request

    alice = ctx.test_users.add("alice")
    session_id = "01a1de3b-4512-74d0-8b43-841dc78a0577"
    codex = alice.home / ".codex"
    session_dir = codex / "sessions"
    session_dir.mkdir(parents=True)
    path = session_dir / f"rollout-2026-11-15T12-00-00-{session_id}.jsonl"
    path.write_text(json.dumps({"payload": {"type": "message", "role": "user", "content": "初始问题"}}) + "\n")
    (codex / "session_index.jsonl").write_text(
        json.dumps({"id": session_id, "thread_name": "旧标题"}) + "\n" +
        json.dumps({"id": session_id, "thread_name": "更新后的会话标题"}) + "\n"
    )
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=100, output_tokens=20, total_tokens=120)
    repo.insert_usage(ctx.conn, "alice", None, ctx.now(), d, str(path), "model")
    app = create_app(ctx)
    request = Request({"type": "http", "method": "GET", "path": "/users/alice", "query_string": b"", "headers": [], "app": app, "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http", "root_path": ""})
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/users/{name}")
    def render():
        return endpoint("alice", request, days=7, page=1, model="", account_filter="", admin=("admin", "test")).body.decode()
    html = render()
    assert 'class="session-title" title="更新后的会话标题"' in html
    assert 'class="session-reference"' in html
    assert 'data-chart-kind="daily"' in html and html.count('class="chart-hit"') == 7
    assert 'data-cached-short=' in html and 'chart-inspector.js' in html
    (codex / "session_index.jsonl").unlink()
    assert 'class="session-title" title="初始问题"' in render()


def test_usage_comparison_shows_every_user(ctx):
    from starlette.requests import Request

    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=100, output_tokens=20, total_tokens=120)
    for i in range(7):
        repo.insert_usage(ctx.conn, f"user{i}", None, ctx.now(), d, f"session{i}", "model")
    app = create_app(ctx)
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [], "app": app, "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http", "root_path": ""})
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/")
    html = endpoint(request, days=7, admin=("admin", "test")).body.decode()
    assert '其他用户' not in html
    assert html.count('class="compare-line ') == 7
    for i in range(7):
        assert f'<th class="num">user{i}</th>' in html
        assert f'user{i}' in html
    assert 'compare-line compare-6' in html


def test_usage_charts_default_to_seven_days(ctx):
    from starlette.requests import Request

    ctx.test_users.add("alice")
    app = create_app(ctx)
    def request(path):
        return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [], "app": app, "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http", "root_path": ""})
    dashboard = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/")
    detail = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/users/{name}")
    assert 'href="?days=7#comparison" class="active"' in dashboard(request("/"), admin=("admin", "test")).body.decode()
    html = detail("alice", request("/users/alice"), admin=("admin", "test")).body.decode()
    assert 'option value="7" selected' in html and html.count('class="chart-hit"') == 7
    assert 'href="?days=30#comparison" class="active"' in dashboard(request("/"), days=30, admin=("admin", "test")).body.decode()


def test_usage_dates_use_beijing_days_across_views(c, ctx, monkeypatch):
    import os
    from datetime import datetime, timezone

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        ctx.test_clock.t = int(datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc).timestamp())
        ctx.test_users.add("alice")
        account = add_acc(ctx)
        delta = {field: 0 for field in repo.USAGE_FIELDS}
        delta.update(input_tokens=100, total_tokens=100)
        before = ctx.now() - 9 * 3600
        after = ctx.now() - 7 * 3600
        repo.insert_usage(ctx.conn, "alice", account.id, before, delta, "yesterday", "model")
        repo.insert_usage(ctx.conn, "alice", account.id, after, delta, "today", "model")
        captured = {}
        render = c.app.state.templates.TemplateResponse
        def capture(request, name, context, **kwargs):
            captured[name] = context
            return render(request, name, context, **kwargs)
        monkeypatch.setattr(c.app.state.templates, "TemplateResponse", capture)
        assert c.get("/users").status_code == 200
        assert captured["users.html"]["rows"][0]["t24"] == 100
        assert c.get("/users/alice").status_code == 200
        assert captured["user_detail.html"]["summary"]["today"] == 100
        assert captured["user_detail.html"]["daily"][-1]["total"] == 100
        assert c.get("/usage").status_code == 200
        assert captured["usage.html"]["periods"][0]["total"] == 100
        assert c.get("/").status_code == 200
        assert captured["dashboard.html"]["daily"][-1]["total"] == 100
    finally:
        monkeypatch.undo()
        time.tzset()


def test_audit_pagination_filters_and_date_validation(c, ctx, monkeypatch):
    for i in range(55):
        events.audit(ctx.conn, "filter-user", "filter.action", f"record-{i:02}", "needle", now=ctx.now() + i)
        ctx.conn.execute("INSERT INTO alerts(ts,level,kind,target,message) VALUES (?, 'warning', 'test', ?, 'needle')",
                         (ctx.now() + i, f"alert-{i:02}"))
    captured = {}
    render = c.app.state.templates.TemplateResponse
    def capture(request, name, context, **kwargs):
        captured.update(context)
        return render(request, name, context, **kwargs)
    monkeypatch.setattr(c.app.state.templates, "TemplateResponse", capture)
    assert c.get("/audit?actor=filter-user&action=filter.action&q=needle&level=warning").status_code == 200
    assert len(captured["audit"]) == 50 and len(captured["alerts"]) == 50
    assert captured["audit_pagination"]["total"] == 55
    assert "actor=filter-user" in captured["audit_pagination"]["next"]
    assert c.get(captured["audit_pagination"]["next"]).status_code == 200
    assert len(captured["audit"]) == 5
    assert captured["alert_pagination"]["page"] == 1
    assert c.get("/audit?q=no-match").status_code == 200
    assert captured["audit_pagination"]["total"] == 0
    assert c.get("/audit?start=2026-13-01").status_code == 422
    assert c.get("/audit?end=9999-12-31").status_code == 422
    assert c.get("/audit?start=2026-11-18&end=2026-11-17").status_code == 422


def test_current_account_quota_is_bound_to_member_and_handles_stale_samples(ctx):
    from codexctl.web.views import _current_account_quota

    account = add_acc(ctx, label="member-account")
    other = add_acc(ctx, label="other-account", account_id="other-account")
    now = ctx.now()
    repo.insert_quota_sample(ctx.conn, other.id, now, "primary", 99, 300, now + 200, "api")
    assert _current_account_quota(ctx, None) is None
    assert _current_account_quota(ctx, account)["has_quota"] is False
    repo.insert_quota_sample(ctx.conn, account.id, now, "primary", 27, 300, now + 200, "api")
    repo.insert_quota_sample(ctx.conn, account.id, now, "secondary", 45, 10080, now + 10000, "api")
    quota = _current_account_quota(ctx, account)
    assert quota["remaining"] == 73 and quota["name"] == "5小时"
    assert len(quota["windows"]) == 2 and quota["windows"][1]["remaining"] == 55
    assert not quota["stale"] and not quota["expired"]
    ctx.test_clock.t = now + 201
    quota = _current_account_quota(ctx, account)
    assert quota["remaining"] is None and quota["used"] is None
    assert quota["expired"] and quota["stale"] and "待更新" in quota["reset"]
    assert quota["windows"][1]["remaining"] == 55


def test_member_quota_context_and_daily_line_points(c, ctx, monkeypatch):
    ctx.test_users.add("alice")
    account = add_acc(ctx)
    repo.start_assignment(ctx.conn, "alice", account.id, ctx.now())
    repo.insert_quota_sample(ctx.conn, account.id, ctx.now(), "primary", 40, 300, ctx.now() + 3600, "api")
    delta = {field: 0 for field in repo.USAGE_FIELDS}
    delta.update(input_tokens=100, total_tokens=100)
    repo.insert_usage(ctx.conn, "alice", account.id, ctx.now(), delta, "line-session", "model")
    captured = {}
    render = c.app.state.templates.TemplateResponse
    def capture(request, name, context, **kwargs):
        captured.update(context)
        return render(request, name, context, **kwargs)
    monkeypatch.setattr(c.app.state.templates, "TemplateResponse", capture)
    assert c.get("/users/alice").status_code == 200
    assert captured["current_account_quota"]["remaining"] == 60
    assert captured["daily_line_path"].startswith("M ")
    assert captured["daily_line_path"].count("L ") == 6
    assert all("center_x" in day for day in captured["daily"])
