import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "deploy" / "codex-wrapper.sh"


def run_wrapper(tmp_path, *args):
    real = tmp_path / "real-codex"
    real.write_text('#!/bin/sh\necho "real $*"\n')
    real.chmod(0o755)
    return subprocess.run(
        ["sh", str(WRAPPER), *args], capture_output=True, text=True, env={"CODEX_REAL": str(real), "PATH": "/usr/bin:/bin"}
    )


def test_blocks_login_and_logout(tmp_path):
    for cmd in ("login", "logout"):
        r = run_wrapper(tmp_path, cmd)
        assert r.returncode == 1
        assert "管理员" in r.stderr


def test_allows_login_status(tmp_path):
    r = run_wrapper(tmp_path, "login", "status")
    assert r.returncode == 0 and r.stdout == "real login status\n"


def test_passes_through_other_commands(tmp_path):
    r = run_wrapper(tmp_path, "exec", "hello world")
    assert r.stdout == "real exec hello world\n"
