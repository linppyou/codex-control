from __future__ import annotations

import errno
import os
import re
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import runner as runner_mod
from .runner import Runner
from .sysusers import SysUser

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_LINK = "auth.json"
_LINK_TMP = ".auth.json.codexctl.tmp"
_FILE_TMP = ".auth.json.tmp"


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class LinkState:
    kind: str  # "symlink" | "file" | "missing" | "no_codex_dir" | "unsafe"
    target: str | None = None


class Publisher:
    def __init__(
        self,
        accounts_dir: Path,
        setfacl: str = "setfacl",
        getfacl: str = "getfacl",
        run: Runner = runner_mod.run,
    ) -> None:
        self.accounts_dir = accounts_dir
        self._setfacl = setfacl
        self._getfacl = getfacl
        self._run = run

    # ---- 账号凭证文件 ----

    def account_file(self, label: str) -> Path:
        if not LABEL_RE.fullmatch(label):
            raise PublishError(f"非法账号标签：{label!r}")
        return self.accounts_dir / label / "auth.json"

    def publish(self, label: str, data: bytes, readers: list[SysUser]) -> None:
        target = self.account_file(label)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.accounts_dir, 0o711)
        target.parent.mkdir(exist_ok=True)
        os.chmod(target.parent, 0o711)
        tmp = target.parent / _FILE_TMP
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._apply_acl(tmp, readers)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def set_readers(self, label: str, readers: list[SysUser]) -> None:
        self._apply_acl(self.account_file(label), readers)

    def read_readers(self, label: str) -> set[str]:
        out = self._run([self._getfacl, "--omit-header", "--absolute-names", str(self.account_file(label))])
        names = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split()[0].split(":")
            if len(parts) == 3 and parts[0] == "user" and parts[1] and "r" in parts[2]:
                names.add(parts[1])
        return names

    def _apply_acl(self, path: Path, readers: list[SysUser]) -> None:
        entries = ["u::rw-", "g::---", "o::---"]
        names = sorted({u.name for u in readers})
        if names:
            entries += [f"u:{n}:r--" for n in names] + ["m::r--"]
        self._run([self._setfacl, "--set", ",".join(entries), str(path)])

    # ---- 用户软链接 ----

    def ensure_codex_dir(self, user: SysUser) -> None:
        """创建或校验 ~/.codex：普通目录、0700、属主为该用户。"""
        fd = self._open_codex_dir(user, create=True)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _open_codex_dir(self, user: SysUser, create: bool) -> int:
        return self._open_user_dir(user, ".codex", create)

    def _open_user_dir(self, user: SysUser, name: str, create: bool) -> int:
        home_fd = os.open(user.home, os.O_RDONLY | os.O_DIRECTORY)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            try:
                fd = os.open(name, flags, dir_fd=home_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=home_fd)
                try:
                    fd = os.open(name, flags, dir_fd=home_fd)
                except OSError as e2:
                    if e2.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise PublishError(f"{user.home}/{name} 不是普通目录（可能是软链接），拒绝操作") from e2
                    raise
                st = os.fstat(fd)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid():
                    os.fchown(fd, user.uid, user.gid)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PublishError(f"{user.home}/{name} 不是普通目录（可能是软链接），拒绝操作") from e
                raise
        finally:
            os.close(home_fd)
        if os.fstat(fd).st_uid != user.uid:
            os.close(fd)
            raise PublishError(f"{user.home}/{name} 的属主不是 {user.name}，拒绝操作")
        return fd

    def link_skills(self, user: SysUser, target: Path) -> None:
        """替换用户的新旧 Skill 目录，目录操作不跟随用户提供的软链接。"""
        for name in (".codex", ".agents"):
            fd = self._open_user_dir(user, name, create=True)
            try:
                try:
                    if os.readlink("skills", dir_fd=fd) == str(target):
                        continue
                except OSError:
                    pass
                temporary = ".skills.codexctl.tmp"
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=fd)
                os.symlink(str(target), temporary, dir_fd=fd)
                try:
                    try:
                        existing = os.stat("skills", dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None and stat.S_ISDIR(existing.st_mode):
                        shutil.rmtree("skills", dir_fd=fd)
                    os.rename(temporary, "skills", src_dir_fd=fd, dst_dir_fd=fd)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=fd)
            finally:
                os.close(fd)

    def link_user(self, user: SysUser, label: str, now: int) -> Path | None:
        """把 ~/.codex/auth.json 原子地指向账号文件；原有普通文件改名备份并返回备份路径。"""
        target = str(self.account_file(label))
        fd = self._open_codex_dir(user, create=True)
        try:
            backup_name = None
            try:
                st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and not stat.S_ISLNK(st.st_mode):
                backup_name = f"{_LINK}.bak.{now}"
                n = 0
                while True:
                    try:
                        os.stat(backup_name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        break
                    n += 1
                    backup_name = f"{_LINK}.bak.{now}.{n}"
                os.rename(_LINK, backup_name, src_dir_fd=fd, dst_dir_fd=fd)
            try:
                os.unlink(_LINK_TMP, dir_fd=fd)
            except FileNotFoundError:
                pass
            except OSError as e:
                raise PublishError(f"无法清理 {user.home}/.codex/{_LINK_TMP}：{e}") from e
            os.symlink(target, _LINK_TMP, dir_fd=fd)
            # POSIX rename 原子替换目标；用 os.rename 而非 os.replace，因为前者在 os.supports_dir_fd 中有明确支持。
            os.rename(_LINK_TMP, _LINK, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        return user.home / ".codex" / backup_name if backup_name else None

    def unlink_user(self, user: SysUser) -> bool:
        """只删除软链接；普通文件（用户自己的）保持不动。用户可控的异常一律返回 False。"""
        try:
            fd = self._open_codex_dir(user, create=False)
        except (PublishError, OSError):
            return False
        try:
            st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISLNK(st.st_mode):
                return False
            os.unlink(_LINK, dir_fd=fd)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def read_link(self, user: SysUser) -> LinkState:
        try:
            fd = self._open_codex_dir(user, create=False)
        except FileNotFoundError:
            return LinkState("no_codex_dir")
        except (PublishError, OSError) as e:
            return LinkState("unsafe", str(e))
        try:
            try:
                st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return LinkState("missing")
            if stat.S_ISLNK(st.st_mode):
                return LinkState("symlink", os.readlink(_LINK, dir_fd=fd))
            return LinkState("file")
        except OSError as e:
            return LinkState("unsafe", str(e))
        finally:
            os.close(fd)
