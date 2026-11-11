from __future__ import annotations

import shutil
import sqlite3
from typing import Callable

from . import db
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
    acl_tools = True
    for tool in (settings.setfacl_bin, settings.getfacl_bin):
        if which(tool) is None:
            problems.append(f"未找到 {tool}，请执行 apt install acl")
            acl_tools = False
    if not settings.codex_bin.exists():
        problems.append(f"未找到 codex 可执行文件：{settings.codex_bin}")
    try:
        load_key(settings.key_file)
    except (OSError, VaultError) as e:
        problems.append(f"主密钥不可用：{e}")
    if not settings.state_dir.is_dir():
        problems.append(f"状态目录不存在：{settings.state_dir}（请先运行 codexctl init）")
        return problems
    try:
        db.connect(settings.db_path).close()
    except sqlite3.Error as e:
        problems.append(f"数据库不可用：{e}")
    if acl_tools:
        probe = settings.state_dir / ".acl-probe"
        try:
            probe.write_bytes(b"")
            run([settings.setfacl_bin, "-m", "u:root:r", str(probe)])
        except (OSError, CommandError) as e:
            problems.append(f"{settings.state_dir} 所在文件系统不支持 ACL：{e}")
        finally:
            probe.unlink(missing_ok=True)
    return problems
