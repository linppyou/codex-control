from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl import db
from codexctl.config import Settings
from codexctl.context import Context
from codexctl.publisher import Publisher
from codexctl.vault import Vault
from fakes import FakeClock, FakeRunner, FakeUsers


@pytest.fixture
def ctx(tmp_path):
    """完整的测试 Context：内存数据库、随机主密钥、假命令执行器、假用户表、假时钟、HTTP 路由表。

    测试通过以下附加属性操控替身：
      ctx.test_runner  —— FakeRunner（记录 setfacl/getfacl 调用）
      ctx.test_users   —— FakeUsers（add("alice") 创建用户）
      ctx.test_clock   —— FakeClock（修改 .t 推进时间）
      ctx.test_routes  —— {(method, url): handler(request) -> httpx.Response}
    """
    settings = Settings(
        state_dir=tmp_path / "state",
        key_file=tmp_path / "master.key",
        codex_bin=tmp_path / "codex",
        codex_wrapper=tmp_path / "bin" / "codex",
        codex_requirements=tmp_path / "etc" / "requirements.toml",
    )
    settings.state_dir.mkdir()
    settings.codex_bin.write_text("#!/bin/sh\nexit 0\n")
    settings.codex_bin.chmod(0o755)
    runner = FakeRunner()
    users = FakeUsers(tmp_path / "home")
    clock = FakeClock()
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        fn = routes.get((request.method, str(request.url)))
        return fn(request) if fn else httpx.Response(404)

    c = Context(
        settings=settings,
        conn=db.connect(":memory:"),
        vault=Vault(AESGCM.generate_key(bit_length=256)),
        publisher=Publisher(settings.accounts_dir, run=runner),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        lookup_user=users.lookup,
        list_users=users.list,
        user_exists=users.exists,
        create_user=users.create,
        clock=clock,
    )
    c.test_runner = runner
    c.test_users = users
    c.test_clock = clock
    c.test_routes = routes
    return c
