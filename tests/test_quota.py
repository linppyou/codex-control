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
