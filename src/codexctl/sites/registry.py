from __future__ import annotations

import base64
import hashlib
import ipaddress
import sqlite3
import unicodedata
from dataclasses import dataclass
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

import codexctl.db as db
import codexctl.events as events
import codexctl.sites.access as site_access
import codexctl.sites.probe as site_probe
from codexctl.context import Context
from codexctl.vault import Vault

COLLECTOR = "/usr/local/libexec/codexctl-site-inventory"


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    host: str
    port: int
    host_key: str | None
    public_key: str
    revision: int

    @property
    def authorized_key(self) -> str:
        return f'restrict,command="{COLLECTOR}" {self.public_key} codexctl-site-{self.id}'

    @property
    def fingerprint(self) -> str:
        if not self.host_key:
            return ""
        raw = base64.b64decode(self.host_key.split()[1])
        return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


class ServerError(ValueError):
    pass


_FIELDS = "id, name, host, port, host_key, public_key, revision"


def list_servers(conn: sqlite3.Connection) -> list[Server]:
    return [Server(**dict(row)) for row in conn.execute(f"SELECT {_FIELDS} FROM site_servers ORDER BY rowid")]


def get(conn: sqlite3.Connection, server_id: str) -> Server | None:
    row = conn.execute(f"SELECT {_FIELDS} FROM site_servers WHERE id = ?", (server_id,)).fetchone()
    return Server(**dict(row)) if row else None


def _validate(name: str, host: str, port: str, host_key: str) -> tuple[str, str, int, str | None]:
    name = name.strip()
    if not name or len(name) > 60 or any(unicodedata.category(char).startswith("C") for char in name):
        raise ServerError("请输入有效的服务器名称（最多 60 个字符）")
    try:
        ip = ipaddress.ip_address(host.strip())
        if not site_probe.public_ip(str(ip)):
            raise ValueError
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError as exc:
        raise ServerError("请输入有效的公网 IPv4 或 IPv6 地址") from exc
    port = port.strip()
    if not port.isascii() or not port.isdecimal() or len(port) > 5 or not 1 <= int(port) <= 65535:
        raise ServerError("SSH 端口应为 1 至 65535")
    parts = host_key.split()
    pinned = None
    if parts:
        try:
            if len(parts) < 2 or parts[0] != "ssh-ed25519" or len(parts[1]) > 128:
                raise ValueError
            key = serialization.load_ssh_public_key(" ".join(parts[:2]).encode())
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError
            pinned = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        except ValueError as exc:
            raise ServerError("请输入完整的 ssh-ed25519 主机公钥") from exc
    return name, str(ip), int(port), pinned


def _aad(server_id: str) -> bytes:
    return f"site-ssh:{server_id}".encode("ascii")


def private_key(conn: sqlite3.Connection, vault: Vault, server: Server) -> bytes | None:
    row = conn.execute(
        "SELECT private_key FROM site_servers WHERE id = ? AND revision = ?", (server.id, server.revision)
    ).fetchone()
    return vault.decrypt(row["private_key"], _aad(server.id)) if row else None


def create(ctx: Context, actor: str, name: str, host: str, port: str, host_key: str) -> Server:
    name, host, port_number, pinned = _validate(name, host, port, host_key)
    server_id = uuid4().hex
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption(),
    )
    try:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                """
                INSERT INTO site_servers (id, name, host, port, host_key, public_key, private_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (server_id, name, host, port_number, pinned, public, ctx.vault.encrypt(private, _aad(server_id))),
            )
            events.audit(ctx.conn, actor, "server.create", server_id, name, now=ctx.now())
    except sqlite3.IntegrityError as exc:
        raise ServerError("该 IP 和端口已登记") from exc
    site_access.sync_client_config(ctx)
    return Server(server_id, name, host, port_number, pinned, public, 0)


def update(ctx: Context, actor: str, server: Server, name: str, host: str, port: str, host_key: str) -> None:
    name, host, port_number, pinned = _validate(name, host, port, host_key)
    changed = (host, port_number, pinned) != (server.host, server.port, server.host_key)
    try:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE site_servers SET name=?, host=?, port=?, host_key=?, revision=revision+? WHERE id=?",
                (name, host, port_number, pinned, int(changed), server.id),
            )
            if changed:
                ctx.conn.execute("DELETE FROM site_inventories WHERE server_id = ?", (server.id,))
                ctx.conn.execute("DELETE FROM site_enrollments WHERE server_id = ?", (server.id,))
            events.audit(ctx.conn, actor, "server.update", server.id, name, now=ctx.now())
    except sqlite3.IntegrityError as exc:
        raise ServerError("该 IP 和端口已登记") from exc
    site_access.sync_client_config(ctx)


def delete(ctx: Context, actor: str, server: Server) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM site_servers WHERE id = ?", (server.id,))
        events.audit(ctx.conn, actor, "server.delete", server.id, server.name, now=ctx.now())
    site_access.sync_client_config(ctx)
