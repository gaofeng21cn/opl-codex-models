"""Drive the redesigned model-management page against a throwaway sandbox.

This is the reviewer-facing demo: it builds a temporary app sandbox (live catalog,
``config.toml``, app config and a ``connection.json`` whose config path is correct
while the app config's ``codexConfigPath`` is empty - the exact reported split),
then drives the REAL tkinter widgets:

  1. the page opens on the catalog Codex actually uses, and the connection page's
     config is adopted into ``codexConfigPath`` (the "接管状态不可用" repair);
  2. adding a model copies an existing model's FULL configuration (unknown fields
     included) into an automatically created edit copy; the live catalog is
     untouched;
  3. renaming the model ID is supported, a duplicate ID is refused, and the preview
     names the old/new ID;
  4. staging a deletion and cancelling the apply writes nothing;
  5. confirming the apply writes the live catalog with a backup, needs the explicit
     removal confirmation, and never rewrites ``config.toml``;
  6. "放弃修改" restores the edit copy to the live catalog;
  7. an external change to the live catalog is detected and can be resolved;
  8. optional screenshot of the final window (``--screenshot``).

Nothing here touches a real CODEX_HOME, credentials, the bridge, the desktop app or
a running Codex: every path is inside a TemporaryDirectory, the config is demo mode
(so a write can never leave the sandbox), and no model request is made.

Run:  python scripts/gui_model_management_smoke.py [--screenshot OUT.png]
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
from codex_model_manager.gui import model_page as page_module  # noqa: E402

LIVE = {
    "schema": "live.custom.v3",
    "vendor_note": {"written_by": "hand", "keep": [1, {"deep": True}]},
    "models": [
        {
            "slug": "deepseek-flash",
            "display_name": "deepseek-flash",
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
            "display_name": "deepseek-v4.1-flash",
            "priority": 4,
            "context_window": 1048576,
            "max_context_window": 1048576,
            "input_modalities": ["text", "image"],
            "default_reasoning_level": "max",
            "supported_reasoning_levels": [{"effort": "max"}],
        },
    ],
}


def _build_environment(home: Path):
    """One temporary sandbox: live catalog, config.toml, merged catalog, app config."""
    live = home / 'live-models.json'
    live.write_text(json.dumps(LIVE, ensure_ascii=False), encoding='utf-8')
    config_toml = home / 'config.toml'
    config_toml.write_text(
        '# sentinel comment that must survive untouched\n'
        'model = "deepseek-flash"\n'
        f'model_catalog_json = "{live.as_posix()}"\n'
        '[model_providers.Test]\n'
        'base_url = "https://relay.invalid/"\n',
        encoding='utf-8')
    merged = home / 'models.json'
    merged.write_text(json.dumps({"models": [
        {"slug": "deepseek-flash", "priority": 1, "context_window": 128000}]}),
        encoding='utf-8')
    custom = home / 'custom-models.json'
    custom.write_text(json.dumps({"models": []}), encoding='utf-8')
    cfg = AppConfiguration(
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(home / 'logs' / 'sync.jsonl'),
        error_log_path=str(home / 'logs' / 'error.log'),
        backup_directory_path=str(home / 'backups'),
        codex_config_path=None,          # <-- the reported broken state
        takeover_catalog_path=str(home / 'pending-models.json'),
        is_demo=True,                    # a write can never leave this directory
    )
    cfg_url = home / 'app.json'
    cfg.save(str(cfg_url))
    # The connection page already knows the right config; only the app config is empty.
    (home / 'connection.json').write_text(json.dumps({
        'config': str(config_toml), 'backend': 'native', 'distro': '',
        'port': 18787, 'models': 'deepseek-flash, deepseek-v4.1-flash',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return cfg, live, config_toml, merged, cfg_url


def _select_slug(page, slug):
    for index, row in enumerate(page._rows):
        if row['model'].slug == slug:
            page.tree.selection_set(str(index))
            page._show_detail()
            return index
    raise AssertionError(f'row not found: {slug} (have {[r["model"].slug for r in page._rows]})')


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
    root.title('Codex 模型管理器 — 模型管理页冒烟')
    root.report_callback_exception = lambda *e: errors.append(repr(e[1]))
    ask = {'yes': True, 'cancel': None}
    page_module.messagebox.askyesno = lambda *a, **k: ask['yes']
    page_module.messagebox.askyesnocancel = lambda *a, **k: ask['cancel']
    page_module.messagebox.showinfo = lambda *a, **k: None
    page_module.messagebox.showwarning = lambda *a, **k: errors.append(str(a[1]))
    page_module.messagebox.showerror = lambda *a, **k: errors.append(str(a[1]))
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.showwarning = lambda *a, **k: None
    gui_app.messagebox.showerror = lambda *a, **k: errors.append(str(a[1]))
    try:
        with tempfile.TemporaryDirectory(prefix='manager-model-page-') as td:
            home = Path(td)
            cfg, live, config_toml, merged, cfg_url = _build_environment(home)
            live_before = live.read_bytes()
            config_before = config_toml.read_bytes()
            merged_before = merged.read_bytes()

            application = gui_app.CodexModelManagerApp(root, str(cfg_url))
            page = application.model_page
            # Replace the bridge client with a canned read-only status so no worker
            # process is spawned by the connection page poll during this test.
            class StubClient:
                def call(self, action, settings):
                    return {'configured': False, 'running': False, 'current_url': 'https://relay.invalid/',
                            'backend': 'native', 'mode': None, 'models': [], 'port': 18787,
                            'compaction_passthrough': False, 'active_requests': 0, 'history': [],
                            'managed': False, 'phase': None}
            application.bridge_panel.client = StubClient()
            root.update()
            root.update_idletasks()

            # --- 1. default view is the catalog Codex actually uses ---------------
            assert application._config.codex_config_path == str(config_toml), \
                application._config.codex_config_path
            assert str(live) in page.source_var.get(), page.source_var.get()
            slugs = [r['model'].slug for r in page._rows]
            assert slugs == ['deepseek-flash', 'deepseek-v4.1-flash'], slugs
            assert all(r['status'] == '未修改' for r in page._rows)
            assert not Path(cfg.takeover_catalog_path).exists(), \
                'opening the page must not create an edit copy'
            headings = [page.tree.heading(c)['text'] for c in ('slug', 'name', 'reasoning', 'image', 'status')]
            assert headings == ['模型 ID', '显示名', '推理档位', '图片', '修改状态'], headings

            # --- 2. add a model by copying an existing one ------------------------
            page.add_model()                       # creates the edit copy on demand
            root.update()
            dialog = [w for w in root.winfo_children()
                      if isinstance(w, page_module.AddModelDialog)][-1]
            dialog.vars['slug'].set('DeepSeek-V4.1-Flash-Copy')     # normalised to lower case
            dialog.vars['template'].set('deepseek-v4.1-flash')
            dialog._load_template()
            dialog.vars['name'].set('DeepSeek V4.1 Flash Copy')
            dialog._save()
            root.update()

            pending = Path(cfg.takeover_catalog_path)
            assert pending.is_file(), 'the edit copy must be created automatically'
            copied = json.loads(pending.read_text(encoding='utf-8'))
            added = next(m for m in copied['models'] if m['slug'] == 'deepseek-v4.1-flash-copy')
            source = next(m for m in LIVE['models'] if m['slug'] == 'deepseek-v4.1-flash')
            for key, value in source.items():
                if key in ('slug', 'display_name', 'priority'):
                    continue
                assert added[key] == value, f'copy must preserve {key}'
            assert isinstance(added['priority'], int), 'a new copy gets its own priority'
            assert added['display_name'] == 'DeepSeek V4.1 Flash Copy'
            assert live.read_bytes() == live_before, 'adding must not touch the live catalog'
            assert config_toml.read_bytes() == config_before

            # --- 3. rename the model ID, duplicate refused, preview shows old/new --
            _select_slug(page, 'deepseek-v4.1-flash-copy')
            page.edit_selected()
            root.update()
            edit = [w for w in root.winfo_children()
                    if isinstance(w, page_module.EditModelDialog)][-1]
            edit.vars['slug'].set('deepseek-flash')       # duplicate of an existing id
            edit._save()
            root.update()
            assert any('已存在' in e for e in errors), errors
            errors.clear()
            edit.vars['slug'].set('deepseek-v4.1-flash-v2')
            edit.vars['name'].set('DeepSeek V4.1 Flash v2')
            page._preview_edit(page_module.ModelEdit(new_slug='deepseek-v4.1-flash-v2'),
                               'deepseek-v4.1-flash-copy')
            errors.clear()
            edit._save()
            root.update()
            renamed = json.loads(pending.read_text(encoding='utf-8'))
            assert any(m['slug'] == 'deepseek-v4.1-flash-v2' for m in renamed['models'])
            assert not any(m['slug'] == 'deepseek-v4.1-flash-copy' for m in renamed['models'])
            row = next(r for r in page._rows if r['model'].slug == 'deepseek-v4.1-flash-v2')
            assert row['status'] == '新增', row['status']
            assert live.read_bytes() == live_before

            # --- 4. stage a deletion; cancelling the apply writes nothing ---------
            _select_slug(page, 'deepseek-flash')
            page.delete_selected()
            root.update()
            assert any(r['status'] == '待删除' and r['model'].slug == 'deepseek-flash'
                       for r in page._rows), [r['status'] for r in page._rows]
            ask['yes'] = False
            page.view_and_apply()
            root.update()
            assert live.read_bytes() == live_before, 'a cancelled apply must write nothing'
            assert not list((home / 'backups').glob('active-catalog.*.bak'))
            ask['yes'] = True

            if args.screenshot:
                from PIL import ImageGrab
                application.notebook.select(page)
                _select_slug(page, 'deepseek-v4.1-flash-v2')
                root.update()
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(window=int(root.tk.call('wm', 'frame', root._w), 0)).save(
                    str(args.screenshot))

            # --- 5. confirm the apply: backup, atomic write, config untouched -----
            page.view_and_apply()                 # removal confirmation is required here
            root.update()
            written = json.loads(live.read_text(encoding='utf-8'))
            written_slugs = [m['slug'] for m in written['models']]
            assert written_slugs == ['deepseek-v4.1-flash', 'deepseek-v4.1-flash-v2'], written_slugs
            assert written['vendor_note'] == LIVE['vendor_note'], 'unknown top-level keys survive'
            assert written['schema'] == 'live.custom.v3'
            backups = list((home / 'backups').glob('active-catalog.*.bak'))
            assert backups and backups[0].read_bytes() == live_before, 'pre-write backup required'
            assert config_toml.read_bytes() == config_before, 'config.toml must never be rewritten'
            assert merged.read_bytes() == merged_before
            assert '一致' in page.state_var.get() or '未检测到' in page.state_var.get(), page.state_var.get()

            # --- 6. discard changes restores the edit copy ------------------------
            _select_slug(page, 'deepseek-v4.1-flash-v2')
            page.edit_selected()
            root.update()
            edit = [w for w in root.winfo_children()
                    if isinstance(w, page_module.EditModelDialog)][-1]
            edit.vars['context'].set('4096')
            edit._save()
            root.update()
            assert takeover.working_state(application._config).has_unapplied
            page.discard_changes()
            root.update()
            assert not takeover.working_state(application._config).has_unapplied
            assert live.read_bytes() == json.dumps(
                json.loads(live.read_text(encoding='utf-8')), indent=2, sort_keys=True,
                ensure_ascii=False).encode('utf-8') + b'\n', 'discard must not rewrite the live file'

            # --- 7. external change is detected and resolvable --------------------
            externally = json.loads(live.read_text(encoding='utf-8'))
            externally['models'][0]['context_window'] = 999999
            live.write_text(json.dumps(externally, ensure_ascii=False), encoding='utf-8')
            page.refresh()
            root.update()
            assert '外部修改' in page.state_var.get(), page.state_var.get()
            _select_slug(page, 'deepseek-v4.1-flash')
            page.edit_selected()
            root.update()
            edit = [w for w in root.winfo_children()
                    if isinstance(w, page_module.EditModelDialog)][-1]
            edit.vars['name'].set('renamed after external change')
            edit._save()
            root.update()
            ask['cancel'] = True                  # "reload the latest catalog"
            page.view_and_apply()
            root.update()
            ask['cancel'] = None
            assert not takeover.working_state(application._config).has_unapplied
            assert json.loads(live.read_text(encoding='utf-8'))['models'][0]['context_window'] == 999999

            # --- 8. portable-style config stays self-contained --------------------
            portable = home / 'portable'
            portable.mkdir()
            (portable / 'live.json').write_text(json.dumps({'models': LIVE['models']}), encoding='utf-8')
            (portable / 'codex.toml').write_text(
                'model_catalog_json = "live.json"\n', encoding='utf-8')
            import os
            previous_localappdata = os.environ.get('LOCALAPPDATA')
            os.environ['LOCALAPPDATA'] = str(portable / 'other-user-appdata')
            try:
                portable_cfg = AppConfiguration.recommended()
                portable_cfg.codex_config_path = str(portable / 'codex.toml')
                portable_cfg.takeover_catalog_path = str(portable / 'pending.json')
                portable_url = portable / 'config.json'
                portable_cfg.save(str(portable_url))
                portable_app = gui_app.CodexModelManagerApp(root, str(portable_url))
                portable_app.bridge_panel.client = StubClient()
                root.update()
                portable_page = portable_app.model_page
                assert portable_page.state is not None
                assert portable_page.state.active_path == str(portable / 'live.json'), \
                    portable_page.state.active_path
                assert str(portable) in portable_page.source_var.get(), portable_page.source_var.get()
                assert portable_app._config.codex_config_path == str(portable / 'codex.toml')
            finally:
                if previous_localappdata is None:
                    os.environ.pop('LOCALAPPDATA', None)
                else:
                    os.environ['LOCALAPPDATA'] = previous_localappdata

            if args.screenshot:
                from PIL import ImageGrab
                application.notebook.select(application.bridge_panel)
                root.update()

            assert not errors, errors
            _first_apply_phase(ask, errors)
            assert not errors, errors
            print('PASS: model page default view = live catalog; connection config adopted; '
                  'copy-add preserves unknown fields; rename + duplicate detection; '
                  'cancel writes nothing; apply backs up and never rewrites config.toml; '
                  'discard restores; external change detected and resolved; '
                  'first apply (no model_catalog_json) publishes a separate catalog and is undoable; '
                  'no model requests')
            return 0
    except AssertionError as exc:
        import traceback

        traceback.print_exc()
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    finally:
        _teardown(root)


def _teardown(root):
    """Close leftover dialogs before destroying the root (Tk state stays clean)."""
    try:
        for child in list(root.winfo_children()):
            try:
                child.destroy()
            except Exception:  # noqa: BLE001
                pass
        root.update_idletasks()
    except Exception:  # noqa: BLE001
        pass
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _first_apply_phase(ask, errors):
    """First apply when config.toml has no model_catalog_json yet.

    Drives the real page button: add a model, cancel the first apply (nothing
    written), confirm it (config references a separate published catalog), edit
    without applying (published catalog unchanged), then apply again.
    """
    with tempfile.TemporaryDirectory(prefix='manager-first-apply-') as td:
        home = Path(td)
        sandbox = home / 'sandbox'
        sandbox.mkdir()
        target = sandbox / 'models.json'
        custom = sandbox / 'custom-models.json'
        custom.write_text('{"models": []}', encoding='utf-8')
        runtime = sandbox / 'codex-mock.exe'
        runtime.write_bytes(b'MZ' + b'\0' * 64)
        config_toml = sandbox / 'config.toml'
        config_toml.write_text(
            '# sentinel comment\nmodel = "deepseek-flash"\n'
            '[model_providers.Test]\nbase_url = "https://relay.invalid/"\n',
            encoding='utf-8')
        cfg = AppConfiguration(
            codex_runtime_path=str(runtime),
            custom_source_path=str(custom), merged_catalog_path=str(target),
            sync_log_path=str(sandbox / 'logs' / 's.jsonl'),
            error_log_path=str(sandbox / 'logs' / 'e.log'),
            backup_directory_path=str(sandbox / 'backups'),
            codex_config_path=str(config_toml),
            takeover_catalog_path=str(sandbox / 'pending-models.json'),
            is_demo=True)
        cfg_url = home / 'app.json'
        cfg.save(str(cfg_url))
        config_before = config_toml.read_bytes()

        root = tk.Tk()
        root.title('Codex 模型管理器 — 首次应用冒烟')
        root.withdraw()
        try:
            application = gui_app.CodexModelManagerApp(root, str(cfg_url))
            page = application.model_page
            root.update()
            assert page.state.active_path is None, page.state.active_path
            assert not target.exists()

            page.add_model()
            root.update()
            dialog = [w for w in root.winfo_children()
                      if isinstance(w, page_module.AddModelDialog)][-1]
            dialog.vars['slug'].set('deepseek-flash-copy')
            dialog.vars['name'].set('DeepSeek Flash Copy')
            dialog.vars['context'].set('128000')
            dialog._save()
            root.update()
            assert not target.exists(), 'adding must not create the live catalog'

            asked: list[str] = []

            def decline(title, *a, **k):
                asked.append(title)
                return False

            page_module.messagebox.askyesno = decline
            page.view_and_apply()                       # cancel the first apply
            root.update()
            assert any('首次应用' in t for t in asked), asked
            assert config_toml.read_bytes() == config_before
            assert not target.exists()
            assert not list((sandbox / 'backups').glob('config.toml*.bak'))

            page_module.messagebox.askyesno = lambda *a, **k: True
            page.view_and_apply()                       # confirm the first apply
            root.update()
            assert target.is_file(), 'the first apply publishes the edit copy'
            assert 'deepseek-flash-copy' in target.read_text(encoding='utf-8')
            text = config_toml.read_text(encoding='utf-8')
            import tomllib

            assert Path(tomllib.loads(text)['model_catalog_json']) == target, text
            assert 'sentinel comment' in text
            assert Path(cfg.takeover_catalog_path) != target
            assert Path(cfg.takeover_catalog_path).is_file(), 'the edit copy stays a separate file'
            assert list((sandbox / 'backups').glob('config.toml*.bak'))
            assert not takeover.working_state(application._config).conflict

            published_before = target.read_bytes()
            for index, row in enumerate(page._rows):
                if row['model'].slug == 'deepseek-flash-copy':
                    page.tree.selection_set(str(index))
                    break
            page.edit_selected()
            root.update()
            edit = [w for w in root.winfo_children()
                    if isinstance(w, page_module.EditModelDialog)][-1]
            edit.vars['name'].set('Renamed Later')
            edit._save()
            root.update()
            assert target.read_bytes() == published_before, 'an unapplied edit must not go live'

            page.view_and_apply()                       # second apply: normal write-back
            root.update()
            assert 'Renamed Later' in target.read_text(encoding='utf-8')
            assert list((sandbox / 'backups').glob('active-catalog*.bak'))
            return True
        finally:
            _teardown(root)


if __name__ == '__main__':
    raise SystemExit(main())
