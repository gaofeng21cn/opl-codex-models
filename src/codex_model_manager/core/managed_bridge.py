"""Bridge lifecycle on the Codex backend's OS. Never retrieves credentials."""
from __future__ import annotations
import contextlib
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from urllib.parse import urlsplit
import uuid
from .bridge import BridgeServer
from .tool_recovery import MODES

IDENTITY = 'codex-local-workarounds-v1'


class BridgeActionError(Exception):
    """Safe UI messages; never wrap subprocess output or request content."""


def atomic(path, raw):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.chmod(name, 0o600); os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def replace_url(raw, provider, expected_url, new_url):
    text = raw.decode('utf-8')
    parsed = tomllib.loads(text)
    try:
        current = parsed['model_providers'][provider]['base_url']
    except KeyError:
        raise BridgeActionError('目标 provider/base_url 不存在，未修改配置。') from None
    if current != expected_url:
        raise BridgeActionError('目标 base_url 被其他操作改动，拒绝覆盖。')
    header = re.compile(r'''^\s*\[model_providers\.(?:"([^"\\]+)"|'([^']+)'|([\w-]+))\]\s*(?:#.*)?$''')
    value = re.compile(r'''^(\s*base_url\s*=\s*)("(?:\\.|[^"\\])*"|'[^']*')(\s*(?:#[^\r\n]*)?)(\r?\n)?$''')
    active = False
    changed = 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            match = header.fullmatch(stripped)
            active = bool(match and provider in match.groups())
        if active and (m := value.fullmatch(line)):
            lines[i] = m[1] + json.dumps(new_url, ensure_ascii=False) + m[3] + (m[4] or '')
            changed += 1
    if changed != 1:
        raise BridgeActionError('不支持此配置排版，无法唯一定位 base_url；未修改。')
    result = ''.join(lines).encode('utf-8')
    wanted = copy.deepcopy(parsed)
    wanted['model_providers'][provider]['base_url'] = new_url
    if tomllib.loads(result.decode('utf-8')) != wanted:
        raise BridgeActionError('配置差异超出单个 base_url，未修改。')
    return result


def clean_env():
    markers = ('API_KEY', 'APIKEY', 'TOKEN', 'SECRET', 'PASSWORD', 'AUTH', 'CREDENTIAL', 'BEARER')
    return {k: v for k, v in os.environ.items()
            if not any(m in k.upper() for m in markers) and k not in ('WSLENV', 'PYTHONPATH')}


def process_identity(pid):
    if not isinstance(pid, int) or pid <= 0: return None
    if os.name != 'nt':
        try:
            p = Path('/proc') / str(pid)
            return {'pid': pid, 'start_ticks': (p/'stat').read_text().rsplit(') ', 1)[1].split()[19],
                    'exe': os.readlink(p/'exe')}
        except (OSError, ValueError, IndexError): return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        size = wintypes.DWORD(32768); path = ctypes.create_unicode_buffer(size.value)
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)): return None
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)): return None
        born = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {'pid': pid, 'start_ticks': str(born), 'exe': path.value}
    finally: kernel.CloseHandle(handle)


def desktop_running(config):
    ps = (Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
          if os.name == 'nt' else Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'))
    if ps.is_file():
        command = "@(Get-Process -Name ChatGPT,Codex -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'Codex' -or $_.Path -like '*OpenAI.Codex*' }).Count; exit 0"
        result = subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=15, env=clean_env(),
                                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        try:
            if result.returncode or int(result.stdout.decode('utf-8-sig').strip()) != 0: return True
        except ValueError:
            raise BridgeActionError('无法确认 Codex 是否退出；未修改连接。') from None
    elif os.name == 'nt':
        raise BridgeActionError('无法检查 Codex 进程；未修改连接。')
    if os.name != 'nt':
        for p in Path('/proc').glob('[0-9]*'):
            try:
                if b'app-server' not in (p/'cmdline').read_bytes().split(b'\0'): continue
                home = next((v.split(b'=', 1)[1] for v in (p/'environ').read_bytes().split(b'\0')
                             if v.startswith(b'CODEX_HOME=')), None)
                if home and Path(os.fsdecode(home)).resolve() == config.parent.resolve(): return True
            except (OSError, ValueError): pass
    return False


def require_closed(config):
    if desktop_running(config):
        raise BridgeActionError('请先从托盘完全退出 Codex，再点击此按钮。切换已运行桥的模式不需要退出。')


def state_path(config):
    return config.with_name(config.name + '.tool-diag-state.json')


def load_state(config):
    path = state_path(config)
    if not path.exists(): return None
    state = json.loads(path.read_text())
    try: same = os.path.samefile(state['config'], config)
    except (OSError, KeyError, TypeError): same = False
    if not same: raise BridgeActionError('已有桥记录属于另一个配置文件，未操作。')
    return state


def save_state(config, state):
    atomic(state_path(config), json.dumps(state, ensure_ascii=False, indent=2).encode())


@contextlib.contextmanager
def transaction_lock(config):
    path = config.with_name(config.name + '.bridge-manager.lock')
    with path.open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0: f.write(b'0'); f.flush()
            f.seek(0)
            try: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise BridgeActionError('另一个窗口正在操作桥，请稍后刷新。') from None
            try: yield
            finally: f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: raise BridgeActionError('另一个窗口正在操作桥，请稍后刷新。') from None
            try: yield
            finally: fcntl.flock(f, fcntl.LOCK_UN)


def valid_url(value, local=False):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/', '/v1', '/v1/')):
        raise BridgeActionError('连接地址必须是不含凭据的 HTTP(S) 根地址或 /v1 地址。')
    if local and (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port):
        raise BridgeActionError('已有桥地址不是受支持的本机回环地址。')
    return url


def health(state):
    if not state: return None
    url = valid_url(state['probe_url'], local=True)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{url.port}/healthz', timeout=1.5) as r:
            data = json.loads(r.read(65536))
        if data.get('identity') != IDENTITY or data.get('instance') != state['instance']: return None
        return data
    except (OSError, ValueError): return None


def selected(config):
    raw = config.read_bytes(); data = tomllib.loads(raw.decode())
    provider = data.get('model_provider'); url = data.get('model_providers', {}).get(provider, {}).get('base_url')
    if not isinstance(provider, str) or not isinstance(url, str):
        raise BridgeActionError('配置中没有明确的 provider/base_url，请选择正在使用的 config.toml。')
    valid_url(url)
    return raw, provider, url


def status(config):
    raw, provider, current = selected(config); state = load_state(config); live = health(state)
    active = bool(state and current == state.get('probe_url') and state.get('phase') in ('active', 'prepared'))
    mode = None
    if state and state.get('experiment_mode_file'):
        try: mode = json.loads(Path(state['experiment_mode_file']).read_text()).get('mode')
        except (OSError, ValueError): pass
    history = []
    if state and state.get('results'):
        try:
            with (Path(state['results'])/'metadata.jsonl').open('rb') as f:
                f.seek(0, 2); f.seek(max(0, f.tell()-65536)); lines = f.read().splitlines()
            for line in lines[-15:]:
                try: entry = json.loads(line)
                except ValueError: continue
                history.append({k: entry.get(k) for k in ('ts', 'mode', 'request_kind', 'upstream_status', 'error')})
        except OSError: pass
    return {'config': str(config), 'provider': provider, 'current_url': current, 'configured': active,
            'running': bool(live), 'mode': live.get('experiment_mode') if live else mode,
            'models': live.get('models') if live else (state or {}).get('models', []),
            'port': urlsplit(state['probe_url']).port if state else 18787,
            'upstream': state.get('original_url') if state else current,
            'compaction_passthrough': bool(live and live.get('compaction_passthrough')),
            'active_requests': live.get('active_requests', 0) if live else 0,
            'managed': bool(state), 'phase': (state or {}).get('phase'), 'history': history,
            'config_sha256': digest(raw)}


def stop_owned(state):
    identity = state.get('process')
    if not identity or process_identity(identity.get('pid')) != identity: return False
    os.kill(identity['pid'], signal.SIGTERM)
    deadline = time.monotonic()+5
    while process_identity(identity['pid']) == identity and time.monotonic() < deadline:
        if os.name != 'nt':
            try: os.waitpid(identity['pid'], os.WNOHANG)
            except ChildProcessError: pass
        time.sleep(.05)
    return process_identity(identity['pid']) != identity


def worker_command():
    if getattr(sys, 'frozen', False):
        worker = Path(sys.executable).with_name('CodexModelManagerWorker.exe')
        return [str(worker), '--bridge-worker']
    return [sys.executable, '-B', str(Path(__file__).resolve().parents[3]/'scripts/bridge_worker.py')]


def spawn_service(state):
    spec = Path(state['results'])/'service.json'
    atomic(spec, json.dumps({k: state[k] for k in ('instance', 'probe_url', 'original_url', 'results', 'experiment_mode_file', 'models')}).encode())
    options = {'creationflags': 0x08000000 | 0x00000200} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(worker_command()+['serve', str(spec)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=clean_env(), cwd=str(Path(state['results'])), **options)
    state['process'] = process_identity(proc.pid)
    for _ in range(60):
        if proc.poll() is not None: break
        if state["process"] and process_identity(proc.pid) == state["process"] and health(state): return
        time.sleep(.1)
    if proc.poll() is None: proc.terminate(); proc.wait(timeout=5)
    raise BridgeActionError('后台桥启动失败，可能端口已被占用；连接地址未改动。')


def set_mode(config, mode):
    state = load_state(config); live = health(state)
    if not state or state.get('mode') != 'workarounds' or not live:
        raise BridgeActionError('桥尚未运行，请先启动或修复连接。')
    if live.get('active_requests') != 0:
        raise BridgeActionError('还有模型请求未结束，请等当前回合完成后切换。')
    if mode not in MODES: raise BridgeActionError('不支持的兼容模式。')
    atomic(state['experiment_mode_file'], json.dumps({'mode': mode}).encode())
    return status(config)


def enable(config, settings):
    raw, provider, current = selected(config); state = load_state(config)
    mode = settings.get('mode', 'protocol'); models = settings.get('models', [])
    if mode not in MODES or not isinstance(models, list) or not models or any(
            not isinstance(m, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,149}', m) for m in models):
        raise BridgeActionError('请选择有效的兼容模式和至少一个模型。')
    models = sorted(set(models)); port = int(settings.get('port', 18787))
    if not 1024 <= port <= 65535: raise BridgeActionError('端口应在 1024–65535 之间。')
    if state and state.get('phase') != 'restored':
        if state.get('mode') != 'workarounds' or current != state.get('probe_url'):
            raise BridgeActionError('发现未结束的诊断或连接冲突，请先恢复直连。')
        live = health(state)
        if live and not settings.get('restart'):
            if sorted(live.get('models') or []) != models or urlsplit(state['probe_url']).port != port:
                raise BridgeActionError('修改适用模型或端口需要退出 Codex 后点击“重启桥”。')
            return set_mode(config, mode)
        require_closed(config)
        if live and live.get('active_requests'): raise BridgeActionError('桥仍有请求，请等待完成。')
        if urlsplit(state['probe_url']).port != port:
            raise BridgeActionError('更换端口前请先恢复直连，再启用桥。')
        identity = state.get('process')
        owned_alive = bool(identity and process_identity(identity.get('pid')) == identity)
        if owned_alive:
            if not stop_owned(state):
                raise BridgeActionError('旧桥进程未能退出，重启已停止；未启动新桥。')
        elif live:
            raise BridgeActionError('无法确认旧桥进程身份，未停止或替换该进程。')
        if health(state):
            raise BridgeActionError('旧桥仍在响应，重启已停止。')
        # A fresh instance prevents an old listener from satisfying readiness.
        state = dict(state, instance=uuid.uuid4().hex, models=models)
        state['phase'] = 'restarting'
        save_state(config, state)
        atomic(state['experiment_mode_file'], json.dumps({'mode': mode}).encode())
        spawn_service(state); state['phase'] = 'active'; save_state(config, state)
        return status(config)
    require_closed(config)
    if settings.get('expected_sha256') and digest(raw) != settings['expected_sha256']:
        raise BridgeActionError('确认期间配置发生变化，请刷新并重新确认。')
    upstream = valid_url(current)
    if upstream.hostname in ('127.0.0.1', 'localhost', '::1'):
        raise BridgeActionError('当前已是未登记的本地地址，不能将它误认作真实上游。请先恢复原桥。')
    if state and health(state): stop_owned(state)
    instance = uuid.uuid4().hex
    results = config.parent/'model-manager-bridge'/instance; results.mkdir(parents=True, exist_ok=False)
    local = f'http://127.0.0.1:{port}' + upstream.path
    after = replace_url(raw, provider, current, local)
    mode_file = results/'mode.json'; atomic(mode_file, json.dumps({'mode': mode}).encode())
    backup = config.with_name(config.name+'.bak-tool-diag-'+instance)
    with backup.open('xb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
    os.chmod(backup, 0o600)
    state = {'config': str(config), 'phase': 'prepared', 'mode': 'workarounds', 'provider': provider,
             'original_url': current, 'probe_url': local, 'instance': instance, 'results': str(results),
             'experiment_mode_file': str(mode_file), 'models': models, 'backup': str(backup),
             'before_sha256': digest(raw), 'after_sha256': digest(after), 'manager_version': 1}
    spawn_service(state)
    wrote_config = False
    try:
        if config.read_bytes() != raw: raise BridgeActionError('准备期间配置被更新，未更改连接。')
        save_state(config, state); atomic(config, after)
        wrote_config = True
        state['phase'] = 'active'; save_state(config, state)
    except Exception:
        if not wrote_config: stop_owned(state)
        raise
    return status(config)


def restore(config):
    state = load_state(config)
    if not state: raise BridgeActionError('没有本工具的恢复记录；不会猜测旧地址。')
    require_closed(config)
    raw, provider, current = selected(config)
    if provider != state['provider']: raise BridgeActionError('当前 provider 已更改，未自动恢复。')
    before = Path(state['backup']).read_bytes()
    if digest(before) != state['before_sha256']: raise BridgeActionError('备份校验失败，配置和服务均保留。')
    if current == state['original_url']: updated = raw
    elif current != state['probe_url']: raise BridgeActionError('连接地址已被其他操作修改，未覆盖配置或停止服务。')
    elif digest(raw) == state['after_sha256']: updated = before
    else: updated = replace_url(raw, provider, current, state['original_url'])
    if config.read_bytes() != raw: raise BridgeActionError('配置刚被更新，请刷新后重试。')
    if updated != raw: atomic(config, updated)
    state.update(phase='restored', restored_sha256=digest(updated), byte_exact_restore=updated == before)
    save_state(config, state); stop_owned(state)
    return status(config)


def current_catalog(config):
    """Read the explicitly selected catalog; return model fields, never config text."""
    from ..parser import parse_models
    from dataclasses import asdict
    try:
        data = tomllib.loads(config.read_text(encoding='utf-8'))
        value = data.get('model_catalog_json')
        if not value:
            return {'path': None, 'models': [], 'message': '未配置自定义目录；运行时内建目录不在此视图中。'}
        if not isinstance(value, str): raise ValueError()
        if os.name != 'nt' and re.match(r'^[A-Za-z]:', value):
            value = '/mnt/' + value[0].lower() + value[2:].replace(chr(92), '/')
        path = Path(value).expanduser()
        if not path.is_absolute(): path = config.parent / path
        if path.stat().st_size > 16 * 1024 * 1024:
            raise BridgeActionError('模型目录过大，未读取。')
        raw = path.read_bytes()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('models'), list): raise ValueError()
        models = parse_models(b'{"models":[]}', raw)
        return {'path': str(path), 'models': [asdict(m) for m in models],
                'message': '配置引用目录（只读）；运行中的 Codex 是否已重载需另行确认。'}
    except BridgeActionError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise BridgeActionError('无法读取配置引用的模型目录，请核对路径、运行位置和 JSON 格式。') from None


def dispatch(action, settings):
    config = Path(settings.get('config', '')).expanduser()
    if not config.is_absolute() or not config.is_file(): raise BridgeActionError('请选择存在的 config.toml 文件。')
    config = config.resolve()
    if settings.get('allowed_root'):
        try: config.relative_to(Path(settings['allowed_root']).resolve())
        except ValueError: raise BridgeActionError('演示模式只允许操作演示目录内的配置。') from None
    if action == 'catalog': return current_catalog(config)
    if action == 'status': return status(config)
    if action not in ('enable', 'restore', 'mode'): raise BridgeActionError('不支持的操作。')
    with transaction_lock(config):
        if action == 'enable': return enable(config, settings)
        if action == 'restore': return restore(config)
        return set_mode(config, settings.get('mode'))


def worker_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'serve':
        spec = json.loads(Path(argv[1]).read_text())
        BridgeServer(upstream_url=spec['original_url'], host='127.0.0.1',
                     port=valid_url(spec['probe_url'], local=True).port,
                     scoped_models=frozenset(spec['models']), instance_id=spec['instance'],
                     experiment_mode_file=spec['experiment_mode_file'],
                     record_path=str(Path(spec['results'])/'metadata.jsonl')).serve_forever()
        return 0
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        result = dispatch(request['action'], request['settings'])
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=True), flush=True); return 0
    except BridgeActionError as exc: message = str(exc)
    except Exception as exc: message = '操作未完成（' + type(exc).__name__ + '）。请刷新状态；未输出配置正文或凭据。'
    print(json.dumps({'ok': False, 'error': message}, ensure_ascii=True), flush=True); return 1
