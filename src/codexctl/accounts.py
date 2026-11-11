from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .locks import account_lock
from . import events, repo
from .authfile import AuthBundle
from .context import Context
from .db import transaction
from .publisher import LABEL_RE
from .sysusers import SysUser, UserError


class AccountError(Exception):
    pass


def _aad(label: str) -> bytes:
    return f"codexctl:{label}".encode()


def _check_label(label: str) -> None:
    if not LABEL_RE.fullmatch(label):
        raise AccountError("账号标签只能包含小写字母、数字、- 和 _，以字母或数字开头，且不超过 32 个字符")


def check_new_label(ctx: Context, label: str) -> None:
    _check_label(label)
    if repo.get_account(ctx.conn, label) is not None:
        raise AccountError(f"账号标签已存在：{label}")


def require_account(ctx: Context, label: str) -> repo.Account:
    acc = repo.get_account(ctx.conn, label)
    if acc is None:
        raise AccountError(f"账号不存在：{label}")
    return acc


def readers_for(ctx: Context, account_pk: int) -> list[SysUser]:
    users = []
    for name in repo.active_usernames(ctx.conn, account_pk):
        try:
            users.append(ctx.lookup_user(name, ctx.settings.min_uid))
        except UserError:
            continue
    return users


def load_bundle(ctx: Context, acc: repo.Account) -> AuthBundle:
    plaintext = ctx.vault.decrypt(repo.load_secret(ctx.conn, acc.id), _aad(acc.label))
    return AuthBundle.from_bytes(plaintext)


def publish_account(ctx: Context, acc: repo.Account, bundle: AuthBundle | None = None) -> None:
    bundle = bundle or load_bundle(ctx, acc)
    ctx.publisher.publish(acc.label, bundle.to_bytes(), readers_for(ctx, acc.id))


def republish_account(ctx: Context, label: str) -> None:
    """加账号锁后重新读取账号与保险库内容再发布，避免用过期 token 覆盖刚刷新的凭证。"""
    require_account(ctx, label)
    with account_lock(ctx.settings.state_dir, label):
        acc = require_account(ctx, label)
        publish_account(ctx, acc, load_bundle(ctx, acc))


def import_account(ctx: Context, label: str, data: bytes, actor: str) -> repo.Account:
    _check_label(label)
    bundle = AuthBundle.from_bytes(data)
    if repo.get_account(ctx.conn, label) is not None:
        raise AccountError(f"账号标签已存在：{label}")
    dup = repo.find_by_account_id(ctx.conn, bundle.account_id)
    if dup is not None:
        raise AccountError(f"该 ChatGPT 账号已登记为 {dup.label}")
    now = ctx.now()
    with transaction(ctx.conn):
        pk = repo.insert_account(
            ctx.conn,
            label=label,
            email=bundle.email,
            account_id=bundle.account_id,
            plan_type=bundle.plan_type,
            last_refresh=bundle.last_refresh or now,
            access_expires_at=bundle.access_expires_at,
            now=now,
        )
        repo.store_secret(ctx.conn, pk, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
        events.audit(ctx.conn, actor, "account.add", label, f"email={bundle.email} plan={bundle.plan_type}", now=now)
    acc = require_account(ctx, label)
    publish_account(ctx, acc, bundle)
    return acc


def relogin_account(ctx: Context, label: str, data: bytes, actor: str) -> repo.Account:
    acc = require_account(ctx, label)
    bundle = AuthBundle.from_bytes(data)
    if bundle.account_id != acc.account_id:
        raise AccountError(f"登录的是另一个 ChatGPT 账号（{bundle.email}），与 {label} 不一致")
    with account_lock(ctx.settings.state_dir, label):
        now = ctx.now()
        with transaction(ctx.conn):
            repo.store_secret(ctx.conn, acc.id, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
            repo.update_token_meta(
                ctx.conn,
                acc.id,
                email=bundle.email,
                plan_type=bundle.plan_type,
                last_refresh=bundle.last_refresh or now,
                access_expires_at=bundle.access_expires_at,
            )
            for kind in ("refresh_failed", "refresh_retry"):
                events.resolve_alert(ctx.conn, kind, label, now=now)
            events.audit(ctx.conn, actor, "account.relogin", label, now=now)
        acc = require_account(ctx, label)
        publish_account(ctx, acc, bundle)
    return acc


def set_disabled(ctx: Context, label: str, disabled: bool, actor: str) -> None:
    acc = require_account(ctx, label)
    now = ctx.now()
    with transaction(ctx.conn):
        if disabled:
            repo.set_status(ctx.conn, acc.id, "disabled")
        elif acc.status == "disabled":
            repo.set_status(ctx.conn, acc.id, "active")
        events.audit(ctx.conn, actor, "account.disable" if disabled else "account.enable", label, now=now)


def device_login(ctx: Context, label: str) -> bytes:
    """在临时 CODEX_HOME 中运行真实 codex 的 device-auth 登录（交互：验证码打印在终端），返回生成的 auth.json。"""
    _check_label(label)
    ctx.settings.staging_dir.mkdir(parents=True, exist_ok=True)
    ctx.settings.staging_dir.chmod(0o700)
    home = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=ctx.settings.staging_dir))
    try:
        env = {**os.environ, "CODEX_HOME": str(home)}
        try:
            result = subprocess.run([str(ctx.settings.codex_bin), "login", "--device-auth"], env=env, check=False)
        except OSError as e:
            raise AccountError(f"无法运行 codex：{e}") from e
        if result.returncode != 0:
            raise AccountError(f"codex login 失败，退出码 {result.returncode}")
        auth = home / "auth.json"
        if not auth.exists():
            raise AccountError("codex login 结束但没有生成 auth.json")
        return auth.read_bytes()
    finally:
        shutil.rmtree(home, ignore_errors=True)
