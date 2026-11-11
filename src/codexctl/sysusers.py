from __future__ import annotations

import pwd
import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_NOBODY_UID = 65534


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


def lookup_user(name: str, min_uid: int) -> SysUser:
    if not _NAME_RE.match(name):
        raise UserError(f"非法用户名：{name!r}")
    try:
        pw = pwd.getpwnam(name)
    except KeyError as e:
        raise UserError(f"系统中不存在用户：{name}") from e
    if pw.pw_uid < min_uid or pw.pw_uid == _NOBODY_UID:
        raise UserError(f"用户 {name} 的 uid={pw.pw_uid} 不在允许范围内")
    return _from_pw(pw)


def list_users(min_uid: int) -> list[SysUser]:
    users = [
        _from_pw(pw)
        for pw in pwd.getpwall()
        if pw.pw_uid >= min_uid and pw.pw_uid != _NOBODY_UID and _NAME_RE.match(pw.pw_name)
    ]
    return sorted(users, key=lambda u: u.name)
