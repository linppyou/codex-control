from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shlex
from dataclasses import asdict, replace
from pathlib import Path

import codexctl.db as db
import codexctl.events as events
import codexctl.sites.access as site_access
import codexctl.sites.inventory as sites
import codexctl.sites.probe as site_probe
import codexctl.sites.registry as site_registry
import codexctl.sites.ssh as site_ssh
from codexctl.context import Context
from codexctl.sites.installer import INSTALL_ERRORS

TTL = 1800
MAX_PASSWORD_LENGTH = 1024


def _aad(server_id: str) -> bytes:
    return f"site-enrollment:{server_id}".encode("ascii")


def issue(ctx: Context, server: site_registry.Server) -> str:
    token = secrets.token_urlsafe(32)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            """INSERT INTO site_enrollments VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET revision=excluded.revision,
               token_hash=excluded.token_hash, token_ciphertext=excluded.token_ciphertext,
               expires_at=excluded.expires_at""",
            (server.id, server.revision, hashlib.sha256(token.encode()).hexdigest(),
             ctx.vault.encrypt(token.encode(), _aad(server.id)), ctx.now() + TTL),
        )
    return token


def pending(ctx: Context, server: site_registry.Server):
    return ctx.conn.execute(
        "SELECT * FROM site_enrollments WHERE server_id=? AND revision=? AND expires_at>?",
        (server.id, server.revision, ctx.now()),
    ).fetchone()


def authenticate(ctx: Context, token: str) -> site_registry.Server:
    row = ctx.conn.execute(
        """SELECT server_id, revision FROM site_enrollments
           WHERE token_hash=? AND expires_at>?""",
        (hashlib.sha256(token.encode()).hexdigest(), ctx.now()),
    ).fetchone()
    server = site_registry.get(ctx.conn, row["server_id"]) if row else None
    if server is None or server.revision != row["revision"]:
        raise site_registry.ServerError("接入命令已失效，请在服务器详情页重新生成")
    return server


def command(ctx: Context, server: site_registry.Server, base_url: str) -> str | None:
    row = pending(ctx, server)
    if row is None:
        return None
    token = ctx.vault.decrypt(row["token_ciphertext"], _aad(server.id)).decode()
    url = base_url + "/site-enrollment/install"
    # Download fully before executing so a failed download cannot run a partial installer.
    return (
        "( set -eu; umask 077; f=$(mktemp); trap 'rm -f \"$f\"' EXIT; "
        f"curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "
        f"--data-urlencode {shlex.quote('token=' + token)} {shlex.quote(url)} -o \"$f\"; "
        "python3 \"$f\" )"
    )


def installer(
    server: site_registry.Server, token: str = "", base_url: str = "", *, access_key: str | None = None,
) -> str:
    collector = Path(__file__).resolve().parents[1] / "collect_sites.py"
    if not collector.is_file():
        collector = Path(__file__).resolve().parents[3] / "deploy" / "collect-sites.py"
    settings = {
        "collector": base64.b64encode(collector.read_bytes()).decode(),
        "authorized_key": server.authorized_key,
        "server_id": server.id,
        "host_key": server.host_key,
        "token": token,
        "callback": base_url + "/site-enrollment/complete" if base_url else None,
        "access_key": access_key,
    }
    template = Path(__file__).with_name("installer.py").read_text()
    return template.replace("SETTINGS = {}", "SETTINGS = " + repr(settings), 1)


def candidate(server: site_registry.Server, host_key: str) -> site_registry.Server:
    pinned = site_registry.normalize_host_key(host_key)
    if not pinned:
        raise site_registry.ServerError("未读取到服务器主机公钥")
    if server.host_key and server.host_key != pinned:
        raise site_registry.ServerError("主机公钥与已登记值不一致，请核对服务器后更新")
    return replace(server, host_key=pinned)


def validate_password(password: str) -> None:
    if not password or len(password) > MAX_PASSWORD_LENGTH or "\x00" in password:
        raise site_registry.ServerError("请输入有效的 root SSH 密码")


def _shared_enabled(ctx: Context) -> bool:
    settings = ctx.settings
    return settings.site_ssh_enabled and settings.site_ssh_shared and settings.manages_local_codex


def deployment_authorization(ctx: Context) -> str | None:
    return site_access.shared_authorization(ctx.settings) if _shared_enabled(ctx) else None


def verify(ctx: Context, server: site_registry.Server, key: bytes) -> list[sites.Site]:
    inventory = sites.collect(server, key)
    if _shared_enabled(ctx):
        access = site_ssh.run(server, site_access.shared_key(ctx.settings), "id -u", timeout=15)
        if access.returncode or access.stdout.strip() != "0":
            raise site_registry.ServerError("共享部署密钥登录验证失败，请核对目标服务器的 SSH 公钥登录设置")
    return inventory


def install_with_password(
    ctx: Context, server: site_registry.Server, password: str, key: bytes,
) -> tuple[site_registry.Server, list[sites.Site]]:
    script = installer(server, access_key=deployment_authorization(ctx))
    host_key, status = site_ssh.run_installer(server, password, script)
    if status != 0:
        raise site_registry.ServerError(INSTALL_ERRORS.get(status, "采集器安装或 SSH 公钥配置失败"))
    confirmed = candidate(server, host_key)
    return confirmed, verify(ctx, confirmed, key)


def complete(ctx: Context, server: site_registry.Server, token: str, verified: list[sites.Site]) -> None:
    with db.transaction(ctx.conn):
        current = authenticate(ctx, token)
        if current.id != server.id or current.revision != server.revision:
            raise site_registry.ServerError("服务器配置已变更，请重新生成接入命令")
        if current.host_key != server.host_key:
            ctx.conn.execute(
                "UPDATE site_servers SET host_key=?, revision=revision+1 WHERE id=?", (server.host_key, server.id),
            )
            ctx.conn.execute("DELETE FROM site_inventories WHERE server_id=?", (server.id,))
        revision = current.revision + int(current.host_key != server.host_key)
        pending_probe = {**asdict(site_probe.Probe("pending")), "checked_at": ctx.now()}
        rows = [{**asdict(site), "public": pending_probe, "origin": None} for site in verified]
        ctx.conn.execute(
            """INSERT INTO site_inventories VALUES (?, ?, ?, ?, NULL, ?)
               ON CONFLICT(server_id) DO NOTHING""",
            (server.id, revision, ctx.now(), ctx.now(), json.dumps(rows)),
        )
        ctx.conn.execute("DELETE FROM site_enrollments WHERE server_id=?", (server.id,))
        events.audit(ctx.conn, "site-enrollment", "site.enrolled", server.id,
                     json.dumps({"sites": len(verified)}), now=ctx.now())
    site_access.sync_client_config(ctx)
