import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl import accounts, assignments, db, repo
from codexctl.config import Settings
from codexctl.context import Context
from codexctl.publisher import Publisher
from codexctl.vault import Vault
from fakes import make_auth_json

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.geteuid() != 0 or shutil.which("setfacl") is None, reason="需要 root 与 acl"),
]

STATE = Path("/var/lib/codexctl-it")


def as_user(user, *cmd):
    return subprocess.run(["runuser", "-u", user, "--", *cmd], capture_output=True)


@pytest.fixture
def ctx():
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir()
    STATE.chmod(0o711)
    for name in ("alice", "bob"):
        shutil.rmtree(Path("/home") / name / ".codex", ignore_errors=True)
    s = Settings(state_dir=STATE)
    c = Context(
        settings=s,
        conn=db.connect(":memory:"),
        vault=Vault(AESGCM.generate_key(bit_length=256)),
        publisher=Publisher(s.accounts_dir),
        http=httpx.Client(),
    )
    accounts.import_account(c, "acc1", make_auth_json(account_id="a1", refresh_token="rt-a1"), "it")
    accounts.import_account(c, "acc2", make_auth_json(account_id="a2", refresh_token="rt-a2"), "it")
    yield c
    shutil.rmtree(STATE, ignore_errors=True)


def test_assigned_user_can_read_but_not_write(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    r = as_user("alice", "cat", "/home/alice/.codex/auth.json")
    assert r.returncode == 0 and b"rt-a1" in r.stdout
    w = as_user("alice", "sh", "-c", "echo x > /home/alice/.codex/auth.json")
    assert w.returncode != 0
    assert as_user("bob", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_switch_moves_access(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.assign(ctx, "alice", "acc2", "it")
    r = as_user("alice", "cat", "/home/alice/.codex/auth.json")
    assert r.returncode == 0 and b"rt-a2" in r.stdout
    assert as_user("alice", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_republish_keeps_access(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.assign(ctx, "bob", "acc1", "it")
    accounts.publish_account(ctx, repo.get_account(ctx.conn, "acc1"))
    for name in ("alice", "bob"):
        assert as_user(name, "cat", f"/home/{name}/.codex/auth.json").returncode == 0
    assert ctx.publisher.read_readers("acc1") == {"alice", "bob"}


def test_unassign_revokes(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.unassign(ctx, "alice", "it")
    assert not os.path.lexists("/home/alice/.codex/auth.json")
    assert as_user("alice", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_codex_dir_owned_by_user_link_by_root(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    # 安全修复：软链接刻意不 chown，保持 root 所有，用户无法改指向
    assert os.lstat("/home/alice/.codex/auth.json").st_uid == 0
    assert os.stat("/home/alice/.codex").st_uid == 1001
