from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncssh

from codexctl.sites.registry import HOST_KEY_ALGORITHMS, Server, ServerError

_DEFAULT_HOST_KEY_ALGORITHMS = tuple(algorithm for choices in HOST_KEY_ALGORITHMS.values() for algorithm in choices)


def _host_key_algorithms(host_key: str | None) -> tuple[str, ...]:
    if host_key is None:
        return _DEFAULT_HOST_KEY_ALGORITHMS
    return HOST_KEY_ALGORITHMS[host_key.split()[0]]


class _EnrollmentHost(asyncssh.SSHClient):
    def __init__(self, host_key: str | None):
        self.host_key = host_key

    def validate_host_public_key(self, host, addr, port, key):
        presented = key.export_public_key().decode("ascii").strip()
        if self.host_key is None:
            self.host_key = presented
        return self.host_key == presented


async def _run_installer(server: Server, password: str, script: str) -> tuple[str, int | None]:
    try:
        async with asyncssh.connect(
            server.host, server.port, username="root", password=password,
            config=None, known_hosts=([], [], []), server_host_key_algs=_host_key_algorithms(server.host_key),
            client_factory=lambda: _EnrollmentHost(server.host_key),
            client_keys=None, agent_path=None, preferred_auth=["password", "keyboard-interactive"],
            connect_timeout=12,
        ) as conn:
            host_key = conn.get_server_host_key()
            result = await asyncio.wait_for(conn.run(
                "python3 -", input=script, stdout=asyncssh.DEVNULL, stderr=asyncssh.DEVNULL,
            ), timeout=40)
            return host_key.export_public_key().decode("ascii").strip(), result.exit_status
    except asyncssh.HostKeyNotVerifiable:
        raise ServerError("SSH 主机公钥与已登记值不一致，请核对服务器后更新") from None
    except asyncssh.PermissionDenied:
        raise ServerError("root 密码认证失败，请检查密码和目标服务器的 SSH 登录设置") from None
    except TimeoutError:
        raise ServerError("SSH 连接或采集器安装超时，请重试") from None
    except (OSError, asyncssh.Error):
        raise ServerError("无法通过 SSH 连接目标服务器或执行采集器安装") from None


def run_installer(server: Server, password: str, script: str) -> tuple[str, int | None]:
    return asyncio.run(_run_installer(server, password, script))


def run(
    server: Server, key: Path, command: str, *, timeout: int, payload: str | None = None,
) -> subprocess.CompletedProcess[str]:
    host = server.host if server.port == 22 else f"[{server.host}]:{server.port}"
    algorithms = ",".join(_host_key_algorithms(server.host_key))
    with TemporaryDirectory(prefix="codexctl-known-hosts-") as directory:
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text(f"{host} {server.host_key}\n", encoding="ascii")
        return subprocess.run(
            [
                "ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
                "-o", f"HostKeyAlgorithms={algorithms}", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "ConnectTimeout=6",
                "-i", str(key), "-p", str(server.port), f"root@{server.host}", command,
            ],
            **({"input": payload} if payload is not None else {"stdin": subprocess.DEVNULL}),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
