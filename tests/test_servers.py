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
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from test_web import PW, do_login

import codexctl.sites.inventory as sites
import codexctl.sites.probe as site_probe
import codexctl.sites.registry as site_registry
import codexctl.users.webauth as webauth
import codexctl.wp_center as wp_center
from codexctl.web.app import create_app
from codexctl.web.site_analytics import site_comparison


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
    assert rows[0].data is None
    assert rows[1].id == "nginx:manual.example.com" and rows[1].created_at is None


def test_wordpress_query_counts_published_products_and_populated_menus():
    collector = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "collect-sites.py"))
    query = collector["data_query"]("shop_")
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE shop_posts(ID INTEGER, post_type TEXT, post_status TEXT, post_content TEXT);
            CREATE TABLE shop_terms(term_id INTEGER, slug TEXT);
            CREATE TABLE shop_term_taxonomy(term_taxonomy_id INTEGER, term_id INTEGER, taxonomy TEXT);
            CREATE TABLE shop_term_relationships(object_id INTEGER, term_taxonomy_id INTEGER);
        """)
        assert conn.execute(query).fetchone() == (0, 0, 0)
        conn.executescript("""
            INSERT INTO shop_posts VALUES
                (1, 'product', 'publish', ''),
                (2, 'product', 'trash', ''),
                (3, 'product', 'draft', ''),
                (4, 'product', 'importing', ''),
                (5, 'product_variation', 'publish', ''),
                (6, 'nav_menu_item', 'publish', ''),
                (7, 'nav_menu_item', 'publish', ''),
                (8, 'nav_menu_item', 'draft', ''),
                (9, 'nav_menu_item', 'publish', ''),
                (10, 'wp_navigation', 'publish', '<!-- wp:navigation-link {"label":"Shop"} /-->'),
                (11, 'wp_navigation', 'publish', '<!-- wp:page-list /-->'),
                (12, 'wp_navigation', 'draft', '<!-- wp:navigation-link {"label":"About"} /-->');
            INSERT INTO shop_terms VALUES (1, 'header-menu'), (2, 'draft'), (3, 'empty');
            INSERT INTO shop_term_taxonomy VALUES (1, 1, 'nav_menu'), (2, 2, 'nav_menu'), (3, 3, 'nav_menu');
            INSERT INTO shop_term_relationships VALUES (6, 1), (7, 1), (8, 2);
        """)
        assert conn.execute(query).fetchone() == (1, 1, 2)
        conn.executescript("""
            INSERT INTO shop_terms VALUES (77, 'mobile-navigation'), (472, 'f1'), (473, 'f2'),
                (24, 'menu'), (25, 'foot');
            INSERT INTO shop_term_taxonomy VALUES (177, 77, 'nav_menu'), (572, 472, 'nav_menu'),
                (573, 473, 'nav_menu'), (124, 24, 'nav_menu'), (125, 25, 'nav_menu');
            INSERT INTO shop_posts VALUES (538, 'nav_menu_item', 'publish', ''),
                (13670, 'nav_menu_item', 'publish', ''), (15392, 'nav_menu_item', 'publish', ''),
                (13672, 'nav_menu_item', 'publish', ''), (41328, 'nav_menu_item', 'publish', ''),
                (126, 'nav_menu_item', 'publish', '');
            INSERT INTO shop_term_relationships VALUES (538, 177), (13670, 572), (15392, 572),
                (13672, 573), (41328, 124), (126, 125);
        """)
        assert conn.execute(query).fetchone() == (1, 1, 2)
        conn.executescript("""
            INSERT INTO shop_terms VALUES (4, 'custom-navigation');
            INSERT INTO shop_term_taxonomy VALUES (4, 4, 'nav_menu');
            INSERT INTO shop_term_relationships VALUES (9, 177), (6, 4);
        """)
        assert conn.execute(query).fetchone() == (1, 1, 4)


def test_wordpress_collection_keeps_local_credentials_out_of_process_arguments(tmp_path, monkeypatch):
    collector = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "collect-sites.py"))
    config = tmp_path / "wp-config.php"
    config.write_text(r"""<?php
/*
define('DB_HOST', 'elsewhere.example.com');
*/
define('DB_HOST', 'localhost:3307');
define("DB_NAME", "shop");
define('DB_USER', 'shop');
define('DB_PASSWORD', 'secret#with\\and\'symbols');
$table_prefix = 'shop_';
""")

    def run(args, **kwargs):
        assert "--host=localhost" in args
        assert "--port=3307" in args and "--protocol=TCP" in args
        assert "--local-infile=0" in args
        assert all("secret" not in arg for arg in args)
        defaults = Path(args[1].removeprefix("--defaults-file="))
        assert defaults.stat().st_mode & 0o777 == 0o600
        assert "password=\"secret#with\\\\and'symbols\"\n" in defaults.read_text()
        return subprocess.CompletedProcess(args, 0, "0\t3\t4\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert collector["collect_data"](tmp_path, "/usr/bin/mysql", float("inf")) == {
        "products": 0, "importing": 3, "menus": 4,
    }
    config.write_text(config.read_text().replace("localhost:3307", "elsewhere.example.com"))
    assert collector["collect_data"](tmp_path, "/usr/bin/mysql", float("inf")) is None


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
    monkeypatch.setattr(
        sites, "collect", lambda server, key: [replace(site, data=sites.SiteData(10 if server == a else 0, 0, 4))],
    )
    sites._refresh(ctx.conn, ctx)
    snapshot = sites.snapshots(ctx.conn)
    assert len(snapshot[a.id].rows) == len(snapshot[b.id].rows) == 1
    assert snapshot[a.id].rows[0]["data"] == {"products": 10, "importing": 0, "menus": 4}
    assert snapshot[b.id].rows[0]["data"]["products"] == 0
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
    assert retry[a.id].rows[0]["data"] == snapshot[a.id].rows[0]["data"]
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
    username = "admin"
    client, csrf = login_client(ctx, username)
    fields = {"name": "刘子涵", "host": "104.255.153.35", "port": "22", "host_key": host_key()}
    assert client.get("/servers/new").status_code == 200
    assert client.post("/servers", data=fields).status_code == 403
    response = client.post("/servers", data={**fields, "csrf": csrf})
    assert response.status_code == 303
    detail_path = response.headers["location"]
    detail = client.get(detail_path).text
    assert "刘子涵" in detail and "codexctl-site-inventory" in detail and "SHA256:" in detail
    assert client.get(detail_path + "/edit").status_code == 200
    for operation in ("edit", "delete", "enrollment"):
        assert client.post(detail_path + "/" + operation, data=fields).status_code == 403
        assert client.post(detail_path + "/" + operation, data={**fields, "csrf": "wrong"}).status_code == 403
    assert client.post(detail_path + "/enrollment", data={"csrf": csrf}).status_code == 303
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
    assert {r[0] for r in ctx.conn.execute("SELECT actor FROM audit_log WHERE action LIKE 'server.%'")} == {
        f"web:{username}",
    }


def test_wp_center_isolates_remote_page_and_checks_proxy_permissions(ctx, monkeypatch):
    server = create(ctx)
    ctx.settings = replace(ctx.settings, site_ssh_enabled=True, site_ssh_shared=True)
    admin, csrf = login_client(ctx)
    reader, reader_csrf = login_client(ctx, "reader")
    path = f"/servers/{server.id}/wp-center"
    page = """<head></head><script>
    window.AI_CENTER_RUNTIME={version:"2.1.0",transport:"panel"};
    function upload(){if(window.AI_CENTER_RUNTIME?.transport==='panel') return nativeUpload()}
    </script>"""
    monkeypatch.setattr("codexctl.sites.access.shared_key", lambda settings: Path("/unused"))
    monkeypatch.setattr(wp_center, "installed_workspace", lambda *args: ("2.1.0", page))
    monkeypatch.setattr(wp_center, "theme_assets", lambda *args: ({}, {}))

    def start_upload(target, key, action, payload):
        assert (target.id, action, payload) == (server.id, "start_resource_upload", {"name": "test.csv"})
        return {"chunk_size": 1024 * 1024}

    monkeypatch.setattr(wp_center, "plugin", start_upload)
    shell = admin.get(path)
    assert shell.status_code == 200 and 'sandbox="allow-scripts allow-downloads"' in shell.text
    assert csrf in shell.text and "unsafe-inline" not in shell.headers["content-security-policy"]
    document = admin.get(path + "/document")
    assert document.status_code == 200 and csrf not in document.text
    assert "sandbox allow-scripts allow-downloads" in document.headers["content-security-policy"]
    assert "allow-same-origin" not in document.headers["content-security-policy"]
    assert "codexctl_proxy_upload:true" in document.text
    assert "!window.AI_CENTER_RUNTIME?.codexctl_proxy_upload" in document.text
    assert reader.get(path).status_code == reader.get(path + "/document").status_code == 403
    proxy = path + "/proxy/plugin?s=start_resource_upload"
    data = {"payload": '{"name":"test.csv"}'}
    assert reader.post(proxy, data=data, headers={"X-CSRFToken": reader_csrf}).status_code == 403
    assert admin.post(proxy, data=data).status_code == 403
    assert admin.post(proxy, data=data, headers={"X-CSRFToken": csrf}).json() == {
        "status": True, "data": {"chunk_size": wp_center.PROXY_UPLOAD_CHUNK_SIZE},
    }


def test_server_pages_require_login(ctx):
    ctx.test_users.add("reader")
    reader, csrf = login_client(ctx, "reader")
    landing = reader.get("/", follow_redirects=True)
    assert landing.status_code == 200 and 'href="/servers"' in landing.text
    server = create(ctx)
    for path in ("/servers", f"/servers/{server.id}", f"/servers?server_id={server.id}&q=example"):
        page = reader.get(path)
        assert page.status_code == 200
        assert 'href="/servers/new"' not in page.text
        assert f'href="/servers/{server.id}/edit"' not in page.text
        assert f'action="/servers/{server.id}/delete"' not in page.text
        assert f'action="/servers/{server.id}/enrollment"' not in page.text
        assert 'enrollment-command' not in page.text
        assert '采集授权公钥' not in page.text
    for path in ("/servers/new", f"/servers/{server.id}/edit"):
        assert reader.get(path).status_code == 403
    for path in ("/servers", f"/servers/{server.id}/edit", f"/servers/{server.id}/delete", f"/servers/{server.id}/enrollment"):
        assert reader.post(path, data={"csrf": csrf}).status_code == 403
    assert site_registry.get(ctx.conn, server.id) == server
    for path in ("/", "/accounts", "/users", "/usage", "/audit"):
        assert reader.get(path).status_code in (303, 403)
    assert reader.post("/accounts/x/disable", data={"csrf": csrf}).status_code == 403
    anon = TestClient(create_app(ctx), follow_redirects=False)
    for path in ("/servers", "/servers/new", f"/servers/{server.id}", f"/servers/{server.id}/edit"):
        assert anon.get(path).headers["location"] == "/login"
    for path in ("/servers", f"/servers/{server.id}/edit", f"/servers/{server.id}/delete", f"/servers/{server.id}/enrollment"):
        assert anon.post(path, data={"csrf": csrf}).status_code == 403


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
    first = client.get("/servers?view=sites").text
    assert 'datetime="2026-11-15"' in first and "site00.com" in first and "site50.com" not in first
    assert first.count('<details class="panel flush site-server">') == 2
    first_server, next_server = first.split(f'id="site-server-{other.id}"', 1)
    assert first_server.index(f'id="site-server-{server.id}"') < first_server.index("site00.com")
    assert "other-site.com" in next_server and "other-site.com" not in first_server
    assert f'href="/servers?server_id={server.id}">查看该服务器的全部 53 个站点</a>' in first_server
    assert "第 1 /" not in first
    filtered_overview = client.get("/servers?date=2026-11-15").text
    assert f'href="/servers?date=2026-11-15&amp;server_id={server.id}">查看该服务器的全部 51 个站点</a>' in filtered_overview
    scoped_first = client.get(f"/servers?server_id={server.id}").text
    assert f'href="/servers?server_id={server.id}&amp;page=2">下一页</a>' in scoped_first
    second = client.get(f"/servers?server_id={server.id}&page=2").text
    assert "site50.com" in second and "日期未知" in second and "DNS 无可用地址" in second
    assert second.count('<details class="panel flush site-server" open>') == 1
    assert second.index("site50.com") < second.index("dns-error.com") < second.index("unknown.com")
    assert "第 2 / 2 页" in second and "other-site.com" not in second
    scoped = client.get(f"/servers?server_id={other.id}&date=2026-11-15").text
    assert "other-site.com" in scoped and "site00.com" not in scoped
    filtered = client.get(f"/servers?state=down&server_id={server.id}&q=dns").text
    assert "dns-error.com" in filtered and "site00.com" not in filtered
    query = urlencode({"date": ["2026-11-15", "unknown"], "state": ["up", "down"], "q": ".com"}, doseq=True)
    html_query = query.replace("&", "&amp;")
    multi = client.get(f"/servers?{query}&server_id={server.id}&server_id={other.id}&page=2").text
    assert "site00.com" in multi and "other-site.com" in multi
    assert "site50.com" not in multi and "第 2 /" not in multi
    assert f'href="/servers?{html_query}&amp;server_id={server.id}">查看该服务器的全部 52 个站点</a>' in multi
    next_page = client.get(f"/servers?{query}&server_id={server.id}").text
    assert f'href="/servers?{html_query}&amp;server_id={server.id}&amp;page=2">下一页</a>' in next_page
    last_page = client.get(f"/servers?{query}&server_id={server.id}&page=2").text
    assert "site50.com" in last_page and "unknown.com" in last_page and "dns-error.com" not in last_page
    ctx.test_clock.t += 901
    assert "结果已过期" in client.get("/servers?state=stale").text


def test_overview_combines_multiple_filter_values(ctx, monkeypatch):
    client, _ = login_client(ctx)
    first = create(ctx)
    second = create(ctx, name="第二台", host="104.255.152.77")
    third = create(ctx, name="第三台", host="104.255.152.78")
    old = int(datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp())
    new = int(datetime(2026, 11, 14, 17, tzinfo=timezone.utc).timestamp())
    rows = {
        first.id: [sites.Site("1", old, ("old.example.com",)), sites.Site("2", new, ("new.example.com",))],
        second.id: [sites.Site("1", None, ("unknown.example.com",)), sites.Site("2", old, ("slow.example.com",))],
        third.id: [sites.Site("1", old, ("excluded.example.com",))],
    }
    monkeypatch.setattr(sites, "collect", lambda owner, key: rows[owner.id])
    results = {
        "unknown.example.com": site_probe.Probe("down", reason="dns"),
        "slow.example.com": site_probe.Probe("down", reason="timeout"),
    }
    monkeypatch.setattr(
        site_probe, "probe",
        lambda domains, origin: results.get(domains[0], site_probe.Probe("up", 200)),
    )
    sites._refresh(ctx.conn, ctx)
    query = urlencode({
        "server_id": [first.id, second.id, first.id, "invalid", ""],
        "date": ["2026-11-11", "unknown", "2026-11-11", "invalid", ""],
        "state": ["up", "down", "up", "invalid", ""],
        "q": "example.com",
    }, doseq=True)
    page = client.get(f"/servers?{query}")
    assert page.status_code == 200
    assert "筛选结果 2 个站点" in page.text
    assert "old.example.com" in page.text and "unknown.example.com" in page.text
    assert all(domain not in page.text for domain in ("new.example.com", "slow.example.com", "excluded.example.com"))
    for name, values in (("server_id", [first.id, second.id]), ("date", ["2026-11-11", "unknown"]), ("state", ["up", "down"])):
        for value in values:
            assert f'type="checkbox" name="{name}" value="{value}" checked' in page.text
    assert page.text.count("已选 2 项") == 3
    assert 'href="/servers?view=sites">清除筛选</a>' in page.text
    unfiltered = client.get("/servers?server_id=invalid&date=invalid&state=invalid").text
    assert "筛选结果 5 个站点" in unfiltered
    assert 'href="/servers?view=sites">清除筛选</a>' not in unfiltered


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


def test_overview_displays_import_counts_empty_unknown_and_stale_data(ctx, monkeypatch):
    client, _ = login_client(ctx)
    create(ctx)
    rows = [
        sites.Site("1", None, ("imported.example.com",), sites.SiteData(350, 0, 4)),
        sites.Site("2", None, ("empty.example.com",), sites.SiteData(0, 7, 0)),
        sites.Site("3", None, ("unknown.example.com",)),
    ]
    monkeypatch.setattr(sites, "collect", lambda *args: rows)
    monkeypatch.setattr(site_probe, "probe", lambda *args: site_probe.Probe("up", 200))
    sites._refresh(ctx.conn, ctx)
    body = client.get("/servers?q=imported").text
    assert "产品 350" in body and "菜单 4" in body and "上次数据" not in body
    body = client.get("/servers?q=empty").text
    assert "无已发布产品" in body and "无导入菜单" in body and "导入中记录 7" in body
    assert "导入情况待确认" in client.get("/servers?q=unknown").text
    ctx.test_clock.t += sites.STALE_AFTER + 1
    assert "上次数据" in client.get("/servers?q=imported").text


@pytest.mark.parametrize("days", [7, 30, 90])
def test_site_dashboard_counts_creation_dates_per_server(ctx, monkeypatch, days):
    ctx.test_clock.t = int(datetime(2026, 11, 17, 1, tzinfo=sites.BEIJING).timestamp())
    client, _ = login_client(ctx)
    first = create(ctx, name="一号服务器")
    second = create(ctx, name="二号服务器", host="104.255.153.35")
    midnight = int(datetime(2026, 11, 16, 16, tzinfo=timezone.utc).timestamp())
    rows = [
        sites.Site("1", midnight, ("one.example.com", "www.one.example.com")),
        sites.Site("2", midnight - 1, ("two.example.com",)),
        sites.Site("3", midnight - 89 * 86400, ("old.example.com",)),
        sites.Site("4", midnight + 86400, ("future.example.com",)),
        sites.Site("5", None, ("unknown.example.com",)),
    ]
    monkeypatch.setattr(sites, "collect", lambda server, key: rows if server.id == first.id else [rows[0]])
    monkeypatch.setattr(site_probe, "probe", lambda *args: site_probe.Probe("up", 200))
    sites._refresh(ctx.conn, ctx)

    data = site_comparison(ctx.conn, days, ctx.now())
    assert data["total"] == (4 if days == 90 else 3)
    assert data["today"] == 2 and data["unknown_dates"] == 1
    assert len(data["daily"]) == days
    assert data["daily"][-1]["values"] == [1, 1]
    assert data["daily"][-2]["values"] == [1, 0]
    assert [row["id"] for row in data["ranking"]] == [first.id, second.id]
    assert data["pending"] == data["stale"] == []
    assert data["series"][0]["points"][-1]["value"] == 1

    html = client.get(f"/?days={days}").text
    panel = html.split('id="site-comparison"', 1)[1].split('</section>', 1)[0]
    assert html.index('>用户用量对比</h2>') < html.index('id="site-comparison"') < html.index('>未解决告警</span>')
    assert f'href="?days={days}#site-comparison" class="active"' in panel
    assert panel.count('class="chart-hit"') == days
    assert panel.count('class="compare-line ') == 2
    assert 'data-chart-total-label="全部服务器合计"' in panel
    assert "一号服务器" in panel and "二号服务器" in panel
    assert "查看每日服务器建站数量" in panel and "缺少创建日期" in panel


def test_site_dashboard_handles_empty_pending_and_stale_inventories(ctx, monkeypatch):
    client, _ = login_client(ctx)
    assert "暂无服务器，请先在服务器页面添加" in client.get("/").text
    first = create(ctx, name="已采集服务器")
    pending = create(ctx, name="待采集服务器", host="104.255.153.35")
    assert "等待服务器站点数据采集" in client.get("/").text
    monkeypatch.setattr(sites, "collect", lambda *args: [])
    sites._refresh(ctx.conn, ctx)
    ctx.conn.execute("DELETE FROM site_inventories WHERE server_id = ?", (pending.id,))
    ctx.test_clock.t += sites.STALE_AFTER + 1
    data = site_comparison(ctx.conn, 7, ctx.now())
    assert data["pending"] == [pending.name] and data["stale"] == [first.name]
    assert data["total"] == data["today"] == 0 and data["scale"] == 2
    assert data["series"][0]["points"][-1]["y"] == 175
    html = client.get("/?days=123").text
    assert 'href="?days=7#site-comparison" class="active"' in html
    assert "以下服务器使用上次采集数据" in html
    assert "等待采集的服务器" in html
    ctx.conn.execute("UPDATE site_inventories SET error = 'SSH 连接或认证失败' WHERE server_id = ?", (first.id,))
    ctx.test_clock.t -= sites.STALE_AFTER + 1
    assert site_comparison(ctx.conn, 7, ctx.now())["stale"] == [first.name]
    ctx.conn.execute("UPDATE site_inventories SET revision = revision + 1")
    assert site_comparison(ctx.conn, 7, ctx.now())["series"] == []


def test_pages_tolerate_malformed_historical_inventory(ctx):
    client, _ = login_client(ctx)
    server = create(ctx)
    snapshot = [
        {"id": "old", "domains": ["old.example.com"], "created_at": "invalid", "public": {"state": "unknown"}},
        {"id": "valid", "domains": ["valid.example.com"], "created_at": ctx.now(),
         "public": {"state": "up", "checked_at": ctx.now()}, "data": {"products": "bad"}},
        {"id": "missing-domains"},
        None,
    ]
    ctx.conn.execute("INSERT INTO site_inventories VALUES (?, ?, ?, ?, NULL, ?)",
                     (server.id, server.revision, ctx.now(), ctx.now(), json.dumps(snapshot)))
    inventory = sites.snapshots(ctx.conn)[server.id]
    assert len(inventory.rows) == 2
    assert inventory.rows[0]["created_at"] is None
    assert inventory.rows[0]["public"]["state"] == "pending"
    assert inventory.error == "部分站点缓存格式异常，请重新采集"
    response = client.get("/servers?view=sites")
    assert response.status_code == 200 and "valid.example.com" in response.text
    assert "日期未知" in response.text
    assert client.get("/").status_code == 200
    # Read-side normalization keeps the original inventory available for recovery.
    assert json.loads(ctx.conn.execute("SELECT snapshot FROM site_inventories").fetchone()[0]) == snapshot
    ctx.conn.execute("UPDATE site_inventories SET snapshot='invalid json'")
    assert sites.snapshots(ctx.conn)[server.id].error == "站点缓存格式异常，请重新采集"
    assert client.get("/servers").status_code == 200
    assert client.get("/").status_code == 200
