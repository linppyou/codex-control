import json
import os

import pytest

from codexctl import accounts, logscan, repo
from codexctl.authfile import format_rfc3339
from fakes import T0, make_auth_json


def event(ts, total, *, info=True, rate=None):
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": total - 10, "cached_input_tokens": 0, "output_tokens": 10,
                "reasoning_output_tokens": 0, "total_tokens": total,
            }
        } if info else None,
    }
    if rate is not None:
        payload["rate_limits"] = rate
    return json.dumps({"timestamp": format_rfc3339(ts), "type": "event_msg", "payload": payload}) + "\n"


@pytest.fixture
def setup(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    alice = ctx.test_users.add("alice")
    repo.start_assignment(ctx.conn, "alice", acc.id, T0)
    ctx.test_clock.t = T0 + 3600
    path = alice.home / ".codex" / "sessions" / "2027" / "01" / "15" / "rollout-x.jsonl"
    path.parent.mkdir(parents=True)
    return ctx, acc, path


def write(path, text, mode="a", mtime=T0 + 3600):
    with open(path, mode) as f:
        f.write(text)
    os.utime(path, (mtime, mtime))


def usage_rows(ctx):
    return [tuple(r) for r in ctx.conn.execute(
        "SELECT ts, account_pk, input_tokens, output_tokens, total_tokens FROM usage_events ORDER BY id")]


def test_parse_line_filters_and_parses():
    assert logscan.parse_line("not json") is None
    assert logscan.parse_line(json.dumps({"type": "response_item", "payload": {}})) is None
    tc = logscan.parse_line(event(T0, 100))
    assert tc.ts == T0 and tc.totals["total_tokens"] == 100 and tc.rate_limits is None
    assert logscan.parse_line(event(T0, 0, info=False)).totals is None


def test_scan_counts_deltas_and_is_idempotent(setup):
    ctx, acc, path = setup
    write(path, event(T0 + 10, 100) + event(T0 + 20, 250))
    assert logscan.scan_file(ctx, "alice", path) == 2
    assert usage_rows(ctx) == [(T0 + 10, acc.id, 90, 10, 100), (T0 + 20, acc.id, 150, 0, 150)]
    assert logscan.scan_file(ctx, "alice", path) == 0
    write(path, event(T0 + 30, 300))
    assert logscan.scan_file(ctx, "alice", path) == 1
    assert usage_rows(ctx)[-1] == (T0 + 30, acc.id, 50, 0, 50)


def test_partial_line_waits_for_newline(setup):
    ctx, _, path = setup
    line = event(T0 + 10, 100)
    write(path, line[:20])
    assert logscan.scan_file(ctx, "alice", path) == 0
    write(path, line[20:])
    assert logscan.scan_file(ctx, "alice", path) == 1


def test_rate_limit_only_event_keeps_baseline_and_records_quota(setup):
    ctx, acc, path = setup
    rate = {"primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": T0 + 9999}, "secondary": None}
    write(path, event(T0 + 10, 100) + event(T0 + 15, 0, info=False, rate=rate) + event(T0 + 20, 250))
    logscan.scan_file(ctx, "alice", path)
    assert [r[4] for r in usage_rows(ctx)] == [100, 150]
    rows = repo.latest_quota(ctx.conn, acc.id)
    assert [(r["win"], r["used_percent"], r["source"]) for r in rows] == [("primary", 17.0, "log")]


def test_truncated_file_is_rescanned_from_start(setup):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100) + event(T0 + 20, 250))
    logscan.scan_file(ctx, "alice", path)
    write(path, event(T0 + 30, 40), mode="w")
    assert logscan.scan_file(ctx, "alice", path) == 1
    assert usage_rows(ctx)[-1][4] == 40


def test_event_before_assignment_has_no_account(setup):
    ctx, _, path = setup
    write(path, event(T0 - 100, 100))
    logscan.scan_file(ctx, "alice", path)
    assert usage_rows(ctx)[0][1] is None


def test_session_files_filters_by_mtime_and_name(setup):
    ctx, _, path = setup
    write(path, "")
    old = path.parent / "rollout-old.jsonl"
    write(old, "", mtime=T0 - 30 * 86400)
    (path.parent / "notes.txt").write_text("x")
    home = ctx.test_users.get("alice").home
    assert logscan.session_files(home, since=T0 - 86400) == [path]


def test_scan_all_only_scans_assigned_users(setup):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100))
    bob = ctx.test_users.add("bob")
    bob_log = bob.home / ".codex" / "sessions" / "rollout-b.jsonl"
    bob_log.parent.mkdir(parents=True)
    write(bob_log, event(T0 + 10, 100))
    assert logscan.scan_all(ctx) == 1
    assert repo.usage_summary(ctx.conn, since=0)[0]["username"] == "alice"


def test_parse_line_rejects_bad_counters():
    good = json.loads(event(T0, 100))
    for bad in ("abc", True, -1, 2**53, 1.5, None):
        d = json.loads(event(T0, 100))
        d["payload"]["info"]["total_token_usage"]["input_tokens"] = bad
        assert logscan.parse_line(json.dumps(d)) is None, bad
    assert logscan.parse_line("[" * 100000) is None
    assert logscan.parse_line(json.dumps(good)) is not None


def test_future_and_ancient_events_ignored(setup):
    ctx, acc, path = setup
    rate = {"primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": None}}
    write(path, event(ctx.now() + 1000, 100, rate=rate) + event(ctx.now() - 20 * 86400, 200)
          + event(T0 + 10, 300))
    logscan.scan_file(ctx, "alice", path)
    assert [r[4] for r in usage_rows(ctx)] == [300]
    assert repo.latest_quota(ctx.conn, acc.id) == []


def test_fifo_does_not_block(setup):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100))
    os.mkfifo(path.parent / "rollout-f.jsonl")
    assert logscan.scan_all(ctx) == 1


def test_symlinked_sessions_dir_is_not_followed(setup):
    ctx, _, path = setup
    other = path.parent.parent.parent.parent.parent / "elsewhere"
    other.mkdir()
    write(other / "rollout-z.jsonl", event(T0 + 10, 100))
    sessions = path.parents[3]
    import shutil
    shutil.rmtree(sessions)
    sessions.symlink_to(other)
    assert logscan.scan_all(ctx) == 0


def test_endless_line_chunk_is_skipped(setup, monkeypatch):
    ctx, _, path = setup
    monkeypatch.setattr(logscan, "CHUNK_SIZE", 512)
    monkeypatch.setattr(logscan, "MAX_LINE", 32)
    write(path, "x" * 600 + "\n" + event(T0 + 10, 100))
    logscan.scan_file(ctx, "alice", path)
    assert [r[4] for r in usage_rows(ctx)] == [100]


def test_scan_all_survives_file_exception(setup, monkeypatch):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100))
    monkeypatch.setattr(logscan, "scan_file", lambda *a, **k: 1 / 0)
    assert logscan.scan_all(ctx) == 0
