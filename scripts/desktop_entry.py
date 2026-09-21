"""Portable GUI / bundled bridge worker entry point."""
from pathlib import Path
import sys

root = Path(sys._MEIPASS) / 'bridge-runtime' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))

if __name__ == '__main__':
    if sys.argv[1:2] == ['--bridge-worker']:
        from codex_model_manager.core.managed_bridge import worker_main
        raise SystemExit(worker_main(sys.argv[2:]))
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CodexModelManager.Desktop')
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    args = parser.parse_args()
    home = Path(sys.executable).parent if getattr(sys, 'frozen', False) else root
    from codex_model_manager.gui.app import launch
    raise SystemExit(launch(args.config or str(home / 'user-data/config.json')))
