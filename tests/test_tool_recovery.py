import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from codex_model_manager.core.bridge import BridgeServer, translate_request, _sse_response
from codex_model_manager.core.tool_recovery import HINT, MODES, policy, recover_context, stale_diagnostic_only

STALE = '本轮没有可调用的终端执行工具（`exec` 或 `exec_command`），因此无法实际执行 `pwd`。未执行任何命令，未修改文件。'


def body():
    return {'model': 'gpt-6-astra', 'stream': False, 'input': [
        {'type': 'additional_tools', 'tools': [{'type': 'namespace', 'name': 'functions', 'tools': [
            {'type': 'custom', 'name': 'exec', 'description': 'PRIVATE_SCHEMA',
             'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'PRIVATE_GRAMMAR'}}]}]},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': STALE}]},
        {'role': 'user', 'content': 'PRIVATE_USER: 执行 pwd'}]}


def recover(payload):
    return recover_context(payload, translate_request(payload)[1])


def test_recovery_only_changes_copy_and_preserves_declarations_and_user():
    original = body(); saved = copy.deepcopy(original)
    original['instructions'] = 'PRIVATE_INSTRUCTIONS'
    saved = copy.deepcopy(original)
    result, meta = recover(original)
    assert original == saved
    assert meta['removed_messages'] == 1 and meta['hint_added']
    assert result['input'][0] == original['input'][0]
    assert result['input'][-1] == original['input'][-1]
    assert result['instructions'] == original['instructions']
    assert result['stream'] == original['stream']
    assert 'PRIVATE' not in json.dumps(meta)


def test_idempotent_hint_and_conservative_work_preservation():
    payload = body()
    work = {'role': 'assistant', 'content': STALE + '\n工作成果：已经修改了图 2，接下来检查图例。'}
    quoted_user = {'role': 'user', 'content': STALE}
    payload['input'][1:1] = [work, quoted_user,
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': STALE}]},
        {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'old-c', 'input': 'PRIVATE_CODE'},
        {'type': 'custom_tool_call_output', 'call_id': 'old-c', 'output': STALE}]
    once, _ = recover(payload)
    twice, _ = recover(once)
    assert once == twice
    for item in payload['input'][1:6]:
        assert item in once['input']
    assert sum(i.get('role') == 'developer' for i in once['input']) == 1


@pytest.mark.parametrize('changes,reason', [
    ({'previous_response_id': 'server-private'}, 'server_side_history'),
    ({'conversation': {'id': 'server-private'}}, 'server_side_history'),
    ({'input': 'text only'}, 'exec_not_declared_now'),
    ({'input': []}, 'exec_not_declared_now'),
])
def test_incomplete_or_hidden_history_is_not_claimed_repaired(changes, reason):
    payload = body(); payload.update(changes)
    result, meta = recover(payload)
    assert result == payload and meta['skip_reason'] == reason
    assert not meta['hint_added']


def test_inherited_exec_does_not_authorize_recovery():
    source = body(); declared = translate_request(source)[1]
    payload = {'input': [{'role': 'user', 'content': 'go'}]}
    inherited = translate_request(payload, declared)[1]
    result, meta = recover_context(payload, inherited)
    assert result == payload and meta['skip_reason'] == 'exec_not_declared_now'


def test_known_inventory_can_be_removed_but_unknown_text_is_preserved():
    msg = {'role': 'assistant', 'content':
        '本轮实际可见的工具中没有命令执行入口，因此无法在 `/tmp/private-project` 下执行 `pwd`。这是依据本轮工具列表作出的判断，未尝试执行，也未修改文件。\n\n'
        '本轮可见工具名称为：\n```text\nfunctions.functions__wait\nfunctions.clock__sleep\n```\n'
        '其中 `functions.functions__wait` 只能等待已经启动的执行任务，不能启动命令。本轮没有 `functions.exec`、`exec_command` 或其他终端执行工具。'}
    assert stale_diagnostic_only(msg)
    assert not stale_diagnostic_only(dict(msg, content=msg['content'] + '\n保留重要研究结果'))
    assert not stale_diagnostic_only(dict(msg, role='user'))


def test_explicit_empty_tools_do_not_reenable_anything():
    payload = body(); payload['input'][0]['tools'] = []
    result, meta = recover(payload)
    assert result == payload and meta['skip_reason'] == 'exec_not_declared_now'


@pytest.mark.parametrize('value', [{'mode':'invalid'}, {'mode':[]}, {}, {'mode':'off','extra':True}])
def test_invalid_mode_file_is_refused(tmp_path, value):
    path = tmp_path / 'mode.json'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        policy(str(path), True, False)


def test_context_recovery_requires_explicit_model_scope():
    with pytest.raises(ValueError):
        BridgeServer('https://relay.invalid', context_recovery=True)


class FakeRelay(BaseHTTPRequestHandler):
    received = []
    def log_message(self, *_):
        pass
    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        payload = json.loads(raw)
        type(self).received.append((raw, payload))
        converted = any(t.get('type') == 'function' and t.get('name') == 'exec' for t in payload.get('tools', []))
        call = {'type': 'function_call' if converted else 'custom_tool_call', 'name': 'exec',
                'call_id': 'private-call', 'arguments' if converted else 'input': '{"input":"PRIVATE_CODE"}' if converted else 'PRIVATE_CODE'}
        response = {'id':'private-response','status':'completed','output':[call]}
        raw = _sse_response(response) if payload.get('stream') else json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream' if payload.get('stream') else 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


@pytest.mark.parametrize('stream', [False, True])
def test_four_modes_switch_without_restart_preserve_calls_and_isolate_models(tmp_path, stream):
    FakeRelay.received = []
    relay = ThreadingHTTPServer(('127.0.0.1',0), FakeRelay)
    worker = threading.Thread(target=relay.serve_forever,daemon=True);worker.start()
    settings = tmp_path / 'mode.json';settings.write_text('{"mode":"off"}')
    record = tmp_path / 'meta.jsonl'
    bridge = BridgeServer(f'http://127.0.0.1:{relay.server_port}',port=0,
        scoped_models=frozenset({'gpt-6-astra'}),experiment_mode_file=str(settings),record_path=str(record))
    bw = threading.Thread(target=bridge.serve_forever,daemon=True);bw.start();assert bridge.wait_ready()
    try:
        for mode,(protocol,context) in MODES.items():
            settings.write_text(json.dumps({'mode':mode}))
            original = body();original['stream']=stream
            original['input'].insert(-1,{'type':'custom_tool_call','name':'exec','call_id':'history-id','input':'OLD_CODE'})
            original['input'].insert(-1,{'type':'custom_tool_call_output','call_id':'history-id','output':'OLD_OUTPUT'})
            raw=json.dumps(original,indent=3).encode()
            req=Request(f'http://127.0.0.1:{bridge.bound_port}/responses',raw,
                headers={'Content-Type':'application/json','Authorization':'Bearer PRIVATE_AUTH'})
            with urlopen(req,timeout=5) as response:
                output=response.read()
            sent_raw,sent=FakeRelay.received[-1]
            assert bool(sent.get('tools')) == protocol
            assert sent['stream'] == (False if protocol else stream)
            assert (STALE in json.dumps(sent,ensure_ascii=False)) == (not context)
            hints = [c.get('text') for i in sent['input'] if i.get('role') == 'developer'
                     and isinstance(i.get('content'), list) for c in i['content']]
            assert (HINT in hints) == context
            assert any(i.get('call_id')=='history-id' for i in sent['input'])
            assert b'custom_tool_call' in output and b'private-call' in output
            if mode=='off':assert sent_raw==raw
        # Both toggles enabled still leave a different model byte-for-byte intact.
        other=body();other['model']='deepseek-v4.1-flash';raw=json.dumps(other,indent=2).encode()
        with urlopen(Request(f'http://127.0.0.1:{bridge.bound_port}/responses',raw,
                             headers={'Content-Type':'application/json'}),timeout=5) as r:r.read()
        assert FakeRelay.received[-1][0] == raw
    finally:
        bridge.stop();bw.join(3);relay.shutdown();relay.server_close();worker.join(3)
    logs=record.read_text()
    assert all(value not in logs for value in ('PRIVATE_AUTH','PRIVATE_USER','PRIVATE_SCHEMA','PRIVATE_GRAMMAR',
        'PRIVATE_CODE','OLD_CODE','OLD_OUTPUT','private-call','private-response',STALE))
    rows=[json.loads(s) for s in logs.splitlines()]
    assert [r['experiment_mode'] for r in rows] == [*MODES,'out_of_scope']
    assert rows[0]['request_tools']['items'][0]['type']=='custom'
    assert rows[2]['context_recovery']['removed_messages']==1
