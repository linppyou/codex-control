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
