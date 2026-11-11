import os

import pytest

from codexctl import accounts, assignments, repo
from codexctl.sysusers import UserError
from fakes import make_auth_json

BASE_ACL = "u::rw-,g::---,o::---"


@pytest.fixture
def setup(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(account_id="a1"), "t")
    accounts.import_account(ctx, "acc2", make_auth_json(account_id="a2"), "t")
    ctx.test_users.add("alice")
    ctx.test_users.add("bob")
    ctx.test_runner.calls.clear()
    return ctx


def link_of(ctx, name):
    return ctx.test_users.get(name).home / ".codex" / "auth.json"


def test_assign_links_user_and_grants_acl(setup):
    ctx = setup
    assert assignments.assign(ctx, "alice", "acc1", "t") is None
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc1"))
    assert ctx.test_runner.calls == [
        ["setfacl", "--set", BASE_ACL + ",u:alice:r--,m::r--", str(ctx.publisher.account_file("acc1"))]
    ]
    assert repo.active_assignment(ctx.conn, "alice").account_pk == repo.get_account(ctx.conn, "acc1").id


def test_shared_account_lists_all_readers(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    assignments.assign(ctx, "bob", "acc1", "t")
    assert ctx.test_runner.calls[-1][2] == BASE_ACL + ",u:alice:r--,u:bob:r--,m::r--"


def test_switch_links_new_then_revokes_old(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()
    ctx.test_clock.t += 60
    assignments.assign(ctx, "alice", "acc2", "t")
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc2"))
    calls = ctx.test_runner.calls
    assert [c[-1] for c in calls] == [str(ctx.publisher.account_file("acc2")), str(ctx.publisher.account_file("acc1"))]
    assert calls[1][2] == BASE_ACL
    rows = ctx.conn.execute("SELECT ended_at FROM assignments WHERE username='alice' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [ctx.test_clock.t, None]


def test_assign_same_account_is_idempotent(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    assignments.assign(ctx, "alice", "acc1", "t")
    assert ctx.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1


def test_assign_backs_up_existing_file(setup):
    ctx = setup
    codex_dir = ctx.test_users.get("alice").home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("mine")
    backup = assignments.assign(ctx, "alice", "acc1", "t")
    assert backup.read_text() == "mine"


def test_unassign_removes_link_and_acl(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()
    assert assignments.unassign(ctx, "alice", "t") is True
    assert not os.path.lexists(link_of(ctx, "alice"))
    assert ctx.test_runner.calls == [["setfacl", "--set", BASE_ACL, str(ctx.publisher.account_file("acc1"))]]
    assert repo.active_assignment(ctx.conn, "alice") is None
    assert assignments.unassign(ctx, "alice", "t") is False


def test_assign_rejects_disabled_account(setup):
    ctx = setup
    accounts.set_disabled(ctx, "acc1", True, "t")
    with pytest.raises(assignments.AssignError, match="已停用"):
        assignments.assign(ctx, "alice", "acc1", "t")


def test_assign_rejects_unknown_account(setup):
    with pytest.raises(assignments.AssignError, match="账号不存在"):
        assignments.assign(setup, "alice", "nope", "t")


def test_assign_rejects_unknown_user(setup):
    with pytest.raises(UserError):
        assignments.assign(setup, "mallory", "acc1", "t")


def test_reconcile_restores_deleted_link(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    link_of(ctx, "alice").unlink()
    assignments.reconcile_user(ctx, "alice", "t")
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc1"))


def test_reconcile_removes_stale_link(setup):
    ctx = setup
    ctx.publisher.link_user(ctx.test_users.get("bob"), "acc1", now=1)
    assignments.reconcile_user(ctx, "bob", "t")
    assert not os.path.lexists(link_of(ctx, "bob"))


def test_unassign_revokes_acl_even_if_unlink_raises(setup, monkeypatch):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()

    def boom(user):
        raise RuntimeError("unlink failed")

    monkeypatch.setattr(ctx.publisher, "unlink_user", boom)
    with pytest.raises(RuntimeError):
        assignments.unassign(ctx, "alice", "t")
    assert ctx.test_runner.calls == [["setfacl", "--set", BASE_ACL, str(ctx.publisher.account_file("acc1"))]]
    assert repo.active_assignment(ctx.conn, "alice") is None


def test_switch_revokes_old_acl_even_if_link_raises(setup, monkeypatch):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()

    def boom(user, label, now):
        raise RuntimeError("link failed")

    monkeypatch.setattr(ctx.publisher, "link_user", boom)
    with pytest.raises(RuntimeError, match="link failed"):
        assignments.assign(ctx, "alice", "acc2", "t")
    calls = ctx.test_runner.calls
    assert calls[-1] == ["setfacl", "--set", BASE_ACL, str(ctx.publisher.account_file("acc1"))]
