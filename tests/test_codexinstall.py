from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import httpx
import pytest

from codexctl.codexinstall import (
    REQUIREMENTS,
    WRAPPER,
    CodexInstallError,
    ensure_codex,
    package_arch,
    wrapper_script,
)
from codexctl.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def make_settings(tmp_path: Path, **kw) -> Settings:
    values = dict(
        state_dir=tmp_path / "state",
        key_file=tmp_path / "master.key",
        codex_bin=tmp_path / "opt" / "codex" / "bin" / "codex",
        codex_wrapper=tmp_path / "usr-local-bin" / "codex",
        codex_requirements=tmp_path / "etc" / "requirements.toml",
        codex_auto_install=True,
    )
    values.update(kw)
    return Settings(**values)


def make_tar(path: Path, files: dict[str, bytes]) -> bytes:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("/codex") else 0o644
            tf.addfile(info, io.BytesIO(data))
    return path.read_bytes()


def package(tmp_path: Path) -> bytes:
    return make_tar(
        tmp_path / "pkg.tar.gz",
        {"bin/codex": b"#!/bin/sh\nexit 0\n", "share/doc.txt": b"hello"},
    )


def test_wrapper_and_requirements_match_deploy_files():
    assert (ROOT / "deploy" / "codex-wrapper.sh").read_text(encoding="utf-8") == WRAPPER
    assert (ROOT / "deploy" / "requirements.toml").read_text(encoding="utf-8") == REQUIREMENTS


def test_ensure_installs_package_and_helper_files(tmp_path):
    s = make_settings(tmp_path)
    data = package(tmp_path)
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, content=data)

    msg = ensure_codex(s, httpx.Client(transport=httpx.MockTransport(handler)))
    assert "已安装 codex" in msg and "包装脚本" in msg and "强制配置" in msg
    assert s.codex_bin.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert os.access(s.codex_bin, os.X_OK)
    assert (tmp_path / "opt" / "codex" / "share" / "doc.txt").read_text() == "hello"
    assert s.codex_wrapper.read_text(encoding="utf-8") == wrapper_script(s.codex_bin)
    assert stat.S_IMODE(s.codex_wrapper.stat().st_mode) == 0o755
    assert s.codex_requirements.read_text(encoding="utf-8") == REQUIREMENTS
    assert stat.S_IMODE(s.codex_requirements.stat().st_mode) == 0o644
    assert len(urls) == 1


def test_ensure_is_idempotent_and_does_not_download_again(tmp_path):
    s = make_settings(tmp_path)
    data = package(tmp_path)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=data)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    ensure_codex(s, http)
    assert ensure_codex(s, http) == "codex 已就绪"
    assert len(calls) == 1


def test_ensure_keeps_existing_wrapper_and_requirements(tmp_path):
    s = make_settings(tmp_path)
    s.codex_bin.parent.mkdir(parents=True)
    s.codex_bin.write_text("#!/bin/sh\nexit 0\n")
    s.codex_bin.chmod(0o755)
    s.codex_wrapper.parent.mkdir(parents=True)
    s.codex_wrapper.write_text("custom wrapper\n")
    s.codex_requirements.parent.mkdir(parents=True)
    s.codex_requirements.write_text("custom = true\n")
    assert ensure_codex(s, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))) == "codex 已就绪"
    assert s.codex_wrapper.read_text() == "custom wrapper\n"
    assert s.codex_requirements.read_text() == "custom = true\n"


def test_ensure_uses_configured_version_and_arch(tmp_path):
    s = make_settings(tmp_path, codex_version="9.9.9")
    data = package(tmp_path)
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, content=data)

    ensure_codex(s, httpx.Client(transport=httpx.MockTransport(handler)), machine="arm64")
    assert urls == [
        "https://github.com/openai/codex/releases/download/"
        "rust-v9.9.9/codex-package-aarch64-unknown-linux-musl.tar.gz"
    ]
    assert f'REAL="${{CODEX_REAL:-{s.codex_bin}}}"' in s.codex_wrapper.read_text(encoding="utf-8")


def test_ensure_reports_http_failure(tmp_path):
    s = make_settings(tmp_path)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(CodexInstallError, match="HTTP 503"):
        ensure_codex(s, http)
    assert not s.codex_bin.exists()


def test_ensure_rejects_path_traversal(tmp_path):
    s = make_settings(tmp_path)
    data = make_tar(tmp_path / "evil.tar.gz", {"../evil": b"x"})
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data)))
    with pytest.raises(CodexInstallError, match="非法路径"):
        ensure_codex(s, http)
    assert not (tmp_path / "evil").exists()


def test_ensure_rejects_symlink_members(tmp_path):
    s = make_settings(tmp_path)
    path = tmp_path / "link.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("bin/codex")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=path.read_bytes())))
    with pytest.raises(CodexInstallError, match="不支持的文件类型"):
        ensure_codex(s, http)


def test_ensure_requires_bin_directory(tmp_path):
    s = make_settings(tmp_path, codex_bin=tmp_path / "opt" / "codex" / "codex")
    with pytest.raises(CodexInstallError, match="bin/"):
        ensure_codex(s, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


def test_auto_install_can_be_disabled(tmp_path):
    s = make_settings(tmp_path, codex_auto_install=False)
    with pytest.raises(CodexInstallError, match="已关闭自动安装"):
        ensure_codex(s, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


def test_package_arch():
    assert package_arch("x86_64") == "x86_64"
    assert package_arch("AMD64") == "x86_64"
    assert package_arch("aarch64") == "aarch64"
    assert package_arch("arm64") == "aarch64"
    with pytest.raises(CodexInstallError, match="架构"):
        package_arch("riscv64")
