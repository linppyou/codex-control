"""Standalone installer rendered by site_enrollment with per-server settings."""

import base64
import fcntl
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

SETTINGS = {}


def atomic_write(path, content, mode):
    descriptor, temporary = tempfile.mkstemp(prefix=".codexctl-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if os.geteuid() != 0:
        raise SystemExit("请在目标站点服务器上以 root 执行接入命令")
    panel = Path("/www/server/panel")
    if not any((panel / name).is_file() for name in ("data/db/site.db", "data/default.db")):
        raise SystemExit("未找到宝塔站点数据库，请确认在目标宝塔服务器执行")
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().strip()
    collector = Path("/usr/local/libexec/codexctl-site-inventory")
    collector.parent.mkdir(parents=True, exist_ok=True)
    ssh = Path("/root/.ssh")
    ssh.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ssh, 0o700)
    with (ssh / ".codexctl-enrollment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atomic_write(collector, base64.b64decode(SETTINGS["collector"]), 0o755)
        result = subprocess.run([str(collector)], capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise SystemExit("采集脚本自检失败，请检查宝塔数据库及 Python 3 环境")
        json.loads(result.stdout)
        authorized = ssh / "authorized_keys"
        lines = authorized.read_text().splitlines() if authorized.exists() else []
        keys = [SETTINGS["authorized_key"]]
        if SETTINGS.get("access_key"):
            keys.append(SETTINGS["access_key"])
        for authorization in keys:
            public_key = next(part for part in authorization.split() if part.startswith("AAAA"))
            # Match key blobs so retries preserve unrelated authorizations.
            lines = [line for line in lines if public_key not in line.split()]
            lines.append(authorization)
        atomic_write(authorized, ("\n".join(lines) + "\n").encode(), 0o600)
    request = urllib.request.Request(
        SETTINGS["callback"], data=urlencode({"host_key": host_key}).encode(),
        headers={"Authorization": "Bearer " + SETTINGS["token"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "接入校验失败")
        except (ValueError, AttributeError):
            detail = "接入校验失败"
        raise SystemExit(str(detail)) from None
    except urllib.error.URLError:
        raise SystemExit("无法连接管理面板，请检查网络后重试接入命令") from None
    print("接入成功，已验证 %s 个站点。返回面板查看首次巡检结果。" % data["sites"])


if __name__ == "__main__":
    main()
