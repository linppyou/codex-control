import ast
import dataclasses
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import asyncssh
import pytest
from fastapi.testclient import TestClient
from test_servers import create, host_key, login_client

import codexctl.sites.access as site_access
import codexctl.sites.enrollment as site_enrollment
import codexctl.sites.inventory as sites
import codexctl.sites.registry as site_registry
import codexctl.sites.ssh as site_ssh


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


@pytest.mark.parametrize("algorithm", ["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"])
def test_new_server_password_enrollment_accepts_first_host_key(ctx, monkeypatch, algorithm):
    ctx.settings = dataclasses.replace(ctx.settings, site_ssh_enabled=True, site_ssh_shared=True)
    monkeypatch.setattr(site_access, "sync_client_config", lambda ctx: None)
    shared = host_key() + " deployment"
    monkeypatch.setattr(site_access, "shared_authorization", lambda settings: shared)
    monkeypatch.setattr(site_access, "shared_key", lambda settings: Path("/shared"))
    pinned = asyncssh.generate_private_key(algorithm).export_public_key().decode().strip()
    password = "  supplied-only-for-test! "
    order = []
    def install(server, received, script):
        assert server.host_key is None and received == password
        assert server.authorized_key in script and shared in script and password not in script
        assert "'callback': None" in script
        order.append("install")
        return pinned, 0

    def collect(server, key):
        assert order == ["install"] and server.host_key == pinned
        assert key.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
        order.append("collect")
        return [sites.Site("1", None, ("example.com",))]

    def check_shared(server, key, command, **kwargs):
        assert (server.host_key, key, command) == (pinned, Path("/shared"), "id -u")
        order.append("shared")
        return subprocess.CompletedProcess([], 0, "0\n", "")

    monkeypatch.setattr(site_ssh, "run_installer", install)
    monkeypatch.setattr(sites, "collect", collect)
    monkeypatch.setattr(site_ssh, "run", check_shared)
    monkeypatch.setattr(sites, "refresh", lambda context, owner: order.append("refresh"))
    client, csrf = login_client(ctx)
    assert 'type="password" name="ssh_password"' in client.get("/servers/new").text
    response = client.post("/servers", data={
        "name": "自动接入", "host": "198.144.159.123", "port": "22", "csrf": csrf,
        "ssh_password": password,
    })
    assert response.status_code == 303
    server, = site_registry.list_servers(ctx.conn)
    assert server.host_key == pinned and site_enrollment.pending(ctx, server) is None
    assert order == ["install", "collect", "shared", "refresh"]
    detail = client.get(response.headers["location"]).text
    assert "已自动接入" in detail and "已同步" in detail and "enrollment-command" not in detail
    assert password not in detail and password not in "\n".join(ctx.conn.iterdump())


def test_password_enrollment_can_retry_without_recreating_keys(ctx, monkeypatch):
    client, csrf = login_client(ctx)
    pinned = host_key()
    fields = {"name": "待接入", "host": "198.144.159.123", "port": "22", "csrf": csrf,
              "ssh_password": "incorrect-password-for-test"}
    def fail(*args):
        raise site_registry.ServerError("root 密码认证失败")

    monkeypatch.setattr(site_ssh, "run_installer", fail)
    invalid = "password-too-long-for-test" * 100
    response = client.post("/servers", data={**fields, "ssh_password": invalid})
    assert response.status_code == 422 and invalid not in response.text
    assert "请输入有效的 root SSH 密码" in response.text
    assert not site_registry.list_servers(ctx.conn)

    response = client.post("/servers", data=fields)
    assert response.status_code == 303
    server, = site_registry.list_servers(ctx.conn)
    path = urlsplit(response.headers["location"]).path
    detail = client.get(response.headers["location"]).text
    assert "root 密码认证失败" in detail and "enrollment-command" in detail
    assert fields["ssh_password"] not in detail and server.host_key is None
    assert site_enrollment.pending(ctx, server) is not None
    assert client.post(path + "/enrollment/password", data={"ssh_password": "secret"}).status_code == 403
    reader, reader_csrf = login_client(ctx, "reader")
    assert reader.post(path + "/enrollment/password", data={"csrf": reader_csrf}).status_code == 403

    monkeypatch.setattr(site_ssh, "run_installer", lambda *args: (pinned, 0))
    monkeypatch.setattr(sites, "collect", lambda *args: [])
    monkeypatch.setattr(sites, "refresh", lambda *args: None)
    result = client.post(path + "/enrollment/password", data={
        "ssh_password": "current-password-for-test", "csrf": csrf,
    })
    assert result.status_code == 303 and "已自动接入" in client.get(result.headers["location"]).text
    updated, = site_registry.list_servers(ctx.conn)
    assert updated.id == server.id and updated.authorized_key == server.authorized_key
    assert updated.host_key == pinned and site_enrollment.pending(ctx, updated) is None


def test_enrollment_only_learns_the_first_host_key():
    original = asyncssh.generate_private_key("ssh-ed25519")
    changed = asyncssh.generate_private_key("ssh-ed25519")
    first_use = site_ssh._EnrollmentHost(None)
    assert first_use.validate_host_public_key("", "", 22, original)
    assert not first_use.validate_host_public_key("", "", 22, changed)
    known = site_ssh._EnrollmentHost(original.export_public_key().decode().strip())
    assert not known.validate_host_public_key("", "", 22, changed)
    assert known.validate_host_public_key("", "", 22, original)


def test_password_enrollment_allows_reads_and_blocks_conflicting_changes(ctx, monkeypatch):
    client, csrf = login_client(ctx)
    other = TestClient(client.app, follow_redirects=False)
    other.cookies.update(client.cookies)
    server = create(ctx)
    started = threading.Event()
    release = threading.Event()

    def install(*args):
        started.set()
        assert release.wait(timeout=10)
        return server.host_key, 0

    monkeypatch.setattr(site_ssh, "run_installer", install)
    monkeypatch.setattr(sites, "collect", lambda *args: [])
    monkeypatch.setattr(sites, "refresh", lambda *args: None)
    path = f"/servers/{server.id}"
    data = {"csrf": csrf, "ssh_password": "test-password"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, path + "/enrollment/password", data=data)
        try:
            assert started.wait(timeout=5)
            detail = other.get(path).text
            assert "正在自动接入" in detail and "enrollment-command" not in detail
            for operation in ("edit", "delete", "enrollment", "enrollment/password"):
                assert other.post(f"{path}/{operation}", data=data).status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 303


@pytest.mark.parametrize("shared", [False, True])
def test_enrollment_http_flow_and_single_use(ctx, monkeypatch, shared):
    ctx.settings = dataclasses.replace(ctx.settings, site_ssh_enabled=shared, site_ssh_shared=shared)
    access_key = host_key()
    monkeypatch.setattr(site_access, "sync_client_config", lambda ctx: None)
    monkeypatch.setattr(site_access, "shared_authorization", lambda settings: access_key)
    monkeypatch.setattr(site_access, "shared_key", lambda settings: Path("/shared-key"))
    access_checks = []

    def check_access(server, key, command, **kwargs):
        access_checks.append((server.id, key, command))
        return subprocess.CompletedProcess([], 0, "0\n", "")

    monkeypatch.setattr(site_ssh, "run", check_access)
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
    assert (access_key in install.text) == shared
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
    assert access_checks == ([(server.id, Path("/shared-key"), "id -u")] if shared else [])
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


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("via_ssh", [False, True])
def test_install_script_runs_twice_without_duplicate_authorization(ctx, tmp_path, monkeypatch, shared, via_ssh):
    server = create(ctx)
    token = site_enrollment.issue(ctx, server)
    access_key = host_key() + " codexctl-shared" if shared else None
    callback = "" if via_ssh else "https://panel.example.com"
    script = site_enrollment.installer(server, token, callback, access_key=access_key)
    namespace = {"__name__": "installer_test"}
    exec(compile(script, "installer", "exec"), namespace)
    real_path = Path
    (tmp_path / "www/server/panel/data/db").mkdir(parents=True)
    (tmp_path / "www/server/panel/data/db/site.db").touch()
    if not via_ssh:
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
    if shared:
        assert authorized.read_text().count(access_key) == 1
    assert "unrelated-key original-comment" in authorized.read_text()
    assert authorized.stat().st_mode & 0o777 == 0o600
    collector = tmp_path / "usr/local/libexec/codexctl-site-inventory"
    assert collector.stat().st_mode & 0o777 == 0o755
    assert collector.read_bytes() == (Path(__file__).parents[1] / "deploy/collect-sites.py").read_bytes()
    if via_ssh:
        assert not requests
    else:
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
