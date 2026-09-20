"""GUI-to-backend bridge management, without shell command interpolation."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from ..core.managed_bridge import BridgeActionError, clean_env, worker_command
from ..core.wsl_adapter import to_linux_path, wsl_exe


def source_root():
    if getattr(sys, 'frozen', False): return Path(sys._MEIPASS) / 'bridge-runtime'
    return Path(__file__).resolve().parents[3]


class BridgeClient:
    def __init__(self):
        self._paths = {}

    def backend(self, settings):
        selected = settings.get('backend', 'auto')
        if selected != 'auto': return selected
        try:
            data = tomllib.loads(Path(settings['config']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise BridgeActionError('无法读取所选 Codex 配置，请检查文件路径。') from None
        return 'wsl' if data.get('desktop', {}).get('runCodexInWindowsSubsystemForLinux') else 'native'

    def linux_path(self, path, distro):
        if path.startswith('/'): return path
        key = (path, distro)
        if key not in self._paths: self._paths[key] = to_linux_path(path, distro)
        return self._paths[key]

    def call(self, action, settings):
        settings = dict(settings); backend = self.backend(settings)
        if backend == 'wsl' and os.name == 'nt':
            exe = wsl_exe(); distro = settings.get('distro', '').strip()
            if not exe: raise BridgeActionError('未找到 WSL。请安装 WSL 或改选 Windows 后端。')
            if not distro: raise BridgeActionError('请填写 Codex 使用的 WSL 发行版，例如 Ubuntu。')
            script = self.linux_path(str(source_root()/'scripts/bridge_worker.py'), distro)
            settings['config'] = self.linux_path(settings['config'], distro)
            if settings.get('allowed_root'): settings['allowed_root'] = self.linux_path(settings['allowed_root'], distro)
            command = [exe, '--distribution', distro, '--exec', 'python3', '-B', script]
        else:
            command = worker_command()
        try:
            result = subprocess.run(command, input=json.dumps({'action': action, 'settings': settings}).encode(),
                                    capture_output=True, timeout=45, env=clean_env(),
                                    **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        except subprocess.TimeoutExpired:
            raise BridgeActionError('后台操作超时。请刷新状态确认结果，不要重复启用。') from None
        except OSError:
            raise BridgeActionError('无法启动后台管理程序，请检查 Python/WSL 安装。') from None
        try: reply = json.loads(result.stdout)
        except ValueError:
            raise BridgeActionError('后台没有返回有效状态。WSL 模式需要 Python 3.11 或更新版本。') from None
        if not reply.get('ok'): raise BridgeActionError(reply.get('error', '后台操作未完成。'))
        data = reply['result']; data['backend'] = backend
        return data
