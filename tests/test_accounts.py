import pytest

from codexctl import accounts, events, repo
from codexctl.authfile import AuthBundle, AuthFileError
from fakes import T0, make_auth_json


def test_import_encrypts_and_publishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    assert (acc.email, acc.plan_type, acc.status) == ("a@example.com", "pro", "active")
    assert acc.last_refresh == T0 and acc.access_expires_at == T0 + 10 * 86400
    assert b"rt-1" not in repo.load_secret(ctx.conn, acc.id)
    published = ctx.publisher.account_file("acc1").read_bytes()
    assert published == accounts.load_bundle(ctx, acc).to_bytes()
    assert AuthBundle.from_bytes(published).refresh_token == "rt-1"
    audit = ctx.conn.execute("SELECT action, target FROM audit_log").fetchall()
    assert [tuple(r) for r in audit] == [("account.add", "acc1")]


def test_import_rejects_duplicate_chatgpt_account(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="已登记为 acc1"):
        accounts.import_account(ctx, "acc2", make_auth_json(), "test")


def test_import_rejects_duplicate_label(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="标签已存在"):
        accounts.import_account(ctx, "acc1", make_auth_json(account_id="acct-2"), "test")


def test_import_rejects_bad_label(ctx):
    with pytest.raises(accounts.AccountError, match="账号标签"):
        accounts.import_account(ctx, "Bad Label", make_auth_json(), "test")


def test_import_rejects_api_key_file(ctx):
    with pytest.raises(AuthFileError):
        accounts.import_account(ctx, "acc1", b'{"auth_mode":"apikey"}', "test")


def test_relogin_requires_same_account(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="另一个"):
        accounts.relogin_account(ctx, "acc1", make_auth_json(account_id="acct-2"), "test")


def test_relogin_resets_status_and_republishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    repo.set_status(ctx.conn, acc.id, "needs_relogin", "reused")
    events.raise_alert(ctx.conn, "critical", "refresh_failed", "acc1", "x", now=1)
    acc = accounts.relogin_account(ctx, "acc1", make_auth_json(refresh_token="rt-9"), "test")
    assert (acc.status, acc.last_error) == ("active", None)
    assert events.open_alerts(ctx.conn) == []
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-9"


def test_set_disabled_and_enable(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    accounts.set_disabled(ctx, "acc1", True, "test")
    assert repo.get_account(ctx.conn, "acc1").status == "disabled"
    accounts.set_disabled(ctx, "acc1", False, "test")
    assert repo.get_account(ctx.conn, "acc1").status == "active"


def test_require_account_missing(ctx):
    with pytest.raises(accounts.AccountError, match="账号不存在"):
        accounts.require_account(ctx, "nope")


def test_device_login_runs_codex_with_staging_home(ctx, monkeypatch):
    script = ctx.settings.codex_bin
    script.write_text(
        '#!/bin/sh\n'
        '[ "$1 $2" = "login --device-auth" ] || exit 2\n'
        'printf "%s" "$FAKE_AUTH" > "$CODEX_HOME/auth.json"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_AUTH", make_auth_json().decode())
    data = accounts.device_login(ctx, "acc1")
    assert AuthBundle.from_bytes(data).account_id == "acct-1"
    assert list(ctx.settings.staging_dir.iterdir()) == []


def test_device_login_failure(ctx):
    script = ctx.settings.codex_bin
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    with pytest.raises(accounts.AccountError, match="退出码 1"):
        accounts.device_login(ctx, "acc1")
    assert list(ctx.settings.staging_dir.iterdir()) == []


def test_relogin_keeps_disabled_status(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    accounts.set_disabled(ctx, "acc1", True, "test")
    acc = accounts.relogin_account(ctx, "acc1", make_auth_json(refresh_token="rt-9"), "test")
    assert acc.status == "disabled"


def test_enable_keeps_needs_relogin(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    repo.set_status(ctx.conn, acc.id, "needs_relogin", "reused")
    accounts.set_disabled(ctx, "acc1", False, "test")
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.status, acc.last_error) == ("needs_relogin", "reused")
    assert ctx.conn.execute("SELECT count(*) FROM audit_log WHERE action = 'account.enable'").fetchone()[0] == 1


def test_device_login_missing_binary(ctx):
    with pytest.raises(accounts.AccountError, match="无法运行 codex"):
        accounts.device_login(ctx, "acc1")
    assert list(ctx.settings.staging_dir.iterdir()) == []


def test_republish_account_rereads_under_lock(ctx):
    from codexctl import locks

    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.republish_account(ctx, "acc1")
    import pytest
    with pytest.raises(accounts.AccountError):
        accounts.republish_account(ctx, "nope")
    with locks.account_lock(ctx.settings.state_dir, "acc1"):
        with pytest.raises(locks.LockBusy):
            with locks.account_lock(ctx.settings.state_dir, "acc1", blocking=False):
                pass


def test_set_display_name_only_changes_display(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    published = ctx.publisher.account_file("acc1").read_bytes()
    assert acc.name == "acc1"
    assert accounts.set_display_name(ctx, "acc1", "  主力   账号 ", "test") == "主力 账号"
    acc = accounts.require_account(ctx, "acc1")
    assert (acc.label, acc.display_name, acc.name) == ("acc1", "主力 账号", "主力 账号")
    # 标签、凭证与发布文件都不受影响
    assert accounts.load_bundle(ctx, acc).refresh_token == "rt-1"
    assert ctx.publisher.account_file("acc1").read_bytes() == published
    assert accounts.set_display_name(ctx, "acc1", "", "test") is None
    assert accounts.require_account(ctx, "acc1").name == "acc1"
    audit = ctx.conn.execute("SELECT detail FROM audit_log WHERE action = 'account.rename'").fetchall()
    assert [r[0] for r in audit] == ["- -> 主力 账号", "主力 账号 -> -"]


def test_set_display_name_validation(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="不能超过"):
        accounts.set_display_name(ctx, "acc1", "x" * 33, "test")
    with pytest.raises(accounts.AccountError, match="控制字符"):
        accounts.set_display_name(ctx, "acc1", "a​b", "test")
    with pytest.raises(accounts.AccountError, match="不存在"):
        accounts.set_display_name(ctx, "nope", "x", "test")
    # 名称不变时不重复记审计
    accounts.set_display_name(ctx, "acc1", "x", "test")
    accounts.set_display_name(ctx, "acc1", "x", "test")
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'account.rename'").fetchone()[0] == 1
