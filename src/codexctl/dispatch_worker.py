from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
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

from . import db, dispatch_skills, dispatch_tasks, events
from .context import Context
from .publisher import PublishError
from .sysusers import SysUser

log = logging.getLogger(__name__)
_MAX_PARALLEL = 3


class DeliveryError(ValueError):
    pass


class RequestRejected(DeliveryError):
    pass


class AppServer:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.buffer = bytearray()
        self.next_id = 1
        self.completed: tuple[str, str] | None = None

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.proc.stdin.flush()

    def read(self, deadline: float | None) -> dict:
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while (newline := self.buffer.find(b"\n")) < 0:
                if not selector.select(None if deadline is None else max(0, deadline - time.monotonic())):
                    raise DeliveryError("等待 Codex 回执超时")
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
        elif "method" in message and "id" in message:
            # The panel has no handler for agent-initiated approval or input requests.
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
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
                raise RequestRejected(f"Codex 拒绝了 {method} 请求")
            result = message.get("result")
            if isinstance(result, dict):
                return result
            raise DeliveryError(f"Codex 的 {method} 回执无效")
        raise DeliveryError("等待 Codex 回执超时")

    def wait_turn(self, turn_id: str) -> str:
        while self.completed is None or self.completed[0] != turn_id:
            self.read(None)
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
        for path in work.rglob("*"):
            os.chown(path, user.uid, user.gid)
        os.chown(work, user.uid, user.gid)
        work.rename(task_root)
    return inputs


def _env(user: SysUser, name: str) -> dict[str, str]:
    credential = os.environ.get(name, "")
    if not credential:
        raise DeliveryError(f"Worker 缺少部署凭据引用 ENV:{name}")
    return {
        "HOME": str(user.home), "USER": user.name, "LOGNAME": user.name,
        "PATH": f"/usr/local/bin:/usr/bin:/bin:{user.home}/.local/bin", "LANG": "C.UTF-8", name: credential,
    }


def _record(
    ctx: Context, task: sqlite3.Row, status: str, error: str = "", **fields,
) -> None:
    with db.transaction(ctx.conn):
        values = {"status": status, "error": error, **fields}
        clause = ", ".join(f"{key}=?" for key in values)
        ctx.conn.execute(f"UPDATE dispatch_tasks SET {clause} WHERE id=?", (*values.values(), task["id"]))
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
    try:
        with db.transaction(ctx.conn):
            user, label = dispatch_tasks.validate_snapshot(ctx, task)
            dispatch_skills.sync_user_skills(ctx)
            packages = [
                dispatch_skills.get(ctx.conn, binding["id"], with_archive=True)["archive"]
                for binding in json.loads(task["skills"])
            ]
        target = json.loads(task["target_snapshot"])
        env = _env(user, target["credential_env"])
        cwd = str(run_root / task["id"])
        inputs = [{"type": "text", "text": task["prompt"]}, *_stage(run_root, task, user, packages)]
        # Recheck after staging in case an administrator revoked access during extraction.
        with db.transaction(ctx.conn):
            current_user, current_label = dispatch_tasks.validate_snapshot(ctx, task)
        if (current_user, current_label) != (user, label):
            raise DeliveryError("执行用户或 Codex 账号绑定已变更，请重新预览")
        proc = subprocess.Popen(
            [str(ctx.settings.codex_bin), "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            user=user.uid, group=user.gid, extra_groups=[], umask=0o077,
        )
        client = AppServer(proc)
        client.call("initialize", {"clientInfo": {"name": "codexctl_dispatch", "version": "0.1.0"}})
        client.send({"method": "initialized"})
        thread_id = _id(client.call("thread/start", {
            "cwd": cwd, "approvalPolicy": "never", "sandbox": "workspace-write",
            "config": {
                "sandbox_workspace_write.network_access": True,
                "shell_environment_policy.ignore_default_excludes": True,
                "shell_environment_policy.inherit": "all",
            },
        }), "thread")
        _record(ctx, task, "sending", thread_id=thread_id, account_label=label)
        client.call("thread/name/set", {"threadId": thread_id, "name": task["title"]})
        requested = True
        result = client.call("turn/start", {"threadId": thread_id, "input": inputs})
        turn_id = _id(result, "turn")
        _record(ctx, task, "sent", turn_id=turn_id, turn_status="inProgress")
        accepted = True
        _record(ctx, task, "sent", turn_status=client.wait_turn(turn_id))
    except (OSError, ValueError, sqlite3.Error, PublishError) as exc:
        message = str(exc) if isinstance(exc, (DeliveryError, dispatch_tasks.TaskError, dispatch_skills.SkillError, PublishError)) else "无法同步 Skill 或启动、读取 Codex App Server"
        if accepted:
            _record(ctx, task, "sent", message, turn_status="unknown")
        else:
            status = "unknown" if requested and not isinstance(exc, RequestRejected) else "failed"
            _record(ctx, task, status, message)
    finally:
        if proc is not None:
            _stop(proc)


def claim(ctx: Context) -> sqlite3.Row | None:
    with db.transaction(ctx.conn):
        task = ctx.conn.execute("""
            SELECT q.* FROM dispatch_tasks q WHERE q.status='queued' AND NOT EXISTS (
                SELECT 1 FROM dispatch_tasks active
                WHERE (active.recipient=q.recipient OR active.target_id=q.target_id)
                  AND (active.status='sending' OR (active.status='sent' AND active.turn_status='inProgress'))
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
