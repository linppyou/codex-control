from __future__ import annotations

import json
import math
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import codexctl.sites.access as site_access
import codexctl.sites.ssh as site_ssh
from codexctl.config import Settings
from codexctl.sites.registry import Server


_PROBE = """
import json
import os
import time

def cpu():
    with open('/proc/stat', encoding='ascii') as source:
        fields = [int(value) for value in source.readline().split()[1:9]]
    return sum(fields), sum(fields[3:5])

before_total, before_idle = cpu()
time.sleep(0.3)
after_total, after_idle = cpu()
elapsed = after_total - before_total
if elapsed <= 0:
    raise ValueError('CPU sample did not advance')
cpu_percent = 100 * (elapsed - (after_idle - before_idle)) / elapsed
memory = {}
with open('/proc/meminfo', encoding='ascii') as source:
    for line in source:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            memory[parts[0].rstrip(':')] = int(parts[1]) * 1024
total_memory = memory['MemTotal']
available_memory = memory.get('MemAvailable', memory.get('MemFree', 0) + memory.get('Buffers', 0) + memory.get('Cached', 0))
disk = os.statvfs('/')
disk_total = disk.f_blocks * disk.f_frsize
disk_used = (disk.f_blocks - disk.f_bfree) * disk.f_frsize
disk_available = disk.f_bavail * disk.f_frsize
cores = os.cpu_count()
if not cores:
    raise ValueError('CPU count unavailable')
print(json.dumps({
    'cores': cores,
    'load': os.getloadavg()[0],
    'cpu': max(0, min(100, cpu_percent)),
    'memory_total': total_memory,
    'memory_used': max(0, min(total_memory, total_memory - available_memory)),
    'disk_total': disk_total,
    'disk_used': disk_used,
    'disk_available': disk_available,
}))
"""
_COMMAND = "python3 -c " + shlex.quote(_PROBE)


def _read(server: Server, key: Path) -> dict:
    if not server.host_key:
        return {"id": server.id, "error": "未配置 SSH 主机公钥"}
    try:
        result = site_ssh.run(server, key, _COMMAND, timeout=12)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError("metrics command failed")
        metrics = json.loads(result.stdout)
        keys = ("cores", "load", "cpu", "memory_total", "memory_used", "disk_total", "disk_used", "disk_available")
        if not isinstance(metrics, dict) or any(
            type(metrics.get(key)) not in (int, float) or not math.isfinite(metrics[key]) or metrics[key] < 0
            for key in keys
        ):
            raise ValueError("invalid metrics")
        if (
            type(metrics["cores"]) is not int or metrics["cores"] < 1
            or metrics["cpu"] > 100 or metrics["memory_total"] <= 0 or metrics["disk_total"] <= 0
            or metrics["memory_used"] > metrics["memory_total"] or metrics["disk_used"] > metrics["disk_total"]
            or metrics["disk_available"] > metrics["disk_total"] - metrics["disk_used"]
        ):
            raise ValueError("invalid metrics")
        return {"id": server.id, "metrics": {key: metrics[key] for key in keys}}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        return {"id": server.id, "error": "无法连接或读取服务器数据"}


def collect(servers: list[Server], settings: Settings) -> list[dict]:
    if not servers:
        return []
    if not (settings.site_ssh_enabled and settings.site_ssh_shared and settings.manages_local_codex):
        return [{"id": server.id, "error": "需启用共享站点 SSH"} for server in servers]
    try:
        key = site_access.shared_key(settings)
    except (OSError, ValueError):
        return [{"id": server.id, "error": "共享 SSH 密钥不可用"} for server in servers]
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        return list(pool.map(lambda server: _read(server, key), servers))
