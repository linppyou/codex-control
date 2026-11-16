from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import db, dispatch_skills, events, repo, site_probe, site_registry, sites
from .context import Context
from .site_registry import Server
from .sysusers import SysUser, UserError, valid_name

DEPLOYMENT_METHOD = "ssh"

# Keep the fields used by persisted task snapshots stable as assignment metadata changes.
_TARGET_FIELDS = """
    SELECT t.id, t.server_id, t.server_revision, t.domain, t.project, t.users,
           s.name AS server_name, s.host AS server_host, s.port AS server_port
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
    ctx: Context, target_id: str | None, server_id: str, domain: str, project: str, users: str,
) -> None:
    domain = target_domain(domain)
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
            values = (server_id, server["revision"], domain, project, json.dumps(sorted(names)))
            if target_id:
                previous = ctx.conn.execute(
                    "SELECT server_id, server_revision, domain FROM dispatch_targets WHERE id=?", (target_id,),
                ).fetchone()
                if previous is None:
                    raise TaskError("此目标站授权已删除")
                ctx.conn.execute(
                    """UPDATE dispatch_targets
                       SET inventory_site_id = CASE WHEN server_id = ? AND domain = ? THEN inventory_site_id END,
                           server_id=?, server_revision=?, domain=?, project=?, users=?
                       WHERE id=?""",
                    (server_id, domain, *values, target_id),
                )
                if tuple(previous) != values[:3]:
                    ctx.conn.execute("DELETE FROM dispatch_user_deployments WHERE target_id=?", (target_id,))
                    ctx.conn.execute("DELETE FROM dispatch_site_work WHERE target_id=?", (target_id,))
                else:
                    ctx.conn.execute(
                        """DELETE FROM dispatch_user_deployments WHERE target_id=?
                           AND username NOT IN (SELECT value FROM json_each(?))""",
                        (target_id, values[-1]),
                    )
                    ctx.conn.execute(
                        """DELETE FROM dispatch_site_work WHERE target_id=?
                           AND username NOT IN (SELECT value FROM json_each(?))""",
                        (target_id, values[-1]),
                    )
            else:
                target_id = uuid4().hex
                ctx.conn.execute(
                    """INSERT INTO dispatch_targets
                       (id, server_id, server_revision, domain, endpoint, credential_env, project, users)
                       VALUES (?, ?, ?, ?, '', '', ?, ?)""",
                    (target_id, *values),
                )
            now = ctx.now()
            ctx.conn.executemany(
                "INSERT OR IGNORE INTO dispatch_site_work(target_id, username, assigned_at) VALUES (?, ?, ?)",
                ((target_id, name, now) for name in names),
            )
            events.audit(ctx.conn, "admin", "dispatch.target_save", target_id, domain, now=now)
    except sqlite3.IntegrityError as exc:
        raise TaskError("该服务器已登记此目标站") from exc


def delete_target(ctx: Context, target_id: str) -> None:
    with db.transaction(ctx.conn):
        if ctx.conn.execute("DELETE FROM dispatch_targets WHERE id=?", (target_id,)).rowcount:
            events.audit(ctx.conn, "admin", "dispatch.target_delete", target_id, now=ctx.now())


def _ssh_target(conn: sqlite3.Connection, target: sqlite3.Row, user: SysUser) -> dict:
    result = dict(target, deployment_method=DEPLOYMENT_METHOD)
    project = conn.execute(
        """SELECT project FROM dispatch_user_deployments
           WHERE target_id=? AND username=? AND uid=?""",
        (target["id"], user.name, user.uid),
    ).fetchone()
    if project is not None:
        result["project"] = project["project"]
    return result


def list_targets(ctx: Context, username: str) -> list[dict]:
    rows = ctx.conn.execute(_TARGET_FIELDS + " ORDER BY s.name, t.domain").fetchall()
    if username == "admin":
        return [dict(row) for row in rows]
    assigned = [row for row in rows if username in json.loads(row["users"])]
    if not assigned:
        return []
    work = {row["target_id"]: row for row in ctx.conn.execute(
        "SELECT target_id, assigned_at, completed_at FROM dispatch_site_work WHERE username=?", (username,),
    )}
    result = []
    for row in assigned:
        target = dict(row)
        status = work.get(row["id"])
        target.update(assigned_at=status["assigned_at"] if status else None,
                      completed_at=status["completed_at"] if status else None)
        result.append(target)
    return result


def site_work(
    conn: sqlite3.Connection, start: int | None, stop: int | None, status: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.domain, s.name AS server_name, w.username, w.assigned_at, w.completed_at
           FROM dispatch_site_work w
           JOIN dispatch_targets t ON t.id=w.target_id JOIN site_servers s ON s.id=t.server_id
           WHERE (:start IS NULL OR (w.assigned_at>=:start AND w.assigned_at<:stop))
             AND (:status='' OR (w.completed_at IS NOT NULL)=(:status='completed'))
           ORDER BY w.assigned_at DESC, s.name, t.domain, w.username""",
        {"start": start, "stop": stop, "status": status},
    ).fetchall()


def _assigned_target(conn: sqlite3.Connection, username: str, target_id: str) -> sqlite3.Row | None:
    target = conn.execute(_TARGET_FIELDS + " WHERE t.id=?", (target_id,)).fetchone()
    if target is None or username not in json.loads(target["users"]):
        return None
    return target


def complete_site_work(ctx: Context, username: str, target_id: str) -> bool:
    with db.transaction(ctx.conn):
        target = _assigned_target(ctx.conn, username, target_id)
        if target is None:
            return False
        now = ctx.now()
        updated = ctx.conn.execute(
            """UPDATE dispatch_site_work SET completed_at=?
               WHERE target_id=? AND username=? AND completed_at IS NULL""",
            (now, target_id, username),
        )
        if updated.rowcount:
            events.audit(ctx.conn, username, "dispatch.site_complete", target_id, target["domain"], now=now)
        return True


def deployment_target(ctx: Context, username: str, target_id: str) -> dict | None:
    target = _assigned_target(ctx.conn, username, target_id)
    return dict(target) if target is not None else None


def pending_sites(
    conn: sqlite3.Connection, server: Server, inventory: sites.Inventory | None,
) -> list[dict]:
    if inventory is None or inventory.revision != server.revision or inventory.synced_at is None:
        return []
    assigned = conn.execute(
        "SELECT domain, inventory_site_id FROM dispatch_targets WHERE server_id=?",
        (server.id,),
    ).fetchall()
    assigned_domains = {row["domain"] for row in assigned}
    assigned_ids = {row["inventory_site_id"] for row in assigned}
    assigned_aliases = assigned_domains | {
        domain for row in inventory.rows
        if row["id"] in assigned_ids or not assigned_domains.isdisjoint(row["domains"])
        for domain in row["domains"]
    }
    return sorted(
        (row for row in inventory.rows if assigned_aliases.isdisjoint(row["domains"])),
        key=lambda row: row["domains"][0],
    )


def assign_sites(ctx: Context, server_id: str, site_ids: list[str], username: str) -> list[str]:
    selection = list(dict.fromkeys(site_ids))
    if not selection:
        raise TaskError("请至少选择一个待分配站点")
    with db.transaction(ctx.conn):
        server = site_registry.get(ctx.conn, server_id)
        if server is None:
            raise TaskError("所选服务器不存在")
        inventory = sites.snapshots(ctx.conn).get(server.id)
        pending = {row["id"]: row for row in pending_sites(ctx.conn, server, inventory)}
        if any(site_id not in pending for site_id in selection):
            raise TaskError("部分站点已分配或已不在服务器的站点清单中，请核对后重试")
        user, _ = recipient(ctx, username)
        users = json.dumps([user.name])
        selected_aliases: set[str] = set()
        domains = []
        now = ctx.now()
        for site_id in selection:
            aliases = pending[site_id]["domains"]
            if not selected_aliases.isdisjoint(aliases):
                raise TaskError("所选站点的绑定域名有重叠，请只选择其中一个站点")
            selected_aliases.update(aliases)
            domain = aliases[0]
            target_id = uuid4().hex
            ctx.conn.execute(
                """INSERT INTO dispatch_targets
                   (id, server_id, server_revision, domain, endpoint, credential_env, project, users, inventory_site_id)
                   VALUES (?, ?, ?, ?, '', '', '', ?, ?)""",
                (target_id, server.id, server.revision, domain, users, site_id),
            )
            ctx.conn.execute(
                "INSERT INTO dispatch_site_work(target_id, username, assigned_at) VALUES (?, ?, ?)",
                (target_id, user.name, now),
            )
            events.audit(ctx.conn, "admin", "dispatch.site_assign", target_id, f"{domain} → {user.name}", now=now)
            domains.append(domain)
    return domains


def _execution_user(ctx: Context, username: str) -> SysUser:
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
    return user


def recipient(ctx: Context, username: str) -> tuple[SysUser, str]:
    user = _execution_user(ctx, username)
    binding = repo.active_assignment(ctx.conn, username)
    account = repo.get_account_by_pk(ctx.conn, binding.account_pk) if binding else None
    if account is None or account.status != "active":
        raise TaskError("执行用户没有可用的 Codex 账号，请联系管理员")
    return user, account.label


def _payload(source: str, target: dict, skills: list[dict], task_id: str) -> tuple[str, str, str]:
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
        f"Codex 项目：{project}。查询执行环境实际提供的项目与主机标识。\n"
        f"子会话标题：{title}\n"
        f"部署服务器：{json.dumps(target['server_name'], ensure_ascii=False)}\n"
        f"部署方式：SSH。目标主机 {target['server_host']}，SSH 端口 {target['server_port']}。\n\n"
        "本次已确认仅对指定目标域名执行采集、迁移部署和验收。"
        "部署按本任务的 SSH 流程执行。通过实际执行环境中已配置的 SSH 连接直接操作目标站文件、WordPress 和数据库。"
        "通过 SSH 先做只读检查，核对主机身份、目标域名、虚拟主机、WordPress 根目录和权限。"
        "SSH 尚未配置、身份无法核实或权限不足时，可以完成采集和本地准备，记录具体阻塞并暂停远端写入。"
        "折扣列为分发器缺省的三折，本任务保留目标站的真实商品价格与配送配置。\n"
        "按分发 Skill 的连续任务流程，把两个 Skill 的必要文件、目标域名和 SSH 地址及端口交接给独立装修会话。"
        "子会话遵循同一 SSH 部署范围，使用自身执行环境中配置的连接。"
        "记录真实会话标识并跟进。创建结果不明时先查询，避免重复派发。"
        "通过 SSH 备份目标站并核对漂移。远端操作结果不明时先核对实际状态，再决定是否重试。"
        "按所选迁移 Skill 完成真实验收。"
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
        row = ctx.conn.execute(_TARGET_FIELDS + " WHERE t.server_id=? AND t.domain=?", (server_id, domain)).fetchone()
        if row is None or user.name not in json.loads(row["users"]):
            raise TaskError("所选服务器上没有授权给执行用户的目标站")
        target = _ssh_target(ctx.conn, row, user)
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
            (task_id, owner, user.name, user.uid, target["id"], json.dumps(target), json.dumps(skills),
             source, title, prompt, csv_text, ctx.now()),
        )
        return task_id


def validate_snapshot(ctx: Context, task: sqlite3.Row) -> tuple[SysUser, str]:
    snapshot = json.loads(task["target_snapshot"])
    if snapshot.get("deployment_method") != DEPLOYMENT_METHOD:
        raise TaskError("此任务使用旧的部署方式，请返回任务页重新生成 SSH 预览")
    user, label = recipient(ctx, task["recipient"])
    if user.uid != task["recipient_uid"]:
        raise TaskError("执行用户 UID 已变化，请重新预览")
    target = ctx.conn.execute(_TARGET_FIELDS + " WHERE t.id=?", (task["target_id"],)).fetchone()
    if (
        target is None or user.name not in json.loads(target["users"])
        or _ssh_target(ctx.conn, target, user) != snapshot
    ):
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
