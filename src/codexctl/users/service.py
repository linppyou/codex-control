from __future__ import annotations

import codexctl.events as events
import codexctl.gateway.storage as storage
import codexctl.sites.access as site_access
import codexctl.users.codexinstall as codexinstall
import codexctl.users.sysusers as sysusers
import codexctl.users.webauth as webauth
from codexctl.accounts.publisher import PublishError
from codexctl.context import Context
from codexctl.db import transaction
from codexctl.users.sysusers import SysUser, UserError


class UserAdminError(Exception):
    pass


def ensure_codex(ctx: Context) -> str:
    return codexinstall.ensure_codex(ctx.settings, ctx.http)


def validate_new_user(ctx: Context, name: str, password: str, confirm: str, *, panel_login: bool = False) -> None:
    if not sysusers.valid_name(name):
        raise UserError(f"非法用户名：{name!r}")
    if panel_login and name in {"admin", "root"}:
        raise UserError(f"{name} 是保留的面板用户名")
    sysusers.validate_password(password)
    if password != confirm:
        raise UserError("两次输入的密码不一致")
    if ctx.user_exists(name):
        raise UserAdminError(f"用户已存在：{name}")


def create(
    ctx: Context, name: str, password: str, confirm: str, actor: str, *,
    codex_note: str | None = None, panel_login: bool = False,
) -> tuple[SysUser, str]:
    """创建 Linux 用户、设置密码、确保 codex 可用并初始化 ~/.codex。不包含密码的持久化。"""
    validate_new_user(ctx, name, password, confirm, panel_login=panel_login)
    note = ensure_codex(ctx) if codex_note is None else codex_note
    user = ctx.create_user(name, password)
    try:
        ctx.publisher.ensure_codex_dir(user)
    except (PublishError, OSError) as e:
        raise UserAdminError(f"用户 {name} 已创建，但初始化 {user.home}/.codex 失败：{e}") from e
    detail = f"uid={user.uid} home={user.home}"
    if note:
        detail += f"；{note}"
    with transaction(ctx.conn):
        if panel_login:
            webauth.enroll_system_user(ctx.conn, user.name, user.uid)
        events.audit(ctx.conn, actor, "user.create", name, detail, now=ctx.now())
        if ctx.settings.gateway_enabled:
            storage.ensure_user_key(ctx.conn, ctx.vault, name, actor, ctx.now())
    if ctx.settings.site_ssh_enabled and ctx.settings.manages_local_codex and name not in ctx.settings.hidden_users:
        try:
            site_access.provision(ctx.settings, user)
        except (OSError, ValueError) as e:
            raise UserAdminError(
                f"用户 {name} 已创建，但站点 SSH 开通失败：{e}。可用 codexctl user site-ssh {name} 重试"
            ) from e
        ssh_note = (
            "已配置共享站点 SSH 密钥" if ctx.settings.site_ssh_shared
            else "已签发站点 SSH 证书" if ctx.settings.site_ssh_ca_key is not None
            else "已生成站点 SSH 密钥，需在目标服务器授权公钥"
        )
        note = "；".join(filter(None, (note, ssh_note)))
    return user, note
