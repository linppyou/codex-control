from __future__ import annotations

from . import codexinstall, events, sysusers, webauth
from .context import Context
from .db import transaction
from .publisher import PublishError
from .sysusers import SysUser, UserError


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
    return user, note
