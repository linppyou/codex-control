from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

import codexctl.accounts.repository as account_repo
import codexctl.db as db
import codexctl.dispatch.skills as dispatch_skills
import codexctl.dispatch.prompts as dispatch_prompts
import codexctl.events as events
import codexctl.sites.inventory as sites
import codexctl.sites.probe as site_probe
import codexctl.sites.registry as site_registry
import codexctl.users.repository as user_repo
from codexctl.context import Context
from codexctl.users.sysusers import SysUser, UserError, valid_name

MAX_CSV_UPLOAD = 512 * 1024
IMPORT_COLUMNS = ("网站", "新域名", "备注", "折扣")
DEFAULT_MODEL = "gpt-6.1-terra"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_SANDBOX = "danger-full-access"
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
        columns = _import_columns(header, "CSV")
        line = reader.line_num + 1
        for values in reader:
            values = [values[index] if index < len(values) else "" for index in columns]
            if any(value.strip() for value in values):
                rows.append(_import_row(line, values))
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
        columns = _import_columns(header, "XLSX 首个工作表")
        rows = []
        for line, values in enumerate(cells, start=2):
            selected = [values[index] if index < len(values) else None for index in columns]
            fields = [str(cell.value) if cell is not None and cell.value is not None else "" for cell in selected]
            if not any(value.strip() for value in fields):
                continue
            row = _import_row(line, fields)
            if any(cell is not None and cell.data_type in {"f", "e"} for cell in selected):
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


def _import_columns(header: list[str], label: str) -> list[int]:
    if any(header.count(column) != 1 for column in IMPORT_COLUMNS):
        raise TaskError(f"{label}表头须包含网站、新域名、备注、折扣四列，且各列只能出现一次")
    return [header.index(column) for column in IMPORT_COLUMNS]


def _import_row(line: int, values: list[str]) -> ImportRow:
    fields = dict(zip(IMPORT_COLUMNS, values))
    discount = fields["折扣"].strip() or "3"
    row = ImportRow(line, (*values[:3], discount), notes=fields["备注"], discount=discount)
    try:
        row.domain = target_domain(fields["新域名"])
        row.source = source_url(fields["网站"])
        if urlsplit(row.source).hostname == row.domain:
            raise TaskError("网站和新域名须不同")
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
    binding = user_repo.active_assignment(ctx.conn, username)
    account = account_repo.get_account_by_pk(ctx.conn, binding.account_pk) if binding else None
    if account is None or account.status != "active":
        raise TaskError("执行用户没有可用的 Codex 账号，请联系管理员")
    return user, account.label


def _payload(rows: list[ImportRow], snapshot: dict, skills: list[dict], task_id: str, prompt_code: str | None = None) -> tuple[str, str, str]:
    dispatcher, migration = skills
    title = f"{snapshot['filename']} · {len(rows)} 个任务"
    table = io.StringIO(newline="")
    writer = csv.writer(table)
    writer.writerow(IMPORT_COLUMNS)
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
    original = f"原始上传文件为 {snapshot['upload_file']}。\n" if snapshot.get("upload_file") else ""
    try:
        prompt = dispatch_prompts.render(
            prompt_code or dispatch_prompts.DEFAULT_CODE, dispatcher=dispatcher, migration=migration,
            task_id=task_id, snapshot=snapshot, rows=rows, original=original, plan=plan,
        )
    except dispatch_prompts.PromptError as exc:
        raise TaskError(str(exc)) from exc
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
    prompt_code = dispatch_prompts.get_code(ctx.conn)
    snapshot["prompt_template_sha256"] = hashlib.sha256(prompt_code.encode()).hexdigest()
    title, prompt, csv_text = _payload(rows, snapshot, skills, task_id, prompt_code)
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
            source_url, title, prompt, csv, upload, model, reasoning_effort, sandbox, working_directory, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview', ?)""",
        (task_id, user.name, user.name, user.uid, task_id, target_snapshot, skills_snapshot,
         rows[0].source or rows[0].values[0], title, prompt, csv_text, upload,
         DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, DEFAULT_SANDBOX, str(user.home / "wp_front"), ctx.now()),
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
        _validate_legacy_target(ctx.conn, task["target_id"], snapshot, user.name)
    for approved in json.loads(task["skills"]):
        skill = dispatch_skills.get(ctx.conn, approved["id"])
        if skill is None or dict(skill) != approved:
            raise TaskError("所选 Skill 已更新或删除，请重新预览")
    return user, label


def _validate_legacy_target(conn: sqlite3.Connection, target_id: str, snapshot: dict, username: str) -> None:
    target = conn.execute(_LEGACY_TARGET_FIELDS, (target_id,)).fetchone()
    # Older SSH tasks may carry a project override and have no API credential.
    fields = set(snapshot) - {"deployment_method", "inventory_site_id"}
    if snapshot.get("deployment_method") == "ssh":
        fields.discard("project")
    if target is None or username not in json.loads(target["users"]) or any(
        key not in target.keys() or target[key] != snapshot[key] for key in fields
    ):
        raise TaskError("目标站服务器或部署配置已变更，请重新导入 CSV")


def _execution_options(task: sqlite3.Row, model: str | None, reasoning_effort: str | None,
                       sandbox: str | None) -> tuple[str, str, str]:
    model = (task["model"] if model is None else model) or DEFAULT_MODEL
    reasoning_effort = (task["reasoning_effort"] if reasoning_effort is None else reasoning_effort) or DEFAULT_REASONING_EFFORT
    sandbox = task["sandbox"] if sandbox is None else sandbox
    model = model.strip() or DEFAULT_MODEL
    reasoning_effort = reasoning_effort.strip() or DEFAULT_REASONING_EFFORT
    if sandbox != DEFAULT_SANDBOX:
        raise TaskError("任务统一使用不受 Codex 沙箱限制的执行模式")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}", model):
        raise TaskError("模型 ID 最多 128 个字符，请使用字母、数字或 . _ : / + -")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", reasoning_effort):
        raise TaskError("思考程度须为最多 32 个字符的小写英文标识")
    return model, reasoning_effort, sandbox


def execution_directory(user: SysUser, task, value: str | None = None) -> str:
    if value is None:
        value = task["working_directory"]
    value = (value or "").strip()
    if not value or value == "~":
        path = user.home / "wp_front" if not value else user.home
    elif value.startswith("~/"):
        path = user.home / value[2:]
    else:
        path = Path(value)
        if not path.is_absolute():
            path = user.home / path
    if len(str(path)) > 4096 or any(not char.isprintable() for char in str(path)):
        raise TaskError("工作目录须为有效路径，最多 4096 个字符")
    return str(Path(os.path.normpath(path)))


def save_prompt(
    ctx: Context, task: sqlite3.Row, actor: str, prompt: str, *,
    model: str | None = None, reasoning_effort: str | None = None, sandbox: str | None = None,
    working_directory: str | None = None,
) -> None:
    with db.transaction(ctx.conn):
        current = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
        if current is None or actor == "admin" or current["owner"] != actor:
            raise TaskError("请由导入任务的成员修改提示词")
        if current["status"] != "preview":
            raise TaskError("仅待人工确认的任务可以修改提示词")
        if not prompt.strip():
            raise TaskError("提示词不能为空")
        model, reasoning_effort, sandbox = _execution_options(current, model, reasoning_effort, sandbox)
        user, _ = recipient(ctx, current["recipient"])
        directory = execution_directory(user, current, working_directory)
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET prompt=?, model=?, reasoning_effort=?, sandbox=?, working_directory=? WHERE id=?",
            (prompt, model, reasoning_effort, sandbox, directory, task["id"]),
        )
        events.audit(ctx.conn, actor, "dispatch.prompt", task["id"], task["title"], now=ctx.now())


def confirm(
    ctx: Context, task: sqlite3.Row, actor: str, prompt: str | None = None, *,
    model: str | None = None, reasoning_effort: str | None = None,
    sandbox: str | None = None, working_directory: str | None = None,
) -> None:
    with db.transaction(ctx.conn):
        current = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
        if current is None or current["status"] != "preview":
            return
        if actor == "admin" or current["owner"] != actor:
            raise TaskError("请由导入任务的成员确认发送")
        if prompt is None:
            prompt = current["prompt"]
        if not prompt.strip():
            raise TaskError("提示词不能为空")
        model, reasoning_effort, sandbox = _execution_options(current, model, reasoning_effort, sandbox)
        user, _ = validate_snapshot(ctx, current)
        directory = execution_directory(user, current, working_directory)
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET prompt=?, status='queued', confirmed_at=?, model=?, reasoning_effort=?, sandbox=?, working_directory=? WHERE id=?",
            (prompt, ctx.now(), model, reasoning_effort, sandbox, directory, task["id"]),
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
