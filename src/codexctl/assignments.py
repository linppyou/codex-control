from __future__ import annotations

import os
from pathlib import Path

from . import accounts, events, procs, repo
from .context import Context
from .db import transaction
from .locks import account_lock
from .sysusers import UserError


class AssignError(Exception):
    pass


def _sync_acl(ctx: Context, account_pk: int) -> None:
    acc = repo.get_account_by_pk(ctx.conn, account_pk)
    # 与刷新/重新发布互斥，避免读者列表在并发发布中被旧值覆盖（调用方不得已持有同一账号的锁）
    with account_lock(ctx.settings.state_dir, acc.label):
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


def stale_codex_procs(ctx: Context, username: str) -> list[procs.CodexProc]:
    """该用户仍在使用旧账号的 codex 进程（启动早于最近一次分配变化）。"""
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    return procs.stale_procs(
        procs.list_codex_procs(user.uid, ctx.settings.codex_bin.parent, home=user.home),
        repo.last_assignment_change(ctx.conn, username),
    )


def restart_codex(ctx: Context, username: str, actor: str) -> tuple[list[int], list[int]]:
    """结束该用户的旧 codex 进程，返回 (已结束, 无法结束) 的 pid 列表。"""
    stale = stale_codex_procs(ctx, username)
    if not stale:
        return [], []
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    # 防止 pid 复用误杀：只结束进程号与启动时间都未变的进程。
    current = {
        (p.pid, p.started_at)
        for p in procs.list_codex_procs(user.uid, ctx.settings.codex_bin.parent, home=user.home)
    }
    pids = [p.pid for p in stale if (p.pid, p.started_at) in current]
    if not pids:
        return [], []
    ended, failed = procs.terminate(pids)
    if ended:
        with transaction(ctx.conn):
            events.audit(
                ctx.conn, actor, "restart_codex", username, ",".join(str(p) for p in ended), now=ctx.now()
            )
    return ended, failed


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
