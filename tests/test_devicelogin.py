from __future__ import annotations

import time

import pytest

from codexctl.config import Settings
from codexctl.devicelogin import DeviceLoginManager


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def script(tmp_path, body):
    p = tmp_path / "codex"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


@pytest.fixture
def mgr_factory(tmp_path):
    made = []

    def make(body, clock=time.time):
        s = Settings(state_dir=tmp_path / "state", key_file=tmp_path / "k", codex_bin=script(tmp_path, body))
        s.state_dir.mkdir(exist_ok=True)
        m = DeviceLoginManager(s, clock=clock)
        made.append(m)
        return m, s

    yield make
    for m in made:
        for lid in list(m._logins):
            m.cancel(lid)
            p = m._logins[lid].proc
            if p.poll() is None:
                p.kill()


PRINT = 'printf "1. open\\n   https://auth.openai.com/codex/device\\n2. code\\n   BRMV-AA1D3\\n"\n'


def test_success(mgr_factory, tmp_path):
    marker = tmp_path / "go"
    m, s = mgr_factory(PRINT + f'while [ ! -e {marker} ]; do sleep 0.02; done\necho \'{{"x":1}}\' > "$CODEX_HOME/auth.json"\nexit 0\n')
    lid = m.start("alice", "add")
    assert wait_for(lambda: m.status(lid)["state"] == "waiting")
    st = m.status(lid)
    assert st["url"] == "https://auth.openai.com/codex/device"
    assert st["code"] == "BRMV-AA1D3"
    assert (s.staging_dir.stat().st_mode & 0o777) == 0o700
    marker.write_text("")
    assert wait_for(lambda: m.status(lid)["state"] == "succeeded")
    home = m._logins[lid].home
    assert m.take_result(lid) == b'{"x":1}\n'
    assert not home.exists()
    with pytest.raises(ValueError):
        m.take_result(lid)


def test_failure(mgr_factory):
    m, _ = mgr_factory("exit 3\n")
    lid = m.start("bob", "relogin")
    assert wait_for(lambda: m.status(lid)["state"] == "failed")
    assert "3" in m.status(lid)["error"]
    with pytest.raises(ValueError):
        m.take_result(lid)


def test_cancel(mgr_factory):
    m, _ = mgr_factory("sleep 30\n")
    lid = m.start("carol", "add")
    login = m._logins[lid]
    m.cancel(lid)
    assert m.status(lid)["state"] == "cancelled"
    assert login.proc.poll() is not None
    assert not login.home.exists()


def test_duplicate_label_and_validation(mgr_factory):
    m, _ = mgr_factory("sleep 30\n")
    m.start("dave", "add")
    with pytest.raises(ValueError, match="进行中"):
        m.start("dave", "relogin")
    with pytest.raises(ValueError):
        m.start("Bad Label", "add")
    with pytest.raises(ValueError):
        m.start("eve", "other")
    assert m.status("nope") is None


def test_sweep_expires_and_removes(mgr_factory):
    t = [1000.0]
    m, _ = mgr_factory("sleep 30\n", clock=lambda: t[0])
    lid = m.start("fred", "add")
    login = m._logins[lid]
    t[0] += 14 * 60
    m.sweep()
    assert m.status(lid)["state"] == "pending"
    t[0] += 2 * 60
    m.sweep()
    assert m.status(lid)["state"] == "expired"
    assert login.proc.poll() is not None and not login.home.exists()
    t[0] += 11 * 60
    m.sweep()
    assert m.status(lid) is None


def test_ansi_codes(mgr_factory):
    m, _ = mgr_factory(
        'printf "\\033[94mhttps://auth.openai.com/codex/device\\033[0m\\n\\033[1m\\033[94mBRMV-AA1D3\\033[0m\\n"\nsleep 30\n'
    )
    lid = m.start("gina", "add")
    assert wait_for(lambda: m.status(lid)["state"] == "waiting")
    st = m.status(lid)
    assert st["code"] == "BRMV-AA1D3"
    assert st["url"] == "https://auth.openai.com/codex/device"
