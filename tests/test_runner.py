import pytest

from codexctl.runner import CommandError, run


def test_run_returns_stdout():
    assert run(["sh", "-c", "echo hi"]) == "hi\n"


def test_run_raises_with_stderr():
    with pytest.raises(CommandError, match="退出码 3：bad"):
        run(["sh", "-c", "echo bad >&2; exit 3"])


def test_run_missing_binary():
    with pytest.raises(CommandError, match="执行失败"):
        run(["/nonexistent/binary"])
