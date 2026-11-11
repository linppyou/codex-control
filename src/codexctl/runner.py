from __future__ import annotations

import subprocess
from typing import Protocol


class CommandError(RuntimeError):
    pass


class Runner(Protocol):
    def __call__(self, args: list[str], input: bytes | None = None, timeout: float = 30) -> str: ...


def run(args: list[str], input: bytes | None = None, timeout: float = 30) -> str:
    try:
        proc = subprocess.run(args, input=input, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CommandError(f"{args[0]} 执行失败：{e}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise CommandError(f"{args[0]} 退出码 {proc.returncode}：{stderr}")
    return proc.stdout.decode(errors="replace")
