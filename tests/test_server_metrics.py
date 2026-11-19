import asyncio
import json
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import anyio
import httpx
from fastapi.testclient import TestClient
from test_servers import create
from test_web_pages import c, mgr

import codexctl.users.webauth as webauth
from codexctl.web import server_metrics
from codexctl.web.app import SESSION_COOKIE
from codexctl.web.dependencies import current_admin


METRICS = {
    "cores": 24,
    "load": 6.0,
    "cpu": 30.0,
    "memory_total": 32 * 1024**3,
    "memory_used": 14 * 1024**3,
    "disk_total": 500 * 1024**3,
    "disk_used": 200 * 1024**3,
    "disk_available": 280 * 1024**3,
}


def test_metrics_use_server_identity_cache_and_admin_permission(c, ctx, monkeypatch):
    server = create(ctx)
    ctx.settings = replace(ctx.settings, site_ssh_enabled=True, site_ssh_shared=True)
    monkeypatch.setattr(server_metrics.site_access, "shared_key", lambda settings: Path("/tmp/test-key"))
    calls = []

    def run(target, key, command, **kwargs):
        calls.append(target.revision)
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(METRICS), stderr="")

    monkeypatch.setattr(server_metrics.site_ssh, "run", run)
    dashboard = c.get("/").text
    assert server.name in dashboard and 'data-server-id="' + server.id + '"' in dashboard
    assert all('data-metric="' + key + '"' in dashboard for key in ("load", "cpu", "memory", "disk"))

    response = c.get("/dashboard/server-metrics")
    assert response.status_code == 200
    assert response.json()["servers"] == [{"id": server.id, "metrics": METRICS}]
    assert c.get("/dashboard/server-metrics").json() == response.json()
    assert calls == [server.revision]
    assert c.get("/dashboard/server-metrics?refresh=1").status_code == 200
    assert calls == [server.revision, server.revision]

    ctx.conn.execute("UPDATE site_servers SET revision = revision + 1 WHERE id = ?", (server.id,))
    assert c.get("/dashboard/server-metrics").status_code == 200
    assert calls == [server.revision, server.revision, server.revision + 1]

    ctx.test_users.add("alice")
    token, _ = webauth.create_session(ctx.conn, "alice", now=ctx.now())
    member = TestClient(c.app, follow_redirects=False)
    member.cookies.set(SESSION_COOKIE, token)
    assert member.get("/dashboard/server-metrics").status_code == 403
    anonymous = TestClient(c.app, follow_redirects=False)
    assert anonymous.get("/dashboard/server-metrics").status_code == 303


def test_metrics_handle_unavailable_and_malformed_servers(ctx, monkeypatch):
    server = create(ctx)
    assert server_metrics.collect([server], ctx.settings) == [
        {"id": server.id, "error": "需启用共享站点 SSH"}
    ]
    monkeypatch.setattr(
        server_metrics.site_ssh, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout='{"cores": 0}', stderr=""),
    )
    assert server_metrics._read(server, Path("/tmp/test-key")) == {
        "id": server.id, "error": "无法连接或读取服务器数据",
    }


def test_dashboard_shows_every_server_with_four_gauges(c, ctx):
    servers = [create(ctx, name=f"服务器 {i}", host=f"198.144.159.{i}") for i in range(1, 10)]

    response = c.get("/")

    assert response.status_code == 200
    region = response.text.split('data-server-metrics>', 1)[1].split('</section>', 1)[0]
    assert '<details' not in region
    for server in servers:
        card = region.split(f'data-server-id="{server.id}">', 1)[1].split('</article>', 1)[0]
        assert card.count('class="server-meter"') == 4


def test_metrics_isolate_decoder_recursion(ctx, monkeypatch):
    server = create(ctx)

    def decode(_):
        raise RecursionError("JSON input exceeds the decoder depth")

    monkeypatch.setattr(server_metrics.json, "loads", decode)
    monkeypatch.setattr(
        server_metrics.site_ssh, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
    )
    assert server_metrics._read(server, Path("/tmp/test-key")) == {
        "id": server.id, "error": "无法连接或读取服务器数据",
    }


def test_overlapping_manual_refreshes_share_a_sample_without_blocking_other_routes(c, monkeypatch):
    async def exercise():
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        loop = asyncio.get_running_loop()
        collecting = asyncio.Event()
        authenticated_twice = asyncio.Event()
        release = threading.Event()
        authenticated = 0
        samples = []

        async def authenticate():
            nonlocal authenticated
            authenticated += 1
            if authenticated == 2:
                authenticated_twice.set()
            return "admin", ""

        def collect(servers, settings):
            samples.append(servers)
            loop.call_soon_threadsafe(collecting.set)
            assert release.wait(timeout=10)
            return []

        monkeypatch.setitem(c.app.dependency_overrides, current_admin, authenticate)
        monkeypatch.setattr(server_metrics, "collect", collect)
        transport = httpx.ASGITransport(app=c.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = "/dashboard/server-metrics?refresh=1"
            requests = [asyncio.create_task(client.get(url))]
            try:
                await asyncio.wait_for(collecting.wait(), timeout=5)
                requests.append(asyncio.create_task(client.get(url)))
                await asyncio.wait_for(authenticated_twice.wait(), timeout=5)
                response = await asyncio.wait_for(client.get("/favicon.ico"), timeout=5)
                assert response.status_code == 301
            finally:
                release.set()
                responses = await asyncio.gather(*requests)

        assert len(samples) == 1
        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json() == responses[1].json()

    asyncio.run(exercise())


def test_metrics_probe_reads_linux_statistics():
    result = subprocess.run(
        ["python3", "-c", server_metrics._PROBE], capture_output=True, text=True, check=True, timeout=5,
    )
    metrics = json.loads(result.stdout)
    assert metrics["cores"] > 0 and 0 <= metrics["cpu"] <= 100 and metrics["load"] >= 0
    assert 0 <= metrics["memory_used"] <= metrics["memory_total"]
    assert 0 <= metrics["disk_used"] <= metrics["disk_total"]
