from codexctl.config import Settings
from codexctl.runner import CommandError
from codexctl.selfcheck import selfcheck
from codexctl.vault import generate_key_file
from fakes import FakeRunner


def good_settings(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    key = tmp_path / "master.key"
    generate_key_file(key)
    codex = tmp_path / "codex"
    codex.write_text("")
    return Settings(state_dir=state, key_file=key, codex_bin=codex)


def test_reports_missing_pieces(tmp_path):
    s = Settings(state_dir=tmp_path / "state", key_file=tmp_path / "k", codex_bin=tmp_path / "codex")
    joined = "\n".join(selfcheck(s, run=FakeRunner(), which=lambda t: None))
    assert "apt install acl" in joined
    assert "codex 可执行文件" in joined
    assert "主密钥不可用" in joined
    assert "codexctl init" in joined


def test_all_good(tmp_path):
    runner = FakeRunner()
    assert selfcheck(good_settings(tmp_path), run=runner, which=lambda t: "/usr/bin/" + t) == []
    assert runner.calls[0][:3] == ["setfacl", "-m", "u:root:r"]
    assert not (tmp_path / "state" / ".acl-probe").exists()


def test_acl_unsupported(tmp_path):
    runner = FakeRunner()
    runner.fail_with = CommandError("Operation not supported")
    problems = selfcheck(good_settings(tmp_path), run=runner, which=lambda t: "/usr/bin/" + t)
    assert len(problems) == 1 and "不支持 ACL" in problems[0]
