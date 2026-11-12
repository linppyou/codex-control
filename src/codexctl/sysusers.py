from __future__ import annotations

import pwd
import re
from dataclasses import dataclass
from pathlib import Path

from . import runner as runner_mod
from .runner import CommandError, Runner

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_NOBODY_UID = 65534
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


class UserError(ValueError):
    pass


@dataclass(frozen=True)
class SysUser:
    name: str
    uid: int
    gid: int
    home: Path


def _from_pw(pw: pwd.struct_passwd) -> SysUser:
    return SysUser(pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir))


def _getpwnam(name: str) -> pwd.struct_passwd:
    return pwd.getpwnam(name)


def valid_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None


def user_exists(name: str) -> bool:
    if not valid_name(name):
        return False
    try:
        _getpwnam(name)
    except KeyError:
        return False
    return True


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise UserError(f"密码至少 {MIN_PASSWORD_LEN} 个字符")
    if len(password) > MAX_PASSWORD_LEN:
        raise UserError(f"密码最长 {MAX_PASSWORD_LEN} 个字符")
    if any(ch in password for ch in "\r\n\0"):
        raise UserError("密码不能包含换行或 NUL 字符")


def lookup_user(name: str, min_uid: int) -> SysUser:
    if not valid_name(name):
        raise UserError(f"非法用户名：{name!r}")
    try:
        pw = _getpwnam(name)
    except KeyError as e:
        raise UserError(f"系统中不存在用户：{name}") from e
    if pw.pw_uid < min_uid or pw.pw_uid == _NOBODY_UID:
        raise UserError(f"用户 {name} 的 uid={pw.pw_uid} 不在允许范围内")
    return _from_pw(pw)


def list_users(min_uid: int) -> list[SysUser]:
    users = [
        _from_pw(pw)
        for pw in pwd.getpwall()
        if pw.pw_uid >= min_uid and pw.pw_uid != _NOBODY_UID and valid_name(pw.pw_name)
    ]
    return sorted(users, key=lambda u: u.name)


def create_user(
    name: str,
    password: str,
    *,
    min_uid: int = 1000,
    shell: str = "/bin/bash",
    run: Runner = runner_mod.run,
    useradd: str = "useradd",
    chpasswd: str = "chpasswd",
) -> SysUser:
    """创建 Linux 用户并通过 chpasswd 的 stdin 设置密码（密码不出现在 argv 中）。"""
    if not valid_name(name):
        raise UserError(f"非法用户名：{name!r}")
    validate_password(password)
    if user_exists(name):
        raise UserError(f"用户已存在：{name}")
    try:
        run([useradd, "--create-home", "--shell", shell, "--", name])
    except CommandError as e:
        raise UserError(f"创建用户失败：{e}") from e
    try:
        run([chpasswd], input=f"{name}:{password}\n".encode())
    except CommandError as e:
        raise UserError(f"用户 {name} 已创建，但设置密码失败：{e}") from e
    try:
        pw = _getpwnam(name)
    except KeyError as e:
        raise UserError(f"用户 {name} 创建后无法从系统读取") from e
    if pw.pw_uid < min_uid or pw.pw_uid == _NOBODY_UID:
        raise UserError(f"新用户 {name} 的 uid={pw.pw_uid} 不在允许范围内")
    return _from_pw(pw)
