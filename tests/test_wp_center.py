import json
import re
import runpy
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codexctl import wp_center, webauth
from codexctl.web.app import create_app
from test_servers import create
from test_web import PW, do_login


def test_center_preserves_site_and_certificate_actions(ctx, monkeypatch):
    server = create(ctx)
    webauth.create_admin(ctx.conn, "admin", PW, now=ctx.now())
    webauth.create_admin(ctx.conn, "viewer", PW, now=ctx.now())
    client = TestClient(create_app(ctx), base_url="https://panel.example", follow_redirects=False)
    path = f"/wp-center/{server.id}"
    calls = []

    def request(selected, key, operation, **fields):
        calls.append((selected.id, operation, fields))
        if operation == "create":
            return {"id": "AIC-1116-TEST", "auto_started": True}
        if operation == "history":
            return {"jobs": [{
                "id": "AIC-1116-TEST", "created_at": "2026-11-16T16:30:00+08:00",
                "status": "deployed_with_warnings", "template": "<template.zip>", "rows": [{
                    "domain": "new.example.com", "site_state": "deployed_with_warnings",
                    "ssl": "failed", "ssl_detail": "<failure>", "error": "", "can_retry": True,
                }],
            }], "more": False}
        return {}

    monkeypatch.setattr(wp_center, "request", request)
    assert client.get("/wp-center").status_code == 303
    assert do_login(client, "viewer").status_code == 303
    assert client.get(f"{path}/history").status_code == 403
    assert do_login(client).status_code == 303
    page = client.get("/wp-center")
    assert "WP 建站中心" in page.text
    assert server.name in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    payload = {"domain": "NEW.example.com", "template": "server-template", "www": "true"}

    assert client.post(f"{path}/create", data=payload).status_code == 403
    assert client.post(f"{path}/create", data={**payload, "csrf": csrf, "domain": "http://bad.example.com"}).status_code == 422
    assert not calls
    result = client.post(f"{path}/create", data={**payload, "csrf": csrf})
    assert result.status_code == 200
    assert calls[-1] == (server.id, "create", {
        "domain": "new.example.com", "template": "server-template", "sql": "", "old_domain": "", "www": True,
    })
    history = client.get(f"{path}/history")
    assert "重试 SSL" in history.text and "已建成" in history.text
    assert "&lt;template.zip&gt;" in history.text and "&lt;failure&gt;" in history.text
    assert client.post(f"{path}/retry-ssl", data={
        "csrf": csrf, "job": "AIC-1116-TEST", "domain": "new.example.com",
    }).status_code == 303
    assert calls[-1] == (server.id, "retry_ssl", {"job": "AIC-1116-TEST", "domain": "new.example.com"})
    assert [r[0] for r in ctx.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'wp.%'")] == [
        "wp.create", "wp.retry_ssl",
    ]


def test_bridge_uses_plugin_plan_and_rejects_a_duplicate_active_domain(tmp_path):
    bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/wp-center.py"))
    clean_domain = lambda value: (value, value)

    class Service:
        root = tmp_path
        runs = []

        def inspect_template(self, _):
            return {"databases": [{"id": "embedded-sql"}]}

        def inspect_sql_resource(self, _):
            return {"candidates": [
                {"domain": "old.example.com", "evidence": "WordPress home / siteurl"},
                {"domain": "elsewhere.example.com", "evidence": "URL 引用"},
            ]}

        def _provision_runs(self, _):
            return self.runs

        def create_plan(self, payload):
            return payload

    service = Service()
    payload = {"domain": "new.example.com", "template": "chosen-zip", "www": True}
    assert bridge["create"](service, payload, clean_domain) == {
        "batch": "new.example.com", "prefixes": "www",
        "resources": {"template": "chosen-zip", "sql": "embedded-sql"},
        "old_domain": "old.example.com", "old_domain_confirmed": True, "ssl": True,
    }
    service.runs = [{"domain": "www.new.example.com"}]
    with pytest.raises(bridge["RequestError"], match="已有远程任务"):
        bridge["create"](service, payload, clean_domain)


def test_bridge_history_exposes_retry_only_after_the_site_is_built():
    bridge = runpy.run_path(str(Path(__file__).parents[1] / "deploy/wp-center.py"))
    conn = sqlite3.connect(":memory:")

    class Service:
        _ensure_provision_worker = lambda self: None

        @contextmanager
        def connection(self):
            yield conn

    job = {
        "id": "AIC-1116-TEST", "status": "deployed_with_warnings", "created_at": "2026-11-16",
        "plan": {
            "rows": [{"domain": "built.example.com", "status": "ready"}, {"domain": "broken.example.com", "status": "ready"}],
            "resources": {"template": {"name": "site.zip"}},
        },
        "execution": {"results": {
            "built.example.com": {"status": "deployed_with_warnings", "ssl": {"status": "failed", "reason": "DNS"}},
            "broken.example.com": {"status": "failed", "error": "导入失败"},
        }},
    }
    conn.execute("CREATE TABLE jobs (body TEXT)")
    conn.execute("INSERT INTO jobs VALUES (?)", (json.dumps(job),))

    def retryable(result):
        return result.get("status") == "deployed_with_warnings" and result["ssl"]["status"] == "failed"

    rows = bridge["history"](Service(), 1, retryable, str)["jobs"][0]["rows"]
    assert [(row["can_retry"], row["ssl"], row["ssl_detail"]) for row in rows] == [(True, "failed", "DNS"), (False, "", "")]
    job["status"] = "ssl_pending"
    job["execution"]["results"]["built.example.com"]["operation_state"] = "queued"
    conn.execute("UPDATE jobs SET body = ?", (json.dumps(job),))
    pending = bridge["history"](Service(), 1, retryable, str)["jobs"][0]["rows"][0]
    assert not pending["can_retry"] and pending["ssl"] == "pending"
    conn.close()
