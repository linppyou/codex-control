import os
import stat

import pytest

from codexctl.publisher import Publisher, PublishError
from codexctl.runner import CommandError
from codexctl.sysusers import SysUser
from fakes import FakeRunner


def make_user(tmp_path, name="alice"):
    home = tmp_path / "home" / name
    home.mkdir(parents=True)
    return SysUser(name, os.getuid(), os.getgid(), home)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def pub(tmp_path, runner):
    return Publisher(tmp_path / "accounts", run=runner)


def test_publish_writes_file_and_sets_acl_before_rename(tmp_path, pub, runner):
    alice = make_user(tmp_path)
    pub.publish("acc1", b'{"x":1}', [alice])
    f = tmp_path / "accounts" / "acc1" / "auth.json"
    assert f.read_bytes() == b'{"x":1}'
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o711
    assert stat.S_IMODE((tmp_path / "accounts").stat().st_mode) == 0o711
    assert runner.calls == [
        ["setfacl", "--set", "u::rw-,g::---,o::---,u:alice:r--,m::r--", str(f.parent / ".auth.json.tmp")]
    ]
    assert not (f.parent / ".auth.json.tmp").exists()


def test_publish_without_readers_uses_minimal_acl(tmp_path, pub, runner):
    pub.publish("acc1", b"{}", [])
    assert runner.calls[0][2] == "u::rw-,g::---,o::---"


def test_publish_failure_keeps_previous_file(tmp_path, pub, runner):
    pub.publish("acc1", b"old", [])
    runner.fail_with = CommandError("setfacl boom")
    with pytest.raises(CommandError):
        pub.publish("acc1", b"new", [])
    d = tmp_path / "accounts" / "acc1"
    assert (d / "auth.json").read_bytes() == b"old"
    assert not (d / ".auth.json.tmp").exists()


def test_rejects_bad_label(pub):
    with pytest.raises(PublishError, match="非法账号标签"):
        pub.account_file("../etc")


def test_set_readers_sorts_and_deduplicates(tmp_path, pub, runner):
    alice, bob = make_user(tmp_path, "alice"), make_user(tmp_path, "bob")
    pub.set_readers("acc1", [bob, alice, bob])
    assert runner.calls[0][2] == "u::rw-,g::---,o::---,u:alice:r--,u:bob:r--,m::r--"


def test_link_user_creates_codex_dir_and_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    assert pub.link_user(alice, "acc1", now=100) is None
    codex_dir = alice.home / ".codex"
    assert stat.S_IMODE(codex_dir.stat().st_mode) == 0o700
    assert os.readlink(codex_dir / "auth.json") == str(pub.account_file("acc1"))
    state = pub.read_link(alice)
    assert (state.kind, state.target) == ("symlink", str(pub.account_file("acc1")))


def test_link_user_backs_up_regular_file(tmp_path, pub):
    alice = make_user(tmp_path)
    (alice.home / ".codex").mkdir(mode=0o700)
    (alice.home / ".codex" / "auth.json").write_text("mine")
    backup = pub.link_user(alice, "acc1", now=123)
    assert backup == alice.home / ".codex" / "auth.json.bak.123"
    assert backup.read_text() == "mine"
    assert os.path.islink(alice.home / ".codex" / "auth.json")


def test_link_user_replaces_existing_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    pub.link_user(alice, "acc1", now=1)
    pub.link_user(alice, "acc2", now=2)
    assert os.readlink(alice.home / ".codex" / "auth.json") == str(pub.account_file("acc2"))


def test_link_user_refuses_symlinked_codex_dir(tmp_path, pub):
    alice = make_user(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (alice.home / ".codex").symlink_to(elsewhere)
    with pytest.raises(PublishError, match="不是普通目录"):
        pub.link_user(alice, "acc1", now=1)
    assert list(elsewhere.iterdir()) == []


def test_unlink_user_removes_only_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    pub.link_user(alice, "acc1", now=1)
    assert pub.unlink_user(alice) is True
    assert pub.read_link(alice).kind == "missing"
    (alice.home / ".codex" / "auth.json").write_text("mine")
    assert pub.unlink_user(alice) is False
    assert (alice.home / ".codex" / "auth.json").read_text() == "mine"


def test_read_link_states(tmp_path, pub):
    alice = make_user(tmp_path)
    assert pub.read_link(alice).kind == "no_codex_dir"
    (alice.home / ".codex").mkdir()
    assert pub.read_link(alice).kind == "missing"
    (alice.home / ".codex" / "auth.json").write_text("x")
    assert pub.read_link(alice).kind == "file"


def test_read_readers_parses_getfacl(pub, runner):
    runner.outputs["getfacl"] = (
        "user::rw-\nuser:alice:r--\nuser:bob:r--\t#effective:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    assert pub.read_readers("acc1") == {"alice", "bob"}
    assert runner.calls[0][:3] == ["getfacl", "--omit-header", "--absolute-names"]
