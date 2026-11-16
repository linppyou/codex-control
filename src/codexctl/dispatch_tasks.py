from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import db, dispatch_skills, events, repo, site_probe
from .context import Context
from .sysusers import SysUser, UserError, valid_name

_TARGET_FIELDS = """
    SELECT t.*, s.name AS server_name, s.host AS server_host
      FROM dispatch_targets t JOIN site_servers s ON s.id = t.server_id
        AND s.revision = t.server_revision
"""


class TaskError(ValueError):
    pass


def _url(value: str):
    value = value.strip()
    if not value or len(value) > 2048 or any(
        not char.isprintable() or char.isspace() or char == "\\" for char in value
    ):
        raise ValueError
    parts = urlsplit(value if "://" in value else "https://" + value)
    if (
        parts.scheme not in {"http", "https"} or not parts.hostname
        or parts.username is not None or parts.password is not None or parts.fragment or parts.port == 0
    ):
        raise ValueError
    return parts


def source_url(value: str) -> str:
    try:
        parts = _url(value)
        host = site_probe.hostname(parts.hostname)
        if host is None:
            raise ValueError
        authority = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except ValueError as exc:
        raise TaskError("请输入有效的公开源站网址，例如 https://example.com") from exc


def target_domain(value: str) -> str:
    try:
        parts = _url(value)
        host = site_probe.hostname(parts.hostname)
        if not host or parts.port or parts.query or parts.path not in ("", "/"):
            raise ValueError
        return host
    except ValueError as exc:
        raise TaskError("请输入目标站域名，或从所选服务器的授权站点中选择") from exc


def save_target(
    ctx: Context, target_id: str | None, server_id: str, domain: str, endpoint: str,
    credential_env: str, project: str, users: str,
) -> None:
    domain = target_domain(domain)
    endpoint = endpoint.strip()
    try:
        parts = _url(endpoint)
        if "://" not in endpoint or parts.scheme != "https":
            raise ValueError
    except ValueError as exc:
        raise TaskError("请输入完整的 HTTPS 部署接口地址") from exc
    credential_env = credential_env.strip().removeprefix("ENV:")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", credential_env) or credential_env in {
        "HOME", "PATH", "SHELL", "LANG", "USER", "LOGNAME", "CODEX_HOME",
    } or credential_env.startswith(("LD_", "PYTHON", "NODE_", "BASH_", "GIT_", "CODEX_", "OPENAI_")):
        raise TaskError("请输入专用的部署凭据环境变量名，例如 BAOTA_SHOP_API_KEY")
    project = project.strip()
    if len(project) > 128 or any(not char.isprintable() for char in project):
        raise TaskError("Codex 项目名称或 ID 最多 128 个字符")
    names = set(filter(None, re.split(r"[\s,，]+", users.strip())))
    if not names or len(names) > 100 or any(
        not valid_name(name) or name in {"root", "admin"} for name in names
    ):
        raise TaskError("请填写有权部署此站点的普通用户名，用空格或逗号分隔")
    try:
        with db.transaction(ctx.conn):
            server = ctx.conn.execute("SELECT revision FROM site_servers WHERE id=?", (server_id,)).fetchone()
            if server is None:
                raise TaskError("所选服务器不存在")
            values = (server_id, server["revision"], domain, endpoint, credential_env, project, json.dumps(sorted(names)))
            if target_id:
                updated = ctx.conn.execute(
                    """UPDATE dispatch_targets SET server_id=?, server_revision=?, domain=?, endpoint=?,
                       credential_env=?, project=?, users=? WHERE id=?""",
                    (*values, target_id),
                )
                if not updated.rowcount:
                    raise TaskError("此目标站授权已删除")
            else:
                target_id = uuid4().hex
                ctx.conn.execute("INSERT INTO dispatch_targets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (target_id, *values))
            events.audit(ctx.conn, "admin", "dispatch.target_save", target_id, domain, now=ctx.now())
    except sqlite3.IntegrityError as exc:
        raise TaskError("该服务器已登记此目标站") from exc


def delete_target(ctx: Context, target_id: str) -> None:
    with db.transaction(ctx.conn):
        if ctx.conn.execute("DELETE FROM dispatch_targets WHERE id=?", (target_id,)).rowcount:
            events.audit(ctx.conn, "admin", "dispatch.target_delete", target_id, now=ctx.now())


def list_targets(conn: sqlite3.Connection, username: str) -> list[sqlite3.Row]:
    rows = conn.execute(_TARGET_FIELDS + " ORDER BY s.name, t.domain").fetchall()
    return [row for row in rows if username == "admin" or username in json.loads(row["users"])]


def recipient(ctx: Context, username: str) -> tuple[SysUser, str]:
    if not ctx.settings.manages_local_codex:
        raise TaskError("此主机为纯网关模式。任务发送需要已绑定 Codex 的本机 Linux 用户")
    if not valid_name(username) or username in {"root", "admin"}:
        raise TaskError("请选择绑定了 Codex 的普通执行用户")
    try:
        user = ctx.lookup_user(username, max(1, ctx.settings.min_uid))
        if user.uid <= 0 or user.gid < 0:
            raise UserError("无效的执行身份")
    except (UserError, OSError) as exc:
        raise TaskError("找不到有效的 Linux 执行用户") from exc
    binding = repo.active_assignment(ctx.conn, username)
    account = repo.get_account_by_pk(ctx.conn, binding.account_pk) if binding else None
    if account is None or account.status != "active":
        raise TaskError("执行用户没有可用的 Codex 账号，请联系管理员")
    return user, account.label


def _payload(source: str, target: sqlite3.Row, skills: list[dict], task_id: str) -> tuple[str, str, str]:
    dispatcher, migration = skills
    parts = urlsplit(source)
    title = f"{parts.netloc}{parts.path.rstrip('/')} 迁移 {target['domain']}"
    table = io.StringIO()
    writer = csv.writer(table, lineterminator="\n")
    writer.writerow(("网站", "新域名", "备注", "折扣"))
    writer.writerow((source, target["domain"], "", "3"))
    csv_text = table.getvalue()
    project = json.dumps(target["project"], ensure_ascii=False) if target["project"] else "当前执行环境的项目"
    prompt = (
        f"使用 ${dispatcher['name']} 分发本次单站任务，装修执行使用 ${migration['name']}。\n"
        f"任务标识：{task_id}\n"
        f"任务 CSV：当前工作目录中的 task.csv。仅处理数据行 1，同时最多 1 个装修会话。\n"
        f"{csv_text}\n"
        f"迁移引擎目录：当前工作目录中的 .agents/skills/{migration['name']}。\n"
        "本机用户级 Skill 已统一链接到管理员发布目录。创建子会话时显式传递本任务的两个 Skill 路径，"
        "跨主机执行时先传递本任务的 Skill 文件并确认版本。\n"
        f"Codex 项目：{project}。查询执行环境实际提供的项目与主机标识。\n"
        f"子会话标题：{title}\n"
        f"部署服务器：{json.dumps(target['server_name'], ensure_ascii=False)}（{target['server_host']}）\n"
        f"部署接口：wp建站中心 AI 装修设计，{target['endpoint']}\n"
        f"凭据引用：ENV:{target['credential_env']}。凭据值不得写入提示词、日志或归档。\n\n"
        "本次已确认仅对指定目标域名执行采集、迁移部署和验收。"
        "先调用 capabilities 和 sites.list，核对目标绑定、面板站点 ID、WordPress 根目录及权限，"
        "再用 site.probe 核对实际站点数据。发现站点身份不一致或权限不足时报告阻塞。"
        "折扣列为分发器缺省的三折，本任务保留目标站的真实商品价格与配送配置。\n"
        "按分发 Skill 的连续任务流程，把两个 Skill 的必要文件、部署接口和凭据引用交接给独立装修会话。"
        "记录真实会话标识并跟进。创建结果不明时先查询，避免重复派发。"
        "部署前备份并核对漂移，部署接口重试沿用幂等键，按所选迁移 Skill 完成真实验收。"
        "最后用中文汇报实际分发、上线、验收和归档结果以及具体阻塞。"
    )
    return title, prompt, csv_text


def create_preview(
    ctx: Context, owner: str, username: str, server_id: str, source: str, domain: str,
    dispatcher_id: str, migration_id: str,
) -> str:
    source = source_url(source)
    domain = target_domain(domain)
    if site_probe.hostname(urlsplit(source).hostname) == domain:
        raise TaskError("源站和目标站须使用不同域名")
    with db.transaction(ctx.conn):
        user, _ = recipient(ctx, username if owner == "admin" else owner)
        target = ctx.conn.execute(_TARGET_FIELDS + " WHERE t.server_id=? AND t.domain=?", (server_id, domain)).fetchone()
        if target is None or user.name not in json.loads(target["users"]):
            raise TaskError("所选服务器上没有授权给执行用户的目标站")
        skills = []
        for skill_id, kind in ((dispatcher_id, "dispatcher"), (migration_id, "migration")):
            skill = dispatch_skills.get(ctx.conn, skill_id)
            if skill is None or skill["kind"] != kind:
                raise TaskError(f"请选择有效的{dispatch_skills.KINDS[kind]}")
            skills.append(dict(skill))
        task_id = uuid4().hex
        title, prompt, csv_text = _payload(source, target, skills, task_id)
        ctx.conn.execute(
            """INSERT INTO dispatch_tasks
               (id, owner, recipient, recipient_uid, target_id, target_snapshot, skills,
                source_url, title, prompt, csv, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview', ?)""",
            (task_id, owner, user.name, user.uid, target["id"], json.dumps(dict(target)), json.dumps(skills),
             source, title, prompt, csv_text, ctx.now()),
        )
        return task_id


def validate_snapshot(ctx: Context, task: sqlite3.Row) -> tuple[SysUser, str]:
    user, label = recipient(ctx, task["recipient"])
    if user.uid != task["recipient_uid"]:
        raise TaskError("执行用户 UID 已变化，请重新预览")
    target = ctx.conn.execute(_TARGET_FIELDS + " WHERE t.id=?", (task["target_id"],)).fetchone()
    if target is None or dict(target) != json.loads(task["target_snapshot"]):
        raise TaskError("目标站服务器或部署授权已变更，请重新预览")
    for approved in json.loads(task["skills"]):
        skill = dispatch_skills.get(ctx.conn, approved["id"])
        if skill is None or dict(skill) != approved:
            raise TaskError("所选 Skill 已更新或删除，请重新预览")
    return user, label


def confirm(ctx: Context, task: sqlite3.Row, actor: str) -> None:
    with db.transaction(ctx.conn):
        current = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
        if current is None or current["status"] != "preview":
            return
        validate_snapshot(ctx, current)
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET status='queued', confirmed_at=? WHERE id=?", (ctx.now(), task["id"])
        )
        events.audit(ctx.conn, actor, "dispatch.confirm", task["id"], task["title"], now=ctx.now())
