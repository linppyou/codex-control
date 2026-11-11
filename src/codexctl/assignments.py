from __future__ import annotations

import os
from pathlib import Path

from . import accounts, events, repo
from .context import Context
from .db import transaction
from .sysusers import UserError


class AssignError(Exception):
    pass


def _sync_acl(ctx: Context, account_pk: int) -> None:
    acc = repo.get_account_by_pk(ctx.conn, account_pk)
    ctx.publisher.set_readers(acc.label, accounts.readers_for(ctx, account_pk))


def assign(ctx: Context, username: str, label: str, actor: str) -> Path | None:
    """分配或切换。顺序：写数据库 → 给新账号加读权限 → 改链接 → 撤销旧账号读权限。返回备份文件路径（如有）。"""
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    acc = repo.get_account(ctx.conn, label)
    if acc is None:
        raise AssignError(f"账号不存在：{label}")
    if acc.status == "disabled":
        raise AssignError(f"账号 {label} 已停用，不能分配")
    current = repo.active_assignment(ctx.conn, username)
    switching = current is not None and current.account_pk != acc.id
    now = ctx.now()
    if current is None or switching:
        with transaction(ctx.conn):
            old_label = "-"
            if current is not None:
                old_label = repo.get_account_by_pk(ctx.conn, current.account_pk).label
                repo.end_assignment(ctx.conn, username, now)
            repo.start_assignment(ctx.conn, username, acc.id, now)
            events.audit(ctx.conn, actor, "assign", username, f"{old_label} -> {label}", now=now)
    try:
        _sync_acl(ctx, acc.id)
        backup = ctx.publisher.link_user(user, label, now)
    finally:
        if switching:
            # 即使改链接失败，也必须撤销旧账号的读权限
            _sync_acl(ctx, current.account_pk)
    return backup


def unassign(ctx: Context, username: str, actor: str) -> bool:
    """回收：删除软链接并撤销读权限。绝不调用 codex logout（会吊销同账号所有用户共用的 token）。"""
    current = repo.active_assignment(ctx.conn, username)
    if current is None:
        return False
    now = ctx.now()
    with transaction(ctx.conn):
        repo.end_assignment(ctx.conn, username, now)
        label = repo.get_account_by_pk(ctx.conn, current.account_pk).label
        events.audit(ctx.conn, actor, "unassign", username, label, now=now)
    try:
        try:
            user = ctx.lookup_user(username, ctx.settings.min_uid)
        except UserError:
            user = None
        if user is not None:
            ctx.publisher.unlink_user(user)
    finally:
        # 无论查找用户或删除链接是否出错，都必须撤销读权限
        _sync_acl(ctx, current.account_pk)
    return True


def reconcile_user(ctx: Context, username: str, actor: str) -> None:
    """按数据库记录修复用户的链接与对应账号的 ACL（管理员的"一键修复"）。"""
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    current = repo.active_assignment(ctx.conn, username)
    now = ctx.now()
    # 同步全部账号的 ACL，修复此前 setfacl 失败遗留的撤销
    for a in repo.list_accounts(ctx.conn):
        _sync_acl(ctx, a.id)
    if current is not None:
        acc = repo.get_account_by_pk(ctx.conn, current.account_pk)
        ctx.publisher.link_user(user, acc.label, now)
    else:
        state = ctx.publisher.read_link(user)
        prefix = str(ctx.publisher.accounts_dir) + os.sep
        target = state.target or ""
        if os.path.isabs(target):
            target = os.path.normpath(target)
        if state.kind == "symlink" and os.path.isabs(target) and target.startswith(prefix):
            ctx.publisher.unlink_user(user)
    with transaction(ctx.conn):
        events.audit(ctx.conn, actor, "fix", username, now=now)
