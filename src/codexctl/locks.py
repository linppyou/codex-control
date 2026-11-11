from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusy(Exception):
    pass


@contextmanager
def account_lock(state_dir: Path, label: str, *, blocking: bool = True) -> Iterator[None]:
    """跨进程的账号级互斥锁（刷新与重新登录共用）。"""
    lock_dir = Path(state_dir) / "locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"refresh-{label}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as e:
            raise LockBusy(label) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
