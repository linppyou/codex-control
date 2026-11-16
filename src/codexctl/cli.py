from __future__ import annotations

import argparse
import getpass
import logging
import sqlite3
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable

from . import (
    accounts, assignments, codexinstall, daemon, db, dispatch_worker, locks, logscan, pricing, refresher, repo,
    selfcheck, useradmin, watcher, webauth,
)
from .authfile import AuthFileError
from .config import Settings, load_settings
from .context import Context, open_context
from .publisher import PublishError
from .runner import CommandError
from .sysusers import UserError
from .useradmin import UserAdminError
from .vault import VaultError, generate_key_file

DEFAULT_CONFIG = Path("/etc/codexctl/config.toml")
ACTOR = "cli"
_HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, VaultError, CommandError, webauth.AuthError, locks.LockBusy, OSError,
    sqlite3.Error, LookupError, UserAdminError, codexinstall.CodexInstallError,
)


def _safe(s: object) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(s))


def _positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("必须 >= 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codexctl", description="Codex 账号集中管理")
    p.add_argument("-c", "--config", type=Path, default=None)
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
    a = acc.add_parser("name", help="设置账号的备注名（只用于展示，留空则清除）")
    a.add_argument("label")
    a.add_argument("name", nargs="?", default="")
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
    a = sub.add_parser("restart-codex", help="结束用户仍在使用旧账号的 codex 进程")
    a.add_argument("users", nargs="+")
    sub.add_parser("users", help="列出系统用户及其分配")
    usr = sub.add_parser("user", help="创建 Linux 用户").add_subparsers(dest="ucmd", required=True)
    a = usr.add_parser("add", help="创建用户、设置密码并安装/初始化 codex")
    a.add_argument("name")
    sub.add_parser("quota", help="显示各账号最新额度")
    a = sub.add_parser("usage", help="按用户汇总用量")
    a.add_argument("--days", type=_positive_int, default=7)
    sub.add_parser("backfill-models", help="根据会话日志给历史用量补上模型名")
    sub.add_parser("check", help="运行一次巡检")
    sub.add_parser("daemon", help="运行后台定时任务")
    sub.add_parser("web", help="运行 Web 面板")
    sub.add_parser("dispatch-worker", help="将人工确认的任务发送给 Codex")
    adm = sub.add_parser("admin", help="Web 面板管理员").add_subparsers(dest="admcmd", required=True)
    adm.add_parser("list", help="列出管理员")
    for name, help_text in (("create", "创建管理员"), ("passwd", "修改管理员密码")):
        a = adm.add_parser(name, help=help_text)
        a.add_argument("name")
    return p


def main(argv: list[str] | None = None, context_factory: Callable[[Settings], Context] = open_context) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.config is not None and not args.config.exists():
            print(f"错误：配置文件不存在：{args.config}", file=sys.stderr)
            return 1
        config = args.config if args.config is not None else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
        settings = load_settings(config)
        if args.cmd == "init":
            return cmd_init(settings)
        if args.cmd == "selfcheck":
            return report(selfcheck.selfcheck(settings))
        if args.cmd == "admin":
            return admin_cmd(settings, args)
        ctx = context_factory(settings)
        return dispatch(ctx, args)
    except (*_HANDLED, tomllib.TOMLDecodeError, ValueError) as e:
        print(f"错误：{_safe(e)}", file=sys.stderr)
        return 1


def cmd_init(settings: Settings) -> int:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.chmod(0o711 if settings.manages_local_codex else 0o700)
    if settings.manages_local_codex:
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


def admin_cmd(settings: Settings, args: argparse.Namespace) -> int:
    conn = db.connect(settings.db_path)
    try:
        if args.admcmd == "list":
            for name in webauth.list_admins(conn):
                print(_safe(name))
            return 0
        password = getpass.getpass("密码：")
        if getpass.getpass("再输入一次：") != password:
            print("错误：两次输入的密码不一致", file=sys.stderr)
            return 1
        now = int(time.time())
        if args.admcmd == "create":
            webauth.create_admin(conn, args.name, password, now=now)
            print(_safe(f"已创建管理员 {args.name}"))
        else:
            webauth.set_password(conn, args.name, password, now=now)
            print(_safe(f"已修改 {args.name} 的密码，其所有会话已失效"))
        return 0
    finally:
        conn.close()


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
    if args.cmd == "dispatch-worker":
        dispatch_worker.run(ctx)
        return 0
    if not ctx.settings.manages_local_codex and args.cmd in {
        "assign", "unassign", "fix", "restart-codex", "users", "user", "check", "backfill-models",
    }:
        raise accounts.AccountError("此命令仅用于 managed 模式")
    if args.cmd == "web":
        from .web import server

        server.serve(ctx)
        return 0
    if args.cmd == "account":
        return account_cmd(ctx, args)
    if args.cmd in ("assign", "unassign", "fix"):
        failed = False
        done: list[str] = []
        for name in args.users:
            try:
                if args.cmd == "assign":
                    backup = assignments.assign(ctx, name, args.account, ACTOR)
                    print(_safe(f"{name} -> {args.account}" + (f"（原 auth.json 已备份为 {backup}）" if backup else "")))
                elif args.cmd == "unassign":
                    done_ok = assignments.unassign(ctx, name, ACTOR)
                    print(_safe(f"{name}: {'已回收' if done_ok else '没有生效的分配'}"))
                else:
                    assignments.reconcile_user(ctx, name, ACTOR)
                    print(_safe(f"{name}: 已按记录修复"))
                done.append(name)
            except _HANDLED as e:
                failed = True
                print(_safe(f"错误：{name}: {e}"), file=sys.stderr)
        if args.cmd in ("assign", "unassign"):
            hinted = False
            for name in done:
                try:
                    stale = assignments.stale_codex_procs(ctx, name)
                except _HANDLED:
                    continue
                if stale:
                    hinted = True
                    print(_safe(
                        f"提示：{name} 有 {len(stale)} 个旧 codex 进程仍在使用旧账号，"
                        f"通知对方后执行 codexctl restart-codex {name}"
                    ))
            if not hinted and not failed:
                print("提示：用户需重启正在运行的 codex 才会使用新账号")
        return 1 if failed else 0
    if args.cmd == "restart-codex":
        failed = False
        for name in args.users:
            try:
                ended, unable = assignments.restart_codex(ctx, name, ACTOR)
            except _HANDLED as e:
                failed = True
                print(_safe(f"错误：{name}: {e}"), file=sys.stderr)
                continue
            if ended:
                print(_safe(f"{name}: 已结束 {', '.join(str(p) for p in ended)}"))
            else:
                print(_safe(f"{name}: 没有旧 codex 进程"))
            if unable:
                failed = True
                print(_safe(f"{name}: 部分进程无法结束：{', '.join(str(p) for p in unable)}"), file=sys.stderr)
        return 1 if failed else 0
    if args.cmd == "user":
        return user_cmd(ctx, args)
    if args.cmd == "users":
        for user in ctx.list_users(ctx.settings.min_uid):
            a = repo.active_assignment(ctx.conn, user.name)
            label = repo.get_account_by_pk(ctx.conn, a.account_pk).label if a else "-"
            stale = assignments.stale_codex_procs(ctx, user.name)
            print(
                f"{_safe(user.name):<20} {_safe(label):<12} {_safe(ctx.publisher.read_link(user).kind):<8} "
                f"{len(stale) if stale else '-'}"
            )
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
        summary: dict[tuple[str, str], dict] = {}
        book = pricing.PriceBook(repo.list_prices(ctx.conn))
        for r in repo.usage_summary_by_model(ctx.conn, ctx.now() - args.days * 86400):
            key = (r["username"], r["label"] or "-")
            s = summary.setdefault(key, {
                "total": 0, "input": 0, "output": 0, "turns": 0, "cost": None, "unpriced": 0,
            })
            total = r["total"] or 0
            s["total"] += total
            s["input"] += r["input"] or 0
            s["output"] += r["output"] or 0
            s["turns"] += r["turns"]
            cost = book.cost(r["model"], r["input"], r["cached"], r["output"])
            if total:
                if cost is None:
                    s["unpriced"] += 1
                else:
                    s["cost"] = (s["cost"] or 0) + cost
        for (username, label), s in sorted(summary.items(), key=lambda kv: -kv[1]["total"]):
            if s["cost"] is None:
                cost_text = "未定价"
            else:
                cost_text = pricing.fmt_usd(s["cost"]) + ("*" if s["unpriced"] else "")
            print(
                f"{_safe(username):<20} {_safe(label):<12} total={s['total']} "
                f"input={s['input']} output={s['output']} turns={s['turns']} cost={cost_text}"
            )
        return 0
    if args.cmd == "backfill-models":
        print(f"已补全 {logscan.backfill_models(ctx)} 条用量的模型名")
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


def user_cmd(ctx: Context, args: argparse.Namespace) -> int:
    if args.ucmd != "add":
        raise AssertionError(f"unhandled user command {args.ucmd}")
    password = getpass.getpass("新用户密码：")
    if getpass.getpass("再输入一次：") != password:
        print("错误：两次输入的密码不一致", file=sys.stderr)
        return 1
    user, note = useradmin.create(ctx, args.name, password, password, ACTOR)
    text = f"已创建用户 {user.name}（uid {user.uid}，家目录 {user.home}）"
    if note:
        text += f"；{note}"
    text += f"。请用 codexctl assign --account <标签> {user.name} 分配账号"
    print(_safe(text))
    return 0


def account_cmd(ctx: Context, args: argparse.Namespace) -> int:
    c = args.acmd
    if c == "list":
        for acc in repo.list_accounts(ctx.conn):
            users = len(repo.active_usernames(ctx.conn, acc.id))
            line = (
                f"{_safe(acc.label):<12} {_safe(acc.display_name or '-'):<12} {_safe(acc.email or '-'):<28} {acc.plan_type or '-':<6} {acc.status:<14} "
                f"用户数={users} 上次刷新={fmt_ts(acc.last_refresh)} token到期={fmt_ts(acc.access_expires_at)}"
            )
            print(line + (_safe(f" 错误={acc.last_error}") if acc.last_error else ""))
        return 0
    if c == "add":
        accounts.check_new_label(ctx, args.label)
        acc = accounts.import_account(ctx, args.label, accounts.device_login(ctx, args.label), ACTOR)
        print(_safe(f"已添加 {acc.label}（{acc.email}）"))
        return 0
    if c == "import":
        acc = accounts.import_account(ctx, args.label, args.file.read_bytes(), ACTOR)
        print(_safe(f"已导入 {acc.label}（{acc.email}）"))
        return 0
    if c == "relogin":
        accounts.require_account(ctx, args.label)
        data = args.file.read_bytes() if args.file else accounts.device_login(ctx, args.label)
        accounts.relogin_account(ctx, args.label, data, ACTOR)
        print(f"{args.label} 已重新登录" + ("并发布" if ctx.settings.manages_local_codex else ""))
        return 0
    if c == "refresh":
        try:
            ok = refresher.refresh_account(ctx, accounts.require_account(ctx, args.label), ACTOR, blocking=False)
        except locks.LockBusy:
            print(f"错误：账号 {args.label} 正在被刷新，请稍后再试", file=sys.stderr)
            return 1
        print("刷新成功" if ok else _safe(f"刷新失败：{repo.get_account(ctx.conn, args.label).last_error}"))
        return 0 if ok else 1
    if c in ("disable", "enable"):
        accounts.set_disabled(ctx, args.label, c == "disable", ACTOR)
        print(f"{args.label} 已{'停用' if c == 'disable' else '启用'}")
        return 0
    if c == "name":
        value = accounts.set_display_name(ctx, args.label, args.name, ACTOR)
        print(_safe(f"{args.label} 的名称已设为 {value}") if value else f"{args.label} 的名称已清除")
        return 0
    if c == "republish":
        accounts.republish_account(ctx, args.label)
        print(f"{args.label} 已重新发布")
        return 0
    raise AssertionError(f"unhandled account command {c}")
