import os
import pwd

import pytest

from codexctl import sysusers
from codexctl.runner import CommandError
from codexctl.sysusers import (
    UserError, create_user, list_users, lookup_user, user_exists, validate_password,
)
from fakes import FakeRunner

ME = pwd.getpwuid(os.getuid())


def _pw(name="alice", uid=1000, home="/home/alice"):
    return pwd.struct_passwd((name, "x", uid, uid, "", home, "/bin/bash"))


def test_lookup_current_user():
    u = lookup_user(ME.pw_name, min_uid=0)
    assert u.uid == os.getuid()
    assert str(u.home) == ME.pw_dir


def test_rejects_uid_below_minimum():
    with pytest.raises(UserError, match="不在允许范围内"):
        lookup_user(ME.pw_name, min_uid=ME.pw_uid + 1)


def test_rejects_bad_name():
    with pytest.raises(UserError, match="非法用户名"):
        lookup_user("../etc", min_uid=0)


def test_rejects_missing_user():
    with pytest.raises(UserError, match="不存在"):
        lookup_user("no_such_user_cx", min_uid=0)


def test_list_users_includes_current_user():
    assert ME.pw_name in {u.name for u in list_users(0)}


def test_rejects_trailing_newline_name():
    with pytest.raises(UserError, match="非法用户名"):
        lookup_user(ME.pw_name + "\n", min_uid=0)


def test_user_exists():
    assert user_exists(ME.pw_name)
    assert not user_exists("no_such_user_cx")
    assert not user_exists("../etc")


def test_validate_password():
    validate_password("long-enough")
    for bad in ("short", "", "x" * 129, "line\nbreak", "nul\0byte"):
        with pytest.raises(UserError):
            validate_password(bad)


def test_create_user_runs_useradd_and_passes_password_via_stdin(monkeypatch):
    runner = FakeRunner()
    calls = 0

    def fake_getpwnam(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError(name)
        return _pw(name)

    monkeypatch.setattr(sysusers, "_getpwnam", fake_getpwnam)
    user = create_user("alice", "long-enough", run=runner)
    assert user.name == "alice" and user.uid == 1000
    assert runner.calls == [
        ["useradd", "--create-home", "--shell", "/bin/bash", "--", "alice"],
        ["chpasswd"],
    ]
    assert runner.inputs == [None, b"alice:long-enough\n"]


def test_create_user_rejects_existing_user(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(sysusers, "_getpwnam", lambda name: _pw(name))
    with pytest.raises(UserError, match="已存在"):
        create_user("alice", "long-enough", run=runner)
    assert runner.calls == []


def test_create_user_rejects_bad_input_before_running_commands():
    runner = FakeRunner()
    with pytest.raises(UserError, match="非法用户名"):
        create_user("../etc", "long-enough", run=runner)
    with pytest.raises(UserError, match="至少"):
        create_user("alice", "short", run=runner)
    assert runner.calls == []


def _missing(name):
    raise KeyError(name)


def test_create_user_useradd_failure(monkeypatch):
    monkeypatch.setattr(sysusers, "_getpwnam", _missing)
    runner = FakeRunner()
    runner.fail_with = CommandError("useradd 退出码 1：bad name")
    with pytest.raises(UserError, match="创建用户失败"):
        create_user("alice", "long-enough", run=runner)


def test_create_user_chpasswd_failure_mentions_partial_state(monkeypatch):
    state = {"checked": False}

    def fake_getpwnam(name):
        if not state["checked"]:
            state["checked"] = True
            raise KeyError(name)
        return _pw(name)

    monkeypatch.setattr(sysusers, "_getpwnam", fake_getpwnam)

    def run(args, input=None, timeout=30):
        if args[0] == "chpasswd":
            raise CommandError("chpasswd 退出码 1：boom")
        return ""

    with pytest.raises(UserError, match="已创建，但设置密码失败"):
        create_user("alice", "long-enough", run=run)


def test_create_user_rejects_out_of_range_uid(monkeypatch):
    calls = 0

    def fake_getpwnam(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError(name)
        return _pw(name, uid=500)

    monkeypatch.setattr(sysusers, "_getpwnam", fake_getpwnam)
    with pytest.raises(UserError, match="不在允许范围内"):
        create_user("alice", "long-enough", run=FakeRunner(), min_uid=1000)


def test_create_user_password_only_reaches_chpasswd_stdin(tmp_path, monkeypatch):
    from codexctl.runner import run as real_run

    useradd_log = tmp_path / "useradd.args"
    chpasswd_args = tmp_path / "chpasswd.args"
    chpasswd_input = tmp_path / "chpasswd.input"
    useradd = tmp_path / "useradd"
    useradd.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {useradd_log}\n')
    useradd.chmod(0o755)
    chpasswd = tmp_path / "chpasswd"
    chpasswd.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {chpasswd_args}\ncat > {chpasswd_input}\n')
    chpasswd.chmod(0o755)

    calls = 0

    def fake_getpwnam(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError(name)
        return _pw(name)

    monkeypatch.setattr(sysusers, "_getpwnam", fake_getpwnam)
    create_user(
        "alice", "s3cret-long", run=real_run, useradd=str(useradd), chpasswd=str(chpasswd)
    )
    assert useradd_log.read_text().splitlines() == [
        "--create-home", "--shell", "/bin/bash", "--", "alice",
    ]
    assert chpasswd_args.read_text().strip() == ""
    assert chpasswd_input.read_text() == "alice:s3cret-long\n"
