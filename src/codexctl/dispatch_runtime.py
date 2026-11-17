from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_MAX_CHILDREN = 3


def _tool(name, description, properties=None, required=()):
    return {"type": "function", "name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": list(required),
        "additionalProperties": False,
    }}


_STRING = {"type": "string"}
_TARGET = {"type": "object", "properties": {
    "threadId": _STRING, "hostId": _STRING, "afterCursor": _STRING,
}, "required": ["threadId"], "additionalProperties": False}
TOOLS = [
    _tool("list_projects", "列出本任务实际可用的项目和执行主机。"),
    _tool("list_hosts", "列出本任务实际可用的执行主机。"),
    _tool("create_thread", "为指定 CSV 数据行创建独立 Codex 会话。每行只创建一次，重复调用返回原会话。最多同时运行三个子会话。", {
        "row": {"type": "integer", "minimum": 1}, "title": _STRING, "prompt": _STRING,
        "project_id": _STRING, "host_id": _STRING, "dispatch_id": _STRING,
    }, ("row", "title", "prompt")),
    _tool("list_threads", "列出本批次已创建的全部真实子会话和状态。"),
    _tool("wait_threads", "等待本批次子会话状态变化并返回结果和游标。完成或需处理时立即返回。", {
        "targets": {"type": "array", "items": _TARGET, "minItems": 1, "maxItems": 8},
        "timeoutMs": {"type": "integer", "minimum": 0, "maximum": 60000},
    }, ("targets",)),
    _tool("read_thread", "读取本批次子会话的当前状态和回复。", {"threadId": _STRING}, ("threadId",)),
    _tool("send_message_to_thread", "向本批次已停止的原子会话发送续跑指令，复用原会话。", {
        "threadId": _STRING, "prompt": _STRING,
    }, ("threadId", "prompt")),
    _tool("set_thread_title", "设置本批次子会话标题。", {
        "threadId": _STRING, "title": _STRING,
    }, ("threadId", "title")),
    _tool("cancel_thread", "中断本批次指定子会话。", {"threadId": _STRING}, ("threadId",)),
]


class DispatchRuntime:
    def __init__(self, ctx, task, user, root: Path, env, skills, ssh_instructions, check_cancel):
        self.ctx, self.task, self.user, self.root = ctx, task, user, root
        self.env, self.skills = env, skills
        self.check_cancel = check_cancel
        self.children = {}
        self.pool = ThreadPoolExecutor(max_workers=_MAX_CHILDREN)
        self.project_id = f"dispatch-{task['id']}"
        self.host_id = "local"
        self.instructions = (
            f"本批次已接入真实原生会话工具 list_projects/list_hosts/create_thread/list_threads/"
            f"wait_threads/read_thread/send_message_to_thread/set_thread_title/cancel_thread。"
            f"项目 project_id={self.project_id}，host_id=local，工作目录={root}。"
            "create_thread 的 row 是 task.csv 数据行号，从 1 开始。按工具 schema 传参，"
            "这些工具通过 Worker 调用本机 Codex App Server，返回真实会话 ID。"
            "每行只能有一个会话，最多同时运行三个。每次先检查 list_threads，再派发空闲行。"
            "沿用本批次授权，创建和续跑均继承所选模型、思考程度及两个 Skill。"
            "子会话以结构化结果回报 outcome 为 completed/blocked/failed，摘要为 summary。"
            "会话与 Codex 桌面端共享服务，人工可实时介入；人工中断或接管后停止自动续发该会话。"
            f"Skill 的 Python 脚本可使用本机解释器 {sys.executable}，支持 Python 3.12 语法及 openpyxl。"
            "必须跟进全部子会话并报告具体阻塞。" + ssh_instructions
        )
        self.ssh_instructions = ssh_instructions

    @property
    def running(self):
        return any(child['status'] == 'running' for child in self.children.values())

    def _snapshot(self, child):
        return {key: child[key] for key in (
            'row', 'threadId', 'turnId', 'title', 'status', 'result', 'error',
        )} | {'hostId': self.host_id, 'cursor': str(child['revision'])}

    def sync(self):
        for child in self.children.values():
            future = child.get('future')
            if future is not None and future.done():
                future.result()
            if child['saved_revision'] == child['revision']:
                continue
            self.ctx.conn.execute(
                """UPDATE dispatch_children SET thread_id=?, turn_id=?, title=?, status=?,
                   result_text=?, error=? WHERE task_id=? AND row_number=?""",
                (child['threadId'], child['turnId'], child['title'], child['status'], child['result'],
                 child['error'], self.task['id'], child['row']),
            )
            child['saved_revision'] = child['revision']

    def handle(self, params):
        from .dispatch_worker import DeliveryError
        try:
            self.check_cancel()
            self.sync()
            name, args = params.get('tool'), params.get('arguments', {})
            if not isinstance(args, dict):
                raise DeliveryError('工具参数必须为对象')
            result = self._dispatch(name, args)
            return {'success': True, 'contentItems': [{'type': 'inputText', 'text': json.dumps(result, ensure_ascii=False)}]}
        except (DeliveryError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            # TaskCancelled must escape to the parent delivery cancellation handler.
            from .dispatch_worker import TaskCancelled
            if isinstance(exc, TaskCancelled):
                raise
            return {'success': False, 'contentItems': [{'type': 'inputText', 'text': str(exc)}]}

    def _child(self, args):
        from .dispatch_worker import DeliveryError
        child = self.children.get(args.get('threadId'))
        if child is None:
            raise DeliveryError('该会话不属于本批次，或需要先核对中断前的会话')
        return child

    def _dispatch(self, name, args):
        from .dispatch_worker import DeliveryError
        if name == 'list_projects':
            return {'projects': [{'id': self.project_id, 'project_id': self.project_id, 'name': self.task['title'],
                                  'host_id': self.host_id, 'hostId': self.host_id, 'is_remote': False,
                                  'path': str(self.root), 'isGitRepository': False}], 'max_concurrent': _MAX_CHILDREN}
        if name == 'list_hosts':
            return {'hosts': [{'id': self.host_id, 'host_id': self.host_id, 'name': os.uname().nodename}]}
        if name == 'list_threads':
            self.sync()
            rows = self.ctx.conn.execute('SELECT * FROM dispatch_children WHERE task_id=? ORDER BY row_number',
                                         (self.task['id'],)).fetchall()
            return {'threads': [self._snapshot(self.children[r['thread_id']]) if r['thread_id'] in self.children else {
                'row': r['row_number'], 'threadId': r['thread_id'], 'hostId': self.host_id,
                'status': r['status'], 'result': r['result_text'], 'error': r['error'],
            } for r in rows]}
        if name == 'create_thread':
            return self.create(args)
        if name == 'wait_threads':
            targets = args['targets']
            if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
                raise DeliveryError('每次等待 1 至 8 个本批次会话')
            children = [self._child(target) for target in targets]
            timeout = min(60, max(0, int(args.get('timeoutMs', 1000)) / 1000))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.check_cancel()
                self.sync()
                if any(c['status'] != 'running' and str(c['revision']) != t.get('afterCursor')
                       for c, t in zip(children, targets)):
                    break
                time.sleep(0.1)
            return {'threads': [self._snapshot(child) for child in children]}
        child = self._child(args)
        if name == 'read_thread':
            return self._snapshot(child)
        if name == 'set_thread_title':
            title = self._text(args, 'title', 256)
            if child['status'] == 'running':
                raise DeliveryError('运行中的会话已在创建时设置标题，结束后可再次修改')
            self._resume(child)
            try:
                child['client'].call('thread/name/set', {'threadId': child['threadId'], 'name': title})
            finally:
                from .dispatch_worker import _stop
                child['client'].call('thread/unsubscribe', {'threadId': child['threadId']})
                _stop(child['client'].proc)
                child['client'] = None
                child['released'] = True
            child['title'] = title
            child['revision'] += 1
            self.sync()
            return self._snapshot(child)
        if name == 'send_message_to_thread':
            if child['status'] in {'running', 'unknown', 'interrupted'}:
                raise DeliveryError('会话仍在运行或状态未知，请先核对')
            if sum(c['status'] == 'running' for c in self.children.values()) >= _MAX_CHILDREN:
                raise DeliveryError('三个子会话正在运行，请等待空闲后继续续跑')
            self._start_turn(child, self._text(args, 'prompt', 65536))
            return self._snapshot(child)
        if name == 'cancel_thread':
            if child['status'] == 'running':
                child['cancelled'] = True
            return self._snapshot(child)
        raise DeliveryError('未知的子会话工具')

    @staticmethod
    def _text(args, name, maximum):
        value = args.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f'{name} 不能为空且最多 {maximum} 字符')
        return value

    def create(self, args):
        from .dispatch_worker import DeliveryError, _connect_shared, _id, _stop
        row_number = args.get('row')
        rows = list(csv.DictReader(io.StringIO(self.task['csv'])))
        if type(row_number) is not int or not 1 <= row_number <= len(rows):
            raise DeliveryError('row 必须为本批次有效的 CSV 数据行号')
        if args.get('project_id', self.project_id) != self.project_id or args.get('host_id', self.host_id) != self.host_id:
            raise DeliveryError('只能在 list_projects 返回的本批次项目和主机创建会话')
        existing = self.ctx.conn.execute('SELECT * FROM dispatch_children WHERE task_id=? AND row_number=?',
                                         (self.task['id'], row_number)).fetchone()
        if existing:
            if existing['thread_id'] in self.children:
                return self._snapshot(self.children[existing['thread_id']])
            return {'threadId': existing['thread_id'], 'hostId': self.host_id, 'status': existing['status'],
                    'error': existing['error'], 'row': row_number}
        if sum(c['status'] == 'running' for c in self.children.values()) >= _MAX_CHILDREN:
            raise DeliveryError('三个子会话正在运行，请等待空闲后继续派发')
        title = self._text(args, 'title', 256)
        prompt = self._text(args, 'prompt', 65536)
        dispatch_id = args.get('dispatch_id') or f"{self.task['id']}:{row_number}"
        if not isinstance(dispatch_id, str) or len(dispatch_id) > 256:
            raise DeliveryError('dispatch_id 最多 256 字符')
        self.ctx.conn.execute(
            'INSERT INTO dispatch_children(task_id,row_number,dispatch_id,title,status) VALUES (?,?,?,?,?)',
            (self.task['id'], row_number, dispatch_id, title, 'starting'),
        )
        proc = None
        child = None
        try:
            cwd = self.root / 'children' / str(row_number)
            cwd.mkdir(parents=True, mode=0o700)
            for path in (cwd.parent, cwd):
                os.chown(path, self.user.uid, self.user.gid)
            client = _connect_shared(self.ctx, self.user, cwd, self.env)
            proc = client.proc
            client.call('initialize', {'clientInfo': {'name': 'codexctl_site', 'version': '0.1.0'},
                                       'capabilities': {'experimentalApi': True}})
            client.send({'method': 'initialized'})
            row = rows[row_number - 1]
            snapshot = json.loads(self.task['target_snapshot'])
            target = snapshot.get('targets', [snapshot])[row_number - 1]
            instructions = (
                f"只执行本批次第 {row_number} 行。源站 {row['网站']}，目标域名 {row['新域名']}，"
                f"折扣 {row['折扣']}，备注 {row['备注']}。按已交接 Skill 连续完成采集、部署、验收和归档。"
                f"本行部署目标为 {json.dumps(target, ensure_ascii=False)}。"
                f"所需两个 Skill 绝对路径为 {json.dumps(self.skills, ensure_ascii=False)}。"
                f"task.csv 和 targets.json 位于 {self.root}。使用绝对路径读取。"
                f"Skill 的 Python 脚本可使用本机解释器 {sys.executable}。"
                "你是本行已经创建的独立执行会话，直接执行本行任务，无需再次分发。"
                "仅操作本行目标站。同站写入互斥。" + self.ssh_instructions
            )
            thread_options = {
                'cwd': str(cwd), 'approvalPolicy': 'never', 'sandbox': self.task['sandbox'],
                'historyMode': 'legacy',
                'developerInstructions': instructions,
                'config': {'sandbox_workspace_write.network_access': True,
                           'shell_environment_policy.inherit': 'all',
                           'shell_environment_policy.set': self.env,
                           'shell_environment_policy.ignore_default_excludes': True},
            }
            started = client.call('thread/start', thread_options)
            thread_id = _id(started, 'thread')
            client.thread_id = thread_id
            child = dict(row=row_number, threadId=thread_id, turnId=None, title=title, status='starting',
                         result='', error='', revision=1, saved_revision=0, client=client,
                         cancelled=False, future=None, released=False, thread_options=thread_options)
            self.children[thread_id] = child
            self.sync()
            client.call('thread/name/set', {'threadId': thread_id, 'name': title})
            self._start_turn(child, prompt)
            return self._snapshot(child)
        except (OSError, ValueError) as exc:
            if proc is not None:
                _stop(proc)
            self.ctx.conn.execute('UPDATE dispatch_children SET status=?,error=? WHERE task_id=? AND row_number=?',
                                  ('unknown', str(exc), self.task['id'], row_number))
            if child is not None:
                child.update(status='unknown', error=str(exc))
            raise

    def _resume(self, child):
        from .dispatch_worker import _connect_shared, _stop
        if child.get('future') is not None:
            child['future'].result()
        if child.get('released'):
            client = _connect_shared(self.ctx, self.user, Path(child['thread_options']['cwd']), self.env)
            proc = client.proc
            try:
                client.call('initialize', {'clientInfo': {'name': 'codexctl_site', 'version': '0.1.0'},
                                           'capabilities': {'experimentalApi': True}})
                client.send({'method': 'initialized'})
                resume_options = {key: value for key, value in child['thread_options'].items() if key != 'historyMode'}
                client.call('thread/resume', {**resume_options, 'threadId': child['threadId'], 'excludeTurns': True})
                client.thread_id = child['threadId']
            except (OSError, ValueError):
                _stop(proc)
                raise
            child['client'] = client
            child['released'] = False

    def _start_turn(self, child, prompt):
        from .dispatch_worker import _id
        self._resume(child)
        child['client'].completed = None
        child['client'].result_text = ''
        child.update(status='sending', result='', error='', cancelled=False)
        child['revision'] += 1
        self.sync()
        try:
            result = child['client'].call('turn/start', {
                'threadId': child['threadId'], 'model': self.task['model'], 'effort': self.task['reasoning_effort'],
                'input': [{'type': 'text', 'text': prompt}, *self.skills],
                'outputSchema': {'type': 'object', 'properties': {
                    'outcome': {'type': 'string', 'enum': ['completed', 'blocked', 'failed']},
                    'summary': {'type': 'string'},
                }, 'required': ['outcome', 'summary'], 'additionalProperties': False},
            })
        except (OSError, ValueError) as exc:
            child.update(status='unknown', error=str(exc))
            child['revision'] += 1
            self.sync()
            raise
        child['turnId'] = _id(result, 'turn')
        child['status'] = 'running'
        child['revision'] += 1
        self.sync()
        child['future'] = self.pool.submit(self._wait, child)

    def _wait(self, child):
        from .dispatch_worker import TaskCancelled
        def check():
            if child['cancelled']:
                raise TaskCancelled('子会话已取消')
        try:
            status = child['client'].wait_turn(child['turnId'], check)
            child['result'] = child['client'].result_text
            if status == 'completed':
                try:
                    result = json.loads(child['result'])
                    child['status'] = result['outcome']
                    if child['status'] not in {'completed', 'blocked', 'failed'}:
                        raise ValueError('Invalid outcome')
                except (ValueError, KeyError, TypeError):
                    child.update(status='unknown', error='子会话结束但缺少有效的结构化结果，请核对回复')
            else:
                child['status'] = status
        except TaskCancelled:
            try:
                child['client'].call('turn/interrupt', {'threadId': child['threadId'], 'turnId': child['turnId']})
                child['status'] = 'interrupted'
            except (OSError, ValueError):
                child.update(status='unknown', error='子会话中断回执未确认')
        except (OSError, ValueError) as exc:
            child.update(status='unknown', error=str(exc))
        finally:
            from .dispatch_worker import _stop
            try:
                child['client'].call('thread/unsubscribe', {'threadId': child['threadId']})
            except (OSError, ValueError):
                pass
            _stop(child['client'].proc)
            child['client'] = None
            child['released'] = True
            child['revision'] += 1

    def close(self):
        from .dispatch_worker import _stop
        for child in self.children.values():
            child['cancelled'] = True
        self.pool.shutdown(wait=True)
        self.sync()
        for child in self.children.values():
            if child['client'] is not None:
                _stop(child['client'].proc)
