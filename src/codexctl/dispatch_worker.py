from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import logging
import os
import shlex
import selectors
import signal
import sqlite3
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from . import db, dispatch_runtime, dispatch_skills, dispatch_tasks, events, site_access, site_registry
from .context import Context
from .publisher import PublishError
from .sysusers import SysUser

log = logging.getLogger(__name__)
_MAX_PARALLEL = 3


class DeliveryError(ValueError):
    pass


class RequestRejected(DeliveryError):
    pass


class TaskCancelled(DeliveryError):
    pass


class AppServer:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.buffer = bytearray()
        self.next_id = 1
        self.completed: tuple[str, str] | None = None
        self.result_text = ""
        self.on_request = None
        self.on_message = None

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.proc.stdin.flush()

    def read(self, deadline: float | None, check_cancel=None) -> dict:
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while (newline := self.buffer.find(b"\n")) < 0:
                if check_cancel:
                    check_cancel()
                timeout = None if deadline is None else max(0, deadline - time.monotonic())
                if check_cancel:
                    timeout = min(timeout, 0.25) if timeout is not None else 0.25
                if not selector.select(timeout):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise DeliveryError("等待 Codex 回执超时")
                    continue
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    raise DeliveryError("Codex App Server 连接已断开")
                self.buffer.extend(chunk)
                if len(self.buffer) > 8 * 1024 * 1024:
                    raise DeliveryError("Codex 回执过大")
        line = bytes(self.buffer[:newline])
        del self.buffer[:newline + 1]
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
        except ValueError as exc:
            raise DeliveryError("Codex 返回了无法识别的回执") from exc
        if message.get("method") == "turn/completed":
            params = message.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            if not isinstance(turn, dict):
                raise DeliveryError("Codex 的轮次结束回执无效")
            self.completed = (turn.get("id"), turn.get("status"))
        elif message.get("method") == "item/agentMessage/delta":
            self.result_text = (self.result_text + message.get("params", {}).get("delta", ""))[-65536:]
        elif message.get("method") == "item/completed":
            item = message.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage":
                self.result_text = item.get("text", "")[-65536:]
        elif "method" in message and "id" in message:
            if message["method"] == "item/tool/call" and self.on_request is not None:
                self.send({"id": message["id"], "result": self.on_request(message.get("params", {}))})
            else:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
        if self.on_message is not None:
            self.on_message(message)
        return message

    def call(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            message = self.read(deadline)
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                error = message['error']
                detail = error.get('message', '') if isinstance(error, dict) else ''
                raise RequestRejected(f"Codex 拒绝了 {method} 请求：{detail[:500]}")
            result = message.get("result")
            if isinstance(result, dict):
                return result
            raise DeliveryError(f"Codex 的 {method} 回执无效")
        raise DeliveryError("等待 Codex 回执超时")

    def wait_turn(self, turn_id: str, check_cancel=None) -> str:
        while self.completed is None or self.completed[0] != turn_id:
            self.read(None, check_cancel)
        status = self.completed[1]
        return status if status in ("completed", "failed", "interrupted") else "unknown"


def _stage(run_root: Path, task: sqlite3.Row, user: SysUser, packages: list[bytes]) -> list[dict]:
    bindings = json.loads(task["skills"])
    task_root = run_root / task["id"]
    # The temporary directory is private to the worker until the whole handoff is ready.
    with tempfile.TemporaryDirectory(dir=run_root) as temp:
        work = Path(temp) / "work"
        work.mkdir(mode=0o700)
        skills = work / ".agents" / "skills"
        inputs = []
        for binding, package in zip(bindings, packages, strict=True):
            if hashlib.sha256(package).hexdigest() != binding["sha256"]:
                raise DeliveryError("Skill ZIP 校验失败")
            path = dispatch_skills.unpack(package, skills)
            inputs.append({"type": "skill", "name": binding["name"], "path": str(task_root / path.relative_to(work))})
        (work / "task.csv").write_text(task["csv"], encoding="utf-8")
        snapshot = json.loads(task["target_snapshot"])
        if snapshot.get("upload_file"):
            upload = task["upload"]
            if upload is None or hashlib.sha256(upload).hexdigest() != snapshot["upload_sha256"]:
                raise DeliveryError("上传文件校验失败，请重新导入")
            filename = snapshot["upload_file"]
            if filename not in {"upload.csv", "upload.xlsx"}:
                raise DeliveryError("上传文件名称无效")
            (work / filename).write_bytes(upload)
        if "rows" in snapshot:
            (work / "targets.json").write_text(json.dumps(snapshot["rows"], ensure_ascii=False, indent=2), encoding="utf-8")
        for path in work.rglob("*"):
            os.chown(path, user.uid, user.gid)
        os.chown(work, user.uid, user.gid)
        work.rename(task_root)
    return inputs


def _stage_ssh(ctx: Context, task: sqlite3.Row, user: SysUser, root: Path) -> str:
    snapshot = json.loads(task["target_snapshot"])
    if snapshot.get("deployment_method") != "ssh":
        return ""
    targets = snapshot.get("targets", [snapshot])
    key = ctx.settings.state_dir / "site-access" / f"{user.name}-{user.uid}" / site_access.KEY_NAME
    if not key.is_file():
        raise DeliveryError(f"执行用户 {user.name} 尚未配置站点 SSH 私钥")
    config, known_hosts = [], []
    for server_id in dict.fromkeys(target["server_id"] for target in targets):
        server = site_registry.get(ctx.conn, server_id)
        if server is None or not server.host_key:
            raise DeliveryError("目标服务器尚未登记 SSH 主机公钥")
        host = server.host if server.port == 22 else f"[{server.host}]:{server.port}"
        known_hosts.append(f"{host} {server.host_key}\n")
        config.append(
            f"Host {server.host}\n    HostName {server.host}\n    Port {server.port}\n"
            f"    User root\n    IdentityFile {json.dumps(str(key))}\n"
        )
    directory = root / ".ssh"
    directory.mkdir(mode=0o700)
    known_path = directory / "known_hosts"
    known_path.write_text("".join(known_hosts), encoding="ascii")
    config_path = directory / "config"
    config_path.write_text(
        "Host *\n    BatchMode yes\n    StrictHostKeyChecking yes\n"
        f"    UserKnownHostsFile {json.dumps(str(known_path))}\n"
        "    GlobalKnownHostsFile /dev/null\n    IdentitiesOnly yes\n    IdentityAgent none\n"
        "    UpdateHostKeys no\n    ConnectTimeout 8\n\n" + "\n".join(config), encoding="utf-8",
    )
    binary_dir = root / "bin"
    binary_dir.mkdir(mode=0o700)
    for command in ("ssh", "scp", "sftp"):
        wrapper = binary_dir / command
        wrapper.write_text(f"#!/bin/sh\nexec /usr/bin/{command} -F {shlex.quote(str(config_path))} \"$@\"\n")
        wrapper.chmod(0o700)
    for path in (directory, known_path, config_path, binary_dir, *binary_dir.iterdir()):
        os.chown(path, user.uid, user.gid)
    known_path.chmod(0o600)
    config_path.chmod(0o600)
    return (
        f"本站 SSH 配置为 {config_path}，固定了本批次主机公钥和执行用户密钥。"
        f"所有 SSH/scp/sftp 调用显式使用 -F {shlex.quote(str(config_path))}。"
        "此配置已绕过沙箱内系统 SSH 配置的属主映射问题。"
    )


def _env(user: SysUser, name: str | None) -> dict[str, str]:
    env = {
        "HOME": str(user.home), "USER": user.name, "LOGNAME": user.name,
        "PATH": f"/usr/local/bin:/usr/bin:/bin:{user.home}/.local/bin", "LANG": "C.UTF-8",
    }
    if name is not None:
        credential = os.environ.get(name, "")
        if not credential:
            raise DeliveryError(f"Worker 缺少部署凭据引用 ENV:{name}")
        env[name] = credential
    return env


def _record(
    ctx: Context, task: sqlite3.Row, status: str, error: str = "", **fields,
) -> None:
    with db.transaction(ctx.conn):
        values = {"status": status, "error": error, **fields}
        clause = ", ".join(f"{key}=?" for key in values)
        # Cancellation wins races with startup, delivery, and completion updates.
        ctx.conn.execute(
            f"UPDATE dispatch_tasks SET {clause} WHERE id=? AND status NOT IN ('cancelling', 'cancelled')",
            (*values.values(), task["id"]),
        )
        if fields:
            receipt = ", ".join(f"{key}=?" for key in fields)
            ctx.conn.execute(f"UPDATE dispatch_tasks SET {receipt} WHERE id=? AND status='cancelling'",
                             (*fields.values(), task["id"]))
        if status in {"sent", "failed", "unknown"}:
            action = f"dispatch.{status}" if "turn_status" not in fields else "dispatch.turn"
            events.audit(ctx.conn, task["owner"], action, task["id"], error, now=ctx.now())


def _stop(proc: subprocess.Popen) -> None:
    with suppress(BrokenPipeError):
        proc.stdin.close()
    proc.stdout.close()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _id(result: dict, key: str) -> str:
    item = result.get(key)
    value = item.get("id") if isinstance(item, dict) else None
    if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
        raise DeliveryError(f"Codex 的 {key} 回执缺少标识")
    return value


def deliver(ctx: Context, run_root: Path, task: sqlite3.Row) -> None:
    proc = None
    requested = False
    accepted = False
    client = None
    thread_id = turn_id = None
    cancelled = False
    cancel_error = ""
    runtime = None

    def check_cancel():
        current = ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
        if current is None or current["status"] in {"cancelling", "cancelled"}:
            raise TaskCancelled("成员已取消任务")

    try:
        check_cancel()
        with db.transaction(ctx.conn):
            user, label = dispatch_tasks.validate_snapshot(ctx, task)
            dispatch_skills.sync_user_skills(ctx)
            packages = [
                dispatch_skills.get(ctx.conn, binding["id"], with_archive=True)["archive"]
                for binding in json.loads(task["skills"])
            ]
        target = json.loads(task["target_snapshot"])
        credential = None if target.get("deployment_method") == "ssh" else target.get("credential_env")
        env = _env(user, credential)
        cwd = str(run_root / task["id"])
        inputs = [{"type": "text", "text": task["prompt"]}, *_stage(run_root, task, user, packages)]
        ssh_instructions = _stage_ssh(ctx, task, user, Path(cwd))
        env["PATH"] = f"{cwd}/bin:{env['PATH']}"
        # Site bindings or Skill packages may change while files are being staged.
        with db.transaction(ctx.conn):
            current_user, current_label = dispatch_tasks.validate_snapshot(ctx, task)
        if (current_user, current_label) != (user, label):
            raise DeliveryError("执行用户或 Codex 账号绑定已变更，请重新预览")
        check_cancel()
        proc = subprocess.Popen(
            [str(ctx.settings.codex_bin), "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            user=user.uid, group=user.gid, extra_groups=[], umask=0o077,
        )
        client = AppServer(proc)
        runtime = dispatch_runtime.DispatchRuntime(ctx, task, user, Path(cwd), env, inputs[1:], ssh_instructions,
                                                   check_cancel)
        client.on_request = runtime.handle
        client.on_message = lambda message: runtime.sync()
        client.call("initialize", {"clientInfo": {"name": "codexctl_dispatch", "version": "0.1.0"},
                                   "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized"})
        thread_id = _id(client.call("thread/start", {
            "cwd": cwd, "approvalPolicy": "never", "sandbox": task['sandbox'],
            "dynamicTools": dispatch_runtime.TOOLS,
            "developerInstructions": runtime.instructions,
            "config": {
                "sandbox_workspace_write.network_access": True,
                "shell_environment_policy.ignore_default_excludes": True,
                "shell_environment_policy.inherit": "all",
            },
        }), "thread")
        _record(ctx, task, "sending", thread_id=thread_id, account_label=label)
        client.call("thread/name/set", {"threadId": thread_id, "name": task["title"]})
        check_cancel()
        requested = True
        turn_params = {"threadId": thread_id, "input": inputs}
        if task["model"]:
            turn_params["model"] = task["model"]
        if task["reasoning_effort"]:
            turn_params["effort"] = task["reasoning_effort"]
        result = client.call("turn/start", turn_params)
        turn_id = _id(result, "turn")
        _record(ctx, task, "sent", turn_id=turn_id, turn_status="inProgress")
        accepted = True
        check_cancel()
        status = client.wait_turn(turn_id, check_cancel)
        while runtime.running:
            check_cancel()
            runtime.sync()
            time.sleep(0.1)
        runtime.sync()
        total = len(list(csv.DictReader(io.StringIO(task['csv']))))
        children = ctx.conn.execute('SELECT status FROM dispatch_children WHERE task_id=?', (task['id'],)).fetchall()
        issue = ""
        if status == "completed" and (len(children) != total or any(row['status'] != 'completed' for row in children)):
            issue = "批次尚未全部完成，请核对 Codex 回复及子会话状态"
        _record(ctx, task, "sent", issue, turn_status=status, result_text=client.result_text)
    except TaskCancelled:
        cancelled = True
        if client is not None and thread_id and turn_id:
            try:
                client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except (OSError, ValueError):
                cancel_error = "已停止本机任务进程，中断回执未确认，请核对保留的 Codex 会话"
    except (OSError, ValueError, sqlite3.Error, PublishError) as exc:
        message = str(exc) if isinstance(exc, (DeliveryError, dispatch_tasks.TaskError, dispatch_skills.SkillError, PublishError)) else "无法同步 Skill 或启动、读取 Codex App Server"
        if accepted:
            _record(ctx, task, "sent", message, turn_status="unknown")
        else:
            status = "unknown" if requested and not isinstance(exc, RequestRejected) else "failed"
            _record(ctx, task, status, message)
    finally:
        if runtime is not None:
            runtime.close()
        if proc is not None:
            _stop(proc)
        with db.transaction(ctx.conn):
            current = ctx.conn.execute("SELECT status FROM dispatch_tasks WHERE id=?", (task["id"],)).fetchone()
            if current is not None and (cancelled or current["status"] == "cancelling"):
                if requested and not cancelled:
                    cancel_error = "已停止本机任务进程，原轮次状态未确认，请核对保留的 Codex 会话"
                ctx.conn.execute(
                    "UPDATE dispatch_tasks SET status='cancelled', turn_status=?, error=? WHERE id=?",
                    ("unknown" if cancel_error else "interrupted" if turn_id else None, cancel_error, task["id"]),
                )


def claim(ctx: Context) -> sqlite3.Row | None:
    with db.transaction(ctx.conn):
        task = ctx.conn.execute("""
            SELECT q.* FROM dispatch_tasks q WHERE q.status='queued' AND NOT EXISTS (
                SELECT 1 FROM dispatch_tasks active
                WHERE (active.recipient=q.recipient OR active.target_id=q.target_id OR EXISTS (
                    SELECT 1
                    FROM json_each(COALESCE(json_extract(active.target_snapshot, '$.targets'),
                                            json_array(json(active.target_snapshot)))) a
                    JOIN json_each(COALESCE(json_extract(q.target_snapshot, '$.targets'),
                                            json_array(json(q.target_snapshot)))) b
                      ON json_extract(a.value, '$.server_id')=json_extract(b.value, '$.server_id')
                     AND (json_extract(a.value, '$.id')=json_extract(b.value, '$.id')
                          OR json_extract(a.value, '$.domain')=json_extract(b.value, '$.domain'))
                ))
                  AND (active.status IN ('sending', 'cancelling') OR (active.status='sent' AND active.turn_status='inProgress'))
            )
            ORDER BY q.confirmed_at, q.rowid LIMIT 1
        """).fetchone()
        if task:
            ctx.conn.execute("UPDATE dispatch_tasks SET status='sending' WHERE id=?", (task["id"],))
        return task


def run(ctx: Context) -> None:
    if not ctx.settings.manages_local_codex:
        raise DeliveryError("任务 Worker 需要 managed 模式")
    run_root = ctx.settings.state_dir / "dispatch"
    run_root.mkdir(mode=0o711, exist_ok=True)
    run_root.chmod(0o711)
    with (run_root / "worker.lock").open("wb") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeliveryError("任务 Worker 已在运行") from exc
        with db.transaction(ctx.conn):
            dispatch_skills.sync_user_skills(ctx)
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET status='unknown', error=? WHERE status='sending'",
            ("发送期间 Worker 中断，请先核对原 Codex 会话",),
        )
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET turn_status='unknown', error=? WHERE status='sent' AND turn_status='inProgress'",
            ("Worker 中断，请在 Codex 查看原会话",),
        )
        ctx.conn.execute(
            "UPDATE dispatch_children SET status='unknown', error=? WHERE status IN ('starting','sending','running')",
            ("Worker 中断，请核对原子会话后续跑",),
        )
        ctx.conn.execute(
            "UPDATE dispatch_tasks SET status='cancelled', turn_status='unknown', error=? WHERE status='cancelling'",
            ("取消期间 Worker 中断，请核对保留的 Codex 会话",),
        )

        def work(task: sqlite3.Row) -> None:
            with db.connection(ctx.settings.db_path) as conn:
                deliver(replace(ctx, conn=conn), run_root, task)

        with ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
            pending = set()
            while True:
                for job in tuple(pending):
                    if job.done():
                        pending.remove(job)
                        try:
                            job.result()
                        except Exception:
                            log.exception("记录 Codex 任务回执失败")
                            raise
                while len(pending) < _MAX_PARALLEL and (task := claim(ctx)):
                    pending.add(pool.submit(work, task))
                time.sleep(1)
