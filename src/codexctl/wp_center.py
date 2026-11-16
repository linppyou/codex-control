from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import site_ssh
from .site_registry import Server


class CenterError(ValueError):
    pass


def request(server: Server, key: Path, operation: str, **fields) -> dict:
    if not server.host_key:
        raise CenterError("请先在服务器设置中保存 SSH 主机公钥")
    if not key.is_file():
        raise CenterError("总控尚未安装 WP 建站专用 SSH 密钥")
    try:
        proc = site_ssh.run(
            server, key, "codexctl-wp-center",
            timeout=95 if operation in {"inspect", "create"} else 20,
            payload=json.dumps({"operation": operation, **fields}),
        )
    except subprocess.TimeoutExpired as exc:
        raise CenterError("远程请求超时，请刷新任务列表确认执行状态") from exc
    except (OSError, UnicodeError) as exc:
        raise CenterError("无法执行 WP 建站中心的 SSH 请求") from exc
    if proc.returncode == 255:
        raise CenterError("SSH 连接或认证失败，请检查服务器的建站授权")
    if proc.returncode:
        raise CenterError("远程 WP 建站接口不可用，请检查服务器的建站桥接脚本")
    try:
        if len(proc.stdout) > 4_000_000:
            raise ValueError
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError
        if result.get("ok") is False and isinstance(result.get("message"), str):
            raise CenterError(result["message"][:1500])
        if result.get("ok") is not True or not isinstance(result.get("data"), dict):
            raise ValueError
    except CenterError:
        raise
    except ValueError as exc:
        raise CenterError("远程 WP 建站中心返回的数据格式异常") from exc
    return result["data"]
