import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codex_model_manager.core.bridge import BridgeServer, compaction_reason, _sse_response
from test_tool_recovery import body, FakeRelay, STALE


@pytest.mark.parametrize('mode,model,path,stream', [
    ('protocol', 'gpt-6-astra', '/responses', True),
    ('protocol', 'gpt-6-astra', '/v1/responses', True),
    ('both', 'gpt-6-astra', '/responses/compact', False),
    ('both', 'gpt-6-astra', '/v1/responses/compact', False),
    ('protocol', 'deepseek-v4.1-flash', '/responses', True),
    ('protocol', 'deepseek-v4.1-flash', '/responses/compact', False),
    ('context', 'gpt-6-astra', '/responses', True),
    ('off', 'gpt-6-astra', '/responses', True),
])
def test_compact_is_byte_preserving_and_streams_before_upstream_finishes(tmp_path, mode, model, path, stream):
    release = threading.Event(); received = []
    response = {'id': 'private-response', 'status': 'completed', 'output': [
        {'type': 'compaction', 'id': 'private-item', 'encrypted_content': 'PRIVATE_ENCRYPTED'}]}
    response_raw = _sse_response(response) if stream else json.dumps(response, indent=3).encode()
    prefix = b': native compact progress\n\n' if stream else b''

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, raw))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
            self.send_header('Content-Length', str(len(prefix) + len(response_raw)))
            self.send_header('X-Request-Id', 'test-upstream-id')
            self.end_headers()
            if stream:
                self.wfile.write(prefix); self.wfile.flush()
                assert release.wait(5)
            self.wfile.write(response_raw)

    relay = ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    rw = threading.Thread(target=relay.serve_forever, daemon=True); rw.start()
    settings = tmp_path/'mode.json'; settings.write_text(json.dumps({'mode': mode}))
    record = tmp_path/'record.jsonl'
    bridge = BridgeServer(f'http://127.0.0.1:{relay.server_port}', port=0,
                          scoped_models=frozenset({model}), experiment_mode_file=str(settings), record_path=str(record))
    bw = threading.Thread(target=bridge.serve_forever, daemon=True); bw.start(); assert bridge.wait_ready()
    conn = http.client.HTTPConnection('127.0.0.1', bridge.bound_port, timeout=5)
    try:
        payload = body(); payload.update(model=model, stream=stream)
        payload['input'] += [
            {'type': 'custom_tool_call', 'id': 'ctc_private', 'name': 'exec', 'call_id': 'private-call', 'input': 'PRIVATE_CODE'},
            {'type': 'custom_tool_call_output', 'id': 'ctco_private', 'call_id': 'private-call', 'output': 'PRIVATE_RESULT'}]
        if not path.endswith('/compact'): payload['input'].append({'type': 'compaction_trigger'})
        raw = json.dumps(payload, indent=4, ensure_ascii=False).encode()
        conn.request('POST', path, raw, {'Content-Type': 'application/json', 'Authorization': 'Bearer PRIVATE_AUTH'})
        reply = conn.getresponse(); assert reply.status == 200
        assert reply.getheader('X-Request-Id') == 'test-upstream-id'
        if stream:
            # This read must complete before the fake upstream is allowed to finish.
            assert reply.read(len(prefix)) == prefix
            release.set()
        assert reply.read() == response_raw
        assert received == [(path, raw)]
    finally:
        release.set(); conn.close(); bridge.stop(); bw.join(3)
        relay.shutdown(); relay.server_close(); rw.join(3)
    logs = record.read_text(); row = json.loads(logs)
    assert row['request_kind'] == 'compaction' and row['mode'] == 'passthrough'
    assert row['context_recovery'] == {'enabled': False}
    assert row['experiment_mode'] == mode and row['upstream_status'] == 200
    assert all(s not in logs for s in ('PRIVATE_', STALE, 'private-call', 'private-response', 'private-item'))


def test_history_and_prompt_mentions_do_not_disable_translation():
    for item in ({'type': 'compaction', 'encrypted_content': 'opaque'},
                 {'type': 'context_compaction'}, {'type': 'compaction_trigger'}):
        payload = {'input': [item, {'role': 'user', 'content': 'compaction_trigger /responses/compact'}]}
        assert compaction_reason(payload, '/responses') is None
    assert compaction_reason({'input': 'compaction_trigger'}, '/responses') is None
    assert compaction_reason({'input': [{'type': 'compaction_trigger'}]}, '/responses') == 'compaction_trigger'


def test_compacted_history_returns_to_tool_translation(tmp_path):
    FakeRelay.received = []
    relay = ThreadingHTTPServer(('127.0.0.1', 0), FakeRelay)
    rw = threading.Thread(target=relay.serve_forever, daemon=True); rw.start()
    bridge = BridgeServer(f'http://127.0.0.1:{relay.server_port}', port=0, scoped_models=frozenset({'gpt-6-astra'}))
    bw = threading.Thread(target=bridge.serve_forever, daemon=True); bw.start(); assert bridge.wait_ready()
    conn = http.client.HTTPConnection('127.0.0.1', bridge.bound_port, timeout=5)
    try:
        payload = body(); payload['stream'] = True
        compact = {'type': 'compaction', 'id': 'opaque-id', 'encrypted_content': 'opaque-context'}
        payload['input'].insert(1, compact)
        conn.request('POST', '/responses', json.dumps(payload).encode(), {'Content-Type': 'application/json'})
        reply = conn.getresponse(); assert reply.status == 200
        output = reply.read()
        sent = FakeRelay.received[-1][1]
        assert compact in sent['input'] and sent['stream'] is False
        assert any(t['type'] == 'function' and t['name'] == 'exec' for t in sent['tools'])
        assert b'custom_tool_call' in output and b'private-call' in output
    finally:
        conn.close(); bridge.stop(); bw.join(3); relay.shutdown(); relay.server_close(); rw.join(3)


def test_compact_upstream_error_keeps_status_and_does_not_retry(tmp_path):
    received = []
    error = b'{"error":{"message":"PRIVATE_UPSTREAM_ERROR"}}'
    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(502); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(error)))
            self.send_header('X-Request-Id', 'upstream-error-id'); self.end_headers(); self.wfile.write(error)
    relay = ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    rw = threading.Thread(target=relay.serve_forever, daemon=True); rw.start()
    record = tmp_path/'record.jsonl'
    bridge = BridgeServer(f'http://127.0.0.1:{relay.server_port}', port=0, record_path=str(record),
                          scoped_models=frozenset({'gpt-6-astra'}))
    bw = threading.Thread(target=bridge.serve_forever, daemon=True); bw.start(); assert bridge.wait_ready()
    conn = http.client.HTTPConnection('127.0.0.1', bridge.bound_port, timeout=5)
    try:
        payload = body(); payload['stream'] = True; payload['input'].append({'type': 'compaction_trigger'})
        raw = json.dumps(payload).encode()
        conn.request('POST', '/responses', raw, {'Content-Type': 'application/json'})
        reply = conn.getresponse()
        assert reply.status == 502 and reply.getheader('X-Request-Id') == 'upstream-error-id'
        assert reply.read() == error and received == [raw]
    finally:
        conn.close(); bridge.stop(); bw.join(3); relay.shutdown(); relay.server_close(); rw.join(3)
    assert 'PRIVATE_UPSTREAM_ERROR' not in record.read_text()
    assert json.loads(record.read_text())['upstream_status'] == 502
