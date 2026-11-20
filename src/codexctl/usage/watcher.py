from __future__ import annotations

import os
from dataclasses import dataclass

import codexctl.accounts.repository as account_repo
import codexctl.accounts.service as accounts
import codexctl.events as events
import codexctl.users.repository as user_repo
from codexctl.accounts.publisher import PublishError
from codexctl.context import Context
from codexctl.db import transaction
from codexctl.locks import LockBusy, account_lock
from codexctl.runner import CommandError
from codexctl.users.sysusers import UserError

ACCOUNT_WATCH_KINDS = frozenset({"acl_mismatch", "published_mismatch", "token_expiring"})
WATCH_KINDS = ACCOUNT_WATCH_KINDS | {"link_mismatch", "stale_link"}


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


def _check_accounts(ctx: Context, findings: list[Finding], *, reconcile_disabled: bool) -> dict[str, str]:
    now = ctx.now()
    checked_statuses = {}
    for acc in account_repo.list_accounts(ctx.conn):
        if acc.status == "disabled":
            try:
                with account_lock(ctx.settings.state_dir, acc.label, blocking=False):
                    fresh = account_repo.get_account(ctx.conn, acc.label)
                    if fresh is None or fresh.status != "disabled":
                        continue
                    checked_statuses[acc.label] = acc.status
                    message = _disabled_credential_issue(ctx, acc.label)
                    if message and reconcile_disabled:
                        ctx.publisher.revoke_readers(acc.label)
                    elif message:
                        findings.append(Finding("critical", "acl_mismatch", acc.label, message))
            except LockBusy:
                pass
            except (CommandError, OSError, PublishError) as e:
                checked_statuses[acc.label] = acc.status
                findings.append(Finding("critical", "acl_mismatch", acc.label, f"无法撤销停用账号的凭证读取权限：{e}"))
            continue
        checked_statuses[acc.label] = acc.status
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
        if acc.access_expires_at - now < 86400:
            findings.append(
                Finding("critical", "token_expiring", acc.label, "access_token 将在 24 小时内过期，刷新尚未成功")
            )
    return checked_statuses


def _disabled_credential_issue(ctx: Context, label: str) -> str | None:
    if ctx.publisher.has_pending_publish(label):
        return "停用账号仍有未清理的临时凭证文件"
    if ctx.publisher.account_file(label).exists():
        actual = ctx.publisher.read_readers(label)
        if actual:
            return f"停用账号仍有凭证读取权限：{sorted(actual)}"
    return None


def run_checks(ctx: Context, *, reconcile_disabled: bool = False) -> list[Finding]:
    """巡检并更新告警。守护进程同时收回停用账号遗留的读取权限。"""
    findings: list[Finding] = []
    _check_assigned_links(ctx, findings)
    _check_stale_links(ctx, findings)
    checked_statuses = _check_accounts(ctx, findings, reconcile_disabled=reconcile_disabled)
    now = ctx.now()
    with transaction(ctx.conn):
        current_accounts = account_repo.list_accounts(ctx.conn)
        checked_accounts = {acc.label for acc in current_accounts if acc.status == checked_statuses.get(acc.label)}
        unchecked_accounts = {acc.label for acc in current_accounts} - checked_accounts
        findings = [
            f for f in findings
            if f.kind not in ACCOUNT_WATCH_KINDS or f.target in checked_accounts
        ]
        problems = {(f.kind, f.target) for f in findings}
        for f in findings:
            events.raise_alert(ctx.conn, f.level, f.kind, f.target, f.message, now=now)
        marks = ",".join("?" * len(WATCH_KINDS))
        rows = ctx.conn.execute(
            f"SELECT kind, target FROM alerts WHERE resolved_at IS NULL AND kind IN ({marks})",
            sorted(WATCH_KINDS),
        ).fetchall()
        for r in rows:
            kind, target = r["kind"], r["target"]
            if kind in ACCOUNT_WATCH_KINDS and target in unchecked_accounts:
                continue
            if (kind, target) not in problems:
                events.resolve_alert(ctx.conn, kind, target, now=now)
    return findings
