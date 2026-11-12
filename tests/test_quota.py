import httpx

from codexctl import accounts, events, quota, repo
from codexctl.quota import QuotaWindow
from fakes import T0, make_auth_json

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
BODY = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {
            "used_percent": 42, "limit_window_seconds": 18000,
            "reset_after_seconds": 600, "reset_at": T0 + 600,
        },
        "secondary_window": {
            "used_percent": 96, "limit_window_seconds": 604800, "reset_after_seconds": 86400,
        },
    },
}


def test_parse_usage():
    assert quota.parse_usage(BODY, now=T0) == [
        QuotaWindow("primary", 42.0, 300, T0 + 600),
        QuotaWindow("secondary", 96.0, 10080, T0 + 86400),
    ]


def test_parse_usage_tolerates_missing_parts():
    assert quota.parse_usage({}, now=0) == []
    assert quota.parse_usage({"rate_limit": {"primary_window": None}}, now=0) == []


def test_windows_from_session_log_rate_limits():
    rl = {"primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1794920554}, "secondary": None}
    assert quota.windows_from_rate_limits(rl) == [QuotaWindow("primary", 17.0, 10080, 1794920554)]


def test_poll_quotas_records_and_raises_critical_alert(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["account"] = request.headers["ChatGPT-Account-Id"]
        return httpx.Response(200, json=BODY)

    ctx.test_routes[("GET", USAGE_URL)] = handler
    quota.poll_quotas(ctx)
    assert seen["auth"].startswith("Bearer ") and seen["account"] == "acct-1"
    rows = repo.latest_quota(ctx.conn, acc.id)
    assert {r["win"]: (r["used_percent"], r["source"]) for r in rows} == {
        "primary": (42.0, "api"), "secondary": (96.0, "api"),
    }
    assert [(a["kind"], a["level"]) for a in events.open_alerts(ctx.conn)] == [("quota_high", "critical")]


def test_poll_quotas_survives_http_error(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(500)
    quota.poll_quotas(ctx)
    assert repo.latest_quota(ctx.conn, acc.id) == []


def test_poll_quotas_skips_disabled_accounts(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.set_disabled(ctx, "acc1", True, "t")
    called = []
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: called.append(1) or httpx.Response(200, json=BODY)
    quota.poll_quotas(ctx)
    assert called == []


def test_threshold_levels_and_resolution(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    quota.record(ctx, acc.id, T0, [QuotaWindow("primary", 85.0, 300, None)], "api")
    quota.evaluate_thresholds(ctx, acc)
    assert [a["level"] for a in events.open_alerts(ctx.conn)] == ["warning"]
    quota.record(ctx, acc.id, T0 + 60, [QuotaWindow("primary", 10.0, 300, None)], "api")
    quota.evaluate_thresholds(ctx, acc)
    assert events.open_alerts(ctx.conn) == []


def test_odd_values_are_skipped_or_nulled():
    for bad in (float("nan"), "x", 150, -1, True, None, float("inf")):
        rl = {"primary": {"used_percent": bad, "window_minutes": 300}}
        assert quota.windows_from_rate_limits(rl) == []
        body = {"rate_limit": {"primary_window": {"used_percent": bad}}}
        assert quota.parse_usage(body, now=0) == []
    rl = {"primary": {"used_percent": 5, "window_minutes": "x", "resets_at": -3}}
    assert quota.windows_from_rate_limits(rl) == [QuotaWindow("primary", 5.0, None, None)]
    body = {"rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": [1],
                                              "reset_at": True, "reset_after_seconds": "9"}}}
    assert quota.parse_usage(body, now=0) == [QuotaWindow("primary", 5.0, None, None)]
    assert quota.parse_usage({"rate_limit": []}, now=0) == []


def test_fresh_api_sample_beats_newer_log_sample(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_clock.t = T0 + 100
    quota.record(ctx, acc.id, T0 + 10, [QuotaWindow("primary", 96.0, 300, None)], "api")
    quota.record(ctx, acc.id, T0 + 50, [QuotaWindow("primary", 0.0, 300, None)], "log")
    quota.evaluate_thresholds(ctx, acc)
    assert [a["level"] for a in events.open_alerts(ctx.conn)] == ["critical"]


def test_fresh_api_response_does_not_mix_in_other_log_windows(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    quota.record(ctx, acc.id, T0, [QuotaWindow("secondary", 37, 10080, T0 + 400)], "log")
    quota.record(ctx, acc.id, T0 + 10, [QuotaWindow("primary", 54, 10080, T0 + 600)], "api")
    rows = repo.latest_quota(ctx.conn, acc.id, api_fresh_since=T0)
    assert [(r["win"], r["used_percent"], r["source"]) for r in rows] == [("primary", 54, "api")]


def test_stale_api_falls_back_to_log(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    quota.record(ctx, acc.id, T0, [QuotaWindow("primary", 54, 10080, None)], "api")
    quota.record(ctx, acc.id, T0 + 10, [QuotaWindow("primary", 37, 10080, None)], "log")
    rows = repo.latest_quota(ctx.conn, acc.id, api_fresh_since=T0 + 1)
    assert [(r["used_percent"], r["source"]) for r in rows] == [(37, "log")]


def test_poll_quotas_continues_after_account_exception(ctx, monkeypatch):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.import_account(ctx, "acc2", make_auth_json(account_id="acct-2"), "t")
    calls = []

    def fake_fetch(c, acc):
        calls.append(acc.label)
        if acc.label == "acc1":
            raise RuntimeError("boom")
        return [QuotaWindow("primary", 1.0, 300, None)]

    monkeypatch.setattr(quota, "fetch_usage", fake_fetch)
    quota.poll_quotas(ctx)
    assert calls == ["acc1", "acc2"]


def test_huge_integers_are_dropped_not_overflowing():
    rl = {"primary": {"used_percent": 10.0, "window_minutes": 2**70, "resets_at": 2**64}}
    assert quota.windows_from_rate_limits(rl) == [QuotaWindow("primary", 10.0, None, None)]


def test_poll_quotas_auth_rejected_alert_and_resolution(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(401)
    quota.poll_quotas(ctx)
    alerts = events.open_alerts(ctx.conn)
    assert [(a["kind"], a["level"], a["target"]) for a in alerts] == [("auth_rejected", "critical", "acc1")]
    assert "HTTP 401" in alerts[0]["message"] and "relogin" in alerts[0]["message"]
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json={})
    quota.poll_quotas(ctx)
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == []


def test_poll_quotas_403_also_auth_rejected(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(403)
    quota.poll_quotas(ctx)
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["auth_rejected"]


def test_poll_account_success_failure_and_401(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json=BODY)
    assert quota.poll_account(ctx, acc) is None
    assert len(repo.quota_history(ctx.conn, acc.id, 0)) == 1
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(500)
    assert "500" in quota.poll_account(ctx, acc)
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(401)
    assert "401" in quota.poll_account(ctx, acc)
    assert "auth_rejected" in [a["kind"] for a in events.open_alerts(ctx.conn)]
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: (_ for _ in ()).throw(RuntimeError("boom"))
    assert "boom" in quota.poll_account(ctx, acc)


def test_poll_quotas_prunes_old_samples(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    old = ctx.now() - 91 * 86400
    quota.record(ctx, acc.id, old, [QuotaWindow("primary", 1.0, 300, None)], "api")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(200, json=BODY)
    quota.poll_quotas(ctx)
    assert [h["ts"] for h in repo.quota_history(ctx.conn, acc.id, 0)] == [ctx.now()]


def test_quota_history_grouping_order_since_source(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    quota.record(ctx, acc.id, T0, [QuotaWindow("primary", 10.0, 300, T0 + 5), QuotaWindow("secondary", 20.0, 10080, None)], "api")
    quota.record(ctx, acc.id, T0 + 600, [QuotaWindow("primary", 11.0, 300, None)], "api")
    quota.record(ctx, acc.id, T0 + 700, [QuotaWindow("primary", 99.0, 300, None)], "log")
    h = repo.quota_history(ctx.conn, acc.id, 0)
    assert [x["ts"] for x in h] == [T0 + 600, T0]
    assert h[0] == {"ts": T0 + 600, "primary": {"used_percent": 11.0, "window_minutes": 300, "resets_at": None}, "secondary": None}
    assert h[1]["secondary"] == {"used_percent": 20.0, "window_minutes": 10080, "resets_at": None}
    assert [x["ts"] for x in repo.quota_history(ctx.conn, acc.id, T0 + 1)] == [T0 + 600]
    assert [x["ts"] for x in repo.quota_history(ctx.conn, acc.id, 0, source="log")] == [T0 + 700]
