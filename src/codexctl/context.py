from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

import httpx

import codexctl.db as db
import codexctl.users.sysusers as sysusers
from codexctl.accounts.publisher import Publisher
from codexctl.config import Settings
from codexctl.users.sysusers import SysUser
from codexctl.vault import Vault, load_key


@dataclass
class Context:
    settings: Settings
    conn: sqlite3.Connection
    vault: Vault
    publisher: Publisher
    http: httpx.Client
    lookup_user: Callable[[str, int], SysUser] = sysusers.lookup_user
    list_users: Callable[[int], list[SysUser]] = sysusers.list_users
    user_exists: Callable[[str], bool] = sysusers.user_exists
    create_user: Callable[[str, str], SysUser] = sysusers.create_user
    clock: Callable[[], float] = time.time

    def now(self) -> int:
        return int(self.clock())


def open_context(settings: Settings) -> Context:
    def create_user(name: str, password: str) -> SysUser:
        return sysusers.create_user(name, password, min_uid=settings.min_uid)

    return Context(
        settings=settings,
        conn=db.connect(settings.db_path),
        vault=Vault(load_key(settings.key_file)),
        publisher=Publisher(settings.accounts_dir, settings.setfacl_bin, settings.getfacl_bin),
        http=httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": "codexctl/0.1"}),
        create_user=create_user,
    )
