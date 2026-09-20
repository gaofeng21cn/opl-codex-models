import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest
from codex_model_manager.core import managed_bridge as manager
from codex_model_manager.core.bridge import BridgeServer
from codex_model_manager.services.bridge_client import BridgeClient


@pytest.fixture
def target(tmp_path, monkeypatch):
    config = tmp_path/'config.toml'
    config.write_bytes(b'# preserve\r\nmodel_provider="Relay"\r\nmodel="gpt-6-astra"\r\n[model_providers.Relay]\r\nbase_url="https://relay.invalid/v1" # original\r\n')
    monkeypatch.setattr(manager, 'desktop_running', lambda _: False)
    yield config
    state = manager.load_state(config)
    if state: manager.stop_owned(state)


def options(config):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    return {'config': str(config), 'mode': 'protocol', 'models': ['gpt-6-astra', 'deepseek-v4.1-flash'], 'port': port}


def test_real_background_lifecycle_reconnect_switch_restart_restore(target):
    before = target.read_bytes(); settings = options(target)
    first = manager.dispatch('enable', settings)
    assert first['configured'] and first['running'] and first['compaction_passthrough']
    state = manager.load_state(target); process = state['process']; backup = state['backup']
    assert Path(backup).read_bytes() == before
    # A separate worker process (as used after reopening a GUI) sees the service.
    reply = subprocess.run(manager.worker_command(), input=json.dumps({'action':'status','settings':settings}).encode(), capture_output=True, timeout=15)
    assert json.loads(reply.stdout)['result']['running']
    for mode in ('off', 'context', 'both', 'protocol'):
        changed = manager.dispatch('mode', dict(settings, mode=mode))
        assert changed['mode'] == mode
        assert manager.load_state(target)['process'] == process
    restarted = manager.dispatch('enable', dict(settings, restart=True))
    assert restarted['running'] and manager.load_state(target)['process'] != process
    assert manager.load_state(target)['backup'] == backup
    restored = manager.dispatch('restore', settings)
    assert not restored['configured'] and not restored['running']
    assert target.read_bytes() == before


def test_start_failure_never_points_config_at_dead_port(target):
    before = target.read_bytes(); settings = options(target)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', settings['port'])); occupied.listen()
        with pytest.raises(manager.BridgeActionError, match='启动失败'):
            manager.dispatch('enable', settings)
    assert target.read_bytes() == before
    assert not manager.state_path(target).exists()


def test_refuse_desktop_running_before_writes(target, monkeypatch):
    before = target.read_bytes(); monkeypatch.setattr(manager, 'desktop_running', lambda _: True)
    with pytest.raises(manager.BridgeActionError, match='退出 Codex'):
        manager.dispatch('enable', options(target))
    assert target.read_bytes() == before and not manager.state_path(target).exists()


def test_restore_preserves_unrelated_changes_and_refuses_address_conflict(target):
    settings = options(target); manager.dispatch('enable', settings)
    original = target.read_bytes(); target.write_bytes(original + b'new_setting=true\n')
    manager.dispatch('restore', settings)
    assert b'new_setting=true' in target.read_bytes() and b'# original' in target.read_bytes()
    manager.dispatch('enable', settings)
    state = manager.load_state(target)
    target.write_bytes(manager.replace_url(target.read_bytes(), 'Relay', state['probe_url'], 'https://other.invalid/'))
    before = target.read_bytes()
    with pytest.raises(manager.BridgeActionError, match='其他操作'):
        manager.dispatch('restore', settings)
    assert target.read_bytes() == before and manager.health(state)


def test_existing_experiment_is_adopted_without_new_backup(target):
    settings = options(target); manager.dispatch('enable', settings)
    state = manager.load_state(target); state.pop('manager_version'); state.pop('models')
    manager.save_state(target, state)
    before = target.read_bytes(); original = Path(state['backup']).read_bytes()
    result = manager.dispatch('enable', dict(settings, mode='off'))
    assert result['mode'] == 'off' and target.read_bytes() == before
    assert Path(manager.load_state(target)['backup']).read_bytes() == original
    assert len(list(target.parent.glob('*.bak-tool-diag-*'))) == 1


def test_dead_service_can_restart_without_backing_up_localhost(target):
    settings = options(target); manager.dispatch('enable', settings)
    state = manager.load_state(target); manager.stop_owned(state)
    assert manager.status(target)['configured'] and not manager.status(target)['running']
    assert manager.dispatch('enable', settings)['running']
    assert manager.load_state(target)['backup'] == state['backup']


def test_busy_mode_change_and_model_scope_change_are_refused(target, monkeypatch):
    settings = options(target); manager.dispatch('enable', settings)
    state = manager.load_state(target); mode_before = Path(state['experiment_mode_file']).read_bytes()
    live = manager.health(state)
    with monkeypatch.context() as m:
        m.setattr(manager, 'health', lambda _: dict(live, active_requests=1))
        with pytest.raises(manager.BridgeActionError, match='未结束'):
            manager.dispatch('mode', dict(settings, mode='off'))
    assert Path(state['experiment_mode_file']).read_bytes() == mode_before
    with pytest.raises(manager.BridgeActionError, match='重启桥'):
        manager.dispatch('enable', dict(settings, models=['gpt-6-astra']))


def test_pid_reuse_never_kills_another_process(monkeypatch):
    old = {'pid':123,'start_ticks':'old','exe':'python'}
    monkeypatch.setattr(manager, 'process_identity', lambda _: dict(old, start_ticks='new'))
    monkeypatch.setattr(manager.os, 'kill', lambda *_: pytest.fail('unowned process was killed'))
    assert manager.stop_owned({'process': old}) is False


def test_stale_preview_and_demo_escape_are_refused(target):
    settings = options(target); before = target.read_bytes()
    with pytest.raises(manager.BridgeActionError, match='确认期间'):
        manager.dispatch('enable', dict(settings, expected_sha256='outdated'))
    with pytest.raises(manager.BridgeActionError, match='演示'):
        manager.dispatch('enable', dict(settings, allowed_root=str(target.parent/'different')))
    assert target.read_bytes() == before


def test_worker_status_does_not_print_config_secrets(target):
    target.write_bytes(target.read_bytes() + b'api_key="PRIVATE_FAKE_SECRET"\n')
    settings = options(target)
    reply = subprocess.run(manager.worker_command(), input=json.dumps({'action':'status','settings':settings}).encode(), capture_output=True, timeout=15)
    assert json.loads(reply.stdout)['ok']
    assert b'PRIVATE_FAKE_SECRET' not in reply.stdout + reply.stderr


def test_two_models_share_name_mapping_without_a_second_bridge(tmp_path):
    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert any(t['type']=='function' and t['name']=='exec' for t in data['tools'])
            raw = json.dumps({'id':'resp_test','status':'completed','output':[{'type':'function_call','name':'functions__exec',
                'call_id':'call_test','arguments':json.dumps({'input':'return 1'})}]}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    relay = ThreadingHTTPServer(('127.0.0.1',0), Relay)
    rw = threading.Thread(target=relay.serve_forever,daemon=True); rw.start()
    bridge = BridgeServer(f'http://127.0.0.1:{relay.server_port}', port=0,
                          scoped_models=frozenset({'gpt-6-astra','deepseek-v4.1-flash'}))
    bw = threading.Thread(target=bridge.serve_forever,daemon=True); bw.start(); assert bridge.wait_ready()
    try:
        for model in ('gpt-6-astra','deepseek-v4.1-flash'):
            data = {'model':model,'input':[{'type':'additional_tools','tools':[{'type':'namespace','name':'functions','tools':[{'type':'custom','name':'exec'}]}]}]}
            with urlopen(Request(f'http://127.0.0.1:{bridge.bound_port}/responses',json.dumps(data).encode()), timeout=5) as r:
                call = json.load(r)['output'][0]
            assert call['type']=='custom_tool_call' and call['name']=='exec' and call['call_id']=='call_test'
    finally:
        bridge.stop(); bw.join(3); relay.shutdown(); relay.server_close(); rw.join(3)


def test_restart_refuses_failed_stop_without_spawning(target, monkeypatch):
    settings = options(target)
    manager.dispatch('enable', settings)
    state = manager.load_state(target)
    before = target.read_bytes()
    monkeypatch.setattr(manager, 'stop_owned', lambda _: False)
    monkeypatch.setattr(manager, 'spawn_service', lambda _: pytest.fail('must not spawn'))
    with pytest.raises(manager.BridgeActionError, match='未能退出'):
        manager.dispatch('enable', dict(settings, restart=True))
    assert target.read_bytes() == before
    assert manager.load_state(target) == state
    monkeypatch.undo()


def test_restart_rotates_instance_preserves_restore_record(target):
    settings = options(target)
    manager.dispatch('enable', settings)
    old = manager.load_state(target)
    manager.dispatch('enable', dict(settings, restart=True))
    new = manager.load_state(target)
    assert old['instance'] != new['instance']
    assert manager.health(old) is None
    assert manager.health(new)['instance'] == new['instance']
    for key in ('backup', 'original_url', 'before_sha256', 'after_sha256'):
        assert old[key] == new[key]
