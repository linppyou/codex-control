import json
import runpy
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from codexctl import site_probe, site_registry, sites, webauth
from codexctl.web.app import create_app
from test_web import PW, do_login


def host_key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode()


def create(ctx, name="林佳顺", host="198.144.159.123"):
    return site_registry.create(ctx, "test", name, host, "22", host_key())


def test_inventory_reads_new_database_and_combines_vhosts(tmp_path):
    panel = tmp_path
    (panel / "data/db").mkdir(parents=True)
    vhosts = panel / "vhost/nginx"
    vhosts.mkdir(parents=True)
    with sqlite3.connect(panel / "data/db/site.db") as conn:
        conn.executescript("""
            CREATE TABLE sites(id INTEGER, name TEXT, addtime TEXT);
            CREATE TABLE domain(id INTEGER, pid INTEGER, name TEXT);
            INSERT INTO sites VALUES (1, '198.144.159.123', '2026-11-14 17:00:00+00:00');
            INSERT INTO sites VALUES (2, 'example.com', '2026-11-14 17:00:00+00:00');
            INSERT INTO domain VALUES (1, 1, '198.144.159.123');
            INSERT INTO domain VALUES (2, 2, 'www.example.com');
        """)
    (vhosts / "example.com.conf").write_text("server {\n server_name example.com www.example.com; # test\n}\n")
    (vhosts / "manual.example.com.conf").write_text("server {\n server_name manual.example.com;\n}\n")
    (vhosts / "0.default.conf").write_text("server {\n server_name _ 127.0.0.1;\n}\n")
    collector = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "collect-sites.py"))
    rows = sites.parse_inventory(json.dumps(collector["collect"](panel)))
    assert len(rows) == 2
    assert rows[0].domains == ("example.com", "www.example.com")
    assert sites.deployment_date(rows[0].created_at) == "2026-11-15"
    assert rows[1].id == "nginx:manual.example.com" and rows[1].created_at is None


def test_public_probe_checks_dns_fallback_and_redirect_target(monkeypatch):
    requested = []
    ips = {
        "example.com": (["93.184.216.34"], ""),
        "www.example.com": (["93.184.216.35"], ""),
        "unsafe.example.com": ([], "address"),
    }
    monkeypatch.setattr(site_probe, "_resolve", lambda host, timeout: ips.get(host, ([], "dns")))

    def request(url, host, port, addresses, timeout):
        requested.append((host, addresses))
        if host == "example.com":
            return 302, "", {"location": "https://unsafe.example.com/"}
        return 200, "", {}

    monkeypatch.setattr(site_probe, "_request", request)
    domains = ("example.com", "www.example.com")
    public = site_probe.probe(domains)
    assert public.state == "up" and public.entry_url == "https://www.example.com/"
    assert not any(host == "unsafe.example.com" for host, _ in requested)
    requested.clear()
    assert site_probe.probe(domains, "198.144.159.123").state == "up"
    assert {tuple(ips) for _, ips in requested} == {("198.144.159.123",)}
    result = site_probe.probe(("unknown.example.com",))
    assert result.state == "down" and result.reason == "dns"


def test_resolver_rejects_hosts_with_mixed_public_and_internal_records(monkeypatch):
    monkeypatch.setattr(
        site_probe.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "93.184.216.34 STREAM x\n10.0.0.1 STREAM x\n"),
    )
    assert site_probe._resolve("example.com", 1) == ([], "address")


def test_ssl_failure_can_report_http_fallback(monkeypatch):
    monkeypatch.setattr(site_probe, "_resolve", lambda host, timeout: (["93.184.216.34"], ""))
    monkeypatch.setattr(
        site_probe, "_request",
        lambda url, host, port, addresses, timeout: (0, "tls", {}) if port == 443 else (200, "", {}),
    )
    assert site_probe.probe(("example.com",)).state == "http_only"


def test_probe_allows_slow_https_response(monkeypatch):
    monkeypatch.setattr(site_probe, "_resolve", lambda host, timeout: (["93.184.216.34"], ""))

    def request(url, host, port, addresses, timeout):
        return (200, "", {}) if port == 443 and timeout >= 6 else (0, "timeout", {})

    monkeypatch.setattr(site_probe, "_request", request)
    assert site_probe.probe(("example.com",)).state == "up"


def test_probe_keeps_a_timeout_when_other_alias_has_no_dns(monkeypatch):
    monkeypatch.setattr(
        site_probe, "_resolve", lambda host, timeout: (["93.184.216.34"], "") if host.startswith("www.") else ([], "dns"),
    )
    monkeypatch.setattr(site_probe, "_request", lambda *args: (0, "timeout", {}))
    result = site_probe.probe(("example.com", "www.example.com"))
    assert result.state == "down" and result.reason == "timeout"


def test_probe_batches_limit_parallel_requests_per_server(monkeypatch):
    targets = {
        ((f"{owner}{index}.example.com",), None): owner
        for owner in ("a", "b") for index in range(12)
    }
    lock = threading.Lock()
    active = Counter()
    peak = Counter()

    def probe(domains, origin, clock):
        owner = domains[0][0]
        with lock:
            active[owner] += 1
            peak[owner] = max(peak[owner], active[owner])
        time.sleep(0.01)
        with lock:
            active[owner] -= 1
        return {}

    monkeypatch.setattr(sites, "_probe", probe)
    results = sites._probe_by_server(targets, lambda: 0, 4)
    assert results.keys() == targets.keys()
    assert all(0 < peak[owner] <= 4 for owner in ("a", "b"))


def test_refresh_retains_failed_inventory_and_probes_duplicates(ctx, monkeypatch):
    a, b = create(ctx), create(ctx, "刘子涵", "104.255.153.35")
    site = sites.Site("2", 1794675600, ("example.com", "www.example.com"))
    calls = []

    def probe(domains, origin):
        calls.append((domains, origin))
        return site_probe.Probe("up", 200)

    monkeypatch.setattr(site_probe, "probe", probe)
    monkeypatch.setattr(sites, "collect", lambda server, key: [site])
    sites._refresh(ctx.conn, ctx)
    snapshot = sites.snapshots(ctx.conn)
    assert len(snapshot[a.id].rows) == len(snapshot[b.id].rows) == 1
    assert set(calls) == {(site.domains, None), (site.domains, a.host), (site.domains, b.host)}

    def collect(server, key):
        if server == a:
            raise sites.InventoryError("SSH 采集超时")
        return []

    monkeypatch.setattr(sites, "collect", collect)
    ctx.test_clock.t += 300
    sites._refresh(ctx.conn, ctx)
    retry = sites.snapshots(ctx.conn)
    assert retry[a.id].error == "SSH 采集超时"
    assert retry[a.id].synced_at == snapshot[a.id].synced_at
    assert retry[a.id].rows[0]["public"]["checked_at"] == ctx.now()
    assert retry[b.id].rows == []


def test_refresh_cannot_restore_deleted_server_or_superseded_snapshot(ctx, monkeypatch):
    a, b = create(ctx), create(ctx, "刘子涵", "104.255.153.35")
    site = sites.Site("2", None, ("example.com",))

    def collect(server, key):
        if server == a:
            site_registry.delete(ctx, "test", a)
            site_registry.update(ctx, "test", b, b.name, "104.255.153.36", str(b.port), b.host_key)
        return [site]

    monkeypatch.setattr(sites, "collect", collect)
    monkeypatch.setattr(site_probe, "probe", lambda domains, origin: site_probe.Probe("up", 200))
    sites._refresh(ctx.conn, ctx)
    assert sites.snapshots(ctx.conn) == {}


def test_refresh_rechecks_a_public_timeout(ctx, monkeypatch):
    server = create(ctx)
    monkeypatch.setattr(sites, "collect", lambda *args: [sites.Site("1", None, ("example.com",))])
    results = iter([site_probe.Probe("down", reason="timeout"), site_probe.Probe("up", 200)])
    monkeypatch.setattr(site_probe, "probe", lambda *args: next(results))
    sites._refresh(ctx.conn, ctx)
    assert sites.snapshots(ctx.conn)[server.id].rows[0]["public"]["state"] == "up"


def login_client(ctx, user="admin"):
    ctx.settings = replace(ctx.settings, web_cookie_secure=False)
    webauth.create_admin(ctx.conn, user, PW, now=ctx.now())
    app = create_app(ctx)
    client = TestClient(app, follow_redirects=False)
    assert do_login(client, user).status_code == 303
    csrf = ctx.conn.execute("SELECT csrf FROM web_sessions WHERE username = ?", (user,)).fetchone()[0]
    return client, csrf


def test_admin_server_crud_and_csrf(ctx):
    client, csrf = login_client(ctx)
    fields = {"name": "刘子涵", "host": "104.255.153.35", "port": "22", "host_key": host_key()}
    assert client.get("/servers/new").status_code == 200
    assert client.post("/servers", data=fields).status_code == 403
    response = client.post("/servers", data={**fields, "csrf": csrf})
    assert response.status_code == 303
    detail_path = response.headers["location"]
    detail = client.get(detail_path).text
    assert "刘子涵" in detail and "codexctl-site-inventory" in detail and "SHA256:" in detail
    row = ctx.conn.execute("SELECT private_key FROM site_servers").fetchone()
    assert b"PRIVATE KEY" not in row["private_key"]
    assert "PRIVATE KEY" not in detail
    assert client.post("/servers", data={**fields, "csrf": csrf}).status_code == 422
    assert client.post("/servers", data={**fields, "host": "127.0.0.1", "csrf": csrf}).status_code == 422
    edit = client.post(detail_path + "/edit", data={**fields, "name": "新服务器", "port": "28885", "csrf": csrf})
    assert edit.status_code == 303
    assert "新服务器" in client.get(detail_path).text
    assert client.post(detail_path + "/delete", data={"csrf": csrf}).status_code == 303
    assert client.get(detail_path).status_code == 404
    assert [r[0] for r in ctx.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'server.%'")] == [
        "server.create", "server.update", "server.delete",
    ]


def test_server_pages_require_admin(ctx):
    ctx.test_users.add("reader")
    reader, csrf = login_client(ctx, "reader")
    landing = reader.get("/", follow_redirects=True)
    assert landing.status_code == 200 and 'href="/servers"' not in landing.text
    for path in ("/servers", "/servers/new", "/servers/x", "/servers/x/edit"):
        assert reader.get(path).status_code == 403
    for path in ("/servers", "/servers/x/edit", "/servers/x/delete"):
        assert reader.post(path, data={"csrf": csrf}).status_code == 403
    anon = TestClient(create_app(ctx), follow_redirects=False)
    assert anon.get("/servers").headers["location"] == "/login"


def test_overview_groups_filters_and_paginates(ctx, monkeypatch):
    client, _ = login_client(ctx)
    server = create(ctx)
    other = create(ctx, name="另一台服务器", host="104.255.152.77")
    old = int(datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp())
    new = int(datetime(2026, 11, 14, 17, tzinfo=timezone.utc).timestamp())
    known = [sites.Site(str(i), new, (f"site{i:02d}.com",)) for i in range(51)]
    rows = [*known, sites.Site("51", old, ("dns-error.com",)), sites.Site("52", None, ("unknown.com",))]
    other_rows = [sites.Site("0", new, ("other-site.com",))]
    monkeypatch.setattr(sites, "collect", lambda owner, key: rows if owner.id == server.id else other_rows)
    monkeypatch.setattr(
        site_probe, "probe",
        lambda domains, origin: site_probe.Probe("down", reason="dns") if domains[0] == "dns-error.com"
        else site_probe.Probe("up", 200),
    )
    sites._refresh(ctx.conn, ctx)
    first = client.get("/servers").text
    assert 'datetime="2026-11-15"' in first and "site00.com" in first and "site50.com" not in first
    assert "other-site.com" not in first
    second = client.get("/servers?page=2").text
    assert "site50.com" in second and "日期未知" in second and "DNS 无可用地址" in second
    first_server, next_server = second.split(f'id="site-server-{other.id}"', 1)
    assert first_server.index(f'id="site-server-{server.id}"') < first_server.index("site50.com")
    assert first_server.index("site50.com") < first_server.index("dns-error.com") < first_server.index("unknown.com")
    assert 'datetime="2026-11-15"' in first_server and 'datetime="2026-11-15"' in next_server
    assert "other-site.com" in next_server and "other-site.com" not in first_server
    scoped = client.get(f"/servers?server_id={other.id}&date=2026-11-15").text
    assert "other-site.com" in scoped and "site00.com" not in scoped
    filtered = client.get(f"/servers?state=down&server_id={server.id}&q=dns").text
    assert "dns-error.com" in filtered and "site00.com" not in filtered
    ctx.test_clock.t += 901
    assert "结果已过期" in client.get("/servers?state=stale").text


def test_overview_separates_timeouts_from_confirmed_failures(ctx, monkeypatch):
    client, _ = login_client(ctx)
    create(ctx)
    rows = [sites.Site("1", None, ("slow.example.com",)), sites.Site("2", None, ("missing.example.com",))]
    monkeypatch.setattr(sites, "collect", lambda *args: rows)
    monkeypatch.setattr(
        site_probe, "probe", lambda domains, origin: site_probe.Probe(
            "down", reason="timeout" if domains[0] == "slow.example.com" else "dns",
        ),
    )
    sites._refresh(ctx.conn, ctx)
    pending = client.get("/servers?state=unconfirmed").text
    assert "slow.example.com" in pending and "请求超时" in pending and "missing.example.com" not in pending
    down = client.get("/servers?state=down").text
    assert "missing.example.com" in down and "slow.example.com" not in down
    ctx.test_clock.t += 901
    assert "上次待确认" in client.get("/servers?state=stale").text
