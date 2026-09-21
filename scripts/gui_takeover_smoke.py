"""Drive the real takeover widgets against a temporary sandbox.

This exercises the actual tkinter widgets (not just the core functions) end to end
inside a throwaway directory: import a copy of a live catalog, edit a model, view
the diff, cancel a write-back, write back, then undo. It asserts the exact bytes of
the temporary target at every step.

Nothing here touches a real CODEX_HOME, credentials, the bridge, the desktop app,
or a running Codex: the only paths are under a TemporaryDirectory, the app config
points at them, and the sandbox is removed afterwards. No model requests are made.

Run:  python scripts/gui_takeover_smoke.py [--screenshot OUT.png]
Exit: 0 pass, 1 failure, 2 no display available (skipped, not a pass).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from codex_model_manager.core import takeover  # noqa: E402
from codex_model_manager.core.app_config import AppConfiguration  # noqa: E402
from codex_model_manager.gui import app as gui_app  # noqa: E402

LIVE = {
    "schema": "live.custom.v3",
    "vendor_note": {"written_by": "hand", "keep": [1, {"deep": True}]},
    "models": [
        {
            "slug": "gpt-6-astra",
            "display_name": "GPT-6 Astra",
            "description": "live catalog entry",
            "priority": 3,
            "context_window": 128000,
            "max_context_window": 128000,
            "input_modalities": ["text"],
            "provider_metadata": {"route": "relay-live"},
            "supported_reasoning_levels": [
                {"effort": "high", "description": "provider high", "budget": 200}],
            "default_reasoning_level": "high",
        },
        {
            "slug": "deepseek-v4.1-flash",
            "display_name": "DeepSeek V4.1 Flash",
            "priority": 4,
            "context_window": 1048576,
            "max_context_window": 1048576,
            "default_reasoning_level": "max",
            "supported_reasoning_levels": [{"effort": "max"}],
        },
    ],
}


def _build_environment(home: Path):
    """A complete temporary app sandbox: live catalog, config.toml, merged catalog."""
    live = home / 'live-models.json'
    live.write_text(json.dumps(LIVE, ensure_ascii=False), encoding='utf-8')
    config_toml = home / 'config.toml'
    config_toml.write_text(
        '# sentinel comment that must survive untouched\n'
        'model = "gpt-6-astra"\n'
        f'model_catalog_json = "{live.as_posix()}"\n'
        '[model_providers.Test]\n'
        'base_url = "https://relay.invalid/"\n',
        encoding='utf-8')
    merged = home / 'models.json'
    merged.write_text(json.dumps({"models": [
        {"slug": "gpt-6-astra", "priority": 1, "context_window": 128000}]}),
        encoding='utf-8')
    custom = home / 'custom-models.json'
    custom.write_text(json.dumps({"models": []}), encoding='utf-8')
    cfg = AppConfiguration(
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(home / 'logs' / 'sync.jsonl'),
        error_log_path=str(home / 'logs' / 'error.log'),
        backup_directory_path=str(home / 'backups'),
        codex_config_path=str(config_toml),
        takeover_catalog_path=str(home / 'pending-models.json'),
        is_demo=True,  # demo sandbox: a write can never leave this directory
    )
    cfg_url = home / 'app.json'
    cfg.save(str(cfg_url))
    return cfg, live, config_toml, merged, cfg_url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--screenshot', type=Path)
    args = ap.parse_args()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f'SKIP: no display for tkinter ({exc})', file=sys.stderr)
        return 2

    errors: list[str] = []
    root.title('Codex 模型管理器 — 接管临时沙箱冒烟')
    root.report_callback_exception = lambda *e: errors.append(str(e[1]))
    try:
        with tempfile.TemporaryDirectory(prefix='manager-takeover-smoke-') as td:
            home = Path(td)
            cfg, live, config_toml, merged, cfg_url = _build_environment(home)
            live_before = live.read_bytes()
            config_before = config_toml.read_bytes()
            merged_before = merged.read_bytes()

            application = gui_app.CodexModelManagerApp(root, str(cfg_url))
            panel = application.takeover_panel
            root.update()

            # --- both directories are named before anything is imported ----------
            application.catalog_view.set(gui_app.VIEW_PENDING)
            application.refresh()
            root.update()
            assert '生效配置引用目录' in panel.paths_var.get(), panel.paths_var.get()
            assert '待应用目录' in panel.paths_var.get(), panel.paths_var.get()
            assert str(live) in panel.paths_var.get()
            assert '尚未导入' in panel.state_var.get(), panel.state_var.get()
            assert not Path(cfg.takeover_catalog_path).exists()

            # --- import a copy (the dialog is stubbed to choose the live catalog) -
            gui_app.filedialog.askopenfilename = lambda *a, **k: str(live)
            gui_app.messagebox.showinfo = lambda *a, **k: None
            panel.import_copy()
            root.update()
            pending = Path(cfg.takeover_catalog_path)
            assert pending.is_file(), 'import must create the pending copy'
            copied = json.loads(pending.read_text(encoding='utf-8'))
            assert copied == LIVE, 'import must preserve every unknown field'
            assert live.read_bytes() == live_before, 'import must not touch the live catalog'
            assert application._config.takeover_import['modelCount'] == 2

            # --- the pending view lists the copied models, editable -----------------
            application.refresh()
            root.update()
            application.search_var.set('gpt-6')
            root.update()
            assert application.listbox.size() == 1, application.listbox.size()
            application.listbox.selection_set(0)
            application.show_detail()
            assert '待应用' in application.detail.get('1.0', 'end')
            application.search_var.set('')
            root.update()

            # --- edit a model through the real dialog path -------------------------
            from codex_model_manager.core.custom_models import ModelEdit

            model = next(m for m in application._models_cache if m.slug == 'gpt-6-astra')
            gui_app.EditModelDialog(root, model, lambda edit: application._on_edit_pending('gpt-6-astra', edit))
            root.update()
            dialog = [w for w in root.winfo_children() if isinstance(w, gui_app.EditModelDialog)][-1]
            dialog.vars['context'].set('262144')
            dialog.vars['max_context'].set('262144')
            dialog.vars['efforts'].set('low,high,max')
            dialog.vars['default'].set('max')
            dialog._save()
            root.update()

            edited = json.loads(pending.read_text(encoding='utf-8'))
            assert edited['models'][0]['context_window'] == 262144
            assert edited['models'][0]['default_reasoning_level'] == 'max'
            assert edited['models'][0]['provider_metadata'] == {'route': 'relay-live'}
            assert edited['schema'] == 'live.custom.v3'
            assert live.read_bytes() == live_before, 'editing the copy must not touch the live catalog'

            # --- the diff is shown, and cancelling writes nothing ------------------
            state = takeover.inspect(application._config)
            assert state.diff and [c.slug for c in state.diff.modified] == ['gpt-6-astra']
            rendered = takeover.render_state(state)
            assert 'context_window: 128000 -> 262144' in rendered
            assert 'base_url' not in rendered, 'only model data may be rendered'

            gui_app.messagebox.askyesno = lambda *a, **k: False  # user cancels
            gui_app.messagebox.showwarning = lambda *a, **k: errors.append(str(a[1]))
            panel.apply_writeback()
            root.update()
            assert live.read_bytes() == live_before, 'cancelling must write nothing'
            assert not list((home / 'backups').glob('active-catalog.*.bak')), \
                'a cancelled write-back must not leave a backup behind'
            assert config_toml.read_bytes() == config_before
            assert merged.read_bytes() == merged_before

            # --- confirm, write back, and check the result -------------------------
            gui_app.messagebox.askyesno = lambda *a, **k: True
            panel.apply_writeback()
            root.update()
            written = json.loads(live.read_text(encoding='utf-8'))
            assert written['models'][0]['context_window'] == 262144
            assert written['models'][0]['provider_metadata'] == {'route': 'relay-live'}
            assert written['vendor_note'] == LIVE['vendor_note']
            assert written['models'][1] == LIVE['models'][1]
            assert config_toml.read_bytes() == config_before, 'config.toml must never be rewritten'
            backups = list((home / 'backups').glob('active-catalog.*.bak'))
            assert backups and backups[0].read_bytes() == live_before, 'pre-write backup required'

            if args.screenshot:
                from PIL import ImageGrab
                root.update()
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(window=int(root.tk.call('wm', 'frame', root._w), 0)).save(
                    str(args.screenshot))

            # --- undo restores the exact original bytes ---------------------------
            panel.undo()
            root.update()
            assert live.read_bytes() == live_before, 'undo must restore the exact bytes'
            assert application._config.last_takeover is None

            # --- a removal still needs an explicit confirmation --------------------
            panel.import_copy()
            trimmed = json.loads(pending.read_text(encoding='utf-8'))
            trimmed['models'] = [m for m in trimmed['models'] if m['slug'] != 'deepseek-v4.1-flash']
            pending.write_text(json.dumps(trimmed, ensure_ascii=False), encoding='utf-8')
            asked: list[str] = []
            gui_app.messagebox.askyesno = lambda title, *a, **k: (asked.append(title), False)[1]
            live_before_removal = live.read_bytes()
            panel.apply_writeback()
            root.update()
            assert any('删除' in t for t in asked), asked
            assert live.read_bytes() == live_before_removal, 'declining the removal writes nothing'

            assert not errors, errors
            print('PASS: takeover import -> pending view -> edit -> diff -> cancel -> '
                  'write-back -> undo -> removal confirmation; live catalog, config.toml '
                  'and merged catalog verified untouched where expected; no model requests')
            return 0
    except AssertionError as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


if __name__ == '__main__':
    raise SystemExit(main())
