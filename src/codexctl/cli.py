from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Callable

from . import accounts, assignments, daemon, db, locks, refresher, repo, selfcheck, watcher
from .authfile import AuthFileError
from .config import Settings, load_settings
from .context import Context, open_context
from .publisher import PublishError
from .runner import CommandError
from .sysusers import UserError
from .vault import VaultError, generate_key_file

DEFAULT_CONFIG = Path("/etc/codexctl/config.toml")
ACTOR = "cli"
_HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, VaultError, CommandError, locks.LockBusy, OSError,
)


def _safe(s: object) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(s))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codexctl", description="Codex 账号集中管理")
    p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="初始化目录、主密钥和数据库")
    sub.add_parser("selfcheck", help="检查运行环境")

    acc = sub.add_parser("account", help="账号管理").add_subparsers(dest="acmd", required=True)
    acc.add_parser("list", help="列出账号")
    a = acc.add_parser("add", help="通过 device-auth 登录并添加账号")
    a.add_argument("label")
    a = acc.add_parser("import", help="从 auth.json 文件导入账号")
    a.add_argument("label")
    a.add_argument("file", type=Path)
    a = acc.add_parser("relogin", help="重新登录已有账号（不改用户链接）")
    a.add_argument("label")
    a.add_argument("--file", type=Path, help="从文件导入，而不是运行 device-auth")
    for name, help_text in (
        ("refresh", "立即刷新 token"), ("disable", "停用"), ("enable", "启用"), ("republish", "按保险库重新发布凭证文件"),
    ):
        a = acc.add_parser(name, help=help_text)
        a.add_argument("label")

    a = sub.add_parser("assign", help="把账号分配给用户（已分配则切换）")
    a.add_argument("--account", required=True)
    a.add_argument("users", nargs="+")
    a = sub.add_parser("unassign", help="回收用户的账号")
    a.add_argument("users", nargs="+")
    a = sub.add_parser("fix", help="按数据库记录修复用户链接与 ACL")
    a.add_argument("users", nargs="+")
    sub.add_parser("users", help="列出系统用户及其分配")
    sub.add_parser("quota", help="显示各账号最新额度")
    a = sub.add_parser("usage", help="按用户汇总用量")
    a.add_argument("--days", type=int, default=7)
    sub.add_parser("check", help="运行一次巡检")
    sub.add_parser("daemon", help="运行后台定时任务")
    return p


def main(argv: list[str] | None = None, context_factory: Callable[[Settings], Context] = open_context) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    if args.cmd == "init":
        return cmd_init(settings)
    if args.cmd == "selfcheck":
        return report(selfcheck.selfcheck(settings))
    try:
        ctx = context_factory(settings)
        return dispatch(ctx, args)
    except _HANDLED as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def cmd_init(settings: Settings) -> int:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.chmod(0o711)
    settings.accounts_dir.mkdir(exist_ok=True)
    settings.accounts_dir.chmod(0o711)
    settings.staging_dir.mkdir(exist_ok=True)
    settings.staging_dir.chmod(0o700)
    if not settings.key_file.exists():
        generate_key_file(settings.key_file)
        print(f"已生成主密钥 {settings.key_file}，请单独备份；丢失后所有账号需重新登录")
    db.connect(settings.db_path).close()
    settings.db_path.chmod(0o600)
    return report(selfcheck.selfcheck(settings))


def report(problems: list[str]) -> int:
    if not problems:
        print("自检通过")
        return 0
    for p in problems:
        print(f"✗ {p}", file=sys.stderr)
    return 1


def window_name(minutes: int | None) -> str:
    if minutes is None:
        return "未知窗口"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}天"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时"
    return f"{minutes}分钟"


def fmt_ts(ts: int | None) -> str:
    return "-" if ts is None else time.strftime("%m-%d %H:%M", time.localtime(ts))


def dispatch(ctx: Context, args: argparse.Namespace) -> int:
    if args.cmd == "account":
        return account_cmd(ctx, args)
    if args.cmd == "assign":
        for name in args.users:
            backup = assignments.assign(ctx, name, args.account, ACTOR)
            print(f"{name} -> {args.account}" + (f"（原 auth.json 已备份为 {backup}）" if backup else ""))
        print("提示：用户需重启正在运行的 codex 才会使用新账号")
        return 0
    if args.cmd == "unassign":
        for name in args.users:
            done = assignments.unassign(ctx, name, ACTOR)
            print(f"{name}: {'已回收' if done else '没有生效的分配'}")
        return 0
    if args.cmd == "fix":
        for name in args.users:
            assignments.reconcile_user(ctx, name, ACTOR)
            print(f"{name}: 已按记录修复")
        return 0
    if args.cmd == "users":
        for user in ctx.list_users(ctx.settings.min_uid):
            a = repo.active_assignment(ctx.conn, user.name)
            label = repo.get_account_by_pk(ctx.conn, a.account_pk).label if a else "-"
            print(f"{_safe(user.name):<20} {_safe(label):<12} {_safe(ctx.publisher.read_link(user).kind)}")
        return 0
    if args.cmd == "quota":
        for acc in repo.list_accounts(ctx.conn):
            rows = repo.latest_quota(ctx.conn, acc.id, api_fresh_since=ctx.now() - 600)
            parts = [
                f"{window_name(r['window_minutes'])} {r['used_percent']:.0f}% 重置于 {fmt_ts(r['resets_at'])} ({r['source']})"
                for r in rows
            ] or ["暂无数据"]
            print(f"{acc.label:<12} {acc.status:<14} " + " | ".join(parts))
        return 0
    if args.cmd == "usage":
        for r in repo.usage_summary(ctx.conn, ctx.now() - args.days * 86400):
            print(
                f"{r['username']:<20} {r['label'] or '-':<12} total={r['total']} "
                f"input={r['input']} output={r['output']} turns={r['turns']}"
            )
        return 0
    if args.cmd == "check":
        findings = watcher.run_checks(ctx)
        for f in findings:
            print(_safe(f"[{f.level}] {f.kind} {f.target}: {f.message}"))
        print(f"发现 {len(findings)} 个问题" if findings else "巡检正常")
        return 1 if findings else 0
    if args.cmd == "daemon":
        daemon.run(ctx)
        return 0
    raise AssertionError(f"unhandled command {args.cmd}")


def account_cmd(ctx: Context, args: argparse.Namespace) -> int:
    c = args.acmd
    if c == "list":
        for acc in repo.list_accounts(ctx.conn):
            users = len(repo.active_usernames(ctx.conn, acc.id))
            line = (
                f"{acc.label:<12} {acc.email or '-':<28} {acc.plan_type or '-':<6} {acc.status:<14} "
                f"用户数={users} 上次刷新={fmt_ts(acc.last_refresh)} token到期={fmt_ts(acc.access_expires_at)}"
            )
            print(line + (f" 错误={acc.last_error}" if acc.last_error else ""))
        return 0
    if c == "add":
        acc = accounts.import_account(ctx, args.label, accounts.device_login(ctx, args.label), ACTOR)
        print(f"已添加 {acc.label}（{acc.email}）")
        return 0
    if c == "import":
        acc = accounts.import_account(ctx, args.label, args.file.read_bytes(), ACTOR)
        print(f"已导入 {acc.label}（{acc.email}）")
        return 0
    if c == "relogin":
        data = args.file.read_bytes() if args.file else accounts.device_login(ctx, args.label)
        accounts.relogin_account(ctx, args.label, data, ACTOR)
        print(f"{args.label} 已重新登录并发布")
        return 0
    if c == "refresh":
        try:
            ok = refresher.refresh_account(ctx, accounts.require_account(ctx, args.label), ACTOR, blocking=False)
        except locks.LockBusy:
            print(f"错误：账号 {args.label} 正在被刷新，请稍后再试", file=sys.stderr)
            return 1
        print("刷新成功" if ok else f"刷新失败：{repo.get_account(ctx.conn, args.label).last_error}")
        return 0 if ok else 1
    if c in ("disable", "enable"):
        accounts.set_disabled(ctx, args.label, c == "disable", ACTOR)
        print(f"{args.label} 已{'停用' if c == 'disable' else '启用'}")
        return 0
    if c == "republish":
        accounts.publish_account(ctx, accounts.require_account(ctx, args.label))
        print(f"{args.label} 已重新发布")
        return 0
    raise AssertionError(f"unhandled account command {c}")
