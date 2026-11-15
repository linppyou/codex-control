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


def test_user_recent_sessions_and_breakdown(ctx):
    pk1 = _insert(ctx, label="acc1", account_id="a1")
    pk2 = _insert(ctx, label="acc2", account_id="a2")

    d1 = {f: 0 for f in repo.USAGE_FIELDS}
    d1.update(input_tokens=100, output_tokens=20, total_tokens=120)
    d2 = {f: 0 for f in repo.USAGE_FIELDS}
    d2.update(input_tokens=200, output_tokens=30, total_tokens=230)

    # alice session 1: 2 turns (ts=100, ts=200)
    repo.insert_usage(ctx.conn, "alice", pk1, 100, d1, "file1.jsonl", "gpt-4o")
    repo.insert_usage(ctx.conn, "alice", pk1, 200, d2, "file1.jsonl", "gpt-4o")

    # alice session 2: 1 turn (ts=300) on acc2, o1
    repo.insert_usage(ctx.conn, "alice", pk2, 300, d1, "file2.jsonl", "o1")

    # bob session (different user)
    repo.insert_usage(ctx.conn, "bob", pk1, 400, d1, "file_bob.jsonl", "gpt-4o")

    assert repo.user_session_count(ctx.conn, "alice") == 2
    assert repo.user_session_count(ctx.conn, "bob") == 1
    assert repo.user_session_count(ctx.conn, "charlie") == 0

    sessions = repo.user_recent_sessions(ctx.conn, "alice", limit=10)
    assert len(sessions) == 2
    # Ordered by last_active_at DESC -> file2 (ts=300) first, then file1 (ts=200)
    assert sessions[0]["session_file"] == "file2.jsonl"
    assert sessions[0]["started_at"] == 300
    assert sessions[0]["last_active_at"] == 300
    assert sessions[0]["turns"] == 1
    assert sessions[0]["total_tokens"] == 120
    assert sessions[0]["accounts"] == "acc2"
    assert sessions[0]["models"] == "o1"

    assert sessions[1]["session_file"] == "file1.jsonl"
    assert sessions[1]["started_at"] == 100
    assert sessions[1]["last_active_at"] == 200
    assert sessions[1]["turns"] == 2
    assert sessions[1]["total_tokens"] == 350
    assert sessions[1]["input_tokens"] == 300
    assert sessions[1]["output_tokens"] == 50
    assert sessions[1]["accounts"] == "acc1"
    assert sessions[1]["models"] == "gpt-4o"

    # Breakdown by session files
    breakdown = repo.user_sessions_model_breakdown(ctx.conn, "alice", ["file1.jsonl", "file2.jsonl"])
    assert len(breakdown) == 2
    by_file = {r["session_file"]: r for r in breakdown}
    assert by_file["file1.jsonl"]["model"] == "gpt-4o"
    assert by_file["file1.jsonl"]["total"] == 350
    assert by_file["file2.jsonl"]["model"] == "o1"
    assert by_file["file2.jsonl"]["total"] == 120

    # usage_by_model_for_user
    rows = repo.usage_by_model_for_user(ctx.conn, "alice", today=150, week=50)
    assert len(rows) == 2
    by_model = {r["model"]: r for r in rows}
    assert by_model["o1"]["today"] == 120
    assert by_model["o1"]["week"] == 120
    assert by_model["o1"]["total"] == 120
    assert by_model["gpt-4o"]["today"] == 230  # only ts=200 is >= 150
    assert by_model["gpt-4o"]["week"] == 350
    assert by_model["gpt-4o"]["total"] == 350


def test_daily_usage_and_session_filters(ctx):
    import time

    pk1 = _insert(ctx, label="daily1", account_id="daily-a")
    pk2 = _insert(ctx, label="daily2", account_id="daily-b")
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=100, cached_input_tokens=20, output_tokens=30, total_tokens=130)
    start = int(time.mktime((2026, 1, 2, 0, 0, 0, 0, 0, -1)))
    end = int(time.mktime((2026, 1, 3, 0, 0, 0, 0, 0, -1)))
    repo.insert_usage(ctx.conn, "alice", pk1, start, d, "one", "model-a")
    repo.insert_usage(ctx.conn, "alice", pk2, start + 60, d, "one", "model-b")
    repo.insert_usage(ctx.conn, "alice", pk2, end, d, "two", "model-b")
    repo.insert_usage(ctx.conn, "bob", pk1, start, d, "other", "model-a")
    rows = repo.user_daily_usage(ctx.conn, "alice", start, end)
    assert len(rows) == 1 and rows[0]["day"] == "2026-01-02"
    assert (rows[0]["total"], rows[0]["cached"], rows[0]["turns"]) == (260, 40, 2)
    assert repo.user_session_count(ctx.conn, "alice", "model-a", "daily2") == 1
    sessions = repo.user_recent_sessions(ctx.conn, "alice", model="model-a", account="daily2")
    assert len(sessions) == 1 and sessions[0]["total_tokens"] == 260
    assert repo.user_session_count(ctx.conn, "alice", "model-a", "daily1") == 1
    assert repo.user_recent_sessions(ctx.conn, "alice", limit=1, offset=1)[0]["session_file"] == "one"


def test_usage_daily_by_user_model_respects_range_and_dimensions(ctx):
    d = {f: 0 for f in repo.USAGE_FIELDS}
    d.update(input_tokens=100, cached_input_tokens=25, output_tokens=20, total_tokens=120)
    repo.insert_usage(ctx.conn, "alice", None, 100, d, "a", "model-a")
    repo.insert_usage(ctx.conn, "bob", None, 101, d, "b", "model-a")
    repo.insert_usage(ctx.conn, "alice", None, 102, d, "a", "model-b")
    repo.insert_usage(ctx.conn, "alice", None, 103, d, "a", "model-a")
    rows = repo.usage_daily_by_user_model(ctx.conn, 100, 103)
    assert len(rows) == 3
    assert sum(r["total"] for r in rows) == 360
    assert sum(r["cached"] for r in rows) == 75
    assert {r["username"] for r in rows} == {"alice", "bob"}
    assert {r["model"] for r in rows} == {"model-a", "model-b"}
