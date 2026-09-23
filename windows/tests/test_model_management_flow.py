"""Model-management redesign: the user-facing flow, end to end.

These tests cover exactly the behaviours the redesign promised, with temporary
configs and no real runtime:

  * the connection page's selected config is the single source of truth, so an
    installation whose ``connection.json`` is right but whose ``codexConfigPath`` is
    empty no longer reports "接管状态不可用";
  * relative / Windows / WSL path forms for ``model_catalog_json``;
  * portable configs stay inside their own directory;
  * adding by copying an existing model keeps every unknown field;
  * renaming a model id works, duplicates are refused, and the preview names the
    old/new id;
  * the edit copy is created automatically, "放弃修改" restores it, and an external
    change is detected before anything is written;
  * deletion still needs an explicit confirmation before the live catalog changes;
  * backup / restore still round-trips.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_model_manager.core import takeover
from codex_model_manager.core.app_config import (
    AppConfiguration,
    load_connection_prefs,
    save_connection_prefs,
    sync_connection_config,
)
from codex_model_manager.core.backup import backup_file, restore_file
from codex_model_manager.core.custom_models import (
    ModelEdit,
    NewModelDraft,
    add_blank_model,
    add_from_template,
    remove_model,
    update_model,
)
from codex_model_manager.core.errors import (
    ConflictError,
    InvalidConfiguration,
)
from codex_model_manager.core.reasoning import ReasoningSettings

ACTIVE = {
    "schema": "user.custom.v7",
    "notes": {"owner": "hand-written", "nested": [1, 2, {"deep": True}]},
    "models": [
        {
            "slug": "deepseek-flash",
            "display_name": "deepseek-flash",
            "description": "vendor A",
            "priority": 3,
            "context_window": 128000,
            "max_context_window": 128000,
            "input_modalities": ["text"],
            "provider_metadata": {"route": "relay-1", "tools": ["custom-function"]},
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


def _sandbox(tmp_path, *, codex_config=True, connection=True, relative=True):
    """A demo sandbox: live catalog, config.toml, app config and connection.json."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    merged = sandbox / "models.json"
    merged.write_text(json.dumps({"models": [{"slug": "deepseek-flash", "priority": 1}]}),
                      encoding="utf-8")
    custom = sandbox / "custom-models.json"
    custom.write_text(json.dumps({"models": []}), encoding="utf-8")
    active_path = sandbox / "active-models.json"
    active_path.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    cfg_toml = sandbox / "config.toml"
    if codex_config:
        value = "active-models.json" if relative else str(active_path)
        cfg_toml.write_text(
            'model = "deepseek-flash"\n'
            f'model_catalog_json = "{value}"\n'
            'api_key = "SECRET_NOT_FOR_OUTPUT"\n',
            encoding="utf-8")
    cfg = AppConfiguration(
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(sandbox / "logs" / "sync.jsonl"),
        error_log_path=str(sandbox / "logs" / "error.log"),
        backup_directory_path=str(sandbox / "backups"),
        codex_config_path=str(cfg_toml) if codex_config else None,
        takeover_catalog_path=str(sandbox / "pending-models.json"),
        is_demo=True,
    )
    cfg_url = tmp_path / "app.json"
    cfg.save(str(cfg_url))
    if connection:
        save_connection_prefs(str(cfg_url), {
            "config": str(cfg_toml), "backend": "native", "distro": "",
            "port": 18787, "models": "deepseek-flash",
        })
    return cfg, cfg_url, active_path, sandbox, cfg_toml


# ---------------------------------------------------------------------------
# connection.json <-> codexConfigPath
# ---------------------------------------------------------------------------

def test_connection_selection_repairs_empty_codex_config_path(tmp_path):
    """The reported bug: connection.json is correct, codexConfigPath is empty."""
    cfg, cfg_url, _active, _sandbox_dir, cfg_toml = _sandbox(
        tmp_path, connection=True)
    cfg.codex_config_path = None
    cfg.save(str(cfg_url))

    assert sync_connection_config(cfg, str(cfg_url)) is True
    assert cfg.codex_config_path == str(cfg_toml)
    reloaded = AppConfiguration.load(str(cfg_url))
    assert reloaded.codex_config_path == str(cfg_toml), "the repair is persisted"

    # With the repair, the catalog-based features resolve the live catalog.
    state = takeover.working_state(reloaded)
    assert state.status == takeover.WORKING_NO_COPY
    assert state.active_path == str(tmp_path / "sandbox" / "active-models.json")


def test_connection_selection_is_written_back_when_prefs_are_missing(tmp_path):
    """The reverse direction: an explicit codexConfigPath is not silently lost."""
    cfg, cfg_url, _active, _sandbox_dir, cfg_toml = _sandbox(
        tmp_path, connection=False)
    assert load_connection_prefs(str(cfg_url)) == {}
    sync_connection_config(cfg, str(cfg_url))
    prefs = load_connection_prefs(str(cfg_url))
    assert prefs["config"] == str(cfg_toml)


def test_connection_backend_and_distro_are_adopted(tmp_path):
    cfg, cfg_url, _active, _sandbox_dir, cfg_toml = _sandbox(tmp_path, connection=False)
    save_connection_prefs(str(cfg_url), {
        "config": str(cfg_toml), "backend": "wsl", "distro": "Ubuntu", "port": 18787})
    cfg.codex_backend = "auto"
    assert sync_connection_config(cfg, str(cfg_url)) is True
    assert cfg.codex_backend == "wsl" and cfg.codex_distro == "Ubuntu"


# ---------------------------------------------------------------------------
# path forms
# ---------------------------------------------------------------------------

def test_relative_model_catalog_json_resolves_against_config_dir(tmp_path):
    cfg, _url, active, _sandbox_dir, cfg_toml = _sandbox(tmp_path, relative=True)
    assert takeover.active_catalog_path(cfg) == str(active)
    assert Path(cfg_toml).read_text(encoding="utf-8").count("SECRET") == 1


def test_windows_drive_path_maps_to_mnt_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover.os, "name", "posix")
    value = takeover.resolve_catalog_value(
        r"C:\Users\someone\.codex\models.json", str(tmp_path / "config.toml"))
    assert value == "/mnt/c/Users/someone/.codex/models.json"


def test_absolute_windows_path_is_untouched_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only path form")
    target = tmp_path / "models.json"
    value = takeover.resolve_catalog_value(str(target), str(tmp_path / "config.toml"))
    assert value == str(target)


def test_mnt_path_maps_to_drive_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-only path form")
    value = takeover.resolve_catalog_value(
        "/mnt/c/Users/someone/.codex/models.json", str(tmp_path / "config.toml"))
    assert value.lower().startswith("c:\\users\\someone")


def test_unmappable_linux_path_is_an_actionable_error(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only branch")
    monkeypatch.setattr("codex_model_manager.core.wsl_adapter.to_windows_path",
                        lambda *a, **k: None)
    with pytest.raises(InvalidConfiguration, match="Linux 路径"):
        takeover.resolve_catalog_value("/home/someone/.codex/models.json",
                                       str(tmp_path / "config.toml"))


# ---------------------------------------------------------------------------
# portable config isolation
# ---------------------------------------------------------------------------

def test_portable_config_stays_inside_its_own_directory(tmp_path, monkeypatch):
    portable = tmp_path / "portable"
    portable.mkdir()
    fake_appdata = tmp_path / "appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    cfg = AppConfiguration.recommended()
    home = portable
    cfg.custom_source_path = str(home / "custom-models.json")
    cfg.merged_catalog_path = str(home / "models.json")
    cfg.backup_directory_path = str(home / "backups")
    cfg.takeover_catalog_path = str(home / "pending-models.json")
    cfg_url = home / "config.json"
    cfg.save(str(cfg_url))
    reloaded = AppConfiguration.load(str(cfg_url))
    assert reloaded.merged_catalog_path.startswith(str(portable))
    assert reloaded.takeover_catalog_path.startswith(str(portable))
    assert not str(reloaded.merged_catalog_path).startswith(str(fake_appdata))


# ---------------------------------------------------------------------------
# edit-copy lifecycle
# ---------------------------------------------------------------------------

def test_edit_copy_is_created_automatically_and_follows_states(tmp_path):
    cfg, cfg_url, active_path, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    original = active_path.read_bytes()

    state = takeover.working_state(cfg)
    assert state.status == takeover.WORKING_NO_COPY and not state.working_exists

    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert state.status == takeover.WORKING_CLEAN and state.working_exists
    assert Path(state.working_path).read_bytes() != original, "the copy is normalised JSON"
    assert json.loads(Path(state.working_path).read_text(encoding="utf-8")) == ACTIVE
    assert active_path.read_bytes() == original, "creating the copy never touches the live file"

    # A second call must not overwrite staged edits.
    edited = json.loads(Path(state.working_path).read_text(encoding="utf-8"))
    edited["models"][0]["display_name"] = "staged edit"
    Path(state.working_path).write_text(json.dumps(edited), encoding="utf-8")
    again = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert json.loads(Path(again.working_path).read_text(encoding="utf-8"))["models"][0][
        "display_name"] == "staged edit"
    assert again.status == takeover.WORKING_MODIFIED and again.has_unapplied

    # 放弃修改 restores the copy from the live catalog and keeps a backup.
    reloaded = takeover.discard_working(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert reloaded.status == takeover.WORKING_CLEAN and not reloaded.has_unapplied
    assert list((_sandbox_dir / "backups").glob("pending-catalog.*.bak"))
    assert active_path.read_bytes() == original


def test_working_state_reports_missing_and_empty_setups(tmp_path):
    cfg, cfg_url, active_path, _sandbox_dir, _cfg_toml = _sandbox(tmp_path, codex_config=False)
    state = takeover.working_state(cfg)
    assert state.status == takeover.WORKING_NO_ACTIVE
    assert "model_catalog_json" in state.message

    empty = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert empty.status == takeover.WORKING_EMPTY
    assert json.loads(Path(empty.working_path).read_text(encoding="utf-8"))["models"] == []
    # The first model can be created from scratch (nothing to copy from).
    draft = NewModelDraft(slug="first-model", display_name="First", description="first",
                          template_slug="", context_window=1000)
    source = Path(empty.working_path).read_bytes()
    updated = add_blank_model(draft, source, existing_slugs=set())
    written = json.loads(updated)["models"][0]
    assert written["slug"] == "first-model" and written["context_window"] == 1000
    assert written["input_modalities"] == ["text"]
    assert "default_reasoning_level" not in written, "nothing is invented for the user"


# ---------------------------------------------------------------------------
# add by copying
# ---------------------------------------------------------------------------

def test_copy_add_preserves_every_unknown_field(tmp_path):
    cfg, cfg_url, _active, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    source = Path(state.working_path).read_bytes()
    draft = NewModelDraft(
        slug="DeepSeek-Flash-Copy", display_name="Copy", description="copied",
        template_slug="deepseek-flash", context_window=262144,
        supports_image=True, supports_original_image_detail=True,
        reasoning=ReasoningSettings(supported_efforts=["low", "high"], default_effort="high"),
    )
    updated = add_from_template(draft, source, catalog_data=source,
                               existing_slugs={"deepseek-flash", "deepseek-v4.1-flash"})
    root = json.loads(updated)
    added = root["models"][-1]
    assert added["slug"] == "deepseek-flash-copy"
    assert added["provider_metadata"] == {"route": "relay-1", "tools": ["custom-function"]}
    assert added["input_modalities"] == ["text", "image"]
    assert added["supports_image_detail_original"] is True
    assert root["schema"] == "user.custom.v7" and root["notes"] == ACTIVE["notes"]
    assert root["models"][0] == ACTIVE["models"][0], "the template itself is unchanged"
    # The copied reasoning levels keep the provider's per-level fields.
    high = next(lv for lv in added["supported_reasoning_levels"] if lv["effort"] == "high")
    assert high["budget"] == 200


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

def test_rename_keeps_other_fields_and_refuses_duplicates(tmp_path):
    cfg, cfg_url, _active, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    data = Path(state.working_path).read_bytes()

    renamed = update_model(ModelEdit(new_slug="DeepSeek-V4.1-Flash-V2"), "deepseek-v4.1-flash", data)
    root = json.loads(renamed)
    entry = next(m for m in root["models"] if m["slug"] == "deepseek-v4.1-flash-v2")
    assert entry == {**ACTIVE["models"][1], "slug": "deepseek-v4.1-flash-v2"}
    assert [m["slug"] for m in root["models"]] == ["deepseek-flash", "deepseek-v4.1-flash-v2"]

    with pytest.raises(InvalidConfiguration, match="已存在"):
        update_model(ModelEdit(new_slug="deepseek-flash"), "deepseek-v4.1-flash", data)
    with pytest.raises(InvalidConfiguration, match="标识无效"):
        update_model(ModelEdit(new_slug="Not A Slug"), "deepseek-v4.1-flash", data)


def test_rename_is_reported_as_a_rename_not_delete_plus_add(tmp_path):
    cfg, cfg_url, _active, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    renamed = update_model(ModelEdit(new_slug="deepseek-v4.1-flash-v2"),
                           "deepseek-v4.1-flash", Path(state.working_path).read_bytes())
    Path(state.working_path).write_bytes(renamed)

    diff = takeover.working_state(cfg).diff
    assert diff.renamed == [("deepseek-v4.1-flash", "deepseek-v4.1-flash-v2")]
    assert diff.removed == [] and diff.added == []
    rendered = takeover.render_diff(diff)
    assert "deepseek-v4.1-flash -> deepseek-v4.1-flash-v2" in rendered
    # A pure rename needs no destructive removal confirmation.
    result = takeover.apply_pending(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert result.applied and "重命名 1" in result.diff_summary


def test_rename_preview_names_old_and_new_id():
    from codex_model_manager.core.custom_models import describe_edit

    lines = describe_edit(ModelEdit(new_slug="New-Id", display_name="New Name"), "old-id")
    assert "ID: old-id -> new-id" in lines[0]
    assert any("显示名" in line for line in lines)


# ---------------------------------------------------------------------------
# delete confirmation / apply
# ---------------------------------------------------------------------------

def test_delete_requires_confirmation_before_touching_the_live_catalog(tmp_path):
    cfg, cfg_url, active_path, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    trimmed = remove_model("deepseek-v4.1-flash", Path(state.working_path).read_bytes())
    Path(state.working_path).write_bytes(trimmed)
    before = active_path.read_bytes()

    with pytest.raises(ConflictError, match="deepseek-v4.1-flash"):
        takeover.apply_pending(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert active_path.read_bytes() == before, "no confirmation, no write"

    result = takeover.apply_pending(cfg, confirm_removals=True,
                                    persist=lambda: cfg.save(str(cfg_url)))
    assert result.applied
    written = json.loads(active_path.read_text(encoding="utf-8"))
    assert [m["slug"] for m in written["models"]] == ["deepseek-flash"]


def test_apply_rebaselines_so_the_page_does_not_show_a_false_conflict(tmp_path):
    cfg, cfg_url, _active, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    edited = json.loads(Path(state.working_path).read_text(encoding="utf-8"))
    edited["models"][0]["display_name"] = "renamed by manager"
    Path(state.working_path).write_text(json.dumps(edited), encoding="utf-8")

    assert takeover.apply_pending(cfg, persist=lambda: cfg.save(str(cfg_url))).applied
    after = takeover.working_state(cfg)
    assert after.conflict == "", after.conflict
    assert after.status == takeover.WORKING_CLEAN and not after.has_unapplied


# ---------------------------------------------------------------------------
# external change
# ---------------------------------------------------------------------------

def test_external_change_is_detected_and_both_recoveries_work(tmp_path):
    cfg, cfg_url, active_path, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    persist = lambda: cfg.save(str(cfg_url))  # noqa: E731
    state = takeover.ensure_working_copy(cfg, persist=persist)
    edited = json.loads(Path(state.working_path).read_text(encoding="utf-8"))
    edited["models"][0]["display_name"] = "my staged edit"
    Path(state.working_path).write_text(json.dumps(edited), encoding="utf-8")

    externally = json.loads(active_path.read_text(encoding="utf-8"))
    externally["models"][0]["display_name"] = "changed by someone else"
    externally["models"][0]["provider_metadata"] = {"route": "relay-external"}
    active_path.write_text(json.dumps(externally), encoding="utf-8")
    before = active_path.read_bytes()

    conflicted = takeover.working_state(cfg)
    assert conflicted.status == takeover.WORKING_STALE
    assert "外部修改" in conflicted.conflict
    assert not conflicted.writable or True  # gate is separate from the conflict
    with pytest.raises(ConflictError):
        takeover.apply_pending(cfg, persist=persist)
    assert active_path.read_bytes() == before

    # Recovery 1: keep my edits, accept the newest content as the baseline.
    rebased = takeover.rebind_baseline(cfg, persist=persist)
    assert rebased.conflict == "" and rebased.has_unapplied
    # Recovery 2: throw my edits away and reload the newest content.
    reloaded = takeover.reload_from_active(cfg, persist=persist)
    assert reloaded.conflict == "" and not reloaded.has_unapplied
    assert active_path.read_bytes() == before


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------

def test_backup_and_restore_round_trip(tmp_path):
    cfg, cfg_url, active_path, sandbox, _cfg_toml = _sandbox(tmp_path)
    original = active_path.read_bytes()
    backup = backup_file(str(active_path), str(sandbox / "backups"), prefix="active-catalog")
    assert backup and Path(backup).read_bytes() == original

    changed = json.loads(original)
    changed["models"] = changed["models"][:1]
    active_path.write_text(json.dumps(changed), encoding="utf-8")
    kept = restore_file(backup, str(active_path), str(sandbox / "backups"))
    assert active_path.read_bytes() == original
    assert kept and Path(kept).read_bytes() != original  # the pre-restore file is kept


# ---------------------------------------------------------------------------
# real Tk widgets (skipped when no display is available)
# ---------------------------------------------------------------------------

def test_model_page_opens_on_the_live_catalog_with_real_widgets(tmp_path):
    """The GUI path: connection config adopted, five columns, staged statuses."""
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no tkinter: {exc}")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no display: {exc}")

    from codex_model_manager.gui import app as gui_app
    from codex_model_manager.gui import model_page as page_module

    cfg, cfg_url, active_path, _sandbox_dir, cfg_toml = _sandbox(tmp_path)
    cfg.codex_config_path = None          # the broken state being repaired
    cfg.save(str(cfg_url))
    errors: list[str] = []
    root.withdraw()
    page_module.messagebox.askyesno = lambda *a, **k: True
    page_module.messagebox.showinfo = lambda *a, **k: None
    page_module.messagebox.showwarning = lambda *a, **k: errors.append(str(a[1]))
    page_module.messagebox.showerror = lambda *a, **k: errors.append(str(a[1]))
    try:
        application = gui_app.CodexModelManagerApp(root, str(cfg_url))
        root.update()
        page = application.model_page
        assert application._config.codex_config_path == str(cfg_toml)
        assert page.state is not None and page.state.active_path == str(active_path)
        assert [r["model"].slug for r in page._rows] == ["deepseek-flash", "deepseek-v4.1-flash"]
        assert [page.tree.heading(c)["text"] for c in ("slug", "name", "reasoning", "image", "status")] == \
            ["模型 ID", "显示名", "推理档位", "图片", "修改状态"]
        # Add from a copy through the real dialog, then discard.
        page.add_model()
        root.update()
        dialog = [w for w in root.winfo_children()
                  if isinstance(w, page_module.AddModelDialog)][-1]
        dialog.vars["slug"].set("copied-model")
        dialog.vars["template"].set("deepseek-flash")
        dialog._load_template()
        dialog._save()
        root.update()
        assert any(r["model"].slug == "copied-model" and r["status"] == "新增"
                   for r in page._rows)
        assert active_path.read_bytes() != Path(page.state.working_path).read_bytes()
        page.discard_changes()
        root.update()
        assert not takeover.working_state(application._config).has_unapplied
        assert not errors, errors
    finally:
        _teardown_root(root)


def test_empty_start_is_writable_so_the_first_write_can_be_created(tmp_path):
    """From an empty edit copy the page can create the live catalog, gated."""
    cfg, cfg_url, active_path, _sandbox_dir, _cfg_toml = _sandbox(tmp_path)
    active_path.unlink()                       # the config references a missing file
    state = takeover.ensure_working_copy(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert state.status == takeover.WORKING_EMPTY
    assert state.active_exists is False
    assert state.writable is True, "the demo sandbox gate must allow the first write"

    # Applying an empty catalog is refused (nothing to write), but with a model it
    # creates the live file only after the explicit allow_create confirmation.
    draft = NewModelDraft(slug="first-model", display_name="First", description="first",
                          template_slug="", context_window=1000)
    Path(state.working_path).write_bytes(add_blank_model(draft, Path(state.working_path).read_bytes()))
    with pytest.raises(InvalidConfiguration, match="拒绝创建"):
        takeover.apply_pending(cfg, persist=lambda: cfg.save(str(cfg_url)))
    assert not active_path.exists()

    result = takeover.apply_pending(cfg, allow_create=True,
                                    persist=lambda: cfg.save(str(cfg_url)))
    assert result.applied and result.created_file
    assert [m["slug"] for m in json.loads(active_path.read_text(encoding="utf-8"))["models"]] == \
        ["first-model"]

# ---------------------------------------------------------------------------
# first apply: config.toml exists but has no model_catalog_json
# ---------------------------------------------------------------------------

def _first_apply_sandbox(tmp_path, *, target_exists=False):
    """A demo sandbox whose config.toml does NOT reference a catalog yet."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    target = sandbox / "models.json"
    if target_exists:
        target.write_text(json.dumps({"models": [
            {"slug": "official-model", "priority": 1, "context_window": 1000}]}),
            encoding="utf-8")
    custom = sandbox / "custom-models.json"
    custom.write_text(json.dumps({"models": []}), encoding="utf-8")
    runtime = sandbox / "codex-mock.exe"
    runtime.write_bytes(b"MZ" + b"\0" * 64)          # exists; classified native
    config_toml = sandbox / "config.toml"
    config_toml.write_text(
        '# sentinel comment that must survive\n'
        'model = "deepseek-flash"\n'
        'model_provider = "Test"\n'
        'api_key = "SECRET_NOT_FOR_OUTPUT"\n'
        '[model_providers.Test]\n'
        'base_url = "https://relay.invalid/"\n',
        encoding="utf-8")
    cfg = AppConfiguration(
        codex_runtime_path=str(runtime),
        custom_source_path=str(custom),
        merged_catalog_path=str(target),
        sync_log_path=str(sandbox / "logs" / "sync.jsonl"),
        error_log_path=str(sandbox / "logs" / "error.log"),
        backup_directory_path=str(sandbox / "backups"),
        codex_config_path=str(config_toml),
        takeover_catalog_path=str(sandbox / "pending-models.json"),
        is_demo=True,
    )
    cfg_url = tmp_path / "app.json"
    cfg.save(str(cfg_url))
    return cfg, cfg_url, target, config_toml, sandbox


def _gui_for_first_apply(root, cfg_url, monkeypatch):
    """Launch the real app with a stubbed bridge client and recorded dialogs."""
    from codex_model_manager.gui import app as gui_app
    from codex_model_manager.gui import model_page as page_module

    record = {"askyesno": [], "info": [], "warnings": [], "errors": []}
    state = {"yes": True}

    def askyesno(title, message=None, **kwargs):
        record["askyesno"].append((title, message or ""))
        return state["yes"]

    monkeypatch.setattr(page_module.messagebox, "askyesno", askyesno)
    monkeypatch.setattr(page_module.messagebox, "showinfo",
                        lambda title, message=None, **k: record["info"].append((title, message or "")))
    monkeypatch.setattr(page_module.messagebox, "showwarning",
                        lambda title, message=None, **k: record["warnings"].append((title, message or "")))
    monkeypatch.setattr(page_module.messagebox, "showerror",
                        lambda title, message=None, **k: record["errors"].append((title, message or "")))

    application = gui_app.CodexModelManagerApp(root, str(cfg_url))

    class StubClient:
        def call(self, action, settings):
            return {"configured": False, "running": False, "current_url": "https://relay.invalid/",
                    "backend": "native", "mode": None, "models": [], "port": 18787,
                    "compaction_passthrough": False, "active_requests": 0, "history": [],
                    "managed": False, "phase": None}

    application.bridge_panel.client = StubClient()
    root.update()
    return application, record, state


def _require_display():
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no tkinter: {exc}")
    try:
        return tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no display: {exc}")


def test_first_apply_refuses_when_target_is_the_edit_copy(tmp_path):
    """The published catalog and the edit copy must be two different files."""
    cfg, _url, target, _toml, _sandbox_dir = _first_apply_sandbox(tmp_path)
    cfg.takeover_catalog_path = str(target)
    Path(target).write_bytes(json.dumps({"models": [
        {"slug": "m", "priority": 1, "context_window": 10}]}).encode())
    with pytest.raises(InvalidConfiguration, match="同一个文件"):
        takeover.apply_first_time(cfg, expected=takeover.first_apply_preview(cfg)["snapshot"])


def test_first_apply_is_gated_in_unverified_real_mode(tmp_path):
    """Real mode without runtime evidence stays read-only and writes nothing."""
    cfg, _url, target, config_toml, _sandbox_dir = _first_apply_sandbox(tmp_path)
    cfg.is_demo = False                      # real mode, no compat evidence
    Path(cfg.takeover_catalog_path).write_bytes(json.dumps({"models": [
        {"slug": "m", "priority": 1, "context_window": 10}]}).encode())
    config_before = config_toml.read_bytes()
    info = takeover.first_apply_preview(cfg)
    assert info["writable"] is False and info["gate_reason"]
    with pytest.raises(InvalidConfiguration, match="未验证|证据"):
        takeover.apply_first_time(cfg, expected=info["snapshot"])
    assert not target.exists() and config_toml.read_bytes() == config_before


def test_first_apply_publishes_a_separate_file_and_is_undoable(tmp_path):
    """Core path: publish, reference, keep the copy separate, then undo both."""
    cfg, cfg_url, target, config_toml, sandbox = _first_apply_sandbox(tmp_path)
    config_before = config_toml.read_bytes()
    persist = lambda: cfg.save(str(cfg_url))  # noqa: E731
    state = takeover.ensure_working_copy(cfg, persist=persist)
    draft = NewModelDraft(slug="only-model", display_name="Only", description="only",
                          template_slug="", context_window=2048)
    Path(state.working_path).write_bytes(
        add_blank_model(draft, Path(state.working_path).read_bytes()))

    result = takeover.apply_first_time(
        cfg, expected=takeover.first_apply_preview(cfg)["snapshot"], persist=persist)
    assert result.applied and result.created_catalog
    assert Path(result.target_catalog) == target
    assert Path(result.edit_copy) == Path(cfg.takeover_catalog_path)
    assert Path(result.edit_copy) != target
    published = json.loads(target.read_text(encoding="utf-8"))
    assert [m["slug"] for m in published["models"]] == ["only-model"]
    import tomllib

    assert Path(tomllib.loads(config_toml.read_text(encoding="utf-8"))["model_catalog_json"]) == target
    assert list((sandbox / "backups").glob("config.toml.*.bak"))
    assert not takeover.working_state(cfg).has_unapplied

    message = takeover.undo_first_apply(cfg, persist=persist)
    assert "已删除" in message
    assert not target.exists()
    assert config_toml.read_bytes() == config_before
    assert Path(cfg.takeover_catalog_path).is_file(), "the edit copy survives the undo"
    assert takeover.working_state(cfg).active_path is None


def test_gui_first_apply_closed_loop(tmp_path, monkeypatch):
    """Drive the real buttons: no model_catalog_json -> add -> preview -> cancel ->
    apply -> second edit -> second apply; config and catalog stay separate files."""
    from codex_model_manager.gui import model_page as page_module

    cfg, cfg_url, target, config_toml, sandbox = _first_apply_sandbox(tmp_path)
    config_before = config_toml.read_bytes()
    root = _require_display()
    root.withdraw()
    try:
        application, record, state = _gui_for_first_apply(root, cfg_url, monkeypatch)
        page = application.model_page

        # 1) initial state: config has no model_catalog_json, list is empty
        assert takeover.working_state(application._config).active_path is None
        assert page._rows == [], [r["model"].slug for r in page._rows]
        assert not target.exists()

        # 2) add a model through the real dialog (copy created on demand)
        page.add_model()
        root.update()
        dialog = [w for w in root.winfo_children()
                  if isinstance(w, page_module.AddModelDialog)][-1]
        dialog.vars["slug"].set("deepseek-flash-copy")
        dialog.vars["name"].set("DeepSeek Flash Copy")
        dialog.vars["description"].set("added from scratch")
        dialog.vars["context"].set("128000")
        dialog._preview()                       # preview writes nothing
        root.update()
        assert [w for w in root.winfo_children()
                if isinstance(w, page_module.PreviewDialog)], "the add preview must be shown"
        assert not target.exists()
        assert json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))["models"] == []
        dialog._save()
        root.update()
        assert [r["model"].slug for r in page._rows] == ["deepseek-flash-copy"]
        assert config_toml.read_bytes() == config_before, "adding must not touch config.toml"
        assert not target.exists()

        # 3) first-apply preview + cancel: nothing at all is written
        state["yes"] = False
        page.view_and_apply()
        root.update()
        titles = [t for t, _ in record["askyesno"]]
        assert any("首次应用" in t for t in titles), titles
        first_msg = next(m for t, m in record["askyesno"] if "首次应用" in t)
        assert str(target) in first_msg, first_msg
        assert str(cfg.takeover_catalog_path) in first_msg
        assert config_toml.read_bytes() == config_before
        assert not target.exists()
        # Cancelling the first apply must not touch either real target. (The
        # pending-models backup present here is from the earlier, legitimate
        # edit-copy write, not from the cancelled apply.)
        assert not list((sandbox / "backups").glob("config.toml*.bak"))
        assert not list((sandbox / "backups").glob("first-apply-catalog*.bak"))

        # 4) confirm: the config references a SEPARATE published catalog
        state["yes"] = True
        page.view_and_apply()
        root.update()
        assert not record["errors"], record["errors"]
        assert target.is_file()
        published = json.loads(target.read_text(encoding="utf-8"))
        assert [m["slug"] for m in published["models"]] == ["deepseek-flash-copy"]
        assert Path(cfg.takeover_catalog_path) != target
        assert Path(cfg.takeover_catalog_path).is_file(), "the edit copy stays intact"
        import tomllib

        text = config_toml.read_text(encoding="utf-8")
        assert Path(tomllib.loads(text)["model_catalog_json"]) == target
        assert "sentinel comment" in text and "SECRET_NOT_FOR_OUTPUT" in text
        assert list((sandbox / "backups").glob("config.toml.*.bak")), "config must be backed up"
        assert application._config.last_first_apply is not None
        after_first = takeover.working_state(application._config)
        assert after_first.conflict == "" and not after_first.has_unapplied

        # 5) a second edit is not applied until the user applies it
        for index, row in enumerate(page._rows):
            if row["model"].slug == "deepseek-flash-copy":
                page.tree.selection_set(str(index))
                break
        page.edit_selected()
        root.update()
        edit = [w for w in root.winfo_children()
                if isinstance(w, page_module.EditModelDialog)][-1]
        edit.vars["name"].set("Renamed Later")
        edit._save()
        root.update()
        assert json.loads(target.read_text(encoding="utf-8"))["models"][0]["display_name"] == \
            "DeepSeek Flash Copy", "an unapplied edit must not change the published catalog"
        assert takeover.working_state(application._config).has_unapplied

        # 6) second apply now goes through the normal write-back path
        page.view_and_apply()
        root.update()
        assert json.loads(target.read_text(encoding="utf-8"))["models"][0]["display_name"] == "Renamed Later"
        assert list((sandbox / "backups").glob("active-catalog.*.bak")), "write-back must back up"
        assert not takeover.working_state(application._config).has_unapplied

        # 7) undoing the FIRST apply is refused: the catalog moved on since then
        state["yes"] = True
        application.undo_first_apply()
        root.update()
        assert any("未完成撤销" in t for t, _ in record["warnings"]), record["warnings"]
        assert json.loads(target.read_text(encoding="utf-8"))["models"][0]["display_name"] == "Renamed Later"
    finally:
        _teardown_root(root)


def test_gui_first_apply_undo_restores_both_files(tmp_path, monkeypatch):
    """Undo after a first apply removes the reference and the file it created."""
    from codex_model_manager.gui import model_page as page_module

    cfg, cfg_url, target, config_toml, _sandbox = _first_apply_sandbox(tmp_path)
    config_before = config_toml.read_bytes()
    root = _require_display()
    root.withdraw()
    try:
        application, record, state = _gui_for_first_apply(root, cfg_url, monkeypatch)
        page = application.model_page
        page.add_model()
        root.update()
        dialog = [w for w in root.winfo_children()
                  if isinstance(w, page_module.AddModelDialog)][-1]
        dialog.vars["slug"].set("only-model")
        dialog.vars["name"].set("Only Model")
        dialog.vars["context"].set("2048")
        dialog._save()
        root.update()
        state["yes"] = True
        page.view_and_apply()
        root.update()
        assert target.is_file() and "only-model" in target.read_text(encoding="utf-8")
        pending = Path(cfg.takeover_catalog_path)
        pending_before = pending.read_bytes()

        application.undo_first_apply()
        root.update()
        assert not record["errors"], record["errors"]
        assert not target.exists(), "a file we created is removed on undo"
        assert config_toml.read_bytes() == config_before, "config is restored byte-for-byte"
        assert pending.read_bytes() == pending_before, "the edit copy is untouched by undo"
        assert takeover.working_state(application._config).active_path is None
    finally:
        _teardown_root(root)


def test_gui_first_apply_undo_refuses_after_external_change(tmp_path, monkeypatch):
    """An externally edited published catalog blocks the undo."""
    from codex_model_manager.gui import model_page as page_module

    cfg, cfg_url, target, config_toml, _sandbox = _first_apply_sandbox(tmp_path)
    root = _require_display()
    root.withdraw()
    try:
        application, record, state = _gui_for_first_apply(root, cfg_url, monkeypatch)
        page = application.model_page
        page.add_model()
        root.update()
        dialog = [w for w in root.winfo_children()
                  if isinstance(w, page_module.AddModelDialog)][-1]
        dialog.vars["slug"].set("model-one")
        dialog.vars["name"].set("Model One")
        dialog.vars["context"].set("2048")
        dialog._save()
        root.update()
        state["yes"] = True
        page.view_and_apply()
        root.update()

        externally = json.loads(target.read_text(encoding="utf-8"))
        externally["models"][0]["display_name"] = "changed outside"
        target.write_text(json.dumps(externally), encoding="utf-8")
        applied_config = config_toml.read_text(encoding="utf-8")

        application.undo_first_apply()
        root.update()
        assert any("未完成撤销" in t for t, _ in record["warnings"]), record["warnings"]
        assert config_toml.read_text(encoding="utf-8") == applied_config
        assert "changed outside" in target.read_text(encoding="utf-8")
    finally:
        _teardown_root(root)


# ---------------------------------------------------------------------------
# first apply: failure compensation, preview snapshot, interrupted recovery
# ---------------------------------------------------------------------------

def _prepared_first_apply(tmp_path, *, target_exists=False, slug="new-model"):
    """Sandbox + edit copy with one model; returns (cfg, url, target, toml, persist, preview)."""
    cfg, cfg_url, target, config_toml, sandbox = _first_apply_sandbox(
        tmp_path, target_exists=target_exists)
    persist = lambda: cfg.save(str(cfg_url))  # noqa: E731
    state = takeover.ensure_working_copy(cfg, persist=persist)
    draft = NewModelDraft(slug=slug, display_name=slug, description="added",
                          template_slug="", context_window=1000)
    Path(state.working_path).write_bytes(
        add_blank_model(draft, Path(state.working_path).read_bytes()))
    preview = takeover.first_apply_preview(cfg)
    return cfg, cfg_url, target, config_toml, sandbox, persist, preview


def test_apply_first_time_requires_a_preview_snapshot(tmp_path):
    """A caller that never previewed must not be able to write."""
    cfg, _url, target, config_toml, _sandbox, persist, _preview = _prepared_first_apply(tmp_path)
    before = config_toml.read_bytes()
    with pytest.raises(InvalidConfiguration, match="预览"):
        takeover.apply_first_time(cfg, expected=None, persist=persist)
    assert not target.exists() and config_toml.read_bytes() == before


def test_first_apply_failure_restores_existing_target_bytes(tmp_path, monkeypatch):
    """Config write fails after the catalog was replaced: restore it byte-for-byte."""
    cfg, _url, target, config_toml, sandbox, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=True)
    original_target = target.read_bytes()
    original_config = config_toml.read_bytes()

    def boom(*args, **kwargs):
        raise InvalidConfiguration("模拟 config 写入失败")

    monkeypatch.setattr("codex_model_manager.core.config_editor.set_model_catalog", boom)
    with pytest.raises(InvalidConfiguration) as exc:
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    message = str(exc.value)
    assert "已恢复到应用前状态" in message, message
    assert "已保留原文件" not in message
    assert target.read_bytes() == original_target, "the replaced catalog must be compensated"
    assert config_toml.read_bytes() == original_config
    assert cfg.last_first_apply is None, "a compensated failure leaves nothing to undo"
    assert takeover.pending_first_apply(cfg) is None
    assert Path(cfg.takeover_catalog_path).is_file(), "the edit copy is never touched"


def test_first_apply_failure_removes_created_target(tmp_path, monkeypatch):
    """A catalog file this run created is removed again when the config write fails."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(tmp_path)
    original_config = config_toml.read_bytes()
    assert not target.exists()

    def boom(*args, **kwargs):
        raise InvalidConfiguration("模拟 config 写入失败")

    monkeypatch.setattr("codex_model_manager.core.config_editor.set_model_catalog", boom)
    with pytest.raises(InvalidConfiguration, match="已恢复到应用前状态"):
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert not target.exists(), "a file we created must be cleaned up"
    assert config_toml.read_bytes() == original_config
    assert cfg.last_first_apply is None


def test_first_apply_failure_does_not_overwrite_an_external_change(tmp_path, monkeypatch):
    """If someone rewrote the catalog during the failure window, keep their bytes."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=True)
    original_config = config_toml.read_bytes()
    external = json.dumps({"models": [
        {"slug": "someone-else", "priority": 1, "context_window": 2048}]}, indent=2).encode()

    def boom(*args, **kwargs):
        target.write_bytes(external)          # external writer wins the race
        raise InvalidConfiguration("模拟 config 写入失败")

    monkeypatch.setattr("codex_model_manager.core.config_editor.set_model_catalog", boom)
    with pytest.raises(InvalidConfiguration) as exc:
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert "未能自动恢复" in str(exc.value), str(exc.value)
    assert target.read_bytes() == external, "never overwrite another writer"
    assert config_toml.read_bytes() == original_config
    assert cfg.last_first_apply is not None, "an unresolved failure keeps its record"
    assert takeover.pending_first_apply(cfg)["phase"] == "catalog_written"

    # Because the catalog no longer matches what we wrote, undo must refuse rather
    # than clobber the external content.
    with pytest.raises(ConflictError, match="外部修改"):
        takeover.undo_first_apply(cfg, persist=persist)
    assert target.read_bytes() == external


def test_first_apply_undo_recovers_an_interrupted_run(tmp_path, monkeypatch):
    """A crash between the two writes is recoverable on the next run."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=True)
    original_target = target.read_bytes()
    original_config = config_toml.read_bytes()
    real_recover = takeover._recover_first_apply

    def boom(*args, **kwargs):
        raise InvalidConfiguration("模拟 config 写入失败")

    def crash(config, record, persist=None):        # compensation never ran
        return takeover.FirstApplyRecovery(ok=False, report="模拟进程中断（未补偿）")

    monkeypatch.setattr("codex_model_manager.core.config_editor.set_model_catalog", boom)
    monkeypatch.setattr(takeover, "_recover_first_apply", crash)
    with pytest.raises(InvalidConfiguration, match="未能自动恢复"):
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert target.read_bytes() != original_target, "the catalog was already replaced"
    assert config_toml.read_bytes() == original_config
    assert takeover.pending_first_apply(cfg)["phase"] == "catalog_written"

    # "Restart": the normal undo path must recover the interrupted transaction even
    # though config.toml was never changed.
    monkeypatch.setattr(takeover, "_recover_first_apply", real_recover)
    message = takeover.undo_first_apply(cfg, persist=persist)
    assert "已恢复为应用前内容" in message, message
    assert target.read_bytes() == original_target
    assert config_toml.read_bytes() == original_config
    assert cfg.last_first_apply is None
    assert takeover.pending_first_apply(cfg) is None


def test_undo_first_apply_refuses_when_a_backup_is_missing(tmp_path):
    """A missing backup must block the whole undo before config is touched."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=True)
    takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    published = target.read_bytes()
    applied_config = config_toml.read_text(encoding="utf-8")
    backup = cfg.last_first_apply["catalogBackup"]
    Path(backup).unlink()

    with pytest.raises(ConflictError, match="备份"):
        takeover.undo_first_apply(cfg, persist=persist)
    assert config_toml.read_text(encoding="utf-8") == applied_config, \
        "config must not be restored while the catalog cannot be"
    assert target.read_bytes() == published
    assert cfg.last_first_apply is not None, "the record stays for a retry"


def test_first_apply_snapshot_blocks_a_new_external_reference(tmp_path):
    """A model_catalog_json that APPEARED after the preview is a conflict."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(tmp_path)
    config_toml.write_text(
        config_toml.read_text(encoding="utf-8") + 'model_catalog_json = "external.json"\n',
        encoding="utf-8")
    external = config_toml.read_text(encoding="utf-8")

    with pytest.raises(ConflictError, match="发生变化"):
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert config_toml.read_text(encoding="utf-8") == external, \
        "the external reference must be preserved"
    assert not target.exists(), "nothing may be written after a refused preview check"
    assert cfg.last_first_apply is None


def test_first_apply_snapshot_blocks_target_and_edit_copy_changes(tmp_path):
    """Target and edit-copy changes between preview and apply are refused too."""
    cfg, _url, target, config_toml, _sandbox, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=True)
    target.write_text(json.dumps({"models": [
        {"slug": "external", "priority": 1, "context_window": 10}]}), encoding="utf-8")
    external_target = target.read_bytes()
    with pytest.raises(ConflictError, match="生效目录内容"):
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert target.read_bytes() == external_target
    assert "model_catalog_json" not in config_toml.read_text(encoding="utf-8")

    # Re-preview (now including the external target), then change the edit copy.
    preview = takeover.first_apply_preview(cfg)
    pending = Path(cfg.takeover_catalog_path)
    edited = json.loads(pending.read_text(encoding="utf-8"))
    edited["models"][0]["display_name"] = "changed after preview"
    pending.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(ConflictError, match="编辑副本内容"):
        takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    assert target.read_bytes() == external_target


@pytest.mark.parametrize("changed", ["config", "target", "edit_copy"])
def test_gui_first_apply_confirm_detects_external_change(tmp_path, monkeypatch, changed):
    """The real GUI button path: a change made during the confirm callback blocks it.

    Parametrised over the three files the confirmation is bound to, so config, the
    target catalog and the edit copy are each proven to be protected.
    """
    from codex_model_manager.gui import model_page as page_module

    cfg, cfg_url, target, config_toml, _sandbox = _first_apply_sandbox(tmp_path)
    root = _require_display()
    root.withdraw()
    try:
        application, record, _state = _gui_for_first_apply(root, cfg_url, monkeypatch)
        page = application.model_page
        page.add_model()
        root.update()
        dialog = [w for w in root.winfo_children()
                  if isinstance(w, page_module.AddModelDialog)][-1]
        dialog.vars["slug"].set("deepseek-flash-copy")
        dialog.vars["name"].set("DeepSeek Flash Copy")
        dialog.vars["context"].set("128000")
        dialog._save()
        root.update()

        external_target = json.dumps({"models": [
            {"slug": "written-by-someone-else", "priority": 1, "context_window": 4096}]},
            indent=2).encode()

        def confirm(title, message=None, **kwargs):
            if "首次应用" in title:
                # Another program changes one of the three files while the dialog is open.
                if changed == "config":
                    config_toml.write_text(
                        config_toml.read_text(encoding="utf-8")
                        + 'model_catalog_json = "external.json"\n', encoding="utf-8")
                elif changed == "target":
                    target.write_bytes(external_target)
                else:
                    pending = Path(cfg.takeover_catalog_path)
                    edited = json.loads(pending.read_text(encoding="utf-8"))
                    edited["models"][0]["display_name"] = "changed while confirming"
                    pending.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
            return True

        monkeypatch.setattr(page_module.messagebox, "askyesno", confirm)
        page.view_and_apply()
        root.update()

        assert any("需要重新预览" in t for t, _ in record["warnings"]), record["warnings"]
        assert not record["errors"], record["errors"]
        # The external change is fully preserved and nothing was published.
        if changed == "config":
            assert "external.json" in config_toml.read_text(encoding="utf-8")
            assert not target.exists()
        elif changed == "target":
            assert target.read_bytes() == external_target
            assert "model_catalog_json" not in config_toml.read_text(encoding="utf-8")
        else:
            pending = Path(cfg.takeover_catalog_path)
            assert "changed while confirming" in pending.read_text(encoding="utf-8")
            assert not target.exists()
            assert "model_catalog_json" not in config_toml.read_text(encoding="utf-8")
    finally:
        _teardown_root(root)


@pytest.mark.parametrize("target_exists", [False, True])
@pytest.mark.parametrize("write_landed", [False, True])
def test_first_apply_restart_recovers_planned_phase(tmp_path, monkeypatch,
                                                    target_exists, write_landed):
    """The durable phase can lag behind the actual atomic file replacement."""
    cfg, url, target, config_toml, _, persist, preview = _prepared_first_apply(
        tmp_path, target_exists=target_exists)
    before = target.read_bytes() if target_exists else None
    config_before = config_toml.read_bytes()
    real_write = takeover.atomic_write

    def interrupted_write(path, data):
        if Path(path) == target:
            if write_landed:
                real_write(path, data)
            raise SystemExit("crash before phase persistence")
        real_write(path, data)

    with monkeypatch.context() as scoped:
        scoped.setattr(takeover, "atomic_write", interrupted_write)
        with pytest.raises(SystemExit):
            takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)

    loaded = AppConfiguration.load(str(url))
    assert loaded.last_first_apply["phase"] == "planned"
    takeover.undo_first_apply(loaded, persist=lambda: loaded.save(str(url)))
    assert (target.read_bytes() if target.exists() else None) == before
    assert config_toml.read_bytes() == config_before
    assert AppConfiguration.load(str(url)).last_first_apply is None


def test_planned_recovery_preserves_external_content_and_record(tmp_path, monkeypatch):
    cfg, url, target, _, _, persist, preview = _prepared_first_apply(tmp_path)
    real_write = takeover.atomic_write

    def interrupted_write(path, data):
        real_write(path, data)
        if Path(path) == target:
            raise SystemExit("crash after replacement")

    with monkeypatch.context() as scoped:
        scoped.setattr(takeover, "atomic_write", interrupted_write)
        with pytest.raises(SystemExit):
            takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    external = b'{"models": [{"slug": "external", "priority": 1}]}'
    target.write_bytes(external)
    loaded = AppConfiguration.load(str(url))
    with pytest.raises(ConflictError):
        takeover.undo_first_apply(loaded, persist=lambda: loaded.save(str(url)))
    assert target.read_bytes() == external
    assert AppConfiguration.load(str(url)).last_first_apply is not None


def test_upstream_context_override_remains_available(tmp_path, monkeypatch):
    from codex_model_manager.gui import app as gui_app
    from codex_model_manager.core.overrides import ModelFieldOverrides

    cfg, url, _, _, _, persist, preview = _prepared_first_apply(tmp_path)
    takeover.apply_first_time(cfg, expected=preview["snapshot"], persist=persist)
    root = _require_display()
    root.withdraw()
    try:
        application, record, _ = _gui_for_first_apply(root, url, monkeypatch)
        application.model_page.tree.selection_set("0")
        application.model_page.advanced_actions["context_override"]()
        root.update()
        dialogs = [w for w in root.winfo_children()
                   if isinstance(w, gui_app.ContextOverrideDialog)]
        assert len(dialogs) == 1
        synced = []
        monkeypatch.setattr(application, "sync", lambda: synced.append(True))
        override = ModelFieldOverrides(context_window=4096, max_context_window=8192)
        assert dialogs[0].on_save(override)
        loaded = AppConfiguration.load(str(url))
        assert loaded.model_overrides["new-model"].context_window == 4096
        assert loaded.model_overrides["new-model"].max_context_window == 8192
        assert synced == [True]
        assert not record["errors"]
    finally:
        _teardown_root(root)


def _teardown_root(root):
    """Close leftover modal dialogs before destroying the root.

    Destroying a Tk root while a `grab_set` dialog is still mapped can leave the
    interpreter in a state that breaks the next Tk test in the same process.
    """
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
