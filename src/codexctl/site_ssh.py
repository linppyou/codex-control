from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from .site_registry import Server


def run(
    server: Server, key: Path, command: str, *, timeout: int, payload: str | None = None,
) -> subprocess.CompletedProcess[str]:
    host = server.host if server.port == 22 else f"[{server.host}]:{server.port}"
    with TemporaryDirectory(prefix="codexctl-known-hosts-") as directory:
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text(f"{host} {server.host_key}\n", encoding="ascii")
        return subprocess.run(
            [
                "ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
                "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "ConnectTimeout=6",
                "-i", str(key), "-p", str(server.port), f"root@{server.host}", command,
            ],
            **({"input": payload} if payload is not None else {"stdin": subprocess.DEVNULL}),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
