import os
import signal
from pathlib import Path

from codexctl import procs


def make_root(tmp_path, btime=1_700_000_000):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text("cpu  0 0 0 0\nbtime " + str(btime) + "\n")
    return root


def add_proc(root, pid, *, uid=1000, exe="/opt/codex/bin/codex", start_ticks=100, comm="codex", cmdline=None):
    d = root / str(pid)
    d.mkdir()
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    # 第 3..52 个字段；starttime 是第 22 个字段，即去掉 pid/comm 后的第 20 项。
    fields = ["S"] + ["0"] * 49
    fields[19] = str(start_ticks)
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    if exe is not None:
        os.symlink(exe, d / "exe")
    if cmdline is not None:
        (d / "cmdline").write_bytes(b"\0".join(part.encode() for part in cmdline) + b"\0")
    return d


def started_at(root, ticks):
    btime = int((root / "stat").read_text().split("btime ")[1].split()[0])
    return btime + ticks // procs._clock_ticks()


def test_lists_only_processes_of_user_and_codex_dir(tmp_path):
    root = make_root(tmp_path)
    add_proc(root, 101, uid=1000, start_ticks=200)
    add_proc(root, 102, uid=1001)  # 别的用户
    add_proc(root, 103, uid=1000, exe="/usr/bin/codex")  # 不是 codex_bin 目录
    add_proc(root, 104, uid=1000, start_ticks=300)

    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root)
    assert [p.pid for p in found] == [101, 104]
    assert found[0].started_at == started_at(root, 200)
    assert found[0].exe == "/opt/codex/bin/codex"


def test_deleted_suffix_and_odd_comm(tmp_path):
    root = make_root(tmp_path)
    add_proc(root, 201, exe="/opt/codex/bin/codex (deleted)", comm="codex app-server (x)")
    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root)
    assert [p.pid for p in found] == [201]
    assert found[0].exe == "/opt/codex/bin/codex"
    assert found[0].started_at == started_at(root, 100)


def test_skips_processes_that_vanish_or_lack_exe(tmp_path):
    root = make_root(tmp_path)
    add_proc(root, 301)
    add_proc(root, 302, exe=None)  # 读不到 exe
    (root / "303").mkdir()  # 没有 status/stat/exe
    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root)
    assert [p.pid for p in found] == [301]


def test_missing_proc_root_is_silent(tmp_path):
    assert procs.list_codex_procs(1000, Path("/opt/codex/bin"), tmp_path / "nope") == []


def test_stale_boundary(tmp_path):
    root = make_root(tmp_path)
    add_proc(root, 401, start_ticks=0)
    add_proc(root, 402, start_ticks=1000)
    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root)
    changed = started_at(root, 1000)
    stale = procs.stale_procs(found, changed)
    assert [p.pid for p in stale] == [401]  # 等于 changed_at 不算旧
    assert procs.stale_procs(found, None) == []


def test_terminate_escalates_after_grace():
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))

    ended, failed = procs.terminate(
        [7], grace=0.4, interval=0.2, kill=kill, sleep=lambda _s: None, alive=lambda _p: False
    )
    assert ended == [7] and failed == []
    assert killed == [(7, signal.SIGTERM)]

    killed.clear()
    ended, failed = procs.terminate(
        [7], grace=0.4, interval=0.2, kill=kill, sleep=lambda _s: None, alive=lambda _p: True
    )
    assert ended == [7] and failed == []
    assert killed == [(7, signal.SIGTERM), (7, signal.SIGKILL)]


def test_terminate_handles_missing_and_permission():
    def kill(pid, sig):
        if pid == 1:
            raise ProcessLookupError
        raise PermissionError

    ended, failed = procs.terminate([1, 2], grace=0, kill=kill)
    assert ended == [1]
    assert failed == [2]


def test_terminate_marks_process_gone_during_grace_as_ended():
    calls = {"n": 0}

    def alive(pid):
        calls["n"] += 1
        return calls["n"] == 1

    ended, failed = procs.terminate(
        [9], grace=0.4, interval=0.2, kill=lambda p, s: None, sleep=lambda _s: None, alive=alive
    )
    assert ended == [9] and failed == []


def test_includes_self_installed_codex_under_home(tmp_path):
    root = make_root(tmp_path)
    home = tmp_path / "home" / "alice"
    self_installed = str(home / ".codex/packages/standalone/releases/x/bin/codex")
    add_proc(root, 501, exe=self_installed)
    add_proc(root, 502, exe=str(home / "other" / "codex"))

    assert procs.list_codex_procs(1000, Path("/opt/codex/bin"), root) == []
    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root, home=home)
    assert [p.pid for p in found] == [501]


def test_skips_updater_supervisor(tmp_path):
    root = make_root(tmp_path)
    add_proc(root, 601, cmdline=["codex", "app-server", "daemon", "pid-update-loop"])
    add_proc(root, 602, cmdline=["codex", "app-server", "--managed-daemon"])
    found = procs.list_codex_procs(1000, Path("/opt/codex/bin"), root)
    assert [p.pid for p in found] == [602]
