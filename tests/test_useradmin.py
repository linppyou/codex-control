from __future__ import annotations

import dataclasses
import stat

import pytest

from codexctl import useradmin
from codexctl.codexinstall import CodexInstallError
from codexctl.sysusers import UserError
from codexctl.useradmin import UserAdminError

PW = "long-enough-password"


def test_create_user_sets_up_home_and_codex(ctx):
    user, note = useradmin.create(ctx, "alice", PW, PW, "web:root_admin")
    assert user.name == "alice"
    assert ctx.test_users.exists("alice")
    codex_dir = ctx.test_users.get("alice").home / ".codex"
    assert codex_dir.is_dir()
    assert stat.S_IMODE(codex_dir.stat().st_mode) == 0o700
    assert "codex" in note
    assert ctx.settings.codex_wrapper.is_file()
    assert ctx.settings.codex_requirements.is_file()

    row = ctx.conn.execute(
        "SELECT actor, action, target, detail FROM audit_log WHERE action = 'user.create'"
    ).fetchone()
    assert tuple(row)[:3] == ("web:root_admin", "user.create", "alice")
    assert f"uid={user.uid}" in row["detail"] and str(user.home) in row["detail"]
    assert PW not in row["detail"]
    for r in ctx.conn.execute("SELECT detail FROM audit_log"):
        assert PW not in r["detail"]


def test_create_user_rejects_existing_user(ctx):
    ctx.test_users.add("alice")
    with pytest.raises(UserAdminError, match="已存在"):
        useradmin.create(ctx, "alice", PW, PW, "web:root_admin")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


@pytest.mark.parametrize("with_ca", [False, True])
def test_create_user_provisions_site_ssh_when_enabled(ctx, monkeypatch, tmp_path, with_ca):
    ctx.settings = dataclasses.replace(
        ctx.settings, site_ssh_enabled=True, site_ssh_ca_key=tmp_path / "site-ca" if with_ca else None,
    )
    provisioned = []
    monkeypatch.setattr(useradmin.site_access, "provision", lambda settings, user: provisioned.append(user))

    user, note = useradmin.create(ctx, "alice", PW, PW, "web:root_admin")

    assert provisioned == [user]
    expected_note = "已签发站点 SSH 证书" if with_ca else "需在目标服务器授权公钥"
    assert expected_note in note


@pytest.mark.parametrize(
    "name,password,confirm,error",
    [
        ("Bad Name", PW, PW, "非法用户名"),
        ("alice", "short", "short", "至少"),
        ("alice", PW, "different-password", "不一致"),
        ("alice", PW + "\n", PW + "\n", "换行"),
    ],
)
def test_create_user_validates_input(ctx, name, password, confirm, error):
    with pytest.raises(UserError, match=error):
        useradmin.create(ctx, name, password, confirm, "web:root_admin")
    assert not ctx.test_users.exists("alice")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_codex_install_failure_does_not_create_user(ctx, tmp_path):
    ctx.settings = dataclasses.replace(
        ctx.settings,
        codex_bin=tmp_path / "missing" / "bin" / "codex",
        codex_wrapper=tmp_path / "missing-wrapper",
        codex_requirements=tmp_path / "missing-requirements",
    )
    with pytest.raises(CodexInstallError, match="HTTP 404"):
        useradmin.create(ctx, "alice", PW, PW, "web:root_admin")
    assert not ctx.test_users.exists("alice")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_create_user_reports_codex_dir_failure(ctx, monkeypatch):
    def boom(user):
        raise OSError("disk full")

    monkeypatch.setattr(ctx.publisher, "ensure_codex_dir", boom)
    with pytest.raises(UserAdminError, match="已创建，但初始化"):
        useradmin.create(ctx, "alice", PW, PW, "web:root_admin")
    assert ctx.test_users.exists("alice")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
