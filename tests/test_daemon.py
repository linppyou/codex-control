from codexctl.daemon import build_scheduler, safe_job


def test_scheduler_has_expected_jobs(ctx):
    sched = build_scheduler(ctx)
    jobs = {j.id: j.trigger.interval.total_seconds() for j in sched.get_jobs()}
    assert jobs == {"refresh": 600, "quota": 60, "logscan": 60, "watch": 300}


def test_safe_job_swallows_and_logs_errors(caplog):
    def boom(ctx):
        raise RuntimeError("x")

    safe_job("boom", boom, None)()
    assert "定时任务 boom 失败" in caplog.text
