from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import codexctl.db as db
import codexctl.sites.probe as site_probe
import codexctl.sites.registry as site_registry
import codexctl.sites.ssh as site_ssh
from codexctl.context import Context
from codexctl.sites.registry import Server
from codexctl.vault import VaultError

log = logging.getLogger(__name__)
INTERVAL = 300
STALE_AFTER = 900
BEIJING = ZoneInfo("Asia/Shanghai")
_MAX_PROBES = 16
_PROBES_PER_SERVER = 4
_RETRIES_PER_SERVER = 2

ProbeTarget = tuple[tuple[str, ...], str | None]


@dataclass(frozen=True)
class SiteData:
    products: int
    importing: int
    menus: int


@dataclass(frozen=True)
class Site:
    id: str
    created_at: int | None
    domains: tuple[str, ...]
    data: SiteData | None = None


@dataclass(frozen=True)
class Inventory:
    revision: int
    attempted_at: int
    synced_at: int | None
    error: str | None
    rows: list[dict]


class InventoryError(Exception):
    pass


def deployment_date(timestamp: int | None) -> str | None:
    if type(timestamp) is not int or not 0 <= timestamp < 253402185600:
        return None
    return datetime.fromtimestamp(timestamp, BEIJING).date().isoformat()


def _domains(values: list) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("invalid domain list")
    return tuple(dict.fromkeys(host for value in values if (host := site_probe.hostname(value))))


def _prefer(domains: tuple[str, ...], name: str | None) -> tuple[str, ...]:
    return tuple(sorted(set(domains), key=lambda host: (host != name, host.startswith("www."), host)))


def _site_data(value: object) -> SiteData | None:
    if not isinstance(value, dict):
        return None
    products, importing, menus = (value.get(key) for key in ("products", "importing", "menus"))
    if all(type(count) is int and count >= 0 for count in (products, importing, menus)):
        return SiteData(products, importing, menus)
    return None


def parse_inventory(payload: str) -> list[Site]:
    data = json.loads(payload)
    if data["version"] != 1 or not isinstance(data["sites"], list) or not isinstance(data["vhosts"], dict):
        raise ValueError("unsupported inventory")
    vhosts = {name: _domains(domains) for name, domains in data["vhosts"].items()}
    sites = []
    seen_ids = set()
    for row in data["sites"]:
        site_id, created_at = row["id"], row["created_at"]
        if type(site_id) is not int or site_id in seen_ids:
            raise ValueError("invalid or duplicate site ID")
        seen_ids.add(site_id)
        if created_at is not None and (type(created_at) is not int or not 0 <= created_at < 253402185600):
            raise ValueError("invalid site timestamp")
        name = site_probe.hostname(row["name"])
        bound = _domains(row["domains"])
        aliases = vhosts.get(row["name"], ())
        if not aliases:
            aliases = tuple(host for domain in bound for host in vhosts.get(domain, ()))
        domains = _prefer((*bound, *aliases) or ((name,) if name else ()), name)
        if domains:
            sites.append(Site(str(site_id), created_at, domains, _site_data(row.get("data"))))
    claimed = {host for site in sites for host in site.domains}
    for name, hosts in sorted(vhosts.items()):
        if hosts and claimed.isdisjoint(hosts):
            domains = _prefer(hosts, site_probe.hostname(name))
            sites.append(Site(f"nginx:{name}", None, domains))
            claimed.update(domains)
    return sites


def collect(server: Server, key: bytes) -> list[Site]:
    if not server.host_key:
        raise InventoryError("待配置 SSH 主机公钥")
    try:
        with TemporaryDirectory(prefix="codexctl-site-ssh-") as directory:
            key_path = Path(directory) / "key"
            key_path.touch(mode=0o600)
            key_path.write_bytes(key)
            proc = site_ssh.run(server, key_path, "codexctl-site-inventory", timeout=22)
    except subprocess.TimeoutExpired as exc:
        raise InventoryError("SSH 采集超时") from exc
    except (OSError, UnicodeError) as exc:
        raise InventoryError("无法执行 SSH 采集") from exc
    if proc.returncode == 255:
        raise InventoryError("SSH 连接或认证失败")
    if proc.returncode:
        raise InventoryError("远程站点采集失败")
    try:
        if len(proc.stdout) > 2_000_000:
            raise ValueError("inventory too large")
        return parse_inventory(proc.stdout)
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryError("远程站点数据格式异常") from exc


def _cached_probe(value: object) -> dict:
    probe = asdict(site_probe.Probe("pending")) | {"checked_at": 0}
    if not isinstance(value, dict):
        return probe
    if isinstance(value.get("state"), str) and value["state"] in {"up", "http_only", "redirect", "restricted", "down", "pending"}:
        probe["state"] = value["state"]
    for key in ("reason", "entry_url", "final_host"):
        if isinstance(value.get(key), str):
            probe[key] = value[key]
    for key in ("code", "checked_at"):
        if type(value.get(key)) is int and 0 <= value[key] < 253402185600:
            probe[key] = value[key]
    return probe


def snapshots(conn: sqlite3.Connection) -> dict[str, Inventory]:
    inventories = {}
    for row in conn.execute("SELECT * FROM site_inventories"):
        error = row["error"]
        try:
            cached = json.loads(row["snapshot"])
        except (ValueError, TypeError):
            cached = None
        rows = []
        if not isinstance(cached, list):
            cached = []
            error = error or "站点缓存格式异常，请重新采集"
        for item in cached:
            if not isinstance(item, dict) or not isinstance(item.get("domains"), (list, tuple)):
                error = error or "部分站点缓存格式异常，请重新采集"
                continue
            domains = _domains(list(item["domains"]))
            if not domains or not isinstance(item.get("id"), (str, int)):
                error = error or "部分站点缓存格式异常，请重新采集"
                continue
            data = _site_data(item.get("data"))
            created = item.get("created_at")
            rows.append({
                "id": str(item["id"]), "domains": list(domains),
                "created_at": created if deployment_date(created) is not None else None,
                "data": asdict(data) if data is not None else None,
                "public": _cached_probe(item.get("public")),
                "origin": _cached_probe(item["origin"]) if item.get("origin") is not None else None,
            })
        inventories[row["server_id"]] = Inventory(
            row["revision"], row["attempted_at"], row["synced_at"], error, rows,
        )
    return inventories


def _collect(server: Server, key: bytes, clock) -> tuple[list[Site] | None, int, str | None]:
    if not server.host_key:
        return None, clock(), "待配置 SSH 主机公钥"
    try:
        return collect(server, key), clock(), None
    except InventoryError as exc:
        log.warning("服务器 %s 的站点采集失败：%s", server.id, exc)
        return None, clock(), str(exc)


def _aliases(site: Site) -> set[str]:
    return {domain.removeprefix("www.") for domain in site.domains}


def _probe(domains: tuple[str, ...], origin: str | None, clock):
    return {**asdict(site_probe.probe(domains, origin)), "checked_at": clock()}


def _probe_by_server(targets: dict[ProbeTarget, str], clock, per_server: int) -> dict[ProbeTarget, dict]:
    batches = defaultdict(list)
    for target, server_id in targets.items():
        batches[server_id].append(target)

    def check_batch(batch):
        with ThreadPoolExecutor(max_workers=per_server) as workers:
            checks = {target: workers.submit(_probe, *target, clock) for target in batch}
            return {target: check.result() for target, check in checks.items()}

    results = {}
    with ThreadPoolExecutor(max_workers=_MAX_PROBES // per_server) as servers:
        for batch_results in servers.map(check_batch, batches.values()):
            results.update(batch_results)
    return results


def _refresh(conn: sqlite3.Connection, ctx: Context, server_id: str | None = None) -> None:
    previous = snapshots(conn)
    clock = ctx.now
    inventories = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        requests = {}
        for server in site_registry.list_servers(conn):
            if server_id is not None and server.id != server_id:
                continue
            try:
                key = site_registry.private_key(conn, ctx.vault, server) if server.host_key else b""
            except VaultError:
                log.warning("服务器 %s 的 SSH 密钥无法解密", server.id)
                requests[server] = None
                continue
            if key is not None:
                requests[server] = pool.submit(_collect, server, key, clock)
        for server, request in requests.items():
            current, attempted, error = request.result() if request else (None, clock(), "SSH 密钥无法解密")
            cached = previous.get(server.id)
            if cached and cached.revision != server.revision:
                cached = None
            synced = attempted if current is not None else (cached.synced_at if cached else None)
            if current is None:
                current = [
                    Site(row["id"], row["created_at"], tuple(row["domains"]), _site_data(row.get("data")))
                    for row in cached.rows
                ] if cached else []
            inventories[server] = (current, attempted, synced, error)

    counts = Counter(alias for current, *_ in inventories.values() for site in current for alias in _aliases(site))
    if server_id is not None:
        counts.update(
            alias
            for owner_id, cached in previous.items() if owner_id != server_id
            for row in cached.rows for alias in {host.removeprefix("www.") for host in row["domains"]}
        )
    duplicate = {alias for alias, count in counts.items() if count > 1}
    targets: dict[ProbeTarget, str] = {}
    for server, (current, *_) in inventories.items():
        for site in current:
            targets.setdefault((site.domains, None), server.id)
            if duplicate & _aliases(site):
                targets[(site.domains, server.host)] = server.id

    results = _probe_by_server(targets, clock, _PROBES_PER_SERVER)
    retries = {
        (domains, origin): targets[(domains, origin)]
        for (domains, origin), result in results.items()
        if origin is None and result["state"] == "down" and result["reason"] in {"timeout", "connect"}
    }
    results.update(_probe_by_server(retries, clock, _RETRIES_PER_SERVER))

    for server, (current, attempted, synced, error) in inventories.items():
        rows = [
            {
                **asdict(site),
                "public": results[site.domains, None],
                "origin": results.get((site.domains, server.host)),
            }
            for site in current
        ]
        conn.execute(
            """
            INSERT INTO site_inventories(server_id, revision, attempted_at, synced_at, error, snapshot)
            SELECT id, revision, ?, ?, ?, ? FROM site_servers WHERE id = ? AND revision = ?
            ON CONFLICT(server_id) DO UPDATE SET
                revision = excluded.revision, attempted_at = excluded.attempted_at,
                synced_at = excluded.synced_at, error = excluded.error, snapshot = excluded.snapshot
            WHERE excluded.attempted_at >= site_inventories.attempted_at
            """,
            (attempted, synced, error, json.dumps(rows), server.id, server.revision),
        )
        log.info("服务器 %s 已巡检 %d 条站点记录", server.id, len(rows))


def refresh(ctx: Context, server_id: str | None = None) -> None:
    # The site job uses its own connection so network I/O never holds the web or daemon DB locks.
    with closing(db.connect(ctx.settings.db_path)) as conn:
        _refresh(conn, ctx, server_id)
