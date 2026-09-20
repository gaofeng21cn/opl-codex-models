"""Build portable Windows GUI and WSL worker sources. No user state."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name != 'nt': raise SystemExit('Run this build with Windows Python 3.12.')
    if (ROOT/'dist/CodexModelManager/user-data').exists():
        raise SystemExit('Build stopped: dist contains user-data. Move the existing portable folder to a safe location before rebuilding.')
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--console', '--hide-console', 'hide-early', '--name', 'CodexModelManager',
               '--icon', str(ROOT/'src/codex_model_manager/gui/assets/app.ico'),
               '--paths', str(ROOT/'src'), '--collect-submodules', 'codex_model_manager', '--collect-all', 'sv_ttk',
               '--add-data', str(ROOT/'src/codex_model_manager') + ';bridge-runtime/src/codex_model_manager',
               '--add-data', str(ROOT/'scripts/bridge_worker.py') + ';bridge-runtime/scripts',
               str(ROOT/'scripts/desktop_entry.py')]
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = ROOT/'dist/CodexModelManager'
    for name in ('QUICKSTART.md', 'README.md', 'LICENSE', 'SOURCE_NOTICE.md',
                 'THIRD_PARTY_NOTICES.txt', 'SUN_VALLEY_LICENSE.txt'):
        shutil.copy2(ROOT/name, bundle/name)
    for p in bundle.rglob('__pycache__'): shutil.rmtree(p)
    archive = shutil.make_archive(str(ROOT/'dist/CodexModelManager-Windows-portable'), 'zip', ROOT/'dist', 'CodexModelManager')
    print(archive)


if __name__ == '__main__': main()
