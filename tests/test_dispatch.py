import dataclasses
import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from codexctl import db, dispatch_skills, dispatch_tasks, dispatch_worker, repo, site_registry, sites, webauth
from codexctl.web.app import SESSION_COOKIE, create_app


def package(name, version="1.0"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: Test skill\nmetadata:\n  version: '{version}'\n---\nTest")
        archive.writestr(f"{name}/scripts/example.py", "# fixture")
        archive.writestr("install.py", "raise RuntimeError('Must never run')")
    return stream.getvalue()


def session(ctx, app, name):
    token, csrf = webauth.create_session(ctx.conn, name, now=ctx.now())
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, token)
    return client, csrf


def task_form(ctx):
    users = {}
    for uid, name in enumerate(("alice", "bob"), start=1001):
        ctx.test_users.add(name)
        users[name] = dataclasses.replace(ctx.test_users.get(name), uid=uid, gid=uid)
        account = repo.insert_account(
            ctx.conn, label=name, email=None, account_id=name, plan_type=None,
            last_refresh=ctx.now(), access_expires_at=ctx.now() + 60, now=ctx.now(),
        )
        repo.start_assignment(ctx.conn, name, account, ctx.now())
    lookup_user = ctx.lookup_user

    def lookup(name, min_uid):
        if name in users:
            return users[name]
        return lookup_user(name, min_uid)

    ctx.lookup_user = lookup
    server = site_registry.create(ctx, "admin", "站点服务器", "1.1.1.1", "22", "")
    dispatch_tasks.save_target(
        ctx, None, server.id, "shop.example.com",
        "https://panel.example.com/plugin?action=a&name=ai_center&s=renovation_api",
        "BAOTA_SHOP_API_KEY", "试点项目", "alice",
    )
    selections = {}
    for kind, name in (("dispatcher", "task-dispatcher"), ("migration", "html-to-wp-engine-sol")):
        data = package(name)
        selections[kind] = dispatch_skills.save(ctx, None, kind, "", data, dispatch_skills.inspect(data))
    return dict(server_id=server.id, source="https://source.example.com/home?region=us",
                target="shop.example.com", **selections)


def test_admin_assigns_only_pending_inventory_sites(ctx):
    form = task_form(ctx)
    second = site_registry.create(ctx, "admin", "第二台服务器", "8.8.8.8", "22", "")

    def inventory(server_id, rows):
        ctx.conn.execute(
            "INSERT OR REPLACE INTO site_inventories VALUES (?, 0, ?, ?, NULL, ?)",
            (server_id, ctx.now(), ctx.now(), json.dumps([
                {"id": site_id, "domains": domains} for site_id, domains in rows
            ])),
        )

    first = form["server_id"]
    inventory(first, [
        ("1", ["www.shop.example.com", "shop.example.com"]),
        ("2", ["pending.example.com", "www.pending.example.com"]),
    ])
    inventory(second.id, [("2", ["other.example.com"])])
    app = create_app(ctx)
    admin, csrf = session(ctx, app, "admin")
    alice, alice_csrf = session(ctx, app, "alice")
    bob, _ = session(ctx, app, "bob")
    page = admin.get("/tasks").text
    assert 'action="/tasks/assign"' in page and 'name="source"' not in page
    assert "www.shop.example.com" not in page
    assert "pending.example.com" in page and "other.example.com" in page
    payload = dict(server_id=first, site_id="2", recipient="alice")
    assert alice.post("/tasks/assign", data=dict(payload, csrf=alice_csrf)).status_code == 403
    assert admin.post("/tasks/assign", data=payload).status_code == 403
    assert admin.post("/tasks/assign", data=dict(payload, csrf=csrf, site_id="missing")).status_code == 422
    assert admin.post("/tasks/assign", data=dict(payload, csrf=csrf)).status_code == 303
    assert admin.post("/tasks/assign", data=dict(payload, csrf=csrf, recipient="bob")).status_code == 422

    page = admin.get("/tasks").text
    assert "pending.example.com" not in page and "other.example.com" in page
    assert "pending.example.com" in alice.get("/tasks").text
    assert "待配置部署" in alice.get("/tasks").text
    assert "pending.example.com" not in bob.get("/tasks").text
    assert alice.post("/tasks/preview", data=dict(
        form, csrf=alice_csrf, target="pending.example.com",
    )).status_code == 422

    target = next(row for row in dispatch_tasks.list_targets(ctx, "alice") if row["domain"] == "pending.example.com")
    dispatch_tasks.save_target(
        ctx, target["id"], first, target["domain"], "https://panel.example.com/migrate",
        "BAOTA_PENDING_API_KEY", "", "alice",
    )
    assert alice.post("/tasks/preview", data=dict(
        form, csrf=alice_csrf, target=target["domain"],
    )).status_code == 303
    inventory(first, [("2", ["renamed.example.com"])])
    assert "renamed.example.com" not in admin.get("/tasks").text
    assert ctx.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='dispatch.site_assign'"
    ).fetchone()[0] == 1


def test_admin_assigns_a_batch_or_leaves_all_sites_pending(ctx):
    form = task_form(ctx)
    server_id = form["server_id"]
    ctx.conn.execute(
        "INSERT INTO site_inventories VALUES (?, 0, ?, ?, NULL, ?)",
        (server_id, ctx.now(), ctx.now(), json.dumps([
            {"id": "2", "domains": ["one.example.com", "alias.example.com"],
             "created_at": int(datetime(2026, 11, 14, 10, tzinfo=timezone.utc).timestamp())},
            {"id": "3", "domains": ["two.example.com"],
             "created_at": int(datetime(2026, 11, 15, 17, tzinfo=timezone.utc).timestamp())},
            {"id": "4", "domains": ["three.example.com", "alias.example.com"]},
        ])),
    )
    app = create_app(ctx)
    admin, csrf = session(ctx, app, "admin")
    alice, _ = session(ctx, app, "alice")
    bob, _ = session(ctx, app, "bob")
    page = admin.get("/tasks").text
    assert page.index("2026-11-16</h4>") < page.index("two.example.com") < page.index("2026-11-14</h4>")
    assert page.index("2026-11-14</h4>") < page.index("one.example.com") < page.index("日期未知</h4>")
    base = dict(server_id=server_id, recipient="alice", csrf=csrf)
    first, second, overlapping = (f"{server_id}:{site_id}" for site_id in ("2", "3", "4"))
    for selection in ([], [first, "other-server:3"], [first, f"{server_id}:missing"], [first, overlapping]):
        assert admin.post("/tasks/assign", data=dict(base, site_ids=selection)).status_code == 422
        assert ctx.conn.execute("SELECT COUNT(*) FROM dispatch_targets").fetchone()[0] == 1

    response = admin.post("/tasks/assign", data=dict(base, site_ids=[first, second, second]))
    assert response.status_code == 303
    pending = admin.get(response.headers["location"]).text
    assert "已将 2 个站点分配给 alice" in pending
    assert "one.example.com" not in pending and "two.example.com" not in pending
    assert "暂无待分配站点" in pending
    assert admin.post("/tasks/assign", data=dict(base, site_ids=[first, second])).status_code == 422
    assert "one.example.com" in alice.get("/tasks").text and "two.example.com" in alice.get("/tasks").text
    assert "one.example.com" not in bob.get("/tasks").text and "two.example.com" not in bob.get("/tasks").text


def test_user_previews_and_confirms_exactly_once(ctx):
    form = task_form(ctx)
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    app = create_app(ctx)
    alice, csrf = session(ctx, app, "alice")
    bob, bob_csrf = session(ctx, app, "bob")
    assert "shop.example.com" in alice.get("/tasks").text
    assert "shop.example.com" not in bob.get("/tasks").text
    assert bob.post("/tasks/preview", data=dict(form, recipient="alice", csrf=bob_csrf)).status_code == 422

    response = alice.post("/tasks/preview", data=dict(form, recipient="bob", csrf=csrf))
    assert response.status_code == 303
    url = response.headers["location"]
    draft = alice.get(url)
    assert "source.example.com/home 迁移 shop.example.com" in draft.text
    assert "网站,新域名,备注,折扣" in draft.text
    assert "ENV:BAOTA_SHOP_API_KEY" in draft.text
    assert "确认并发送" in draft.text
    assert dispatch_worker.claim(ctx) is None
    assert bob.get(url).status_code == 404
    assert bob.post(url + "/confirm", data={"csrf": bob_csrf, "confirmed": "1"}).status_code == 404
    assert alice.post(url + "/confirm", data={"csrf": csrf}).status_code == 409
    assert alice.post(url + "/confirm", data={"csrf": "invalid", "confirmed": "1"}).status_code == 403
    for _ in range(2):
        assert alice.post(url + "/confirm", data={"csrf": csrf, "confirmed": "1"}).status_code == 303
    task = dispatch_worker.claim(ctx)
    assert task["recipient"] == "alice"
    assert task["source_url"] == form["source"]
    assert dispatch_worker.claim(ctx) is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='dispatch.confirm'").fetchone()[0] == 1


def test_member_configures_and_completes_only_their_assigned_site(ctx):
    form = task_form(ctx)
    ctx.conn.execute(
        "INSERT INTO site_inventories VALUES (?, 0, ?, ?, NULL, ?)",
        (form["server_id"], ctx.now(), ctx.now(), json.dumps([
            {"id": "2", "domains": ["assigned.example.com"]},
        ])),
    )
    dispatch_tasks.assign_sites(ctx, form["server_id"], ["2"], "alice")
    target = next(row for row in dispatch_tasks.list_targets(ctx, "alice") if row["domain"] == "assigned.example.com")
    path = f"/tasks/sites/{target['id']}"
    app = create_app(ctx)
    alice, csrf = session(ctx, app, "alice")
    bob, bob_csrf = session(ctx, app, "bob")
    admin, _ = session(ctx, app, "admin")
    secret = "test-only-user-key"
    deployment = {"endpoint": "https://panel.example.com/migrate", "credential": secret, "project": "独立项目"}

    assert "配置部署" in alice.get("/tasks").text
    assert bob.get(path + "/deployment").status_code == 404
    assert admin.get(path + "/deployment").status_code == 404
    assert bob.post(path + "/deployment", data=dict(deployment, csrf=bob_csrf)).status_code == 404
    assert alice.post(path + "/deployment", data=deployment).status_code == 403
    assert alice.post(path + "/deployment", data=dict(
        deployment, csrf=csrf, endpoint="http://panel.example.com/migrate",
    )).status_code == 422
    assert alice.post(path + "/deployment", data=dict(deployment, csrf=csrf)).status_code == 303
    editor = alice.get(path + "/deployment").text
    assert "留空保留" in editor and secret not in editor
    row = ctx.conn.execute("SELECT credential FROM dispatch_user_deployments").fetchone()
    assert isinstance(row["credential"], bytes) and secret.encode() not in row["credential"]
    version = ctx.conn.execute("SELECT version FROM dispatch_user_deployments").fetchone()["version"]
    assert alice.post(path + "/deployment", data=dict(deployment, csrf=csrf, credential="")).status_code == 303
    assert ctx.conn.execute("SELECT version FROM dispatch_user_deployments").fetchone()["version"] == version
    preview = alice.post("/tasks/preview", data=dict(form, csrf=csrf, target="assigned.example.com"))
    assert preview.status_code == 303
    draft = alice.get(preview.headers["location"]).text
    assert "https://panel.example.com/migrate" in draft and "ENV:BAOTA_DEPLOY_API_KEY" in draft and secret not in draft
    assert alice.post(path + "/deployment", data=dict(deployment, csrf=csrf, project="更新", credential="")).status_code == 303
    assert alice.post(preview.headers["location"] + "/confirm", data={"csrf": csrf, "confirmed": "1"}).status_code == 409

    assigned_day = sites.deployment_date(ctx.now())
    assert "assigned.example.com" in admin.get(f"/tasks/assignments?day={assigned_day}&status=pending").text
    assert "assigned.example.com" not in admin.get("/tasks/assignments?day=2026-11-16").text
    assert bob.get("/tasks/assignments").status_code == 403
    assert bob.post(path + "/complete", data={"csrf": bob_csrf}).status_code == 404
    assert alice.post(path + "/complete").status_code == 403
    ctx.test_clock.t += 3600
    assert alice.post(path + "/complete", data={"csrf": csrf}).status_code == 303
    first_completion = ctx.now()
    ctx.test_clock.t += 3600
    assert alice.post(path + "/complete", data={"csrf": csrf}).status_code == 303
    status = ctx.conn.execute("SELECT completed_at FROM dispatch_site_work WHERE target_id=?", (target["id"],)).fetchone()
    assert status["completed_at"] == first_completion
    assert 'class="badge ok">已完成' in alice.get("/tasks").text
    assert "assigned.example.com" not in admin.get("/tasks/assignments?status=pending").text
    completed = admin.get(f"/tasks/assignments?day={assigned_day}&status=completed").text
    assert "assigned.example.com" in completed and 'class="badge ok">已完成' in completed
    assert "shop.example.com" not in completed


def test_migration_backfills_historical_assignment_dates(ctx, tmp_path):
    task_form(ctx)
    ctx.conn.execute("DROP TABLE dispatch_site_work")
    file = tmp_path / "existing.db"
    connection = sqlite3.connect(file)
    ctx.conn.backup(connection)
    connection.close()
    migrated = db.connect(file)
    try:
        row = migrated.execute("SELECT username, assigned_at, completed_at FROM dispatch_site_work").fetchone()
        assert tuple(row) == ("alice", ctx.now(), None)
    finally:
        migrated.close()


def test_admin_skill_crud_and_stale_preview(ctx):
    form = task_form(ctx)
    app = create_app(ctx)
    admin, csrf = session(ctx, app, "admin")
    alice, alice_csrf = session(ctx, app, "alice")
    response = alice.post("/tasks/preview", data=dict(form, csrf=alice_csrf))
    url = response.headers["location"]

    original = package("skill-example")
    data = dict(csrf=csrf, kind="migration", label="示例 Skill")
    files = {"package": ("example.zip", original, "application/zip")}
    assert alice.get("/skills").status_code == 403
    assert alice.post("/skills/save", data=dict(data, csrf=alice_csrf), files=files).status_code == 403
    assert admin.post("/skills/save", data=data, files=files).status_code == 303
    skill = next(row for row in dispatch_skills.list_skills(ctx.conn) if row["name"] == "skill-example")
    download = f"/skills/{skill['id']}/download"
    assert admin.get(download).content == original
    assert alice.get(download).status_code == 403
    assert admin.post("/skills/save", data=dict(data, skill_id=skill["id"], label="新名称")).status_code == 303
    assert "新名称" in admin.get("/skills").text
    updated = package("html-to-wp-engine-sol", "2.0")
    assert admin.post("/skills/save", data=dict(data, skill_id=form["migration"]),
                      files={"package": ("update.zip", updated, "application/zip")}).status_code == 303
    stale = alice.post(url + "/confirm", data={"csrf": alice_csrf, "confirmed": "1"})
    assert stale.status_code == 409 and "Skill 已更新" in stale.text
    assert dispatch_worker.claim(ctx) is None
    assert admin.post(f"/skills/{skill['id']}/delete", data={"csrf": csrf}).status_code == 303
    assert admin.get(download).status_code == 404


def test_zip_traversal_rejected(ctx):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../SKILL.md", "---\nname: escaped\n---\n")
    app = create_app(ctx)
    admin, csrf = session(ctx, app, "admin")
    response = admin.post(
        "/skills/save", data={"kind": "migration", "csrf": csrf},
        files={"package": ("unsafe.zip", stream.getvalue(), "application/zip")},
        follow_redirects=True,
    )
    assert "非法路径" in response.text
    assert dispatch_skills.list_skills(ctx.conn) == []


@pytest.mark.parametrize("self_configured", [False, True])
def test_worker_attaches_both_packages_and_persists_acceptance(ctx, tmp_path, monkeypatch, self_configured):
    form = task_form(ctx)
    credential_env = "BAOTA_SHOP_API_KEY"
    if self_configured:
        target = dispatch_tasks.list_targets(ctx, "alice")[0]
        dispatch_tasks.save_user_deployment(
            ctx, target["id"], "alice", "https://panel.example.com/migrate", "fixture-secret", "",
        )
        credential_env = dispatch_tasks.USER_CREDENTIAL_ENV
    task_id = dispatch_tasks.create_preview(ctx, "alice", "alice", form["server_id"], form["source"], form["target"],
                                            form["dispatcher"], form["migration"])
    row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    dispatch_tasks.confirm(ctx, row, "alice")
    task = dispatch_worker.claim(ctx)
    run_root = tmp_path / "dispatch"
    run_root.mkdir()
    monkeypatch.setenv("BAOTA_SHOP_API_KEY", "unused-admin-key" if self_configured else "fixture-secret")
    monkeypatch.setenv(dispatch_tasks.USER_CREDENTIAL_ENV, "unused-global-key")
    monkeypatch.setattr(dispatch_worker.os, "chown", lambda *args: None)

    # A stdio fixture verifies the actual JSON-RPC payload and can complete the turn before its acknowledgement.
    fake = tmp_path / "app-server.py"
    fake.write_text("""
import json
import os
import sys
from pathlib import Path
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']
    params = message['params']
    assert 'fixture-secret' not in line
    if method == 'thread/start':
        assert params['sandbox'] == 'workspace-write'
        assert os.environ['BAOTA_SHOP_API_KEY'] == 'fixture-secret'
        result = {'thread': {'id': 'codex-thread'}}
    elif method == 'thread/name/set':
        assert params == {'threadId': 'codex-thread', 'name': 'source.example.com/home 迁移 shop.example.com'}
        result = {}
    elif method == 'turn/start':
        prompt, dispatcher, migration = params['input']
        assert prompt['type'] == 'text' and 'ENV:BAOTA_SHOP_API_KEY' in prompt['text']
        assert Path('task.csv').read_text().splitlines()[0] == '网站,新域名,备注,折扣'
        for item in (dispatcher, migration):
            assert item['type'] == 'skill'
            path = Path(item['path'])
            assert path.is_file() and path.parent.name == item['name']
            assert (path.parent / 'scripts/example.py').is_file()
            assert not (path.parent / 'install.py').exists()
        result = {'turn': {'id': 'codex-turn'}}
        print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'id': 'codex-turn', 'status': 'completed'}}}), flush=True)
    else:
        result = {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
""".replace("BAOTA_SHOP_API_KEY", credential_env), encoding="utf-8")
    popen = subprocess.Popen

    def launch(args, **kwargs):
        user = ctx.lookup_user("alice", 1000)
        assert kwargs.pop("user") == user.uid
        assert kwargs.pop("group") == user.gid
        assert kwargs.pop("extra_groups") == []
        assert kwargs["env"][credential_env] == "fixture-secret"
        assert "unused-admin-key" not in kwargs["env"].values()
        assert "unused-global-key" not in kwargs["env"].values()
        assert args[-1] == "app-server"
        return popen([sys.executable, str(fake)], **kwargs)

    monkeypatch.setattr(dispatch_worker.subprocess, "Popen", launch)
    dispatch_worker.deliver(ctx, run_root, task)
    sent = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    assert (sent["status"], sent["thread_id"], sent["turn_id"], sent["turn_status"]) == (
        "sent", "codex-thread", "codex-turn", "completed",
    )
    assert "fixture-secret" not in json.dumps(dict(sent))
