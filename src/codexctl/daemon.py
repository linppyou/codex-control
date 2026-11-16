from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from . import logscan, quota, refresher, sites, watcher
from .context import Context
from .gateway import storage

log = logging.getLogger(__name__)

# (任务名, 函数, 间隔秒数)。单线程执行器保证任务串行，同一时刻只有一个刷新在进行。
JOBS: list[tuple[str, Callable[[Context], object], int]] = [
    ("refresh", refresher.refresh_due, 600),
    ("quota", quota.poll_quotas, 0),  # 0 表示取 settings.quota_poll_seconds
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
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
    )
    start = datetime.now()
    jobs = JOBS if ctx.settings.manages_local_codex else JOBS[:2]
    if ctx.settings.gateway_enabled:
        jobs = [*jobs, ("api-usage", storage.prune_usage, 86400)]
    for i, (name, fn, seconds) in enumerate(jobs):
        if name == "quota":
            seconds = ctx.settings.quota_poll_seconds
        sched.add_job(
            safe_job(name, fn, ctx),
            "interval",
            seconds=seconds,
            id=name,
            next_run_time=start + timedelta(seconds=5 * i),
        )
    sched.add_executor(ThreadPoolExecutor(1), alias="sites")
    sched.add_job(
        safe_job("sites", sites.refresh, ctx),
        "interval",
        seconds=sites.INTERVAL,
        id="sites",
        executor="sites",
        next_run_time=start,
    )
    return sched


def run(ctx: Context) -> None:
    sched = build_scheduler(ctx)
    if threading.current_thread() is threading.main_thread():

        def stop(signum, frame) -> None:
            sched.shutdown(wait=True)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    log.info("codexctl daemon 启动")
    sched.start()
