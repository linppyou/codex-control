import dataclasses
import io
import json
import subprocess
import sys
import zipfile

from fastapi.testclient import TestClient

from codexctl import dispatch_skills, dispatch_tasks, dispatch_worker, repo, site_registry, webauth
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
    for name in ("alice", "bob"):
        ctx.test_users.add(name)
        account = repo.insert_account(
            ctx.conn, label=name, email=None, account_id=name, plan_type=None,
            last_refresh=ctx.now(), access_expires_at=ctx.now() + 60, now=ctx.now(),
        )
        repo.start_assignment(ctx.conn, name, account, ctx.now())
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


def test_worker_attaches_both_packages_and_persists_acceptance(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    task_id = dispatch_tasks.create_preview(ctx, "alice", "alice", form["server_id"], form["source"], form["target"],
                                            form["dispatcher"], form["migration"])
    row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    dispatch_tasks.confirm(ctx, row, "alice")
    task = dispatch_worker.claim(ctx)
    run_root = tmp_path / "dispatch"
    run_root.mkdir()
    monkeypatch.setenv("BAOTA_SHOP_API_KEY", "fixture-secret")
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
""", encoding="utf-8")
    popen = subprocess.Popen

    def launch(args, **kwargs):
        user = ctx.lookup_user("alice", 1000)
        assert kwargs.pop("user") == user.uid
        assert kwargs.pop("group") == user.gid
        assert kwargs.pop("extra_groups") == []
        assert args[-1] == "app-server"
        return popen([sys.executable, str(fake)], **kwargs)

    monkeypatch.setattr(dispatch_worker.subprocess, "Popen", launch)
    dispatch_worker.deliver(ctx, run_root, task)
    sent = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    assert (sent["status"], sent["thread_id"], sent["turn_id"], sent["turn_status"]) == (
        "sent", "codex-thread", "codex-turn", "completed",
    )
    assert "fixture-secret" not in json.dumps(dict(sent))
