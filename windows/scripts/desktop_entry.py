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
    parser.add_argument('--smoke-test', metavar='REPORT',
                        help='Run isolated GUI checks and write a UTF-8 report; no user config is loaded.')
    args = parser.parse_args()
    if args.smoke_test:
        import contextlib
        import runpy
        import traceback
        with open(args.smoke_test, 'w', encoding='utf-8') as report:
            with contextlib.redirect_stdout(report), contextlib.redirect_stderr(report):
                print('frozen=' + str(bool(getattr(sys, 'frozen', False))))
                print('executable=' + sys.executable)
                sys.argv = ['gui_model_management_smoke.py']
                try:
                    runpy.run_path(str(root / 'scripts/gui_model_management_smoke.py'),
                                   run_name='__main__')
                except SystemExit:
                    raise
                except Exception:
                    traceback.print_exc()
                    raise SystemExit(1)
        raise SystemExit(0)
    home = Path(sys.executable).parent if getattr(sys, 'frozen', False) else root
    from codex_model_manager.gui.app import launch
    raise SystemExit(launch(args.config or str(home / 'user-data/config.json')))
