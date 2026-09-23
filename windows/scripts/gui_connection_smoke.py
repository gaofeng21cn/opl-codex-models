"""Drive actual tkinter widgets against a temporary config and real local service.

No model requests, real Codex configuration, or credentials. The desktop-exited
precondition is stubbed ONLY inside this test process, for the temporary target.
"""
import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from codex_model_manager.core import managed_bridge as manager
from codex_model_manager.core.app_config import AppConfiguration
from codex_model_manager.gui.app import CodexModelManagerApp
from codex_model_manager.gui import bridge_panel


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--screenshot', type=Path); args = ap.parse_args()
    errors = []
    with tempfile.TemporaryDirectory(prefix='manager-gui-smoke-') as td:
        home = Path(td); target = home/'codex.toml'
        merged = home/'models.json'; custom = home/'custom-models.json'
        model = {'slug':'gpt-6-astra','display_name':'GPT','description':'test','priority':1,
                 'visibility':'list','context_window':128000,'max_context_window':128000,'input_modalities':['text']}
        merged.write_text(json.dumps({'models':[model]})); custom.write_text('{"models":[]}')
        before = (f'# sentinel\nmodel_provider="Test"\nmodel_catalog_json="{merged.as_posix()}"\n'
                  '[model_providers.Test]\nbase_url="https://relay.invalid/"\n').encode()
        target.write_bytes(before)
        cfg = AppConfiguration(custom_source_path=str(custom), merged_catalog_path=str(merged),
            sync_log_path=str(home/'sync.jsonl'), error_log_path=str(home/'error.log'),
            backup_directory_path=str(home/'backups'), codex_config_path=str(target))
        cfg.save(str(home/'app.json'))
        root = tk.Tk(); root.title('Codex manager isolated GUI test')
        root.report_callback_exception = lambda *e: errors.append(e[0].__name__)
        app = CodexModelManagerApp(root, str(home/'app.json')); panel = app.bridge_panel
        class LocalClient:
            def call(self, action, settings):
                assert Path(settings['config']) == target
                result = manager.dispatch(action, settings); result['backend'] = 'native'; return result
        panel.client = LocalClient(); panel.backend.set('native')
        old_running = manager.desktop_running; manager.desktop_running = lambda _: False
        old_ask = bridge_panel.messagebox.askyesno; old_error = bridge_panel.messagebox.showerror
        bridge_panel.messagebox.askyesno = lambda *a, **k: True
        bridge_panel.messagebox.showerror = lambda *a, **k: errors.append(a[1])
        def wait(predicate):
            deadline = time.monotonic()+20
            while time.monotonic() < deadline:
                root.update()
                if predicate(): return
                time.sleep(.03)
            raise AssertionError('GUI operation timed out: ' + panel.note.get())
        try:
            wait(lambda: panel.snapshot is not None and not panel.busy)
            assert not panel.snapshot['configured'] and target.read_bytes() == before
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); panel.port.set(str(sock.getsockname()[1]))
            panel.models.set('gpt-6-astra, deepseek-v4.1-flash')
            panel.start_button.invoke()
            wait(lambda: not panel.busy and panel.snapshot.get('running'))
            assert panel.snapshot['configured'] and panel.snapshot['compaction_passthrough']
            assert sorted(panel.snapshot['models']) == ['deepseek-v4.1-flash','gpt-6-astra']
            for key in ('off','context','both','protocol'):
                panel.mode.set(bridge_panel.MODE_LABELS[key]); panel.mode_button.invoke()
                wait(lambda: not panel.busy and panel.snapshot.get('mode')==key)
            panel.restart_button.invoke()
            wait(lambda: not panel.busy)
            assert panel.snapshot['running']
            if args.screenshot:
                from PIL import ImageGrab
                root.update(); args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(window=int(root.tk.call('wm', 'frame', root._w), 0)).save(args.screenshot)
            panel.details_button.invoke(); root.update()
            assert panel.details.winfo_ismapped()
            panel.details_button.invoke(); root.update()
            assert not panel.details.winfo_ismapped()
            # Model tab still renders and filters its live catalog independently.
            app.notebook.select(app.model_page); app.model_page.search_var.set('gpt-6'); root.update()
            assert len(app.model_page._rows) == 1, [r['model'].slug for r in app.model_page._rows]
            app.model_page.tree.selection_set('0'); app.model_page._show_detail()
            assert 'gpt-6-astra' in app.model_page.detail.get('1.0','end')
            app.notebook.select(panel); panel.restore_button.invoke()
            wait(lambda: not panel.busy and not panel.snapshot['configured'])
            assert target.read_bytes() == before and not panel.snapshot['running']
            assert not errors, errors
            print('PASS: real GUI widgets; start, reconnect/status, 0/A/B/AB, two-model scope, restart, exact restore, model tab; no model requests')
        finally:
            state = manager.load_state(target)
            if state: manager.stop_owned(state)
            manager.desktop_running = old_running
            bridge_panel.messagebox.askyesno = old_ask; bridge_panel.messagebox.showerror = old_error
            try:
                if root.winfo_exists(): root.destroy()
            except tk.TclError:
                pass


if __name__ == '__main__': main()
