import pytest

from codexctl import cli
from fakes import make_auth_json


def run_cli(ctx, *argv):
    return cli.main(list(argv), context_factory=lambda s: ctx)


@pytest.fixture(autouse=True)
def _no_default_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", tmp_path / "absent-default.toml")


def test_account_import_and_list(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    assert run_cli(ctx, "account", "import", "acc1", str(f)) == 0
    assert run_cli(ctx, "account", "list") == 0
    out = capsys.readouterr().out
    assert "acc1" in out and "a@example.com" in out and "用户数=0" in out


def test_assign_users_and_unassign(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    ctx.test_users.add("alice")
    assert run_cli(ctx, "assign", "--account", "acc1", "alice") == 0
    assert run_cli(ctx, "users") == 0
    out = capsys.readouterr().out
    assert "alice -> acc1" in out
    assert any(line.split() == ["alice", "acc1", "symlink"] for line in out.splitlines())
    assert run_cli(ctx, "unassign", "alice") == 0
    assert "alice: 已回收" in capsys.readouterr().out


def test_check_reports_findings(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    ctx.publisher.account_file("acc1").write_bytes(b"{}")
    assert run_cli(ctx, "check") == 1
    assert "published_mismatch acc1" in capsys.readouterr().out


def test_errors_print_message_and_return_1(ctx, capsys):
    assert run_cli(ctx, "assign", "--account", "nope", "alice") == 1
    assert "错误：" in capsys.readouterr().err


def test_window_name():
    assert cli.window_name(300) == "5小时"
    assert cli.window_name(10080) == "7天"
    assert cli.window_name(None) == "未知窗口"


def test_refresh_busy_account(ctx, tmp_path, capsys):
    from codexctl import locks

    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    with locks.account_lock(ctx.settings.state_dir, "acc1"):
        assert run_cli(ctx, "account", "refresh", "acc1") == 1
    assert "错误：账号 acc1 正在被刷新，请稍后再试" in capsys.readouterr().err


def test_check_output_sanitized(ctx, monkeypatch, capsys):
    from codexctl import watcher

    monkeypatch.setattr(
        watcher, "run_checks", lambda c: [watcher.Finding("error", "k", "t", "bad \x1b[31m link")]
    )
    assert run_cli(ctx, "check") == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out and "bad ?[31m link" in out


def test_explicit_missing_config(ctx, capsys):
    assert run_cli(ctx, "-c", "/nonexistent/x.toml", "account", "list") == 1
    assert "错误：配置文件不存在：/nonexistent/x.toml" in capsys.readouterr().err


def test_malformed_config(ctx, tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    assert run_cli(ctx, "-c", str(bad), "account", "list") == 1
    assert "错误：" in capsys.readouterr().err
    bad.write_text('bogus_key = 1\n')
    assert run_cli(ctx, "-c", str(bad), "account", "list") == 1


def test_init_permission_error_handled(ctx, tmp_path, monkeypatch, capsys):
    def boom(s):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "cmd_init", boom)
    assert run_cli(ctx, "init") == 1
    assert "错误：denied" in capsys.readouterr().err


def test_multi_user_continues_after_failure(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    ctx.test_users.add("alice")
    assert run_cli(ctx, "assign", "--account", "acc1", "ghost", "alice") == 1
    cap = capsys.readouterr()
    assert "错误：ghost:" in cap.err
    assert "alice -> acc1" in cap.out


def test_usage_days_must_be_positive(ctx):
    with pytest.raises(SystemExit):
        run_cli(ctx, "usage", "--days", "0")


def test_sqlite_error_handled(ctx, monkeypatch, capsys):
    import sqlite3

    def boom(conn):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(cli.repo, "list_accounts", boom)
    assert run_cli(ctx, "account", "list") == 1
    assert "错误：" in capsys.readouterr().err
