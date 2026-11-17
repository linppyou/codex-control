import csv
import dataclasses
import io
import json
import os
import stat
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from codexctl import db, dispatch_runtime, dispatch_skills, dispatch_tasks, dispatch_worker, repo, site_registry, webauth
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
    inventory(ctx, server, ("8", ["shop.example.com", "www.shop.example.com"]))
    selections = {}
    for kind, name in (("dispatcher", "task-dispatcher"), ("migration", "html-to-wp-engine-sol")):
        data = package(name)
        selections[kind] = dispatch_skills.save(ctx, None, kind, "", data, dispatch_skills.inspect(data))
    return dict(server_id=server.id, source="https://source.example.com/home?region=us",
                target="shop.example.com", **selections)


def inventory(ctx, server, *sites):
    now = ctx.now()
    snapshot = [{"id": site_id, "domains": domains} for site_id, domains in sites]
    ctx.conn.execute(
        "INSERT OR REPLACE INTO site_inventories VALUES (?, ?, ?, ?, NULL, ?)",
        (server.id, server.revision, now, now, json.dumps(snapshot)),
    )


def import_file(client, csrf, form, content=None, filename="tasks.csv", **extra):
    if content is None:
        content = f'网站,新域名,备注,折扣\n{form["source"]},{form["target"]},"产品, 海报",8.5折\n'.encode()
    return client.post(
        "/tasks/import", data=dict(csrf=csrf, dispatcher=form["dispatcher"], migration=form["migration"], **extra),
        files={"task_csv": (filename, content, "application/octet-stream")},
    )


def test_user_previews_and_confirms_exactly_once(ctx):
    form = task_form(ctx)
    ctx.settings = dataclasses.replace(ctx.settings, web_cookie_secure=False)
    app = create_app(ctx)
    alice, csrf = session(ctx, app, "alice")
    bob, bob_csrf = session(ctx, app, "bob")
    admin, admin_csrf = session(ctx, app, "admin")
    assert "CSV 或 XLSX 文件" in alice.get("/tasks").text
    assert "CSV 或 XLSX 文件" in bob.get("/tasks").text
    assert "CSV 或 XLSX 文件" not in admin.get("/tasks").text
    assert import_file(admin, admin_csrf, form, recipient="alice").status_code == 403
    assert import_file(alice, "invalid", form).status_code == 403

    for _ in range(2):
        response = import_file(alice, csrf, form, recipient="bob")
        assert response.status_code == 200
        assert "产品, 海报" in response.text and "8.5折" in response.text and "已匹配" in response.text
    rows = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchall()
    assert len(rows) == 1 and rows[0]["owner"] == "alice"
    assert list(csv.reader(io.StringIO(rows[0]["csv"]))) == [
        ["网站", "新域名", "备注", "折扣"], [form["source"], form["target"], "产品, 海报", "8.5折"],
    ]
    url = f"/tasks/{rows[0]['id']}"
    draft = alice.get(url)
    assert "tasks.csv · 1 个任务" in draft.text
    assert "网站,新域名,备注,折扣" not in rows[0]["prompt"]
    assert "产品, 海报" not in rows[0]["prompt"]
    assert "source.example.com" not in rows[0]["prompt"]
    assert "SSH 主机 1.1.1.1，端口 22" in draft.text
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
    assert import_file(bob, bob_csrf, dict(form, target="www.shop.example.com")).status_code == 200
    assert ctx.conn.execute("SELECT COUNT(*) FROM dispatch_targets").fetchone()[0] == 0
    bob_task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE owner='bob'").fetchone()
    assert bob_task["recipient"] == "bob"
    assert json.loads(bob_task["target_snapshot"])["targets"][0]["id"] == json.loads(task["target_snapshot"])["targets"][0]["id"]
    assert bob.post(f"/tasks/{bob_task['id']}/confirm", data={"csrf": bob_csrf, "confirmed": "1"}).status_code == 303
    assert dispatch_worker.claim(ctx) is None


def test_confirm_saves_execution_options_and_preserves_them_on_retry(ctx):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), "alice")
    assert import_file(alice, csrf, form).status_code == 200
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    url = f"/tasks/{task['id']}"
    draft = alice.get(url)
    assert 'name="model"' in draft.text and 'name="reasoning_effort"' in draft.text
    assert 'value="gpt-6.1-terra"' in draft.text and 'value="medium"' in draft.text
    values = {"csrf": csrf, "model": " gpt-6.1-sol ", "reasoning_effort": " xhigh "}
    response = alice.post(url + "/confirm", data=values)
    assert response.status_code == 409
    assert 'value=" gpt-6.1-sol "' in response.text
    assert 'value=" xhigh "' in response.text
    assert alice.post(url + "/confirm", data=dict(values, confirmed="1")).status_code == 303
    assert alice.post(url + "/confirm", data={"csrf": csrf, "confirmed": "1"}).status_code == 303
    saved = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert (saved["model"], saved["reasoning_effort"]) == ("gpt-6.1-sol", "xhigh")
    assert saved["status"] == "queued"
    detail = alice.get(url).text
    assert "gpt-6.1-sol" in detail and "xhigh" in detail
    assert 'name="model"' not in detail


def test_blank_execution_options_use_task_defaults(ctx):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), "alice")
    import_file(alice, csrf, form)
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    response = alice.post(f"/tasks/{task['id']}/confirm", data={
        "csrf": csrf, "confirmed": "1", "model": "  ", "reasoning_effort": "  ",
    })
    assert response.status_code == 303
    saved = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert (saved["model"], saved["reasoning_effort"]) == ("gpt-6.1-terra", "medium")


@pytest.mark.parametrize("options", [
    {"model": "bad\nmodel"}, {"model": "m" * 129},
    {"reasoning_effort": "high\nlow"}, {"reasoning_effort": "h" * 33},
])
def test_confirm_rejects_invalid_execution_options(ctx, options):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), "alice")
    import_file(alice, csrf, form)
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    response = alice.post(f"/tasks/{task['id']}/confirm", data={"csrf": csrf, "confirmed": "1", **options})
    assert response.status_code == 409
    saved = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert saved["status"] == "preview"
    assert saved["model"] == "gpt-6.1-terra" and saved["reasoning_effort"] == "medium"


def test_prompt_editing_saves_draft_and_confirms_current_text(ctx):
    form = task_form(ctx)
    app = create_app(ctx)
    alice, csrf = session(ctx, app, "alice")
    bob, bob_csrf = session(ctx, app, "bob")
    admin, admin_csrf = session(ctx, app, "admin")
    assert import_file(alice, csrf, form).status_code == 200
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    url = f"/tasks/{task['id']}"
    draft = "手工修改\n</textarea><script>example()</script>\n保留换行和空格  "
    assert '<textarea class="dispatch-prompt dispatch-prompt-editor"' in alice.get(url).text
    assert "<textarea" not in admin.get(url).text
    assert alice.post(url + "/prompt", data={"csrf": "invalid", "prompt": draft}).status_code == 403
    assert bob.post(url + "/prompt", data={"csrf": bob_csrf, "prompt": draft}).status_code == 404
    assert admin.post(url + "/prompt", data={"csrf": admin_csrf, "prompt": draft}).status_code == 403
    assert alice.post(url + "/prompt", data={"csrf": csrf, "prompt": draft}).status_code == 303
    saved = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert saved["prompt"] == draft and saved["status"] == "preview"
    assert dispatch_worker.claim(ctx) is None
    page = alice.get(url).text
    assert "&lt;/textarea&gt;&lt;script&gt;" in page
    assert "<script>example()" not in page
    assert import_file(alice, csrf, form).status_code == 200
    assert ctx.conn.execute("SELECT prompt FROM dispatch_tasks").fetchone()[0] == draft

    for prompt in ("", "  \n  "):
        for action in ("/prompt", "/confirm"):
            response = alice.post(url + action, data={"csrf": csrf, "confirmed": "1", "prompt": prompt})
            assert response.status_code == 409 and "提示词不能为空" in response.text
        assert ctx.conn.execute("SELECT prompt FROM dispatch_tasks").fetchone()[0] == draft

    final_prompt = "确认发送时修改\n" + draft
    response = alice.post(url + "/confirm", data={"csrf": csrf, "prompt": final_prompt})
    assert response.status_code == 409 and "确认发送时修改" in response.text
    assert ctx.conn.execute("SELECT prompt FROM dispatch_tasks").fetchone()[0] == draft
    assert alice.post(url + "/confirm", data={
        "csrf": csrf, "confirmed": "1", "prompt": final_prompt,
    }).status_code == 303
    assert "<textarea" not in alice.get(url).text
    assert alice.post(url + "/prompt", data={"csrf": csrf, "prompt": draft}).status_code == 409
    assert alice.post(url + "/confirm", data={
        "csrf": csrf, "confirmed": "1", "prompt": draft,
    }).status_code == 303
    queued = dispatch_worker.claim(ctx)
    assert queued["prompt"] == final_prompt
    assert queued["csv"] == task["csv"] and queued["skills"] == task["skills"]
    assert queued["target_snapshot"] == task["target_snapshot"]
    assert dispatch_worker.claim(ctx) is None


def test_import_matches_across_servers_and_renders_each_row(ctx):
    form = task_form(ctx)
    first = site_registry.get(ctx.conn, form["server_id"])
    second = site_registry.create(ctx, "admin", "另一台服务器", "8.8.8.8", "2222", "")
    inventory(ctx, first, ("8", ["shop.example.com", "www.shop.example.com"]), ("9", ["shared.example.com"]))
    inventory(ctx, second, ("8", ["shared.example.com"]), ("10", ["new.example.com"]))
    alice, csrf = session(ctx, create_app(ctx), "alice")
    content = (
        '折扣,网站,备注,新域名\r\n'
        '8,one.example.com,"长备注,<草稿>\n下一行",HTTPS://WWW.SHOP.EXAMPLE.COM./\r\n'
        ',two.example.com,,new.example.com\r\n'
        '3,three.example.com,,shared.example.com\r\n'
        '3,four.example.com,,missing.example.com\r\n'
        '3,five.example.com,,dupe.example.com\r\n'
        '3,six.example.com,,DUPE.EXAMPLE.COM\r\n'
        '10.1,seven.example.com,,bad.example.com\r\n'
    ).encode("gb18030")
    response = import_file(alice, csrf, form, content)
    assert response.status_code == 422
    for value in (
        "HTTPS://WWW.SHOP.EXAMPLE.COM./", "长备注,&lt;草稿&gt;\n下一行", "已匹配 2 行", "另一台服务器",
        "全部站点匹配成功", "匹配到多个站点", "找不到该新域名", "新域名重复", "折扣须大于 0",
    ):
        assert value in response.text
    assert "查看批次提示词" not in response.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM dispatch_tasks").fetchone()[0] == 0
    content = (
        '折扣,网站,备注,新域名\r\n'
        '8,one.example.com,"长备注,<草稿>\n下一行",HTTPS://WWW.SHOP.EXAMPLE.COM./\r\n'
        ',two.example.com,,new.example.com\r\n'
    ).encode("gb18030")
    response = import_file(alice, csrf, form, content)
    assert response.status_code == 200 and "查看批次提示词" in response.text
    tasks = ctx.conn.execute("SELECT * FROM dispatch_tasks ORDER BY rowid").fetchall()
    assert len(tasks) == 1
    assert len(list(csv.reader(io.StringIO(tasks[0]["csv"])))) == 3
    assert len(json.loads(tasks[0]["target_snapshot"])["targets"]) == 2
    assert "全部 2 个数据行" in tasks[0]["prompt"]
    assert list(csv.reader(io.StringIO(tasks[0]["csv"])))[1] == [
        "https://one.example.com/", "www.shop.example.com", "长备注,<草稿>\n下一行", "8",
    ]
    assert "2222" in tasks[0]["prompt"]
    assert "missing.example.com" not in tasks[0]["prompt"]
    assert "单站任务" not in tasks[0]["prompt"]
    assert "长备注" not in tasks[0]["prompt"]
    assert tasks[0]["upload"] == content

    inventory(ctx, second, ("10", ["new.example.com", "www.shop.example.com"]))
    stale = alice.post(f"/tasks/{tasks[0]['id']}/confirm", data={"csrf": csrf, "confirmed": "1"})
    assert stale.status_code == 409 and "服务器绑定已变更" in stale.text
    assert import_file(alice, csrf, form, b"invalid,headers\nrow").status_code == 422
    unmatched = import_file(alice, csrf, dict(form, target="missing.example.com"))
    assert unmatched.status_code == 422 and "已匹配 0 行" in unmatched.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM dispatch_tasks").fetchone()[0] == 1


def workbook_bytes(rows):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    workbook.create_sheet("说明").append(["忽略此表"])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
def test_import_ignores_extra_columns_and_defaults_discount(ctx, extension):
    form = task_form(ctx)
    data = [
        ["额外列", " 折扣 ", "备注", "网站", "新域名", "额外列", ""],
        ["=1/0", "  ", "产品,海报\n第二行", form["source"], form["target"], "#VALUE!", "忽略末列"],
        ["仅额外列有值", "", "", "", "", "=1/0"],
    ]
    if extension == "xlsx":
        raw = workbook_bytes(data)
    else:
        stream = io.StringIO(newline="")
        csv.writer(stream).writerows(data)
        raw = stream.getvalue().encode("utf-8-sig")
    rows = dispatch_tasks.parse_import(raw, f"tasks.{extension}")
    assert len(rows) == 1
    assert rows[0].line == 2 and rows[0].error == ""
    assert rows[0].discount == "3"
    assert rows[0].values == (form["source"], form["target"], "产品,海报\n第二行", "3")

    alice, csrf = session(ctx, create_app(ctx), "alice")
    response = import_file(alice, csrf, form, raw, filename=f"tasks.{extension}")
    assert response.status_code == 200 and "已匹配 1 行" in response.text
    assert "仅额外列有值" not in response.text
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert list(csv.reader(io.StringIO(task["csv"])))[1][-1] == "3"
    assert "已按 3 折填入 task.csv" in task["prompt"]
    assert task["upload"] == raw


@pytest.mark.parametrize("extension", ["csv", "xlsx"])
@pytest.mark.parametrize("header", [
    ["原域名", "新域名", "备注", "折扣"],
    ["网站", "新域名", "折扣"],
    ["网站", "新域名", "备注", "折扣", "网站"],
])
def test_import_requires_unique_required_headers(extension, header):
    raw = workbook_bytes([header]) if extension == "xlsx" else (",".join(header) + "\n").encode()
    with pytest.raises(dispatch_tasks.TaskError, match="网站、新域名、备注、折扣"):
        dispatch_tasks.parse_import(raw, f"tasks.{extension}")


def test_csv_import_pads_empty_trailing_fields_and_ignores_unheaded_values():
    rows = dispatch_tasks.parse_import(
        "网站,新域名,备注,折扣,其他\nsource.example.com,shop.example.com\n"
        "other.example.com,new.example.com,,8.5折,忽略,也忽略\n".encode()
    )
    assert [row.error for row in rows] == ["", ""]
    assert [row.discount for row in rows] == ["3", "8.5折"]


def test_batch_queue_blocks_overlapping_site_and_accepts_legacy_task(ctx):
    form = task_form(ctx)
    server = site_registry.get(ctx.conn, form["server_id"])
    inventory(ctx, server, ("8", ["shop.example.com"]), ("9", ["second.example.com", "www.second.example.com"]))
    content = (
        "网站,新域名,备注,折扣\n"
        "source.example.com,shop.example.com,,\n"
        "other.example.com,second.example.com,,\n"
    ).encode()
    rows = dispatch_tasks.parse_import(content)
    alice_id = dispatch_tasks.create_previews(ctx, "alice", rows, form["dispatcher"], form["migration"])
    alice_task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (alice_id,)).fetchone()
    dispatch_tasks.confirm(ctx, alice_task, "alice")
    assert dispatch_worker.claim(ctx)["id"] == alice_id

    rows = dispatch_tasks.parse_import(
        "网站,新域名,备注,折扣\nsource.example.com,www.second.example.com,,\n".encode()
    )
    bob_id = dispatch_tasks.create_previews(ctx, "bob", rows, form["dispatcher"], form["migration"])
    bob_task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (bob_id,)).fetchone()
    # A persisted single-site SSH preview must still validate and share the site's queue lock.
    legacy_target = json.loads(bob_task["target_snapshot"])["targets"][0]
    ctx.conn.execute("UPDATE dispatch_tasks SET target_snapshot=?, target_id=? WHERE id=?",
                     (json.dumps(legacy_target), legacy_target["id"], bob_id))
    bob_task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (bob_id,)).fetchone()
    dispatch_tasks.confirm(ctx, bob_task, "bob")
    assert dispatch_worker.claim(ctx) is None
    ctx.conn.execute("UPDATE dispatch_tasks SET status='sent', turn_status='completed' WHERE id=?", (alice_id,))
    assert dispatch_worker.claim(ctx)["id"] == bob_id


def test_xlsx_import_shares_matching_and_preserves_values(ctx):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), "alice")
    raw = workbook_bytes([
        ["折扣", "新域名", "备注", "网站"],
        [8.5, form["target"], "产品,海报\n第二行", form["source"]],
        [],
        [None, "missing.example.com", None, "other.example.com"],
        ["=1+2", "formula.example.com", None, "other.example.com"],
    ])
    response = import_file(alice, csrf, form, raw, filename="任务.XLSX")
    assert response.status_code == 422
    for text in ("产品,海报\n第二行", "8.5", "已匹配 1 行", "找不到该新域名", "单元格含公式"):
        assert text in response.text
    assert "查看批次提示词" not in response.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM dispatch_tasks").fetchone()[0] == 0
    rows = dispatch_tasks.parse_import(raw, "任务.xlsx")
    assert [row.line for row in rows] == [2, 4, 5]
    assert rows[1].values[2:] == ("", "3")
    raw = workbook_bytes([
        ["折扣", "新域名", "备注", "网站"],
        [8.5, form["target"], "产品,海报\n第二行", form["source"]],
    ])
    response = import_file(alice, csrf, form, raw, filename="任务.XLSX")
    assert response.status_code == 200 and "查看批次提示词" in response.text
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    assert task["upload"] == raw
    assert "upload.xlsx" in task["prompt"]
    assert "产品,海报" not in task["prompt"]
    assert "8.5" not in task["prompt"]
    run_root = ctx.settings.state_dir / "xlsx-staging"
    run_root.mkdir()
    user = ctx.lookup_user("alice", 1000)
    skill_packages = [dispatch_skills.get(ctx.conn, skill["id"], with_archive=True)["archive"]
                      for skill in json.loads(task["skills"])]
    # The upload is staged byte-for-byte alongside the normalized CSV and deployment mapping.
    from unittest.mock import patch
    with patch.object(dispatch_worker.os, "chown"):
        dispatch_worker._stage(run_root, task, user, skill_packages)
    assert (run_root / task["id"] / "upload.xlsx").read_bytes() == raw
    assert list(csv.reader(io.StringIO(task["csv"])))[1] == [
        form["source"], form["target"], "产品,海报\n第二行", "8.5",
    ]
    assert "忽略此表" not in response.text


@pytest.mark.parametrize("raw, filename", [
    (b"broken", "broken.xlsx"),
    (b"broken", "broken.xls"),
    (workbook_bytes([["wrong header"]]), "header.xlsx"),
    (workbook_bytes([dispatch_tasks.IMPORT_COLUMNS]), "empty.xlsx"),
    (b"x" * (dispatch_tasks.MAX_CSV_UPLOAD + 1), "large.xlsx"),
])
def test_bad_spreadsheets_report_validation_error(raw, filename):
    with pytest.raises(dispatch_tasks.TaskError):
        dispatch_tasks.parse_import(raw, filename)


def test_admin_skill_crud_and_stale_preview(ctx):
    form = task_form(ctx)
    app = create_app(ctx)
    admin, csrf = session(ctx, app, "admin")
    alice, alice_csrf = session(ctx, app, "alice")
    assert import_file(alice, alice_csrf, form).status_code == 200
    url = "/tasks/" + ctx.conn.execute("SELECT id FROM dispatch_tasks").fetchone()[0]

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


def test_sync_user_skills_publishes_catalog_updates_and_deletions(ctx):
    form = task_form(ctx)
    for user in ctx.list_users(1000):
        old = user.home / ".codex" / "skills" / "old-version"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("old")
    previous_umask = os.umask(0o077)
    try:
        with db.transaction(ctx.conn):
            dispatch_skills.sync_user_skills(ctx)
    finally:
        os.umask(previous_umask)
    target = ctx.settings.state_dir / "skills"
    first_release = target.resolve()
    assert stat.S_IMODE(first_release.stat().st_mode) == 0o755
    assert stat.S_IMODE(first_release.parent.stat().st_mode) == 0o755
    for user in ctx.list_users(1000):
        for directory in (".codex", ".agents"):
            link = user.home / directory / "skills"
            assert link.is_symlink() and link.resolve() == first_release
            assert not (link / "old-version").exists()
            assert "1.0" in (link / "html-to-wp-engine-sol" / "SKILL.md").read_text()
            assert stat.S_IMODE((link / "html-to-wp-engine-sol" / "SKILL.md").stat().st_mode) == 0o644
            assert not (link / "install.py").exists()
    with db.transaction(ctx.conn):
        dispatch_skills.sync_user_skills(ctx)
    assert target.resolve() == first_release

    data = package("html-to-wp-engine-sol", "2.0")
    dispatch_skills.save(ctx, form["migration"], "migration", "", data, dispatch_skills.inspect(data))
    dispatch_skills.delete(ctx, form["dispatcher"])
    ctx.test_users.add("charlie")
    with db.transaction(ctx.conn):
        dispatch_skills.sync_user_skills(ctx)
    assert target.resolve() != first_release
    assert "1.0" in (first_release / "html-to-wp-engine-sol" / "SKILL.md").read_text()
    for user in ctx.list_users(1000):
        for directory in (".codex", ".agents"):
            link = user.home / directory / "skills"
            assert "2.0" in (link / "html-to-wp-engine-sol" / "SKILL.md").read_text()
            assert not (link / "task-dispatcher").exists()


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


@pytest.mark.parametrize("cancel_running", [False, True])
@pytest.mark.parametrize("execution", [None, {}, {"model": "gpt-6.1-sol"}, {"reasoning_effort": "xhigh"},
                                        {"model": "gpt-6.1-sol", "reasoning_effort": "high"}])
def test_worker_attaches_both_packages_and_persists_acceptance(ctx, tmp_path, monkeypatch, cancel_running, execution):
    form = task_form(ctx)
    second = site_registry.create(ctx, "admin", "第二台", "8.8.8.8", "2222", "")
    inventory(ctx, second, ("9", ["second.example.com"]))
    raw = (
        f'网站,新域名,备注,折扣\n{form["source"]},{form["target"]},产品海报,8.5折\n'
        'other.example.com,second.example.com,第二站,3\n'
    ).encode("utf-8-sig")
    rows = dispatch_tasks.parse_import(raw)
    dispatch_tasks.create_previews(ctx, "alice", rows, form["dispatcher"], form["migration"], upload=raw)
    task_id = rows[0].task_id
    row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    dispatch_tasks.confirm(ctx, row, "alice", row["prompt"] + "\n用户修改后的发送内容。", **(execution or {}))
    expected = dict(model="gpt-6.1-terra", reasoning_effort="medium")
    if execution is None:
        ctx.conn.execute("UPDATE dispatch_tasks SET model=NULL, reasoning_effort=NULL WHERE id=?", (task_id,))
        expected = {}
    else:
        expected.update(execution)
    task = dispatch_worker.claim(ctx)
    run_root = tmp_path / "dispatch"
    run_root.mkdir()
    monkeypatch.setenv("BAOTA_SHOP_API_KEY", "fixture-secret")
    monkeypatch.setattr(dispatch_worker.os, "chown", lambda *args: None)
    monkeypatch.setattr(ctx.publisher, "link_skills", lambda *args: None)

    monkeypatch.setattr(dispatch_worker, "_stage_ssh", lambda *args: "SSH test instructions")
    monkeypatch.setattr(dispatch_worker, "_browser_environment", lambda *args: "Browser test instructions")

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
        assert params['sandbox'] == 'danger-full-access'
        assert params['approvalPolicy'] == 'never'
        assert 'BAOTA_SHOP_API_KEY' not in os.environ
        result = {'thread': {'id': 'codex-thread'}}
    elif method == 'thread/name/set':
        assert params == {'threadId': 'codex-thread', 'name': 'tasks.csv · 2 个任务'}
        result = {}
    elif method == 'turn/start':
        expected = EXECUTION_OPTIONS
        assert params.get('model') == expected.get('model')
        assert params.get('effort') == expected.get('reasoning_effort')
        assert ('model' in params) == ('model' in expected)
        assert ('effort' in params) == ('reasoning_effort' in expected)
        prompt, dispatcher, migration = params['input']
        assert prompt['type'] == 'text' and '统一分发' in prompt['text']
        assert '用户修改后的发送内容。' in prompt['text']
        assert Path('task.csv').read_text().splitlines()[0] == '网站,新域名,备注,折扣'
        assert '产品海报,8.5折' in Path('task.csv').read_text()
        targets = json.loads(Path('targets.json').read_text())
        assert targets[0]['target']['server_port'] == 22
        assert targets[0]['csv_row'] == 1
        assert targets[1]['target']['server_port'] == 2222
        assert len(Path('task.csv').read_text().splitlines()) == 3
        assert Path('upload.csv').read_bytes().startswith(bytes([239, 187, 191]))
        assert Path('upload.csv').read_text(encoding='utf-8-sig').startswith('网站,新域名,备注,折扣')
        assert '产品海报' not in prompt['text'] and 'other.example.com' not in prompt['text']
        assert 'shop.example.com' in prompt['text'] and 'second.example.com' in prompt['text']
        for item in (dispatcher, migration):
            assert item['type'] == 'skill'
            path = Path(item['path'])
            assert path.is_file() and path.parent.name == item['name']
            assert (path.parent / 'scripts/example.py').is_file()
            assert not (path.parent / 'install.py').exists()
        result = {'turn': {'id': 'codex-turn'}}
        if not CANCEL_RUNNING:
            print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'id': 'codex-turn', 'status': 'completed'}}}), flush=True)
    elif method == 'turn/interrupt':
        assert params == {'threadId': 'codex-thread', 'turnId': 'codex-turn'}
        print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'id': 'codex-turn', 'status': 'interrupted'}}}), flush=True)
        result = {}
    else:
        result = {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
""".replace("CANCEL_RUNNING", repr(cancel_running)).replace("EXECUTION_OPTIONS", repr(expected)), encoding="utf-8")
    popen = subprocess.Popen

    def launch(args, **kwargs):
        user = ctx.lookup_user("alice", 1000)
        assert kwargs.pop("user") == user.uid
        assert kwargs.pop("group") == user.gid
        assert kwargs.pop("extra_groups") == []
        return popen([sys.executable, str(fake)], **kwargs)

    monkeypatch.setattr(dispatch_worker.subprocess, "Popen", launch)
    monkeypatch.setattr(dispatch_worker, '_connect_shared', lambda ctx, user, cwd, env: dispatch_worker.AppServer(
        launch([], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
               stderr=subprocess.DEVNULL, start_new_session=True, user=user.uid, group=user.gid, extra_groups=[])))
    if cancel_running:
        original_wait = dispatch_worker.AppServer.wait_turn

        def wait_and_cancel(client, turn_id, check_cancel):
            dispatch_tasks.cancel(ctx, task_id, "alice")
            assert ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()[0] == "cancelling"
            return original_wait(client, turn_id, check_cancel)

        monkeypatch.setattr(dispatch_worker.AppServer, "wait_turn", wait_and_cancel)
    dispatch_worker.deliver(ctx, run_root, task)
    sent = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    assert (sent["status"], sent["thread_id"], sent["turn_id"], sent["turn_status"]) == (
        "cancelled" if cancel_running else "sent", "codex-thread", "codex-turn", "interrupted" if cancel_running else "completed",
    )
    assert "fixture-secret" not in json.dumps({key: sent[key] for key in sent.keys() if key != "upload"})


@pytest.mark.parametrize("status", ["preview", "queued", "sending", "sent", "failed", "unknown"])
def test_members_cancel_own_tasks_only(ctx, status):
    form = task_form(ctx)
    app = create_app(ctx)
    alice, csrf = session(ctx, app, "alice")
    bob, bob_csrf = session(ctx, app, "bob")
    admin, admin_csrf = session(ctx, app, "admin")
    assert import_file(alice, csrf, form).status_code == 200
    task_id = ctx.conn.execute("SELECT id FROM dispatch_tasks").fetchone()[0]
    ctx.conn.execute("UPDATE dispatch_tasks SET status=?, turn_status='inProgress' WHERE id=?", (status, task_id))
    url = f"/tasks/{task_id}"
    assert "取消任务" in alice.get(url).text
    assert "取消任务" in alice.get("/tasks").text
    assert bob.post(url + "/cancel", data={"csrf": bob_csrf}).status_code == 404
    assert admin.post(url + "/cancel", data={"csrf": admin_csrf}).status_code == 403
    assert alice.post(url + "/cancel", data={"csrf": "invalid"}).status_code == 403
    for _ in range(2):
        assert alice.post(url + "/cancel", data={"csrf": csrf}).status_code == 303
    row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == ("cancelling" if status in {"sending", "sent"} else "cancelled")
    assert "取消任务" not in alice.get(url).text
    assert dispatch_worker.claim(ctx) is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='dispatch.cancel'").fetchone()[0] == 1
    dispatch_worker._record(ctx, row, "sent", thread_id="test-thread")
    assert ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()[0] == row["status"]


def test_cancelled_claim_is_not_started(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    rows = dispatch_tasks.parse_import(f'网站,新域名,备注,折扣\n{form["source"]},{form["target"]},,\n'.encode())
    task_id = dispatch_tasks.create_previews(ctx, "alice", rows, form["dispatcher"], form["migration"])
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    dispatch_tasks.confirm(ctx, task, "alice")
    task = dispatch_worker.claim(ctx)
    dispatch_tasks.cancel(ctx, task_id, "alice")

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Cancelled task started a subprocess")

    monkeypatch.setattr(dispatch_worker.subprocess, "Popen", unexpected_launch)
    dispatch_worker.deliver(ctx, tmp_path, task)
    assert ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()[0] == "cancelled"


def test_old_partial_batch_cannot_be_confirmed_or_executed(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), "alice")
    assert import_file(alice, csrf, form).status_code == 200
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks").fetchone()
    snapshot = json.loads(task["target_snapshot"])
    snapshot["rows"].append(dict(csv_row=2, line=3, target=None, error="站点清单中找不到该新域名"))
    ctx.conn.execute("UPDATE dispatch_tasks SET target_snapshot=? WHERE id=?", (json.dumps(snapshot), task["id"]))
    url = f"/tasks/{task['id']}"
    assert "确认并发送" not in alice.get(url).text
    response = alice.post(url + "/confirm", data={"csrf": csrf, "confirmed": "1"})
    assert response.status_code == 409 and "未匹配或无效站点" in response.text
    assert dispatch_worker.claim(ctx) is None
    # Older queued partial imports are checked again before starting Codex.
    ctx.conn.execute("UPDATE dispatch_tasks SET status='queued' WHERE id=?", (task["id"],))
    task = dispatch_worker.claim(ctx)

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Partial batch must never launch Codex")

    monkeypatch.setattr(dispatch_worker.subprocess, "Popen", unexpected_launch)
    dispatch_worker.deliver(ctx, tmp_path, task)
    stored = ctx.conn.execute("SELECT status, error FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
    assert stored["status"] == "failed" and "未匹配或无效站点" in stored["error"]


def test_legacy_preview_rechecks_assignment_before_confirmation(ctx):
    form = task_form(ctx)
    rows = dispatch_tasks.parse_import(
        f'网站,新域名,备注,折扣\n{form["source"]},{form["target"]},,\n'.encode()
    )
    task_id = dispatch_tasks.create_previews(ctx, "alice", rows, form["dispatcher"], form["migration"])
    target_id = "a" * 32
    server = site_registry.get(ctx.conn, form["server_id"])
    ctx.conn.execute(
        "INSERT INTO dispatch_targets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (target_id, server.id, server.revision, form["target"], "https://deploy.example.com", "DEPLOY_KEY", "", '["alice"]'),
    )
    snapshot = dict(ctx.conn.execute(dispatch_tasks._LEGACY_TARGET_FIELDS, (target_id,)).fetchone())
    snapshot.pop("server_port")
    ctx.conn.execute("UPDATE dispatch_tasks SET target_id=?, target_snapshot=? WHERE id=?",
                     (target_id, json.dumps(snapshot), task_id))
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    assert dispatch_tasks.validate_snapshot(ctx, task)[0].name == "alice"
    ctx.conn.execute("UPDATE dispatch_targets SET users='[\"bob\"]' WHERE id=?", (target_id,))
    snapshot["users"] = '["bob"]'
    ctx.conn.execute("UPDATE dispatch_tasks SET target_snapshot=? WHERE id=?", (json.dumps(snapshot), task_id))
    task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
    with pytest.raises(dispatch_tasks.TaskError, match="部署配置已变更"):
        dispatch_tasks.confirm(ctx, task, "alice")
    assert ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()[0] == "preview"


def test_stage_ssh_uses_pinned_hosts_and_user_key(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    server = site_registry.get(ctx.conn, form['server_id'])
    ctx.conn.execute('UPDATE site_servers SET host_key=? WHERE id=?', (server.public_key, server.id))
    rows = dispatch_tasks.parse_import(f'网站,新域名,备注,折扣\na.example.com,{form["target"]},,3\n'.encode())
    task_id = dispatch_tasks.create_previews(ctx, 'alice', rows, form['dispatcher'], form['migration'])
    task = ctx.conn.execute('SELECT * FROM dispatch_tasks WHERE id=?', (task_id,)).fetchone()
    user = ctx.lookup_user('alice', 1000)
    key = ctx.settings.state_dir / 'site-access' / f'alice-{user.uid}' / 'id_ed25519'
    key.parent.mkdir(parents=True)
    key.write_text('fixture')
    monkeypatch.setattr(dispatch_worker.os, 'chown', lambda *args: None)
    root = tmp_path / 'work'
    root.mkdir()
    instructions = dispatch_worker._stage_ssh(ctx, task, user, root)
    config = (root / '.ssh/config').read_text()
    assert 'StrictHostKeyChecking yes' in config
    assert 'GlobalKnownHostsFile /dev/null' in config
    assert f'IdentityFile "{key}"' in config
    assert 'User root' in config and 'Port 22' in config
    assert (root / '.ssh/known_hosts').read_text() == f'1.1.1.1 {server.public_key}\n'
    assert str(root / '.ssh/config') in instructions
    for name in ('ssh', 'scp', 'sftp'):
        text = (root / 'bin' / name).read_text()
        assert f'exec /usr/bin/{name} -F ' in text
        assert '"$@"' in text


def test_runtime_creates_real_children_once_and_reuses_skill_inputs(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    rows = dispatch_tasks.parse_import(f'网站,新域名,备注,折扣\na.example.com,{form["target"]},,3\n'.encode())
    task_id = dispatch_tasks.create_previews(ctx, 'alice', rows, form['dispatcher'], form['migration'])
    dispatch_tasks.confirm(ctx, ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone(), 'alice')
    task = ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    user = ctx.lookup_user('alice', 1000)
    calls = []

    class FakeClient:
        def __init__(self, proc):
            self.proc = proc
            self.result_text = ''
            self.completed = None

        def send(self, message):
            pass

        def call(self, method, params):
            calls.append((method, params))
            if method == 'thread/start':
                return {'thread': {'id': 'child-1'}}
            if method == 'turn/start':
                return {'turn': {'id': 'turn-1'}}
            return {}

        def wait_turn(self, turn_id, check):
            check()
            self.result_text = '{"outcome":"completed","summary":"checked"}'
            return 'completed'

    monkeypatch.setattr(dispatch_worker, 'AppServer', FakeClient)
    monkeypatch.setattr(dispatch_worker, '_connect_shared', lambda *args: FakeClient(object()))
    monkeypatch.setattr(dispatch_worker, '_stop', lambda proc: None)
    monkeypatch.setattr(dispatch_runtime.os, 'chown', lambda *args: None)
    skills = [{'type': 'skill', 'name': 'migration', 'path': '/tmp/skill/SKILL.md'}]
    runtime = dispatch_runtime.DispatchRuntime(ctx, task, user, tmp_path, {}, skills, 'SSH pinned', lambda: None)
    try:
        options = {'row': 1, 'title': 'a 迁移 shop', 'prompt': 'Read skill and check'}
        first = runtime.create(options)
        second = runtime.create(options)
        assert first['threadId'] == second['threadId'] == 'child-1'
        result = runtime._dispatch('wait_threads', {'targets': [{'threadId': 'child-1'}], 'timeoutMs': 1000})
        assert result['threads'][0]['status'] == 'completed'
        assert len([call for call in calls if call[0] == 'thread/start']) == 1
        assert runtime.children['child-1']['released'] is True
        assert runtime.children['child-1']['client'] is None
        turn = next(params for method, params in calls if method == 'turn/start')
        assert turn['model'] == 'gpt-6.1-terra' and turn['effort'] == 'medium'
        assert turn['input'][1:] == skills
        assert 'outputSchema' in turn
        stored = ctx.conn.execute('SELECT * FROM dispatch_children').fetchone()
        assert stored['status'] == 'completed' and stored['thread_id'] == 'child-1'
        runtime._dispatch('send_message_to_thread', {'threadId': 'child-1', 'prompt': 'Check again'})
        runtime._dispatch('wait_threads', {'targets': [{'threadId': 'child-1'}], 'timeoutMs': 1000})
        assert any(method == 'thread/resume' for method, _ in calls)
        denied = runtime.handle({'tool': 'read_thread', 'arguments': {'threadId': 'unrelated'}})
        assert denied['success'] is False
        wrong_row = runtime.handle({'tool': 'create_thread', 'arguments': dict(options, row=2)})
        assert wrong_row['success'] is False
    finally:
        runtime.close()


def test_runtime_cancels_running_child(ctx, tmp_path, monkeypatch):
    from concurrent.futures import Future
    form = task_form(ctx)
    rows = dispatch_tasks.parse_import(f'网站,新域名,备注,折扣\na.example.com,{form["target"]},,3\n'.encode())
    task_id = dispatch_tasks.create_previews(ctx, 'alice', rows, form['dispatcher'], form['migration'])
    task = ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    runtime = dispatch_runtime.DispatchRuntime(ctx, task, ctx.lookup_user('alice', 1000), tmp_path, {}, [], '', lambda: None)
    calls = []
    class Client:
        proc = object()
        def call(self, method, params):
            calls.append(method)
            return {}
        def wait_turn(self, turn, check):
            check()
    child = dict(row=1, threadId='child-1', turnId='turn-1', title='child', status='running',
                 result='', error='', revision=1, saved_revision=0, cancelled=True, future=None, client=Client())
    monkeypatch.setattr(dispatch_worker, '_stop', lambda proc: None)
    runtime._wait(child)
    assert child['status'] == 'interrupted'
    assert calls == ['turn/interrupt', 'thread/unsubscribe']
    runtime.close()


def test_task_page_shows_blocked_child_and_result(ctx):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), 'alice')
    import_file(alice, csrf, form)
    task = ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    ctx.conn.execute("UPDATE dispatch_tasks SET result_text=?, error=? WHERE id=?",
                     ('等待目标站权限', '批次尚未全部完成', task['id']))
    ctx.conn.execute("INSERT INTO dispatch_children(task_id,row_number,dispatch_id,title,thread_id,status,result_text) VALUES (?,?,?,?,?,?,?)",
                     (task['id'], 1, 'd1', 'site', 'child1', 'blocked', 'SSH blocked'))
    response = alice.get(f'/tasks/{task["id"]}')
    assert response.status_code == 200
    assert '等待目标站权限' in response.text and '执行受阻' in response.text and 'child1' in response.text


@pytest.mark.parametrize('sandbox', ['danger-full-access'])
def test_confirm_persists_sandbox_selection(ctx, sandbox):
    form=task_form(ctx)
    alice,csrf=session(ctx,create_app(ctx),'alice')
    import_file(alice,csrf,form)
    row=ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    assert row['sandbox']=='danger-full-access'
    response=alice.post(f'/tasks/{row["id"]}/confirm',data={'csrf':csrf,'confirmed':'1','sandbox':sandbox})
    assert response.status_code==303
    assert ctx.conn.execute('SELECT sandbox FROM dispatch_tasks').fetchone()[0]==sandbox


def test_confirm_rejects_unknown_sandbox(ctx):
    form=task_form(ctx)
    alice,csrf=session(ctx,create_app(ctx),'alice')
    import_file(alice,csrf,form)
    row=ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    response=alice.post(f'/tasks/{row["id"]}/confirm',data={'csrf':csrf,'confirmed':'1','sandbox':'unknown'})
    assert response.status_code==409
    assert ctx.conn.execute('SELECT status FROM dispatch_tasks').fetchone()[0]=='preview'


def test_confirm_disallows_sandbox_for_new_tasks(ctx):
    form = task_form(ctx)
    alice, csrf = session(ctx, create_app(ctx), 'alice')
    import_file(alice, csrf, form)
    row = ctx.conn.execute('SELECT * FROM dispatch_tasks').fetchone()
    response = alice.post(f'/tasks/{row["id"]}/confirm', data={
        'csrf': csrf, 'confirmed': '1', 'sandbox': 'workspace-write',
    })
    assert response.status_code == 409
    assert ctx.conn.execute('SELECT status FROM dispatch_tasks').fetchone()[0] == 'preview'


def test_shared_connection_uses_user_daemon_socket(ctx, tmp_path, monkeypatch):
    form = task_form(ctx)
    user = ctx.lookup_user('alice', 1000)
    socket = user.home / '.codex/app-server-control/app-server-control.sock'
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.touch()
    calls = []
    class Process:
        pass
    def launch(args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(dispatch_worker.subprocess, 'Popen', launch)
    client = dispatch_worker._connect_shared(ctx, user, tmp_path, {'HOME': str(user.home)})
    assert client.proc is not None
    assert calls[0][0][-1] == str(socket)
    assert calls[0][0][-2].endswith('dispatch_transport.py')
    assert calls[0][1]['user'] == user.uid


def test_browser_preflight_sets_verified_paths(ctx, tmp_path, monkeypatch):
    task_form(ctx)
    user = ctx.lookup_user('alice', 1000)
    env = {}
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, 'ok', '')
    monkeypatch.setattr(dispatch_worker.subprocess, 'run', run)
    text = dispatch_worker._browser_environment(ctx, user, tmp_path, env)
    assert env['HTW_CHROME_BINARY'] == str(ctx.settings.dispatch_chrome_binary)
    assert env['BROWSER_PYTHON'] == str(ctx.settings.dispatch_browser_python)
    assert calls[0][1]['user'] == user.uid
    assert 'chromium.launch' in calls[0][0][-1]
    assert str(ctx.settings.dispatch_chrome_binary) in text
    monkeypatch.setattr(dispatch_worker.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'Permission denied'))
    with pytest.raises(dispatch_worker.DeliveryError, match='Permission denied'):
        dispatch_worker._browser_environment(ctx, user, tmp_path, env)


def test_shared_events_do_not_complete_another_thread(tmp_path):
    fake = tmp_path / 'messages.py'
    fake.write_text('import json\nfor thread in ["unrelated", "owned"]:\n print(json.dumps({"method":"turn/completed","params":{"threadId":thread,"turn":{"id":thread+"-turn","status":"completed"}}}), flush=True)\n')
    proc = subprocess.Popen([sys.executable, str(fake)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = dispatch_worker.AppServer(proc)
    client.thread_id = 'owned'
    client.read(None)
    assert client.completed is None
    client.read(None)
    assert client.completed == ('owned-turn', 'completed')
    dispatch_worker._stop(proc)
