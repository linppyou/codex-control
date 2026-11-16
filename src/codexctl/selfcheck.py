from __future__ import annotations

import os
import shutil
import sqlite3
from typing import Callable

from . import db, webauth
from . import runner as runner_mod
from .config import Settings
from .runner import CommandError, Runner
from .vault import VaultError, load_key


def selfcheck(
    settings: Settings,
    run: Runner = runner_mod.run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    problems: list[str] = []
    acl_tools = settings.manages_local_codex
    if acl_tools:
        for tool in (settings.setfacl_bin, settings.getfacl_bin):
            if which(tool) is None:
                problems.append(f"未找到 {tool}，请执行 apt install acl")
                acl_tools = False
        if not settings.codex_bin.exists():
            problems.append(f"未找到 codex 可执行文件：{settings.codex_bin}")
        elif not os.access(settings.codex_bin, os.X_OK):
            problems.append(f"codex 不可执行：{settings.codex_bin}")
    try:
        load_key(settings.key_file)
    except (OSError, VaultError) as e:
        problems.append(f"主密钥不可用：{e}")
    if not settings.state_dir.is_dir():
        problems.append(f"状态目录不存在：{settings.state_dir}（请先运行 codexctl init）")
        return problems
    try:
        conn = db.connect(settings.db_path)
        try:
            if settings.gateway_enabled and not webauth.has_configured_admin(conn):
                problems.append("请通过 codexctl admin create 或 passwd admin 配置管理员密码")
        finally:
            conn.close()
    except sqlite3.Error as e:
        problems.append(f"数据库不可用：{e}")
    if acl_tools:
        probe_dir = settings.accounts_dir if settings.accounts_dir.is_dir() else settings.state_dir
        probe = probe_dir / ".acl-probe"
        try:
            probe.write_bytes(b"")
            run([settings.setfacl_bin, "-m", "u:root:r", str(probe)])
        except (OSError, CommandError) as e:
            problems.append(f"{probe_dir} 所在文件系统不支持 ACL：{e}")
        finally:
            probe.unlink(missing_ok=True)
    return problems
