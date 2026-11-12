from __future__ import annotations

import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import httpx

from .config import Settings

# 与 deploy/codex-wrapper.sh、deploy/requirements.toml 保持一致（tests/test_codexinstall.py 会比对）。
_WRAPPER = """#!/bin/sh
# Codex 登录状态由管理员通过 codexctl 统一管理，禁止用户自行 login / logout。
# 注意：logout 会在服务端吊销同一账号所有用户共用的 token。
REAL="${CODEX_REAL:-__CODEX_BIN__}"
case "$1" in
  login)
    if [ "$2" = "status" ]; then
      exec "$REAL" "$@"
    fi
    echo "Codex 账号由管理员统一分配，请勿自行 login。如需帮助请联系管理员。" >&2
    exit 1
    ;;
  logout)
    echo "Codex 账号由管理员统一分配，请勿自行 logout（会导致同账号所有用户掉线）。" >&2
    exit 1
    ;;
esac
exec "$REAL" "$@"
"""


def wrapper_script(binary: Path) -> str:
    """包装脚本里的真实 codex 路径跟随 codex_bin 配置。"""
    return _WRAPPER.replace("__CODEX_BIN__", str(binary))


WRAPPER = wrapper_script(Path("/opt/codex/bin/codex"))
REQUIREMENTS = '# 管理员强制配置：只允许 ChatGPT 登录方式，禁止 API key。\nallowed_login_methods = ["chatgpt"]\n'

_ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


class CodexInstallError(Exception):
    pass


def package_arch(machine: str | None = None) -> str:
    value = (machine or platform.machine()).lower()
    arch = _ARCHES.get(value)
    if arch is None:
        raise CodexInstallError(f"不支持自动安装 codex 的 CPU 架构：{value or '未知'}")
    return arch


def codex_ready(binary: Path) -> bool:
    return binary.is_file() and os.access(binary, os.X_OK)


def ensure_codex(settings: Settings, http: httpx.Client, *, machine: str | None = None) -> str:
    """确保 codex 二进制、包装脚本与强制配置就绪；返回给管理员看的说明。"""
    try:
        return _ensure(settings, http, machine)
    except CodexInstallError:
        raise
    except OSError as e:
        raise CodexInstallError(f"准备 codex 失败：{e}") from e


def _ensure(settings: Settings, http: httpx.Client, machine: str | None) -> str:
    actions: list[str] = []
    if not codex_ready(settings.codex_bin):
        if not settings.codex_auto_install:
            raise CodexInstallError(f"未找到 codex 可执行文件：{settings.codex_bin}（已关闭自动安装）")
        _install(settings, http, machine)
        actions.append(f"已安装 codex {settings.codex_version}")
    if _write_missing(settings.codex_wrapper, wrapper_script(settings.codex_bin), 0o755):
        actions.append("已写入 codex 包装脚本")
    if _write_missing(settings.codex_requirements, REQUIREMENTS, 0o644):
        actions.append("已写入 codex 强制配置")
    return "；".join(actions) if actions else "codex 已就绪"


def _install(settings: Settings, http: httpx.Client, machine: str | None) -> None:
    binary = settings.codex_bin
    if binary.parent.name != "bin":
        raise CodexInstallError(f"codex_bin 必须位于安装目录的 bin/ 下：{binary}")
    install_dir = binary.parent.parent
    url = settings.codex_package_url.format(
        version=settings.codex_version, arch=package_arch(machine)
    )
    with tempfile.TemporaryDirectory(prefix="codexctl-install-") as tmp:
        tmpdir = Path(tmp)
        archive = tmpdir / "codex.tar.gz"
        _download(http, url, archive, settings.codex_install_timeout)
        extracted = tmpdir / "extracted"
        extracted.mkdir()
        _extract(archive, extracted)
        source = extracted / "bin" / "codex"
        if not source.is_file():
            raise CodexInstallError("codex 安装包中缺少 bin/codex")
        install_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(install_dir, 0o755)
        for child in extracted.iterdir():
            dest = install_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dest, dirs_exist_ok=True, symlinks=False)
            else:
                shutil.copy2(child, dest)
    if not binary.exists():
        raise CodexInstallError(f"安装后未找到 codex：{binary}")
    os.chmod(binary, 0o755)
    if not codex_ready(binary):
        raise CodexInstallError(f"安装后 codex 不可执行：{binary}")


def _download(http: httpx.Client, url: str, dest: Path, timeout: float) -> None:
    try:
        with http.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                raise CodexInstallError(f"下载 codex 失败：HTTP {resp.status_code}（{url}）")
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 16):
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise CodexInstallError(f"下载 codex 失败：{e}") from e


def _extract(archive: Path, dest: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise CodexInstallError(f"codex 安装包包含非法路径：{member.name}")
                if not (member.isfile() or member.isdir()):
                    raise CodexInstallError(f"codex 安装包包含不支持的文件类型：{member.name}")
            try:
                tf.extractall(dest, members=members, filter="data")
            except TypeError:  # Python < 3.11.4 没有 extraction filter
                tf.extractall(dest, members=members)
    except (tarfile.TarError, OSError) as e:
        raise CodexInstallError(f"解压 codex 安装包失败：{e}") from e


def _write_missing(path: Path, content: str, mode: int) -> bool:
    if os.path.lexists(path):
        return False
    created_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created_dir:
        os.chmod(path.parent, 0o755)
    tmp = path.with_name(f".{path.name}.codexctl.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True
