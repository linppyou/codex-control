import json
import runpy
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import codexctl.sites.access as access
import codexctl.sites.inventory as inventory
import codexctl.sites.panel as panel
import codexctl.sites.probe as probe
import codexctl.wp_center as wp_center
from codexctl.web.routes.site_actions import bump_generation
from test_servers import create, login_client


def prepare(ctx, monkeypatch):
    server = create(ctx)
    ctx.settings = replace(ctx.settings, site_ssh_enabled=True, site_ssh_shared=True)
    monkeypatch.setattr(access, "sync_client_config", lambda context: None)
    monkeypatch.setattr(access, "shared_key", lambda settings: Path("/fixture-key"))
    monkeypatch.setattr(panel, "waf_statuses", lambda *args: {"example.com": True})
    monkeypatch.setattr(inventory, "collect", lambda *args: [
        inventory.Site("7", None, ("example.com",), name="example.com", enabled=True,
                       remark="初始备注", path="/www/wwwroot/example.com"),
    ])
    monkeypatch.setattr(probe, "probe", lambda *args: probe.Probe("up", 200))
    inventory._refresh(ctx.conn, ctx)
    monkeypatch.setattr(inventory, "refresh", lambda *args: None)
    return server


def test_site_management_checks_identity_and_updates_inventory(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, csrf = login_client(ctx)
    calls = []
    remote = {"name": "example.com", "enabled": True, "remark": "初始备注", "path": "/www/wwwroot/example.com"}
    waf = {"enabled": True}

    def call(target, key, operation, **params):
        assert target.id == server.id and params["site_id"] == "7"
        calls.append((operation, params))
        if params["name"] != remote["name"]:
            raise panel.PanelError("站点名称已变化", "conflict")
        if operation == "waf_read":
            return {"ok": True, "site": dict(remote), "enabled": waf["enabled"]}
        if operation == "waf_write":
            assert params["expected"] == waf["enabled"]
            waf["enabled"] = params["enabled"]
            return {"ok": True, "site": dict(remote), "enabled": waf["enabled"]}
        if operation == "inspect":
            return {"ok": True, "site": dict(remote)}
        if operation == "delete":
            assert params["path"] == remote["path"]
            return {"ok": True, "deleted": True}
        if operation == "state":
            assert params["expected"] == remote["enabled"]
            remote["enabled"] = params["enabled"]
        if operation == "remark":
            remote["remark"] = params["remark"]
        return {"ok": True, "site": dict(remote)}

    monkeypatch.setattr(panel, "call", call)
    base = f"/servers/{server.id}/sites/7"
    headers = {"X-CSRFToken": csrf}
    page = client.get("/servers?view=sites").text
    assert "<span>源站直连</span><span>备注</span><span>产品与菜单</span>" in page
    row = page.split('<li class="site-row" data-site-row', 1)[1].split("</li>", 1)[0]
    assert row.index('class="site-result site-origin') < row.index('class="site-result site-remark"') < row.index('class="site-result site-data"')
    assert '<span class="site-remark-text">初始备注</span>' in row
    assert client.get(base + "/waf").json()["enabled"] is True
    assert client.post(base + "/waf", data={"name": "example.com", "expected": "true", "enabled": "false"}, headers=headers).status_code == 200
    assert inventory.snapshots(ctx.conn)[server.id].rows[0]["waf_enabled"] is False
    assert client.post(base + "/state", data={"name": "example.com", "expected": "true", "enabled": "false"}, headers=headers).status_code == 200
    assert "已停用" in client.get("/servers?view=sites").text
    assert client.post(base + "/remark", data={"name": "example.com", "remark": "新备注"}, headers=headers).status_code == 200
    assert inventory.snapshots(ctx.conn)[server.id].rows[0]["remark"] == "新备注"
    assert client.post(base + "/remark", data={"name": "old.example.com", "remark": "wrong"}, headers=headers).status_code == 409
    assert client.post(base + "/remark", data={"name": "example.com"}).status_code == 403
    assert client.get(base + "/inspect").json()["site"]["path"] == remote["path"]
    delete = {"name": "example.com", "path": remote["path"], "confirm": "true",
              "delete_ftp": "false", "delete_database": "false", "delete_path": "false"}
    assert client.post(base + "/delete", data=delete, headers=headers).status_code == 200
    assert inventory.snapshots(ctx.conn)[server.id].rows == []
    assert calls[-1][1]["delete_path"] is False


def test_legacy_collector_retains_last_management_values(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    monkeypatch.setattr(inventory, "collect", lambda *args: [
        inventory.Site("7", None, ("example.com",), name="example.com"),
    ])
    monkeypatch.setattr(panel, "waf_statuses", lambda *args: (_ for _ in ()).throw(panel.PanelError("离线")))
    inventory._refresh(ctx.conn, ctx)
    row = inventory.snapshots(ctx.conn)[server.id].rows[0]
    assert (row["enabled"], row["remark"], row["management_stale"]) == (True, "初始备注", True)
    assert row["waf_enabled"] is True and row["waf_stale"] is True


def test_collector_reads_baota_management_fields(tmp_path):
    (tmp_path / "data/db").mkdir(parents=True)
    (tmp_path / "vhost/nginx").mkdir(parents=True)
    with sqlite3.connect(tmp_path / "data/db/site.db") as conn:
        conn.executescript("""
            CREATE TABLE sites(id INTEGER, name TEXT, addtime TEXT, path TEXT, status TEXT, ps TEXT);
            CREATE TABLE domain(id INTEGER, pid INTEGER, name TEXT);
            INSERT INTO sites VALUES (7, 'example.com', '2026-01-01 00:00:00+00:00', '/www/wwwroot/example.com', '0', '业务备注');
            INSERT INTO domain VALUES (1, 7, 'example.com');
        """)
    collector = runpy.run_path(str(Path(__file__).parents[1] / "deploy/collect-sites.py"))
    site = inventory.parse_inventory(json.dumps(collector["collect"](tmp_path)))[0]
    assert (site.name, site.enabled, site.remark, site.path) == (
        "example.com", False, "业务备注", "/www/wwwroot/example.com",
    )


def test_background_collection_cannot_overwrite_confirmed_change(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    old = inventory.Site("7", None, ("example.com",), name="example.com", enabled=True, remark="旧")

    def collect(*args):
        bump_generation(ctx.conn, server.id)
        snapshot = json.loads(ctx.conn.execute("SELECT snapshot FROM site_inventories WHERE server_id=?", (server.id,)).fetchone()[0])
        snapshot[0]["remark"] = "已修改"
        ctx.conn.execute("UPDATE site_inventories SET snapshot=? WHERE server_id=?", (json.dumps(snapshot), server.id))
        return [old]

    monkeypatch.setattr(inventory, "collect", collect)
    inventory._refresh(ctx.conn, ctx)
    assert inventory.snapshots(ctx.conn)[server.id].rows[0]["remark"] == "已修改"


def test_unconfirmed_operation_marks_inventory_stale(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, csrf = login_client(ctx)
    monkeypatch.setattr(panel, "call", lambda *args, **kwargs: (_ for _ in ()).throw(panel.PanelError("结果待核对", "unknown")))
    response = client.post(f"/servers/{server.id}/sites/7/state", data={
        "name": "example.com", "expected": "true", "enabled": "false",
    }, headers={"X-CSRFToken": csrf})
    assert response.status_code == 502
    assert inventory.snapshots(ctx.conn)[server.id].error == "操作结果待核对，请刷新采集"


def test_waf_read_failure_marks_previous_status_stale(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, _ = login_client(ctx)
    monkeypatch.setattr(panel, "call", lambda *args, **kwargs: (_ for _ in ()).throw(panel.PanelError("插件不可用")))
    assert client.get(f"/servers/{server.id}/sites/7/waf").status_code == 502
    row = inventory.snapshots(ctx.conn)[server.id].rows[0]
    assert row["waf_enabled"] is True and row["waf_stale"] is True


def test_manual_refresh_finishes_with_a_fresh_inventory(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, csrf = login_client(ctx)
    ctx.conn.execute("UPDATE site_inventories SET error='操作结果待核对' WHERE server_id=?", (server.id,))
    monkeypatch.setattr(inventory, "refresh", lambda context, server_id: inventory._refresh(context.conn, context, server_id))
    path = f"/servers/{server.id}/sites/refresh"
    assert client.post(path, headers={"X-CSRFToken": csrf}).status_code == 200
    assert client.get(path).json() == {"done": True, "error": None}


def test_member_can_manage_sites_but_not_delete(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, csrf = login_client(ctx, "reader")
    page = client.get("/servers?view=sites")
    assert 'data-site-action="delete"' not in page.text
    for action in ("probe", "waf", "state", "remark"):
        assert f'data-site-action="{action}"' in page.text
    assert f'/servers/{server.id}/wp-center?site_id=7' in page.text
    base = f"/servers/{server.id}/sites/7"
    calls = []
    def call(target, key, operation, **params):
        calls.append(operation)
        return {"ok": True, "enabled": True, "site": {"enabled": True, "remark": "new", "path": "/site"}}
    monkeypatch.setattr(panel, "call", call)
    headers = {"X-CSRFToken": csrf}
    assert client.post(base + "/probe", headers=headers).status_code == 200
    assert client.get(base + "/waf").status_code == 200
    assert client.post(base + "/waf", data={"name": "example.com", "expected": "true", "enabled": "false"}, headers=headers).status_code == 200
    assert client.post(base + "/state", data={"name": "example.com", "expected": "true", "enabled": "false"}, headers=headers).status_code == 200
    assert client.post(base + "/remark", data={"name": "example.com", "remark": "new"}, headers=headers).status_code == 200
    assert client.post(base + "/remark", data={"name": "example.com"}).status_code == 403
    assert client.post(base + "/delete", headers=headers).status_code == 403
    assert "delete" not in calls


def test_member_can_use_wp_center(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    member, csrf = login_client(ctx, "reader")
    path = f"/servers/{server.id}/wp-center"
    page = """<head></head><script>window.AI_CENTER_RUNTIME={version:"2.1.0",transport:"panel"};
    function upload(){if(window.AI_CENTER_RUNTIME?.transport==='panel') return nativeUpload()}</script>"""
    monkeypatch.setattr(wp_center, "installed_workspace", lambda *args: ("2.1.0", page))
    monkeypatch.setattr(wp_center, "theme_assets", lambda *args: ({}, {}))
    monkeypatch.setattr(wp_center, "plugin", lambda *args: {"kind": "domain", "preview_id": "p1"})
    assert member.get(path).status_code == 200
    assert member.get(path + "?site_id=7").status_code == 200
    assert member.get(path + "/document?site_id=7").status_code == 200
    endpoint = path + "/proxy/plugin?s=maintenance_api"
    data = {"payload": json.dumps({"operation": "preview", "params": {"kind": "domain"}})}
    assert member.post(endpoint, data=data).status_code == 403
    assert member.post(endpoint, data=data, headers={"X-CSRFToken": csrf}).json()["data"]["preview_id"] == "p1"


def test_probe_refreshes_only_the_selected_site_and_checks_duplicate_origin(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    other = create(ctx, "另一台", "104.255.153.35")
    inventory._refresh(ctx.conn, ctx)
    client, csrf = login_client(ctx)
    seen = []
    monkeypatch.setattr(probe, "probe", lambda domains, origin: (seen.append((domains, origin)) or probe.Probe("up", 200)))
    response = client.post(f"/servers/{server.id}/sites/7/probe", headers={"X-CSRFToken": csrf})
    assert response.status_code == 200
    assert seen == [(("example.com",), None), (("example.com",), server.host)]
    assert inventory.snapshots(ctx.conn)[server.id].rows[0]["origin"]["state"] == "up"
    assert inventory.snapshots(ctx.conn)[other.id].rows[0]["id"] == "7"


def test_domain_workspace_focuses_site_and_refreshes_once_after_completion(ctx, monkeypatch):
    server = prepare(ctx, monkeypatch)
    client, csrf = login_client(ctx)
    page = """<head></head><script>
    window.AI_CENTER_RUNTIME={version:"2.1.0",transport:"panel"};
    function upload(){if(window.AI_CENTER_RUNTIME?.transport==='panel') return nativeUpload()}
    </script>"""
    monkeypatch.setattr(wp_center, "installed_workspace", lambda *args: ("2.1.0", page))
    monkeypatch.setattr(wp_center, "theme_assets", lambda *args: ({}, {}))
    monkeypatch.setattr(wp_center, "plugin", lambda *args: {"tasks": [{"id": "job-1", "kind": "domain", "status": "done"}]})
    refreshes = []
    monkeypatch.setattr(inventory, "refresh", lambda context, server_id: refreshes.append(server_id))
    path = f"/servers/{server.id}/wp-center"
    assert "site_id=7#maintenance" in client.get(path + "?site_id=7").text
    document = client.get(path + "/document?site_id=7")
    assert document.status_code == 200 and "mnt-sites-search" in document.text and '"id": "7"' in document.text
    assert client.get(path + "?site_id=999").status_code == 404
    endpoint = path + "/proxy/plugin?s=maintenance_api"
    form = {"payload": json.dumps({"operation": "tasks", "params": {}})}
    for _ in range(2):
        assert client.post(endpoint, data=form, headers={"X-CSRFToken": csrf}).status_code == 200
    assert refreshes == [server.id]


def test_remote_panel_script_calls_and_verifies_native_operations(tmp_path):
    root = tmp_path / "panel"
    (root / "data/db").mkdir(parents=True)
    (root / "class").mkdir()
    (root / "plugin/btwaf").mkdir(parents=True)
    database = root / "data/db/site.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sites(id INTEGER PRIMARY KEY, name TEXT, status TEXT, ps TEXT, path TEXT)")
        conn.execute("INSERT INTO sites VALUES (7, 'example.com', '1', 'old', '/www/wwwroot/example.com')")
    (root / "BTPanel.py").write_text("from contextlib import nullcontext\nsession={}\nclass App:\n def test_request_context(self, *args): return nullcontext()\napp=App()\n")
    (root / "class/public.py").write_text("from types import SimpleNamespace\ndef to_dict_obj(value): return SimpleNamespace(**value)\ndef xssencode2(value): return value\n")
    (root / "class/data.py").write_text(
        "import sqlite3\nclass data:\n def setPs(self, value):\n  with sqlite3.connect('data/db/site.db') as db: db.execute('UPDATE sites SET ps=? WHERE id=?', (value.ps, value.id))\n  return {'status': True}\n"
    )
    (root / "class/panelSite.py").write_text(
        "import sqlite3\nclass panelSite:\n def SiteStop(self, value):\n  with sqlite3.connect('data/db/site.db') as db: db.execute(\"UPDATE sites SET status='0' WHERE id=?\", (value.id,))\n  return {'status': True}\n def SiteStart(self, value):\n  with sqlite3.connect('data/db/site.db') as db: db.execute(\"UPDATE sites SET status='1' WHERE id=?\", (value.id,))\n  return {'status': True}\n def DeleteSite(self, value):\n  with sqlite3.connect('data/db/site.db') as db: db.execute('DELETE FROM sites WHERE id=?', (value.id,))\n  return {'status': True}\n"
    )
    (root / "plugin/btwaf/btwaf_main.py").write_text(
        "from pathlib import Path\nclass btwaf_main:\n def get_site_config_byname(self, value): return {'open': Path('waf').read_text() == 'on'}\n def get_site_config(self, value): return [{'siteName': 'example.com', 'open': Path('waf').read_text() == 'on'}]\n def set_site_obj_open(self, value):\n  Path('waf').write_text('off' if Path('waf').read_text() == 'on' else 'on')\n  return {'status': True}\n"
    )
    (root / "waf").write_text("on")
    source = panel._REMOTE.replace("Path('/www/server/panel')", f"Path({str(root)!r})")

    def invoke(operation, **params):
        result = subprocess.run([sys.executable, "-c", source], input=json.dumps({"operation": operation, "site_id": "7", "name": "example.com", **params}), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    assert invoke("waf_list")["sites"][0]["open"] is True
    assert invoke("waf_write", expected=True, enabled=False)["enabled"] is False
    assert invoke("waf_write", expected=True, enabled=True)["kind"] == "conflict"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE sites SET status='unknown' WHERE id=7")
    assert invoke("state", expected=False, enabled=True)["kind"] == "unavailable"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE sites SET status='1' WHERE id=7")
    assert invoke("state", expected=True, enabled=False)["site"]["enabled"] is False
    assert invoke("remark", remark="新备注")["site"]["remark"] == "新备注"
    assert invoke("delete", path="/changed", confirm=True, delete_ftp=False, delete_database=False, delete_path=False)["kind"] == "conflict"
    assert invoke("delete", path="/www/wwwroot/example.com", confirm=True, delete_ftp=False, delete_database=False, delete_path=False)["deleted"] is True
