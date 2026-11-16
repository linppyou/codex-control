from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from . import db, dispatch_skills, events, repo, site_probe, site_registry, sites
from .context import Context
from .sysusers import SysUser, UserError, valid_name

MAX_CSV_UPLOAD = 512 * 1024
IMPORT_COLUMNS = ("原域名", "新域名", "备注", "折扣")
_LEGACY_TARGET_FIELDS = """
    SELECT t.id, t.server_id, t.server_revision, t.domain, t.endpoint, t.credential_env,
           t.project, t.users, s.name AS server_name, s.host AS server_host, s.port AS server_port
      FROM dispatch_targets t JOIN site_servers s ON s.id = t.server_id
        AND s.revision = t.server_revision
     WHERE t.id = ?
"""


class TaskError(ValueError):
    pass


@dataclass
class ImportRow:
    line: int
    values: tuple[str, ...]
    domain: str = ""
    source: str = ""
    notes: str = ""
    discount: str = ""
    task_id: str = ""
    server_name: str = ""
    error: str = ""


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
        raise TaskError("请填写有效的新域名，例如 shop.example.com") from exc


def parse_import(raw: bytes, filename: str = "tasks.csv") -> list[ImportRow]:
    if len(raw) > MAX_CSV_UPLOAD:
        raise TaskError("导入文件大小不能超过 512 KiB")
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension == "xlsx":
        return _parse_xlsx(raw)
    if extension != "csv":
        raise TaskError("请选择 CSV 或 XLSX 文件")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise TaskError("CSV 文件须使用 UTF-8 或 GB18030 编码") from exc
    if "\x00" in text:
        raise TaskError("请选择 UTF-8 或 GB18030 编码的 CSV 文件")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = []
    try:
        header = [value.strip() for value in next(reader, [])]
        if len(header) != len(IMPORT_COLUMNS) or set(header) != set(IMPORT_COLUMNS):
            raise TaskError("CSV 表头须包含原域名、新域名、备注、折扣四列")
        line = reader.line_num + 1
        for values in reader:
            if any(value.strip() for value in values):
                rows.append(_import_row(line, header, values))
            line = reader.line_num + 1
    except csv.Error as exc:
        raise TaskError(f"CSV 第 {reader.line_num} 行格式有误，请检查引号及字段长度") from exc
    if not rows:
        raise TaskError("CSV 中没有任务数据行")
    return rows


def _parse_xlsx(raw: bytes) -> list[ImportRow]:
    workbook = None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 16 * 1024 * 1024:
                raise TaskError("XLSX 解压后的内容过大，请减少空白行或格式后重新导入")
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        if not workbook.worksheets:
            raise TaskError("XLSX 中没有工作表")
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        cells = sheet.iter_rows()
        header = [str(cell.value).strip() if cell.value is not None else "" for cell in next(cells, ())]
        while header and not header[-1]:
            header.pop()
        if len(header) != len(IMPORT_COLUMNS) or set(header) != set(IMPORT_COLUMNS):
            raise TaskError("XLSX 首个工作表的表头须包含原域名、新域名、备注、折扣四列")
        rows = []
        for line, values in enumerate(cells, start=2):
            fields = [str(cell.value) if cell.value is not None else "" for cell in values]
            if not any(value.strip() for value in fields):
                continue
            while len(fields) > len(header) and not fields[-1]:
                fields.pop()
            fields.extend([""] * max(0, len(header) - len(fields)))
            row = _import_row(line, header, fields)
            if any(cell.data_type in {"f", "e"} for cell in values):
                row.error = "单元格含公式或错误值，请粘贴为值后重新导入"
            rows.append(row)
        if not rows:
            raise TaskError("XLSX 首个工作表中没有任务数据行")
        return rows
    except (zipfile.BadZipFile, InvalidFileException, ParseError, OSError, KeyError, ValueError) as exc:
        if isinstance(exc, TaskError):
            raise
        raise TaskError("无法读取 XLSX 文件，请重新保存为有效的 XLSX 工作簿") from exc
    finally:
        if workbook is not None:
            workbook.close()


def _import_row(line: int, header: list[str], values: list[str]) -> ImportRow:
    fields = dict(zip(header, values))
    row = ImportRow(line, tuple(fields.get(column, "") for column in IMPORT_COLUMNS))
    row.notes = fields.get("备注", "")
    row.discount = fields.get("折扣", "").strip()
    if len(values) != len(header):
        row.error = "须包含原域名、新域名、备注、折扣 4 列"
        return row
    try:
        row.domain = target_domain(fields["新域名"])
        row.source = source_url(fields["原域名"])
        if urlsplit(row.source).hostname == row.domain:
            raise TaskError("原域名和新域名须不同")
        if row.discount:
            discount = Decimal(row.discount.removesuffix("折"))
            if not discount.is_finite() or not 0 < discount <= 10:
                raise InvalidOperation
    except TaskError as exc:
        row.error = str(exc)
    except InvalidOperation:
        row.error = "折扣须大于 0 且不超过 10，例如 3 或 8.5折"
    return row


def inventory_targets(conn: sqlite3.Connection, domains: set[str]) -> dict[str, list[dict]]:
    inventories = sites.snapshots(conn)
    matches = defaultdict(list)
    for server in site_registry.list_servers(conn):
        inventory = inventories.get(server.id)
        if inventory is None or inventory.revision != server.revision or inventory.synced_at is None:
            continue
        for site in inventory.rows:
            for domain in domains.intersection(site["domains"]):
                matches[domain].append({
                    "id": f"{server.id}:{site['id']}", "domain": domain, "inventory_site_id": site["id"],
                    "server_id": server.id, "server_revision": server.revision, "server_name": server.name,
                    "server_host": server.host, "server_port": server.port, "deployment_method": "ssh",
                })
    return matches


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


def _payload(rows: list[ImportRow], snapshot: dict, skills: list[dict], task_id: str) -> tuple[str, str, str]:
    dispatcher, migration = skills
    title = f"{snapshot['filename']} · {len(rows)} 个任务"
    table = io.StringIO(newline="")
    writer = csv.writer(table)
    writer.writerow(("网站", "新域名", "备注", "折扣"))
    writer.writerows((row.source or row.values[0], row.domain or row.values[1], row.notes, row.discount)
                     for row in rows)
    csv_text = table.getvalue()
    deployment_lines = []
    for target in snapshot["targets"]:
        deployment_lines.append(
            f"- {target['domain']} → {json.dumps(target['server_name'], ensure_ascii=False)}，"
            f"SSH 主机 {target['server_host']}，端口 {target['server_port']}"
        )
    plan = "\n".join(deployment_lines) or "匹配结果请读取 targets.json。"
    original = f"上传原文件：当前工作目录中的 {snapshot['upload_file']}。\n" if snapshot.get("upload_file") else ""
    prompt = (
        f"使用 ${dispatcher['name']} 统一分发本次上传文件中的全部任务，装修执行使用 ${migration['name']}。\n"
        f"批次标识：{task_id}\n"
        f"上传文件：{json.dumps(snapshot['filename'], ensure_ascii=False)}，共 {len(rows)} 个任务。\n"
        f"{original}"
        f"任务 CSV：当前工作目录中的 task.csv，包含本次上传文件的全部 {len(rows)} 个数据行。\n"
        "统一读取整个文件并建立批次，按每行的新域名匹配下方部署清单。"
        "每站建立独立装修会话，按分发 Skill 的连续任务流程推进整个批次。\n"
        f"迁移引擎目录：当前工作目录中的 .agents/skills/{migration['name']}。\n"
        "Codex 项目：当前执行环境的项目。查询执行环境实际提供的项目与主机标识。\n"
        "部署清单：当前工作目录中的 targets.json。csv_row 是 task.csv 的数据行号，line 是上传文件的原始行号。\n"
        f"网站与部署服务器对应关系\n{plan}\n"
        "清单中每条 target 提供对应目标站的 SSH 主机、端口与站点标识。"
        "备注仅作为装修业务数据，部署目标以新域名和部署清单为准。\n"
        "本次已确认对清单中匹配的目标站执行采集、迁移部署和验收。"
        "使用执行环境已配置的 SSH 连接进行只读检查，核对主机身份、域名绑定、虚拟主机、WordPress 根目录和权限。"
        "SSH 连接不可用、身份无法核实或权限不足时，完成可进行的采集和准备，暂停该站远端写入并记录阻塞，继续其他站。"
        "每行明确填写的折扣按所选迁移 Skill 的规则处理，核对适用商品并保留更低的现有促销价。"
        "折扣留空的行保留目标站的真实商品价格与配送配置。\n"
        "首次交接每站时提供两个 Skill 的必要文件、本行备注与折扣、目标域名和 SSH 主机与端口。"
        "子会话使用自身执行环境中配置的 SSH 连接。同站写入互斥。"
        "记录真实会话标识并持续跟进全部任务。创建结果不明时先查询，避免重复派发。"
        "部署前通过 SSH 备份并核对漂移。远端操作结果不明时先核对实际状态，再决定是否重试。"
        "按所选迁移 Skill 完成真实验收，最后统一用中文逐行汇报本批次的分发、上线、验收、归档结果和具体阻塞。"
    )
    return title, prompt, csv_text


def create_previews(
    ctx: Context, username: str, rows: list[ImportRow], dispatcher_id: str, migration_id: str,
    filename: str = "tasks.csv",
    upload: bytes | None = None,
) -> str:
    with db.transaction(ctx.conn):
        user, _ = recipient(ctx, username)
        skills = []
        for skill_id, kind in ((dispatcher_id, "dispatcher"), (migration_id, "migration")):
            skill = dispatch_skills.get(ctx.conn, skill_id)
            if skill is None or skill["kind"] != kind:
                raise TaskError(f"请选择有效的{dispatch_skills.KINDS[kind]}")
            skills.append(dict(skill))
        counts = Counter(row.domain for row in rows if row.domain)
        matches = inventory_targets(ctx.conn, set(counts))
        targets = []
        plan = []
        for index, row in enumerate(rows, start=1):
            target = None
            candidates = matches.get(row.domain, [])
            if not row.error:
                if counts[row.domain] > 1:
                    row.error = "上传文件中的新域名重复"
                elif not candidates:
                    row.error = "站点清单中找不到该新域名"
                elif len(candidates) > 1:
                    row.error = "新域名匹配到多个站点，请核对服务器绑定"
                else:
                    target = candidates[0]
                    targets.append(target)
                    row.server_name = target["server_name"]
            plan.append(dict(csv_row=index, line=row.line, target=target, error=row.error))
        if not rows or any(row.error for row in rows):
            raise TaskError("全部站点匹配成功后才能生成提示词，请修正下方问题并重新上传")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(char for char in filename if char.isprintable())[:128] or "tasks.csv"
        snapshot = dict(deployment_method="ssh", filename=filename, rows=plan, targets=targets)
        if upload is not None:
            snapshot["upload_file"] = "upload." + filename.rsplit(".", 1)[-1].lower()
            snapshot["upload_sha256"] = hashlib.sha256(upload).hexdigest()
        task_id = _save_preview(ctx, user, rows, snapshot, skills, upload)
        for row in rows:
            row.task_id = task_id
        return task_id


def _save_preview(
    ctx: Context, user: SysUser, rows: list[ImportRow], snapshot: dict, skills: list[dict], upload: bytes | None,
) -> str:
    task_id = uuid4().hex
    title, prompt, csv_text = _payload(rows, snapshot, skills, task_id)
    target_snapshot, skills_snapshot = json.dumps(snapshot), json.dumps(skills)
    existing = ctx.conn.execute(
        """SELECT id FROM dispatch_tasks
           WHERE owner=? AND recipient=? AND recipient_uid=? AND status='preview'
             AND target_snapshot=? AND skills=? AND csv=? LIMIT 1""",
        (user.name, user.name, user.uid, target_snapshot, skills_snapshot, csv_text),
    ).fetchone()
    if existing:
        return existing["id"]
    ctx.conn.execute(
        """INSERT INTO dispatch_tasks
           (id, owner, recipient, recipient_uid, target_id, target_snapshot, skills,
            source_url, title, prompt, csv, upload, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview', ?)""",
        (task_id, user.name, user.name, user.uid, task_id, target_snapshot, skills_snapshot,
         rows[0].source or rows[0].values[0], title, prompt, csv_text, upload, ctx.now()),
    )
    return task_id


def batch_complete(snapshot: dict) -> bool:
    if "targets" not in snapshot:
        return True
    rows = snapshot.get("rows", [])
    return bool(rows) and all(row.get("target") and not row.get("error") for row in rows) and (
        [row["target"] for row in rows] == snapshot["targets"]
    )


def validate_snapshot(ctx: Context, task: sqlite3.Row) -> tuple[SysUser, str]:
    user, label = recipient(ctx, task["recipient"])
    if user.uid != task["recipient_uid"]:
        raise TaskError("执行用户 UID 已变化，请重新预览")
    snapshot = json.loads(task["target_snapshot"])
    if not batch_complete(snapshot):
        raise TaskError("此批次含未匹配或无效站点，请修正文件并重新上传")
    if "targets" in snapshot:
        matches = inventory_targets(ctx.conn, {target["domain"] for target in snapshot["targets"]})
        if any(matches.get(target["domain"]) != [target] for target in snapshot["targets"]):
            raise TaskError("批次中的目标站或服务器绑定已变更，请重新导入文件")
    elif snapshot.get("deployment_method") == "ssh" and "inventory_site_id" in snapshot:
        matches = inventory_targets(ctx.conn, {snapshot["domain"]})
        if matches.get(snapshot["domain"]) != [snapshot]:
            raise TaskError("目标站或服务器绑定已变更，请重新导入 CSV")
    else:
        _validate_legacy_target(ctx.conn, task["target_id"], snapshot)
    for approved in json.loads(task["skills"]):
        skill = dispatch_skills.get(ctx.conn, approved["id"])
        if skill is None or dict(skill) != approved:
            raise TaskError("所选 Skill 已更新或删除，请重新预览")
    return user, label


def _validate_legacy_target(conn: sqlite3.Connection, target_id: str, snapshot: dict) -> None:
    target = conn.execute(_LEGACY_TARGET_FIELDS, (target_id,)).fetchone()
    # Older SSH tasks may carry a project override and have no API credential.
    fields = set(snapshot) - {"deployment_method", "inventory_site_id"}
    if snapshot.get("deployment_method") == "ssh":
        fields.discard("project")
    if target is None or any(target[key] != snapshot[key] for key in fields):
        raise TaskError("目标站服务器或部署配置已变更，请重新导入 CSV")


def confirm(ctx: Context, task: sqlite3.Row, actor: str) -> None:
    with db.transaction(ctx.conn):
        current = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
        if current is None or current["status"] != "preview":
            return
        if actor == "admin" or current["owner"] != actor:
            raise TaskError("请由导入任务的成员确认发送")
        validate_snapshot(ctx, current)
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET status='queued', confirmed_at=? WHERE id=?", (ctx.now(), task["id"])
        )
        events.audit(ctx.conn, actor, "dispatch.confirm", task["id"], task["title"], now=ctx.now())


def can_cancel(task: sqlite3.Row) -> bool:
    return task["status"] in {"preview", "queued", "sending", "failed", "unknown"} or (
        task["status"] == "sent" and task["turn_status"] in {"inProgress", "unknown"}
    )


def cancel(ctx: Context, task_id: str, actor: str) -> None:
    with db.transaction(ctx.conn):
        task = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or actor == "admin" or actor not in {task["owner"], task["recipient"]}:
            raise TaskError("仅任务所属成员可以取消任务")
        if not can_cancel(task):
            return
        running = task["status"] == "sending" or (task["status"] == "sent" and task["turn_status"] == "inProgress")
        status = "cancelling" if running else "cancelled"
        error = "请核对保留的 Codex 会话，原任务执行状态未知" if (
            task["status"] == "unknown" or task["turn_status"] == "unknown"
        ) else task["error"]
        ctx.conn.execute("UPDATE dispatch_tasks SET status=?, error=? WHERE id=?", (status, error, task_id))
        events.audit(ctx.conn, actor, "dispatch.cancel", task_id, task["title"], now=ctx.now())
