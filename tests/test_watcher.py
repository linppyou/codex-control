import pytest
from fakes import make_auth_json

import codexctl.accounts.service as accounts
import codexctl.events as events
import codexctl.repo as repo
import codexctl.usage.watcher as watcher
import codexctl.users.assignments as assignments
from codexctl.runner import CommandError

ACL_ALICE = "user::rw-\nuser:alice:r--\ngroup::---\nmask::r--\nother::---\n"


@pytest.fixture
def setup(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_users.add("alice")
    ctx.test_users.add("bob")
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.outputs["getfacl"] = ACL_ALICE
    return ctx


def kinds(findings):
    return [(f.kind, f.target) for f in findings]


def test_clean_state_has_no_findings(setup):
    assert watcher.run_checks(setup) == []


def test_reconciles_legacy_disabled_acl_and_reports_failure(setup, monkeypatch):
    ctx = setup
    acc = repo.get_account(ctx.conn, "acc1")
    repo.set_status(ctx.conn, acc.id, "disabled")

    def fail(label):
        raise CommandError("setfacl failed")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.publisher, "revoke_readers", fail)
        assert kinds(watcher.run_checks(ctx, reconcile_disabled=True)) == [("acl_mismatch", "acc1")]
    assert kinds(watcher.run_checks(ctx)) == [("acl_mismatch", "acc1")]
    assert watcher.run_checks(ctx, reconcile_disabled=True) == []
    assert ctx.test_runner.calls[-1][2] == "u::rw-,g::---,o::---"
    assert not events.open_alerts(ctx.conn)


def test_deleted_link_detected_and_resolved_after_fix(setup):
    ctx = setup
    (ctx.test_users.get("alice").home / ".codex" / "auth.json").unlink()
    assert kinds(watcher.run_checks(ctx)) == [("link_mismatch", "alice")]
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["link_mismatch"]
    assignments.reconcile_user(ctx, "alice", "t")
    assert watcher.run_checks(ctx) == []
    assert events.open_alerts(ctx.conn) == []


def test_link_replaced_by_regular_file(setup):
    ctx = setup
    link = ctx.test_users.get("alice").home / ".codex" / "auth.json"
    link.unlink()
    link.write_text("{}")
    assert kinds(watcher.run_checks(ctx)) == [("link_mismatch", "alice")]


def test_stale_link_for_unassigned_user(setup):
    ctx = setup
    ctx.publisher.link_user(ctx.test_users.get("bob"), "acc1", now=1)
    assert kinds(watcher.run_checks(ctx)) == [("stale_link", "bob")]


def test_acl_mismatch(setup):
    ctx = setup
    ctx.test_runner.outputs["getfacl"] = "user::rw-\ngroup::---\nother::---\n"
    assert kinds(watcher.run_checks(ctx)) == [("acl_mismatch", "acc1")]


def test_published_file_mismatch(setup):
    ctx = setup
    ctx.publisher.account_file("acc1").write_bytes(b"{}")
    assert kinds(watcher.run_checks(ctx)) == [("published_mismatch", "acc1")]


def test_token_expiring(setup):
    ctx = setup
    acc = repo.get_account(ctx.conn, "acc1")
    ctx.test_clock.t = acc.access_expires_at - 3600
    findings = watcher.run_checks(ctx)
    assert [(f.kind, f.level) for f in findings] == [("token_expiring", "critical")]


def test_dotdot_target_not_flagged_as_stale(setup):
    ctx = setup
    link = ctx.test_users.get("bob").home / ".codex"
    link.mkdir(parents=True, exist_ok=True)
    (link / "auth.json").symlink_to(str(ctx.publisher.accounts_dir) + "/../x")
    assert watcher.run_checks(ctx) == []


def test_vault_unreadable_is_finding_not_crash(setup, monkeypatch):
    ctx = setup

    def boom(c, acc):
        raise LookupError("x")

    monkeypatch.setattr(accounts, "load_bundle", boom)
    findings = watcher.run_checks(ctx)
    assert [(f.kind, f.level, f.message) for f in findings] == [
        ("published_mismatch", "critical", "无法读取保险库：LookupError")
    ]


def test_unassigned_user_link_alert_resolved(setup):
    ctx = setup
    (ctx.test_users.get("alice").home / ".codex" / "auth.json").unlink()
    watcher.run_checks(ctx)
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["link_mismatch"]
    assignments.unassign(ctx, "alice", "t")
    ctx.test_runner.outputs["getfacl"] = "user::rw-\ngroup::---\nother::---\n"
    watcher.run_checks(ctx)
    assert events.open_alerts(ctx.conn) == []


def test_alert_for_vanished_account_resolved_and_others_untouched(setup):
    ctx = setup
    events.raise_alert(ctx.conn, "critical", "acl_mismatch", "gone", "x", now=1)
    events.raise_alert(ctx.conn, "warning", "quota_high", "acc1", "x", now=1)
    watcher.run_checks(ctx)
    assert [(a["kind"], a["target"]) for a in events.open_alerts(ctx.conn)] == [("quota_high", "acc1")]
