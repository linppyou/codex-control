from codexctl.daemon import build_scheduler, safe_job


def test_scheduler_has_expected_jobs(ctx):
    sched = build_scheduler(ctx)
    jobs = {j.id: j.trigger.interval.total_seconds() for j in sched.get_jobs()}
    assert jobs == {"refresh": 600, "quota": 600, "logscan": 60, "watch": 300, "sites": 300}
    assert sched.get_job("sites").executor == "sites"


def test_quota_interval_follows_settings(ctx):
    import dataclasses

    ctx.settings = dataclasses.replace(ctx.settings, quota_poll_seconds=120)
    jobs = {j.id: j.trigger.interval.total_seconds() for j in build_scheduler(ctx).get_jobs()}
    assert jobs["quota"] == 120


def test_safe_job_swallows_and_logs_errors(caplog):
    def boom(ctx):
        raise RuntimeError("x")

    safe_job("boom", boom, None)()
    assert "定时任务 boom 失败" in caplog.text


def test_jobs_never_skip_and_are_staggered(ctx):
    sched = build_scheduler(ctx)
    assert sched._job_defaults["misfire_grace_time"] is None


def test_run_installs_signal_handlers(ctx, monkeypatch):
    import signal

    from apscheduler.schedulers.blocking import BlockingScheduler

    from codexctl import daemon

    got = []
    monkeypatch.setattr(signal, "signal", lambda s, h: got.append(s))
    monkeypatch.setattr(BlockingScheduler, "start", lambda self: None)
    daemon.run(ctx)
    assert set(got) == {signal.SIGTERM, signal.SIGINT}
