from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexProc:
    pid: int
    exe: str
    started_at: int  # Unix 秒


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _boot_time(proc_root: Path) -> int | None:
    text = _read_text(proc_root / "stat")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _clock_ticks() -> int:
    try:
        ticks = int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100
    return ticks if ticks > 0 else 100


def _uid_of(proc_root: Path, name: str) -> int | None:
    text = _read_text(proc_root / name / "status")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _start_ticks(proc_root: Path, name: str) -> int | None:
    text = _read_text(proc_root / name / "stat")
    if text is None:
        return None
    # 进程名可能含空格或括号，从最后一个 ')' 之后开始切分字段。
    # 余下第一个字段是第 3 个字段（state），starttime 是第 22 个字段。
    rest = text[text.rfind(")") + 1 :].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


def _cmdline_of(proc_root: Path, name: str) -> str:
    try:
        raw = (proc_root / name / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _exe_of(proc_root: Path, name: str) -> str | None:
    try:
        target = os.readlink(proc_root / name / "exe")
    except OSError:
        return None
    suffix = " (deleted)"
    if target.endswith(suffix):
        target = target[: -len(suffix)]
    return target


_UPDATER_MARK = "pid-update-loop"


def _in_codex_location(exe: str, codex_dir: str, home_codex: Path | None) -> bool:
    path = Path(os.path.normpath(exe))
    if os.path.dirname(os.path.normpath(exe)) == codex_dir:
        return True
    if home_codex is None:
        return False
    try:
        path.relative_to(home_codex)
    except ValueError:
        return False
    return True


def list_codex_procs(
    uid: int,
    codex_dir: Path,
    proc_root: Path = Path("/proc"),
    *,
    home: Path | None = None,
) -> list[CodexProc]:
    """列出属于 uid 的 codex 进程。

    可执行文件需位于 codex_dir 下，或（给了 home 时）位于该用户的 ~/.codex 下
    （Codex App 自行安装的版本）。只做升级的 pid-update-loop 监督进程不计入。

    /proc 不可读（如非 Linux 环境）时返回空列表，调用方静默降级。
    """
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    btime = _boot_time(proc_root)
    if btime is None:
        return []
    wanted = os.path.normpath(codex_dir)
    home_codex = Path(os.path.normpath(Path(home) / ".codex")) if home is not None else None
    ticks_per_second = _clock_ticks()
    found: list[CodexProc] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        if _uid_of(proc_root, entry.name) != uid:
            continue
        if _UPDATER_MARK in _cmdline_of(proc_root, entry.name):
            continue
        exe = _exe_of(proc_root, entry.name)
        if exe is None or not _in_codex_location(exe, wanted, home_codex):
            continue
        start_ticks = _start_ticks(proc_root, entry.name)
        if start_ticks is None:
            continue
        found.append(
            CodexProc(pid=int(entry.name), exe=exe, started_at=btime + start_ticks // ticks_per_second)
        )
    return sorted(found, key=lambda p: (p.started_at, p.pid))


def stale_procs(procs: Iterable[CodexProc], changed_at: int | None) -> list[CodexProc]:
    """启动时间早于最近一次分配变化（或回收）的进程仍持有旧账号。"""
    if changed_at is None:
        return []
    return [p for p in procs if p.started_at < changed_at]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(
    pids: Iterable[int],
    *,
    grace: float = 5.0,
    interval: float = 0.2,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = _alive,
) -> tuple[list[int], list[int]]:
    """先 SIGTERM，最多等 grace 秒，仍存活则 SIGKILL。返回 (已结束, 无法结束)。"""
    ended: list[int] = []
    failed: list[int] = []
    pending: list[int] = []
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            ended.append(pid)
        except PermissionError:
            failed.append(pid)
        else:
            pending.append(pid)

    steps = 0 if interval <= 0 else max(int(grace / interval), 0)
    for _ in range(steps):
        if not pending:
            break
        sleep(interval)
        still: list[int] = []
        for pid in pending:
            try:
                running = alive(pid)
            except ProcessLookupError:
                running = False
            except PermissionError:
                running = True
            if running:
                still.append(pid)
            else:
                ended.append(pid)
        pending = still

    for pid in pending:
        try:
            kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            ended.append(pid)
        except PermissionError:
            failed.append(pid)
        else:
            ended.append(pid)

    return sorted(ended), sorted(failed)
