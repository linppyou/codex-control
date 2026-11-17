import ast
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codexctl import site_enrollment, site_registry, sites
from test_servers import create, host_key, login_client


def test_enrollment_token_rotation_expiry_and_connection_changes(ctx):
    server = create(ctx)
    token = site_enrollment.issue(ctx, server)
    row = site_enrollment.pending(ctx, server)
    assert token not in str(dict(row))
    assert site_enrollment.authenticate(ctx, token) == server
    assert token in site_enrollment.command(ctx, server, "https://panel.example.com")
    replacement = site_enrollment.issue(ctx, server)
    with pytest.raises(site_registry.ServerError):
        site_enrollment.authenticate(ctx, token)
    ctx.test_clock.t += site_enrollment.TTL
    assert site_enrollment.command(ctx, server, "https://panel.example.com") is None
    with pytest.raises(site_registry.ServerError):
        site_enrollment.authenticate(ctx, replacement)
    token = site_enrollment.issue(ctx, server)
    site_registry.update(ctx, "test", server, server.name, server.host, "2222", server.host_key)
    with pytest.raises(site_registry.ServerError):
        site_enrollment.authenticate(ctx, token)


def test_new_server_generates_command_and_instructions(ctx):
    client, csrf = login_client(ctx)
    assert "一条命令接入采集" in client.get("/servers/new").text
    added = client.post("/servers", data={
        "name": "测试服务器", "host": "198.144.159.123", "port": "22", "csrf": csrf,
    })
    assert added.status_code == 303
    page = client.get(added.headers["location"]).text
    assert "enrollment-command" in page and "python3" in page
    assert "30 分钟" in page and "root" in page


def test_enrollment_http_flow_and_single_use(ctx, monkeypatch):
    client, csrf = login_client(ctx)
    server = site_registry.create(ctx, "test", "新服务器", "198.144.159.123", "22", "")
    token = site_enrollment.issue(ctx, server)
    anon = TestClient(client.app, follow_redirects=False)
    assert anon.post("/site-enrollment/install").status_code == 403
    assert anon.post("/servers/" + server.id + "/enrollment").status_code == 403
    install = anon.post("/site-enrollment/install", data={"token": token})
    assert install.status_code == 200
    assert "no-store" in install.headers["cache-control"]
    ast.parse(install.text)
    assert server.authorized_key in install.text
    reported_key = host_key()
    checked = []

    def collect(candidate, private):
        assert candidate.host_key == reported_key
        assert private.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
        checked.append(candidate.id)
        return [sites.Site("1", None, ("example.com",))]

    refreshed = []
    monkeypatch.setattr(sites, "collect", collect)
    monkeypatch.setattr(sites, "refresh", lambda context, owner: refreshed.append((context, owner)))
    response = anon.post("/site-enrollment/complete", data={"host_key": reported_key},
                         headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200 and response.json() == {"sites": 1}
    assert checked == [server.id] and refreshed == [(ctx, server.id)]
    assert site_registry.get(ctx.conn, server.id).host_key == reported_key
    snapshot = sites.snapshots(ctx.conn)[server.id]
    assert snapshot.rows[0]["domains"] == ["example.com"]
    assert snapshot.rows[0]["public"]["state"] == "pending"
    assert anon.post("/site-enrollment/install", data={"token": token}).status_code == 403
    assert anon.post("/site-enrollment/complete", data={"host_key": reported_key},
                     headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.post("/servers/" + server.id + "/enrollment", data={"csrf": csrf}).status_code == 303


def test_failed_verification_keeps_token_and_original_server(ctx, monkeypatch):
    client, _ = login_client(ctx)
    server = create(ctx)
    token = site_enrollment.issue(ctx, server)

    def fail(*args):
        raise sites.InventoryError("SSH 连接或认证失败")

    monkeypatch.setattr(sites, "collect", fail)
    headers = {"Authorization": "Bearer " + token}
    response = client.post("/site-enrollment/complete", data={"host_key": server.host_key}, headers=headers)
    assert response.status_code == 422 and "SSH" in response.text
    assert site_enrollment.authenticate(ctx, token) == server
    response = client.post("/site-enrollment/complete", data={"host_key": host_key()}, headers=headers)
    assert response.status_code == 422 and "不一致" in response.text
    assert site_registry.get(ctx.conn, server.id) == server


def test_reenrollment_preserves_existing_revision_and_inventory(ctx):
    server = create(ctx)
    token = site_enrollment.issue(ctx, server)
    ctx.conn.execute(
        "INSERT INTO site_inventories VALUES (?, ?, ?, ?, NULL, '[]')",
        (server.id, server.revision, ctx.now(), ctx.now()),
    )
    site_enrollment.complete(ctx, server, token, [])
    assert site_registry.get(ctx.conn, server.id) == server
    assert sites.snapshots(ctx.conn)[server.id].synced_at == ctx.now()


def test_install_script_runs_twice_without_duplicate_authorization(ctx, tmp_path, monkeypatch):
    server = create(ctx)
    token = site_enrollment.issue(ctx, server)
    script = site_enrollment.installer(server, token, "https://panel.example.com")
    namespace = {"__name__": "installer_test"}
    exec(compile(script, "installer", "exec"), namespace)
    real_path = Path
    (tmp_path / "www/server/panel/data/db").mkdir(parents=True)
    (tmp_path / "www/server/panel/data/db/site.db").touch()
    (tmp_path / "etc/ssh").mkdir(parents=True)
    (tmp_path / "etc/ssh/ssh_host_ed25519_key.pub").write_text(server.host_key)
    (tmp_path / "root/.ssh").mkdir(parents=True)
    authorized = tmp_path / "root/.ssh/authorized_keys"
    authorized.write_text("ssh-ed25519 unrelated-key original-comment\n")
    namespace["Path"] = lambda value: tmp_path / str(real_path(value)).lstrip("/")
    monkeypatch.setattr(namespace["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"sites": []}'))
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        return __import__("io").BytesIO(json.dumps({"sites": 0}).encode())

    monkeypatch.setattr(namespace["urllib"].request, "urlopen", urlopen)
    namespace["main"]()
    namespace["main"]()
    assert authorized.read_text().count(server.authorized_key) == 1
    assert "unrelated-key original-comment" in authorized.read_text()
    assert authorized.stat().st_mode & 0o777 == 0o600
    collector = tmp_path / "usr/local/libexec/codexctl-site-inventory"
    assert collector.stat().st_mode & 0o777 == 0o755
    assert collector.read_bytes() == (Path(__file__).parents[1] / "deploy/collect-sites.py").read_bytes()
    assert requests[0].get_header("Authorization") == "Bearer " + token


def test_public_scheme_only_trusts_local_proxy(ctx):
    client, _ = login_client(ctx)
    server = create(ctx)
    site_enrollment.issue(ctx, server)
    path = "/servers/" + server.id
    assert "http://testserver/site-enrollment/install" in client.get(path, headers={"X-Forwarded-Proto": "https"}).text
    proxied = TestClient(client.app, client=("127.0.0.1", 1234))
    proxied.cookies.update(client.cookies)
    assert "https://testserver/site-enrollment/install" in proxied.get(path, headers={"X-Forwarded-Proto": "https"}).text


def test_targeted_refresh_keeps_other_servers_and_detects_duplicate_domains(ctx, monkeypatch):
    server = create(ctx)
    other = create(ctx, "另一台", "104.255.153.35")
    site = sites.Site("1", None, ("example.com", "www.example.com"))
    token = site_enrollment.issue(ctx, other)
    site_enrollment.complete(ctx, other, token, [site])
    previous = sites.snapshots(ctx.conn)[other.id]
    collected = []
    probed = []

    def collect(owner, key):
        collected.append(owner.id)
        return [site]

    def probe(domains, origin):
        probed.append(origin)
        return sites.site_probe.Probe("up", 200)

    monkeypatch.setattr(sites, "collect", collect)
    monkeypatch.setattr(sites.site_probe, "probe", probe)
    sites._refresh(ctx.conn, ctx, server.id)
    assert collected == [server.id]
    assert set(probed) == {None, server.host}
    assert sites.snapshots(ctx.conn)[other.id] == previous
