import dataclasses
import json

import httpx
import pytest

from codexctl import accounts, events, refresher, repo
from codexctl.authfile import AuthBundle, format_rfc3339
from fakes import T0, make_auth_json, make_jwt

TOKEN_URL = "https://auth.openai.com/oauth/token"


def ok_body(ctx, refresh_token="rt-2"):
    return {
        "access_token": make_jwt({"client_id": "app_test", "exp": ctx.test_clock.t + 10 * 86400}),
        "refresh_token": refresh_token,
        "id_token": make_jwt({
            "email": "a@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "pro"},
        }),
    }


def test_needs_refresh_rules(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    s = ctx.settings
    assert not refresher.needs_refresh(acc, T0 + 86400, s)
    assert not refresher.needs_refresh(
        dataclasses.replace(acc, access_expires_at=T0 + 1800), T0 + 1800, s
    )
    assert refresher.needs_refresh(acc, T0 + 4 * 86400, s)
    assert refresher.needs_refresh(dataclasses.replace(acc, access_expires_at=T0 + 2 * 86400), T0 + 3600, s)
    assert not refresher.needs_refresh(dataclasses.replace(acc, status="disabled"), T0 + 5 * 86400, s)
    assert not refresher.needs_refresh(dataclasses.replace(acc, status="needs_relogin"), T0 + 5 * 86400, s)
    assert not refresher.needs_refresh(
        dataclasses.replace(acc, next_retry_at=T0 + 5 * 86400 + 10), T0 + 5 * 86400, s
    )


def test_refresh_success_rotates_and_publishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body(ctx))

    ctx.test_routes[("POST", TOKEN_URL)] = handler
    ctx.test_clock.t += 5 * 86400
    assert refresher.refresh_account(ctx, acc, "t") is True
    assert seen == {
        "client_id": "app_test", "grant_type": "refresh_token",
        "refresh_token": "rt-1", "scope": "openid profile email",
    }
    updated = repo.get_account(ctx.conn, "acc1")
    assert updated.last_refresh == ctx.test_clock.t
    assert updated.access_expires_at == ctx.test_clock.t + 10 * 86400
    assert accounts.load_bundle(ctx, updated).refresh_token == "rt-2"
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-2"
    assert published.last_refresh == ctx.test_clock.t


def test_refresh_server_error_schedules_backoff(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(503)
    ctx.test_clock.t += 5 * 86400
    assert refresher.refresh_account(ctx, acc, "t") is False
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.status, acc.retry_count, acc.next_retry_at) == ("active", 1, ctx.test_clock.t + 60)
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["refresh_retry"]
    ctx.test_clock.t = acc.next_retry_at
    refresher.refresh_account(ctx, acc, "t")
    assert repo.get_account(ctx.conn, "acc1").next_retry_at == ctx.test_clock.t + 300


def test_refresh_reused_token_needs_relogin(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(
        401, json={"error": {"code": "refresh_token_reused", "message": "already used"}}
    )
    assert refresher.refresh_account(ctx, acc, "t") is False
    acc = repo.get_account(ctx.conn, "acc1")
    assert acc.status == "needs_relogin"
    assert "refresh_token_reused" in acc.last_error
    alerts = events.open_alerts(ctx.conn)
    assert [(a["kind"], a["level"]) for a in alerts] == [("refresh_failed", "critical")]
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-1"


def test_refresh_success_clears_previous_alerts(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(503)
    refresher.refresh_account(ctx, acc, "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(200, json=ok_body(ctx))
    assert refresher.refresh_account(ctx, repo.get_account(ctx.conn, "acc1"), "t") is True
    assert events.open_alerts(ctx.conn) == []
    assert repo.get_account(ctx.conn, "acc1").retry_count == 0


def test_refresh_due_only_refreshes_due_accounts(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.import_account(
        ctx, "acc2", make_auth_json(account_id="a2", last_refresh=format_rfc3339(T0 - 5 * 86400)), "t"
    )
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(200, json=ok_body(ctx))
    assert refresher.refresh_due(ctx) == ["acc2"]


def _route(ctx, handler):
    ctx.test_routes[("POST", TOKEN_URL)] = handler


def test_skips_http_when_row_changed_since_snapshot(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.conn.execute("UPDATE accounts SET last_refresh = last_refresh + 100 WHERE id = ?", (acc.id,))

    def handler(request):
        raise AssertionError("不应发起请求")

    _route(ctx, handler)
    assert refresher.refresh_account(ctx, acc, "t") is True


def test_nonblocking_lock_busy(ctx):
    from codexctl.locks import LockBusy, account_lock

    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    with account_lock(ctx.settings.state_dir, "acc1"):
        with pytest.raises(LockBusy):
            refresher.refresh_account(ctx, acc, "t", blocking=False)


def test_malformed_200_after_rotation_needs_relogin(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    _route(ctx, lambda r: httpx.Response(200, json={"access_token": "not-a-jwt", "refresh_token": "rt-2"}))
    assert refresher.refresh_account(ctx, acc, "t") is False
    assert repo.get_account(ctx.conn, "acc1").status == "needs_relogin"
    assert [(a["kind"], a["level"]) for a in events.open_alerts(ctx.conn)] == [("refresh_failed", "critical")]
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-1"


def test_publish_failure_alerts_then_resolves(ctx, monkeypatch):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    _route(ctx, lambda r: httpx.Response(200, json=ok_body(ctx)))
    real = accounts.publish_account

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(accounts, "publish_account", boom)
    assert refresher.refresh_account(ctx, acc, "t") is True
    assert [(a["kind"], a["level"]) for a in events.open_alerts(ctx.conn)] == [("publish_failed", "critical")]
    monkeypatch.setattr(accounts, "publish_account", real)
    assert refresher.refresh_account(ctx, repo.get_account(ctx.conn, "acc1"), "t") is True
    assert events.open_alerts(ctx.conn) == []


def test_refresh_due_continues_after_exception(ctx, monkeypatch):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.import_account(
        ctx, "acc2", make_auth_json(account_id="a2", last_refresh=format_rfc3339(T0 - 5 * 86400)), "t"
    )
    accounts.import_account(
        ctx, "acc3", make_auth_json(account_id="a3", last_refresh=format_rfc3339(T0 - 5 * 86400)), "t"
    )
    _route(ctx, lambda r: httpx.Response(200, json=ok_body(ctx)))
    real = refresher.refresh_account

    def flaky(c, acc, *a, **k):
        if acc.label == "acc2":
            raise RuntimeError("x")
        return real(c, acc, *a, **k)

    monkeypatch.setattr(refresher, "refresh_account", flaky)
    assert refresher.refresh_due(ctx) == ["acc3"]


def test_retry_escalates_near_expiry(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    _route(ctx, lambda r: httpx.Response(503))
    ctx.test_clock.t = acc.access_expires_at - 3600
    refresher.refresh_account(ctx, acc, "t")
    alerts = events.open_alerts(ctx.conn)
    assert [(a["kind"], a["level"]) for a in alerts] == [("refresh_retry", "critical")]
    assert "24 小时" in alerts[0]["message"]


def test_error_message_not_leaking_body(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    secret = "eyJhbGciOi.eyJzdWIi.sig"
    _route(ctx, lambda r: httpx.Response(401, json={"error": {"code": "invalid_grant", "message": secret}}))
    refresher.refresh_account(ctx, acc, "t")
    got = repo.get_account(ctx.conn, "acc1")
    assert got.status == "needs_relogin"
    assert secret not in got.last_error
    assert all(secret not in a["message"] for a in events.open_alerts(ctx.conn))


@pytest.mark.parametrize(
    "resp",
    [httpx.Response(408), httpx.Response(403, text="<html>blocked</html>", headers={"content-type": "text/html"})],
)
def test_ambiguous_4xx_is_retryable(ctx, resp):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    _route(ctx, lambda r: resp)
    refresher.refresh_account(ctx, acc, "t")
    got = repo.get_account(ctx.conn, "acc1")
    assert (got.status, got.retry_count) == ("active", 1)


def test_failure_keeps_disabled(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    _route(ctx, lambda r: httpx.Response(401, json={"error": "invalid_grant"}))
    ctx.conn.execute("UPDATE accounts SET status='disabled' WHERE id=?", (acc.id,))
    ctx.conn.commit()
    refresher.refresh_account(ctx, acc, "t")
    assert repo.get_account(ctx.conn, "acc1").status == "disabled"
