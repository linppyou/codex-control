from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from . import logscan, quota, refresher, watcher
from .context import Context

log = logging.getLogger(__name__)

# (任务名, 函数, 间隔秒数)。单线程执行器保证任务串行，同一时刻只有一个刷新在进行。
JOBS: list[tuple[str, Callable[[Context], object], int]] = [
    ("refresh", refresher.refresh_due, 600),
    ("quota", quota.poll_quotas, 60),
    ("logscan", logscan.scan_all, 60),
    ("watch", watcher.run_checks, 300),
]


def safe_job(name: str, fn: Callable[[Context], object], ctx: Context) -> Callable[[], None]:
    def job() -> None:
        try:
            fn(ctx)
        except Exception:
            log.exception("定时任务 %s 失败", name)

    return job


def build_scheduler(ctx: Context) -> BlockingScheduler:
    sched = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )
    for name, fn, seconds in JOBS:
        sched.add_job(safe_job(name, fn, ctx), "interval", seconds=seconds, id=name, next_run_time=datetime.now())
    return sched
