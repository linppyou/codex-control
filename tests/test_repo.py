import pytest

from codexctl import pricing, repo


def _insert(ctx, label="acc1", account_id="acct-1"):
    return repo.insert_account(
        ctx.conn, label=label, email="a@example.com", account_id=account_id,
        plan_type="pro", last_refresh=1, access_expires_at=2, now=3,
    )


def test_insert_and_get(ctx):
    pk = _insert(ctx)
    acc = repo.get_account(ctx.conn, "acc1")
    assert acc.id == pk and acc.status == "active" and acc.retry_count == 0
    assert repo.get_account_by_pk(ctx.conn, pk) == acc
    assert repo.find_by_account_id(ctx.conn, "acct-1") == acc
    assert repo.get_account(ctx.conn, "nope") is None
    assert [a.label for a in repo.list_accounts(ctx.conn)] == ["acc1"]


def test_secret_versioning(ctx):
    pk = _insert(ctx)
    repo.store_secret(ctx.conn, pk, b"v1", 10)
    repo.store_secret(ctx.conn, pk, b"v2", 11)
    assert repo.load_secret(ctx.conn, pk) == b"v2"
    assert ctx.conn.execute("SELECT version FROM account_secrets").fetchone()[0] == 2


def test_load_secret_missing(ctx):
    with pytest.raises(LookupError):
        repo.load_secret(ctx.conn, 99)


def test_retry_then_token_update_resets(ctx):
    pk = _insert(ctx)
    repo.mark_retry(ctx.conn, pk, "boom", 500)
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.retry_count, acc.last_error, acc.next_retry_at) == (1, "boom", 500)
    repo.update_token_meta(ctx.conn, pk, email="b@x", plan_type="plus", last_refresh=7, access_expires_at=8)
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.retry_count, acc.last_error, acc.next_retry_at, acc.status) == (0, None, None, "active")
    assert (acc.email, acc.plan_type, acc.last_refresh, acc.access_expires_at) == ("b@x", "plus", 7, 8)


def test_set_status(ctx):
    pk = _insert(ctx)
    repo.set_status(ctx.conn, pk, "needs_relogin", "reused")
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.status, acc.last_error) == ("needs_relogin", "reused")


def test_usage_by_user_and_recent_lists(ctx):
    from codexctl import events

    d = {f: 1 for f in repo.USAGE_FIELDS}
    d["total_tokens"] = 5
    repo.insert_usage(ctx.conn, "alice", None, 100, d, "f")
    repo.insert_usage(ctx.conn, "alice", None, 200, d, "f")
    repo.insert_usage(ctx.conn, "bob", None, 50, d, "f")
    assert repo.usage_by_user(ctx.conn, 100) == {"alice": 10}
    assert repo.usage_by_user(ctx.conn, 0) == {"alice": 10, "bob": 5}
    assert repo.usage_by_user(ctx.conn, 999) == {}
    for i in range(3):
        events.audit(ctx.conn, "a", f"act{i}", "t", now=i)
        events.raise_alert(ctx.conn, "warning", f"k{i}", "t", "m", now=i)
    events.resolve_alert(ctx.conn, "k0", "t", now=9)
    assert [r["action"] for r in repo.recent_audit(ctx.conn, 2)] == ["act2", "act1"]
    alerts = repo.recent_alerts(ctx.conn, 10)
    assert [r["kind"] for r in alerts] == ["k2", "k1", "k0"]
    assert len(repo.recent_alerts(ctx.conn, 1)) == 1


def test_usage_by_user_model_windows_has_token_breakdown(ctx):
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=1000, cached_input_tokens=400, output_tokens=100, total_tokens=1100)
    repo.insert_usage(ctx.conn, "alice", None, 100, d, "f", "gpt-5.2")
    old = {f: 0 for f in repo.USAGE_FIELDS}
    old.update(input_tokens=50, cached_input_tokens=10, output_tokens=5, total_tokens=55)
    repo.insert_usage(ctx.conn, "alice", None, 10, old, "f", "gpt-5.2")
    [row] = repo.usage_by_user_model_windows(ctx.conn, today=50, week=0)
    assert (row["today"], row["week"], row["total"], row["turns"]) == (1100, 1155, 1155, 2)
    assert (row["today_input"], row["today_cached"], row["today_output"]) == (1000, 400, 100)
    assert (row["week_input"], row["week_cached"], row["week_output"]) == (1050, 410, 105)
    assert (row["total_input"], row["total_cached"], row["total_output"]) == (1050, 410, 105)


def test_model_price_crud(ctx):
    prices = repo.list_prices(ctx.conn)
    assert len(prices) == len(pricing.PRICES) and all(p.id for p in prices)
    assert repo.get_price(ctx.conn, prices[0].id) == prices[0]

    pid = repo.insert_price(ctx.conn, pricing.Price("Custom", 1.5, 0.15, 3.0, ("custom-id",)))
    got = repo.get_price(ctx.conn, pid)
    assert got is not None and (got.name, got.aliases) == ("Custom", ("custom-id",))

    changed = pricing.Price("Custom 2", 2.0, 0.2, 4.0, ("custom-2",))
    assert repo.update_price(ctx.conn, pid, changed)
    got = repo.get_price(ctx.conn, pid)
    assert (got.name, got.input_usd, got.aliases) == ("Custom 2", 2.0, ("custom-2",))
    assert repo.delete_price(ctx.conn, pid)
    assert repo.get_price(ctx.conn, pid) is None
    assert not repo.delete_price(ctx.conn, pid)


def test_last_assignment_change(ctx):
    pk = _insert(ctx)
    assert repo.last_assignment_change(ctx.conn, "alice") is None
    repo.start_assignment(ctx.conn, "alice", pk, 100)
    assert repo.last_assignment_change(ctx.conn, "alice") == 100
    repo.end_assignment(ctx.conn, "alice", 150)
    assert repo.last_assignment_change(ctx.conn, "alice") == 150
    repo.start_assignment(ctx.conn, "alice", pk, 200)
    # 回收后重新分配同一个账号不算变化
    assert repo.last_assignment_change(ctx.conn, "alice") == 100
    repo.end_assignment(ctx.conn, "alice", 250)
    assert repo.last_assignment_change(ctx.conn, "alice") == 250
    other = _insert(ctx, label="acc2", account_id="acct-2")
    repo.start_assignment(ctx.conn, "alice", other, 300)
    assert repo.last_assignment_change(ctx.conn, "alice") == 300
    assert repo.last_assignment_change(ctx.conn, "bob") is None
