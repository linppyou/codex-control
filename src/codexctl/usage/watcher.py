from __future__ import annotations

import os
from dataclasses import dataclass

import codexctl.accounts.repository as account_repo
import codexctl.accounts.service as accounts
import codexctl.events as events
import codexctl.users.repository as user_repo
from codexctl.context import Context
from codexctl.db import transaction
from codexctl.runner import CommandError
from codexctl.users.sysusers import UserError

WATCH_KINDS = frozenset({"link_mismatch", "stale_link", "acl_mismatch", "published_mismatch", "token_expiring"})


@dataclass(frozen=True)
class Finding:
    level: str
    kind: str
    target: str
    message: str


def _check_assigned_links(ctx: Context, findings: list) -> None:
    for a in user_repo.all_active(ctx.conn):
        try:
            user = ctx.lookup_user(a.username, ctx.settings.min_uid)
        except UserError:
            findings.append(Finding("warning", "link_mismatch", a.username, "系统中已找不到该用户，请回收分配"))
            continue
        acc = account_repo.get_account_by_pk(ctx.conn, a.account_pk)
        expected = str(ctx.publisher.account_file(acc.label))
        state = ctx.publisher.read_link(user)
        if state.kind != "symlink" or state.target != expected:
            actual = f"{state.kind} {state.target or ''}".strip()
            findings.append(Finding("critical", "link_mismatch", a.username, f"应链接到 {expected}，实际为 {actual}"))


def _check_stale_links(ctx: Context, findings: list) -> None:
    assigned = {a.username for a in user_repo.all_active(ctx.conn)}
    prefix = str(ctx.publisher.accounts_dir) + os.sep
    for user in ctx.list_users(ctx.settings.min_uid):
        if user.name in assigned:
            continue
        state = ctx.publisher.read_link(user)
        target = state.target or ""
        if state.kind == "symlink" and os.path.isabs(target) and os.path.normpath(target).startswith(prefix):
            findings.append(Finding("warning", "stale_link", user.name, f"未分配账号但仍链接到 {state.target}"))


def _check_accounts(ctx: Context, findings: list) -> None:
    now = ctx.now()
    for acc in account_repo.list_accounts(ctx.conn):
        expected = {u.name for u in accounts.readers_for(ctx, acc.id)}
        try:
            actual = ctx.publisher.read_readers(acc.label)
            if actual != expected:
                findings.append(
                    Finding("critical", "acl_mismatch", acc.label, f"ACL 读者为 {sorted(actual)}，应为 {sorted(expected)}")
                )
        except (CommandError, OSError) as e:
            findings.append(Finding("critical", "acl_mismatch", acc.label, f"无法读取 ACL：{e}"))
        try:
            published = ctx.publisher.account_file(acc.label).read_bytes()
        except OSError:
            published = None
        try:
            vault_bytes = accounts.load_bundle(ctx, acc).to_bytes()
        except Exception as e:
            findings.append(
                Finding("critical", "published_mismatch", acc.label, f"无法读取保险库：{type(e).__name__}")
            )
        else:
            if published != vault_bytes:
                findings.append(
                    Finding("critical", "published_mismatch", acc.label, "发布文件与保险库不一致，可执行 account republish")
                )
        if acc.status != "disabled" and acc.access_expires_at - now < 86400:
            findings.append(
                Finding("critical", "token_expiring", acc.label, "access_token 将在 24 小时内过期，刷新尚未成功")
            )


def run_checks(ctx: Context) -> list[Finding]:
    """执行全部巡检，只告警不修复：新问题打开告警，恢复正常的项关闭告警。"""
    findings: list[Finding] = []
    _check_assigned_links(ctx, findings)
    _check_stale_links(ctx, findings)
    _check_accounts(ctx, findings)
    now = ctx.now()
    problems = {(f.kind, f.target) for f in findings}
    with transaction(ctx.conn):
        for f in findings:
            events.raise_alert(ctx.conn, f.level, f.kind, f.target, f.message, now=now)
        marks = ",".join("?" * len(WATCH_KINDS))
        rows = ctx.conn.execute(
            f"SELECT kind, target FROM alerts WHERE resolved_at IS NULL AND kind IN ({marks})",
            sorted(WATCH_KINDS),
        ).fetchall()
        for r in rows:
            if (r["kind"], r["target"]) not in problems:
                events.resolve_alert(ctx.conn, r["kind"], r["target"], now=now)
    return findings
