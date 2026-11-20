from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import codexctl.accounts.repository as account_repo
import codexctl.events as events
import codexctl.users.repository as user_repo
from codexctl.accounts.authfile import AuthBundle
from codexctl.accounts.publisher import LABEL_RE
from codexctl.context import Context
from codexctl.db import transaction
from codexctl.locks import account_lock
from codexctl.users.sysusers import SysUser, UserError
from codexctl.vault import VaultError

ACCOUNT_ALERTS = (
    "refresh_failed", "refresh_retry", "publish_failed", "auth_rejected", "quota_high",
    "acl_mismatch", "published_mismatch", "token_expiring",
)


class AccountError(Exception):
    pass


def _aad(label: str) -> bytes:
    return f"codexctl:{label}".encode()


def _check_label(label: str) -> None:
    if not LABEL_RE.fullmatch(label):
        raise AccountError("账号标签只能包含小写字母、数字、- 和 _，以字母或数字开头，且不超过 32 个字符")


def check_new_label(ctx: Context, label: str) -> None:
    _check_label(label)
    existing = account_repo.get_account(ctx.conn, label, include_deleted=True)
    if existing is not None:
        detail = "（已删除，标签保留）" if existing.status == "deleted" else ""
        raise AccountError(f"账号标签已存在：{label}{detail}")


def require_account(ctx: Context, label: str) -> account_repo.Account:
    acc = account_repo.get_account(ctx.conn, label)
    if acc is None:
        raise AccountError(f"账号不存在：{label}")
    return acc


def readers_for(ctx: Context, account_pk: int) -> list[SysUser]:
    users = []
    for name in user_repo.active_usernames(ctx.conn, account_pk):
        try:
            users.append(ctx.lookup_user(name, ctx.settings.min_uid))
        except UserError:
            continue
    return users


def load_bundle(ctx: Context, acc: account_repo.Account) -> AuthBundle:
    plaintext = ctx.vault.decrypt(account_repo.load_secret(ctx.conn, acc.id), _aad(acc.label))
    return AuthBundle.from_bytes(plaintext)


def publish_account(ctx: Context, acc: account_repo.Account, bundle: AuthBundle | None = None) -> None:
    if not ctx.settings.manages_local_codex or acc.status in ("disabled", "deleted"):
        return
    bundle = bundle or load_bundle(ctx, acc)
    ctx.publisher.publish(acc.label, bundle.to_bytes(), readers_for(ctx, acc.id))


def republish_account(ctx: Context, label: str) -> None:
    """加账号锁后重新读取账号与保险库内容再发布，避免用过期 token 覆盖刚刷新的凭证。"""
    if not ctx.settings.manages_local_codex:
        raise AccountError("网关模式只使用保险库凭证")
    _check_label(label)
    with account_lock(ctx.settings.state_dir, label):
        acc = require_account(ctx, label)
        if acc.status == "disabled":
            raise AccountError(f"账号 {label} 已停用，不能重新发布")
        publish_account(ctx, acc)


def import_account(ctx: Context, label: str, data: bytes, actor: str) -> account_repo.Account:
    _check_label(label)
    bundle = AuthBundle.from_bytes(data)
    with account_lock(ctx.settings.state_dir, label):
        now = ctx.now()
        with transaction(ctx.conn):
            check_new_label(ctx, label)
            dup = account_repo.find_by_account_id(ctx.conn, bundle.account_id)
            if dup is not None:
                detail = "（已删除，账号标识保留）" if dup.status == "deleted" else ""
                raise AccountError(f"该 ChatGPT 账号已登记为 {dup.label}{detail}")
            pk = account_repo.insert_account(
                ctx.conn,
                label=label,
                email=bundle.email,
                account_id=bundle.account_id,
                plan_type=bundle.plan_type,
                last_refresh=bundle.last_refresh or now,
                access_expires_at=bundle.access_expires_at,
                now=now,
            )
            account_repo.store_secret(ctx.conn, pk, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
            events.audit(ctx.conn, actor, "account.add", label, f"email={bundle.email} plan={bundle.plan_type}", now=now)
        acc = require_account(ctx, label)
        publish_account(ctx, acc, bundle)
    return acc


def relogin_account(ctx: Context, label: str, data: bytes, actor: str) -> account_repo.Account:
    _check_label(label)
    bundle = AuthBundle.from_bytes(data)
    with account_lock(ctx.settings.state_dir, label):
        acc = require_account(ctx, label)
        if bundle.account_id != acc.account_id:
            raise AccountError(f"登录的是另一个 ChatGPT 账号（{bundle.email}），与 {label} 不一致")
        now = ctx.now()
        with transaction(ctx.conn):
            account_repo.store_secret(ctx.conn, acc.id, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
            account_repo.update_token_meta(
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


DISPLAY_NAME_MAX = 32


def set_display_name(ctx: Context, label: str, name: str, actor: str) -> str | None:
    """设置账号的备注名（只用于展示，不影响标签、凭证路径和用户链接）；空字符串表示清除。"""
    name = " ".join(name.split())
    if len(name) > DISPLAY_NAME_MAX:
        raise AccountError(f"名称不能超过 {DISPLAY_NAME_MAX} 个字符")
    if any(unicodedata.category(c)[0] == "C" for c in name):
        raise AccountError("名称不能包含控制字符")
    value = name or None
    now = ctx.now()
    with transaction(ctx.conn):
        acc = require_account(ctx, label)
        if value == acc.display_name:
            return value
        account_repo.set_display_name(ctx.conn, acc.id, value)
        events.audit(ctx.conn, actor, "account.rename", label, f"{acc.display_name or '-'} -> {value or '-'}", now=now)
    return value


def set_disabled(ctx: Context, label: str, disabled: bool, actor: str) -> None:
    _check_label(label)
    try:
        with account_lock(ctx.settings.state_dir, label, blocking=False):
            revoked_readers = False
            publishing = False
            try:
                with transaction(ctx.conn):
                    acc = require_account(ctx, label)
                    now = ctx.now()
                    if disabled:
                        if ctx.settings.manages_local_codex:
                            revoked_readers = ctx.publisher.revoke_readers(label) and acc.status != "disabled"
                        account_repo.disable_account(ctx.conn, acc.id)
                        for kind in ACCOUNT_ALERTS:
                            events.resolve_alert(ctx.conn, kind, label, now=now)
                    elif acc.status == "disabled":
                        account_repo.enable_account(ctx.conn, acc.id)
                        publishing = ctx.settings.manages_local_codex
                        publish_account(ctx, require_account(ctx, label))
                    events.audit(ctx.conn, actor, "account.disable" if disabled else "account.enable", label, now=now)
            except BaseException:
                # 数据库已回滚，在释放账号锁前恢复原状态对应的读者。
                if revoked_readers:
                    ctx.publisher.set_readers(label, readers_for(ctx, acc.id))
                elif publishing:
                    ctx.publisher.revoke_readers(label)
                raise
    except sqlite3.Error as e:
        raise AccountError(f"保存账号 {label} 的状态失败：{e}") from e
    except (LookupError, OSError, VaultError) as e:
        raise AccountError(f"更新账号 {label} 的凭证权限失败：{e}") from e


def delete_account(ctx: Context, label: str, confirmation: str, actor: str) -> None:
    _check_label(label)
    if confirmation != label:
        raise AccountError("请输入完整的账号标签确认删除")
    with account_lock(ctx.settings.state_dir, label, blocking=False), transaction(ctx.conn):
        acc = account_repo.get_account(ctx.conn, label, include_deleted=True)
        if acc is None:
            raise AccountError(f"账号不存在：{label}")
        if acc.status == "deleted":
            return
        if acc.status != "disabled":
            raise AccountError(f"请先停用账号 {label}")
        users = user_repo.active_usernames(ctx.conn, acc.id)
        if users:
            raise AccountError(f"请先转移或解除 {label} 的用户分配：{', '.join(users)}")
        if ctx.settings.manages_local_codex:
            try:
                ctx.publisher.unpublish(label)
            except OSError as e:
                raise AccountError(f"清理账号 {label} 的本机凭证失败，可重新删除：{e}") from e
        now = ctx.now()
        account_repo.delete_account(ctx.conn, acc.id)
        for kind in ACCOUNT_ALERTS:
            events.resolve_alert(ctx.conn, kind, label, now=now)
        events.audit(ctx.conn, actor, "account.delete", label, now=now)


def device_login(ctx: Context, label: str) -> bytes:
    """在临时 CODEX_HOME 中运行真实 codex 的 device-auth 登录（交互：验证码打印在终端），返回生成的 auth.json。"""
    if not ctx.settings.manages_local_codex:
        raise AccountError("网关模式请在本地电脑登录，再导入 auth.json")
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
