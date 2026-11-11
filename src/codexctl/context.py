from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from . import db, sysusers
from .config import Settings
from .publisher import Publisher
from .sysusers import SysUser
from .vault import Vault, load_key


@dataclass
class Context:
    settings: Settings
    conn: sqlite3.Connection
    vault: Vault
    publisher: Publisher
    http: httpx.Client
    lookup_user: Callable[[str, int], SysUser] = sysusers.lookup_user
    list_users: Callable[[int], list[SysUser]] = sysusers.list_users
    clock: Callable[[], float] = time.time

    def now(self) -> int:
        return int(self.clock())


def open_context(settings: Settings) -> Context:
    return Context(
        settings=settings,
        conn=db.connect(settings.db_path),
        vault=Vault(load_key(settings.key_file)),
        publisher=Publisher(settings.accounts_dir, settings.setfacl_bin, settings.getfacl_bin),
        http=httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": "codexctl/0.1"}),
    )
