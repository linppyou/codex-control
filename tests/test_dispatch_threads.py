import json
import subprocess
import sys

import pytest

from codexctl.dispatch import tasks, worker
import codexctl.db as db
from codexctl.dispatch.threads import ThreadTools, tool_specs
from test_dispatch import task_form


class ConversationServer:
    def __init__(self):
        self.thread_id = 'parent'
        self.calls = []
        self.sent = []
        self.turns = {}
        self.bridge = None

    def send(self, value):
        self.sent.append(value)

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/start':
            return {'thread': {'id': f'child-{len(self.turns) + 1}'}}
        if method == 'turn/start':
            thread = params['threadId']
            turn = {'id': f'turn-{len(self.calls)}', 'status': 'completed',
                    'items': [{'type': 'agentMessage', 'text': 'Skill loaded'}]}
            self.turns[thread] = turn
            self.bridge.observe({'method': 'turn/started', 'params': {'threadId': thread, 'turn': turn}})
            self.bridge.observe({'method': 'item/completed', 'params': {
                'threadId': thread, 'item': turn['items'][0],
            }})
            self.bridge.observe({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': turn}})
            return {'turn': turn}
        if method == 'thread/read':
            return {'thread': {'turns': [self.turns[params['threadId']]], 'status': {'type': 'idle'}}}
        return {}


@pytest.fixture
def bridge(ctx, tmp_path):
    form = task_form(ctx)
    rows = tasks.parse_import(f'网站,新域名,备注,折扣\n{form["source"]},{form["target"]},,3\n'.encode())
    task_id = tasks.create_previews(ctx, 'alice', rows, form['dispatcher'], form['migration'])
    task = ctx.conn.execute('SELECT * FROM dispatch_tasks WHERE id=?', (task_id,)).fetchone()
    client = ConversationServer()
    skills = [{'type': 'skill', 'name': name, 'path': str(tmp_path / '.agents' / 'skills' / name / 'SKILL.md')}
              for name in ('task-dispatcher', 'html-to-wp-engine-sol')]
    result = ThreadTools(ctx, task, client, {'cwd': str(tmp_path)}, skills, lambda: None)
    client.bridge = result
    return result


def create_arguments(bridge, dispatch_id='dispatch-1'):
    return {'title': 'source.example.com 迁移 shop.example.com', 'prompt': '只测试 Skill，禁止部署',
            'dispatch_id': dispatch_id,
            'target': {'type': 'project', 'projectId': bridge.project_id, 'environment': {'type': 'local'}}}


def test_create_wait_resume_and_idempotency(bridge):
    first = bridge.create_thread(create_arguments(bridge))
    assert first['threadId'] == 'child-1' and first['status'] == 'completed'
    assert bridge.create_thread(create_arguments(bridge)) == first
    assert sum(method == 'thread/start' for method, _ in bridge.client.calls) == 1
    turn = next(params for method, params in bridge.client.calls if method == 'turn/start')
    assert [item['name'] for item in turn['input'] if item['type'] == 'skill'] == ['html-to-wp-engine-sol']
    snapshot = bridge.wait_threads({'targets': [{'threadId': 'child-1'}], 'timeoutMs': 0})
    assert snapshot['threads'][0]['result'] == 'Skill loaded'
    same = bridge.wait_threads({'targets': [{'threadId': 'child-1', 'afterCursor': first['cursor']}], 'timeoutMs': 0})
    assert same['timedOut'] and 'result' not in same['threads'][0]
    resumed = bridge.send_message_to_thread({'threadId': 'child-1', 'prompt': '继续测试，禁止部署'})
    assert resumed['threadId'] == first['threadId'] and resumed['turnId'] != first['turnId']
    assert resumed['status'] == 'completed'
    assert bridge.read_thread({'threadId': 'child-1'}) == resumed


def test_tool_callback_response_and_replay(bridge):
    request = {'id': 91, 'method': 'item/tool/call', 'params': {
        'threadId': 'parent', 'turnId': 'parent-turn', 'callId': 'call-1',
        'tool': 'create_thread', 'arguments': create_arguments(bridge),
    }}
    bridge.handle(request)
    assert bridge.client.sent[-1]['result']['success']
    result = json.loads(bridge.client.sent[-1]['result']['contentItems'][0]['text'])
    assert result['threadId'] == 'child-1'
    bridge.handle(dict(request, id=92))
    assert bridge.client.sent[-1]['id'] == 92
    assert sum(method == 'thread/start' for method, _ in bridge.client.calls) == 1


def test_scope_checks_and_pending_call(bridge):
    with pytest.raises(ValueError, match='本批次'):
        bridge.read_thread({'threadId': 'unrelated'})
    args = create_arguments(bridge)
    args['target']['projectId'] = 'unrelated'
    with pytest.raises(ValueError, match='projectId'):
        bridge.create_thread(args)
    bridge.ctx.conn.execute('INSERT INTO dispatch_tool_calls(task_id,call_id,tool) VALUES (?,?,?)',
                            (bridge.task['id'], 'pending', 'create_thread'))
    bridge.handle({'id': 5, 'params': {'threadId': 'parent', 'callId': 'pending', 'tool': 'create_thread',
                                      'arguments': create_arguments(bridge)}})
    assert not bridge.client.sent[-1]['result']['success']
    assert bridge.client.calls == []


def test_callback_is_queued_before_request_acknowledgement(tmp_path):
    script = tmp_path / 'server.py'
    script.write_text('''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'turn/start':
        print(json.dumps({'id': 50, 'method': 'item/tool/call', 'params': {'tool': 'list_projects'}}), flush=True)
        print(json.dumps({'id': request['id'], 'result': {'turn': {'id': 'root-turn'}}}), flush=True)
    elif request.get('id') == 50:
        print(json.dumps({'method': 'turn/completed', 'params': {'threadId': 'root', 'turn': {'id': 'root-turn', 'status': 'completed'}}}), flush=True)
''')
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = worker.AppServer(process)
    client.thread_id = 'root'
    client.tool_handler = lambda request: client.send({'id': request['id'], 'result': {}})
    try:
        assert client.call('turn/start', {}) == {'turn': {'id': 'root-turn'}}
        assert client.wait_turn('root-turn') == 'completed'
        assert not client.tool_requests
    finally:
        worker._stop(process)


def test_tools_describe_actual_callbacks():
    assert {tool['name'] for tool in tool_specs()} == {
        'list_projects', 'create_thread', 'send_message_to_thread', 'wait_threads', 'read_thread', 'set_thread_title',
    }
    assert all(tool['type'] == 'function' for tool in tool_specs())
    create = next(tool for tool in tool_specs() if tool['name'] == 'create_thread')
    target = create['inputSchema']['properties']['target']
    assert target['required'] == ['type', 'projectId', 'environment']
    assert target['properties']['type']['enum'] == ['project']


def test_failed_parent_turn_preserves_error_for_the_task(bridge):
    child = {'method': 'turn/completed', 'params': {'threadId': 'child', 'turn': {
        'id': 'child-turn', 'status': 'failed', 'error': {'message': 'child failure'},
    }}}
    parent = {'method': 'turn/completed', 'params': {'threadId': 'parent', 'turn': {
        'id': 'parent-turn', 'status': 'failed', 'error': {'message': 'usage limit reached'},
    }}}
    script = f'import json; print(json.dumps({child!r}), flush=True); print(json.dumps({parent!r}), flush=True)'
    process = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = worker.AppServer(process)
    client.thread_id = 'parent'
    try:
        status = client.wait_turn('parent-turn')
        worker._record(bridge.ctx, bridge.task, 'sent', client.turn_error, turn_status=status)
        saved = bridge.ctx.conn.execute('SELECT turn_status,error FROM dispatch_tasks WHERE id=?',
                                        (bridge.task['id'],)).fetchone()
        assert tuple(saved) == ('failed', 'usage limit reached')
    finally:
        worker._stop(process)


def test_stale_read_does_not_undo_completion(bridge):
    first = bridge.create_thread(create_arguments(bridge))
    bridge.client.turns[first['threadId']]['status'] = 'inProgress'
    bridge.refresh(bridge.child(first['threadId']))
    assert bridge.child(first['threadId'])['status'] == 'completed'


def test_cancellation_attempts_every_child(bridge):
    first = bridge.create_thread(create_arguments(bridge))
    second = bridge.create_thread(create_arguments(bridge, 'dispatch-2'))
    for row in bridge.rows():
        bridge.update(row, status='running')
    original = bridge.client.call

    def call(method, params):
        if method == 'turn/interrupt' and params['threadId'] == first['threadId']:
            raise ValueError('test disconnection')
        return original(method, params)

    bridge.client.call = call
    with pytest.raises(ValueError, match='test disconnection'):
        bridge.cancel_children()
    assert bridge.child(first['threadId'])['status'] == 'unknown'
    assert bridge.child(second['threadId'])['status'] == 'interrupted'


def test_existing_database_gains_receipts_without_losing_data(tmp_path):
    path = tmp_path / 'existing.db'
    original = db.connect(path)
    original.execute('DROP TABLE dispatch_tool_calls')
    original.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES (1,'test','bridge-test','test','preserved')")
    original.close()
    upgraded = db.connect(path)
    try:
        assert upgraded.execute("SELECT detail FROM audit_log WHERE action='bridge-test'").fetchone()[0] == 'preserved'
        assert upgraded.execute("SELECT count(*) FROM dispatch_tool_calls").fetchone()[0] == 0
    finally:
        upgraded.close()
