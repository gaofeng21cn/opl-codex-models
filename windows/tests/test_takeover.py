"""Takeover: import a copy of the live catalog, edit it safely, write it back.

Covers what was asked to be proven: unknown fields survive an import, a concurrent
change is refused instead of overwritten, an edit cannot silently delete an
existing model, cancelling writes nothing at all, the write-back is backed up and
undoable, and the demo sandbox / compatibility gates still apply.

These tests need no Codex runtime: the takeover only touches model catalog JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_model_manager.core import takeover
from codex_model_manager.core.app_config import AppConfiguration
from codex_model_manager.core.custom_models import ModelEdit, update_model
from codex_model_manager.core.errors import (
    ConflictError,
    InvalidCatalog,
    InvalidConfiguration,
)
from codex_model_manager.core.reasoning import ReasoningSettings

ACTIVE = {
    "schema": "user.custom.v7",
    "notes": {"owner": "hand-written", "nested": [1, 2, {"deep": True}]},
    "models": [
        {
            "slug": "gpt-6-astra",
            "display_name": "GPT-6 Astra",
            "description": "vendor A",
            "priority": 3,
            "context_window": 128000,
            "max_context_window": 128000,
            "input_modalities": ["text"],
            "provider_metadata": {"route": "relay-1"},
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


def _sandbox(tmp_path, *, active=ACTIVE, is_demo=True, codex_config=True):
    """An app sandbox holding the merged catalog, and (optionally) a live catalog."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    merged = sandbox / "models.json"
    merged.write_text(json.dumps({"models": [{"slug": "gpt-6-astra", "priority": 1}]}),
                      encoding="utf-8")
    custom = sandbox / "custom-models.json"
    custom.write_text(json.dumps({"models": []}), encoding="utf-8")
    active_path = sandbox / "active-models.json"
    if active is not None:
        active_path.write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
    cfg_toml = sandbox / "config.toml"
    if codex_config:
        # A relative value on purpose: it must resolve against the config's own dir.
        cfg_toml.write_text(
            'model = "gpt-6-astra"\n'
            'model_catalog_json = "active-models.json"\n'
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
        is_demo=is_demo,
    )
    cfg_url = tmp_path / "app.json"
    cfg.save(str(cfg_url))
    return cfg, cfg_url, active_path, sandbox


def _tree(root: Path):
    """Bytes of every file under a directory (for exact unmodified comparisons)."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _no_runtime(monkeypatch):
    monkeypatch.setattr("codex_model_manager.core.runtime.find_native", lambda *a, **k: None)
    monkeypatch.setattr("codex_model_manager.core.app_config._discover_wsl", lambda: [])


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def test_import_preserves_unknown_top_level_and_model_fields(tmp_path):
    """The copied catalog keeps every unknown key, at the top level and per model."""
    cfg, _cfg_url, active_path, _sandbox_dir = _sandbox(tmp_path)
    record = takeover.import_active(cfg)
    assert record["activePath"] == str(active_path)
    assert record["modelCount"] == 2

    copied = json.loads(Path(record["pendingPath"]).read_text(encoding="utf-8"))
    assert copied == ACTIVE  # schema, notes, nested values and provider_metadata intact
    assert copied["notes"]["nested"][2] == {"deep": True}
    assert copied["models"][0]["provider_metadata"] == {"route": "relay-1"}
    assert copied["models"][0]["supported_reasoning_levels"][0]["budget"] == 200
    # The live catalog is untouched by an import.
    assert json.loads(active_path.read_text(encoding="utf-8")) == ACTIVE


def test_import_refuses_same_file_and_missing_source(tmp_path):
    cfg, _cfg_url, active_path, _sandbox_dir = _sandbox(tmp_path)
    cfg.takeover_catalog_path = str(active_path)  # pending == active
    with pytest.raises(InvalidConfiguration, match="同一个文件"):
        takeover.import_active(cfg)

    cfg2, _u, _a, sandbox2 = _sandbox(tmp_path / "other", active=None)
    with pytest.raises(InvalidCatalog, match="不存在"):
        takeover.import_active(cfg2)
    # Creating an empty pending catalog has to be asked for explicitly.
    assert not (sandbox2 / "pending-models.json").exists()
    record = takeover.import_active(cfg2, create_empty=True)
    assert json.loads(Path(record["pendingPath"]).read_text(encoding="utf-8"))["models"] == []


def test_import_reports_structure_warning_without_blocking(tmp_path):
    """An imported catalog the write gate would refuse is copied, and flagged."""
    bad = {"models": [{"slug": "no-priority", "context_window": 100}]}
    cfg, _u, _a, _s = _sandbox(tmp_path, active=bad)
    record = takeover.import_active(cfg)
    assert record["modelCount"] == 1
    assert record["structureWarnings"] and "priority" in record["structureWarnings"][0]
    # Import is a read plus a copy; the write is still refused later, by the same gate.
    state = takeover.inspect(cfg)
    assert not state.pending_ok


def test_import_backs_up_a_previous_pending_copy(tmp_path):
    cfg, _u, _a, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending = Path(cfg.takeover_catalog_path)
    pending.write_text(json.dumps({"models": []}), encoding="utf-8")
    takeover.import_active(cfg)  # re-import replaces the edited copy
    backups = list((tmp_path / "sandbox" / "backups").glob("pending-catalog.*.bak"))
    assert backups, "re-import must keep the replaced pending copy"
    assert json.loads(backups[0].read_text(encoding="utf-8"))["models"] == []


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def test_diff_reports_added_modified_removed(tmp_path):
    cfg, _u, _a, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending = cfg.takeover_catalog_path
    root = json.loads(Path(pending).read_text(encoding="utf-8"))
    root["models"][0]["context_window"] = 262144          # modified
    del root["models"][1]                                  # removed
    root["models"].append({"slug": "new-model", "display_name": "New", "priority": 9,
                           "context_window": 1000})        # added
    Path(pending).write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")

    diff = takeover.inspect(cfg).diff
    assert [c.slug for c in diff.added] == ["new-model"]
    assert [c.slug for c in diff.removed] == ["deepseek-v4.1-flash"]
    assert [c.slug for c in diff.modified] == ["gpt-6-astra"]
    assert diff.modified[0].fields["context_window"] == (128000, 262144)
    text = takeover.render_diff(diff)
    assert "context_window: 128000 -> 262144" in text
    assert "新增 1，修改 1，删除 1" in diff.summary()


# ---------------------------------------------------------------------------
# read-only inspection and cancel semantics
# ---------------------------------------------------------------------------

def test_inspect_and_render_write_nothing(tmp_path):
    cfg, _u, _a, sandbox = _sandbox(tmp_path)
    takeover.import_active(cfg)
    before = _tree(sandbox)
    state = takeover.inspect(cfg)
    text = takeover.render_state(state)
    assert "生效配置引用目录" in text and "待应用目录" in text
    assert "SECRET_NOT_FOR_OUTPUT" not in text   # only model data is rendered
    assert _tree(sandbox) == before


def test_apply_without_changes_writes_nothing(tmp_path):
    cfg, _u, _active_path, sandbox = _sandbox(tmp_path)
    takeover.import_active(cfg)
    before = _tree(sandbox)
    result = takeover.apply_pending(cfg)
    assert result.applied is False
    assert "未写入" in result.message
    assert _tree(sandbox) == before
    assert cfg.last_takeover is None


# ---------------------------------------------------------------------------
# conflict refusal
# ---------------------------------------------------------------------------

def test_apply_refuses_when_active_changed_after_import(tmp_path):
    """Someone else edited the live catalog; the write-back is refused, not merged."""
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    externally = json.loads(active_path.read_text(encoding="utf-8"))
    externally["models"][0]["context_window"] = 999999
    active_path.write_text(json.dumps(externally), encoding="utf-8")
    before = active_path.read_bytes()

    # make the pending side differ from the active one, so a write would otherwise happen
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "edited by manager"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")

    state = takeover.inspect(cfg)
    assert state.conflict and "冲突" in state.conflict
    with pytest.raises(ConflictError, match="外部修改"):
        takeover.apply_pending(cfg)
    assert active_path.read_bytes() == before


def test_apply_refuses_silent_removal_without_confirmation(tmp_path):
    """Dropping a model in the pending copy needs an explicit confirmation."""
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending_path = Path(cfg.takeover_catalog_path)
    trimmed = json.loads(pending_path.read_text(encoding="utf-8"))
    trimmed["models"] = [m for m in trimmed["models"] if m["slug"] != "deepseek-v4.1-flash"]
    pending_path.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    before = active_path.read_bytes()

    with pytest.raises(ConflictError) as exc:
        takeover.apply_pending(cfg)
    assert "deepseek-v4.1-flash" in str(exc.value)
    assert active_path.read_bytes() == before
    assert cfg.last_takeover is None

    # With the explicit confirmation the same write goes through.
    result = takeover.apply_pending(cfg, confirm_removals=True)
    assert result.applied
    written = json.loads(active_path.read_text(encoding="utf-8"))
    assert [m["slug"] for m in written["models"]] == ["gpt-6-astra"]


# ---------------------------------------------------------------------------
# write-back, backup, undo
# ---------------------------------------------------------------------------

def test_apply_backs_up_writes_and_undo_restores_exact_bytes(tmp_path):
    cfg, _u, active_path, sandbox = _sandbox(tmp_path)
    original = active_path.read_bytes()
    takeover.import_active(cfg)
    edited = update_model(
        ModelEdit(context_window=262144, max_context_window=262144,
                  reasoning=ReasoningSettings(
                      supported_efforts=["low", "high", "max"], default_effort="max")),
        "gpt-6-astra", Path(cfg.takeover_catalog_path).read_bytes())
    Path(cfg.takeover_catalog_path).write_bytes(edited)

    result = takeover.apply_pending(cfg)
    assert result.applied and result.backup_path
    assert Path(result.backup_path).read_bytes() == original
    written = json.loads(active_path.read_text(encoding="utf-8"))
    assert written["models"][0]["context_window"] == 262144
    assert written["models"][0]["default_reasoning_level"] == "max"
    # unknown fields travel with the write-back too
    assert written["schema"] == "user.custom.v7"
    assert written["models"][0]["provider_metadata"] == {"route": "relay-1"}
    assert written["models"][1] == ACTIVE["models"][1]

    message = takeover.undo_pending(cfg)
    assert "已撤销" in message
    assert active_path.read_bytes() == original
    assert cfg.last_takeover is None
    assert list(sandbox.glob("backups/active-catalog.before-undo.*.bak"))


def test_undo_refuses_after_external_change(tmp_path):
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "manager edit"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")
    assert takeover.apply_pending(cfg).applied

    externally = json.loads(active_path.read_text(encoding="utf-8"))
    externally["models"][0]["description"] = "someone else"
    active_path.write_text(json.dumps(externally), encoding="utf-8")
    before = active_path.read_bytes()

    with pytest.raises(ConflictError, match="冲突"):
        takeover.undo_pending(cfg)
    assert active_path.read_bytes() == before
    assert cfg.last_takeover is not None   # kept, so the user can retry after fixing


# ---------------------------------------------------------------------------
# the shared gates are reused, not weakened
# ---------------------------------------------------------------------------

def test_apply_refused_outside_demo_sandbox(tmp_path):
    """A demo config may not write a catalog outside its own sandbox."""
    outside = tmp_path / "outside-models.json"
    outside.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    cfg, _u, _a, sandbox = _sandbox(tmp_path, codex_config=False)
    # Codex reads a catalog outside the app sandbox: a demo write must refuse it.
    (sandbox / "config.toml").write_text(
        'model_catalog_json = "' + outside.as_posix() + '"\n', encoding="utf-8")
    cfg.codex_config_path = str(sandbox / "config.toml")
    before = outside.read_bytes()

    takeover.import_active(cfg)
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "edited"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")

    state = takeover.inspect(cfg)
    assert state.active_path == str(outside)
    assert not state.writable and "沙箱" in state.gate_reason
    with pytest.raises(InvalidConfiguration, match="沙箱"):
        takeover.apply_pending(cfg)
    assert outside.read_bytes() == before


def test_apply_refused_in_unverified_real_mode(tmp_path, monkeypatch):
    """Real mode without verified compatibility stays read-only, exactly like apply."""
    _no_runtime(monkeypatch)
    cfg, _u, active_path, _s = _sandbox(tmp_path, is_demo=False)
    before = active_path.read_bytes()
    takeover.import_active(cfg)
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "edited"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")

    state = takeover.inspect(cfg)
    assert not state.writable and "未验证" in state.gate_reason
    with pytest.raises(InvalidConfiguration, match="未验证"):
        takeover.apply_pending(cfg)
    assert active_path.read_bytes() == before


def test_apply_refuses_invalid_pending_structure(tmp_path):
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    before = active_path.read_bytes()
    Path(cfg.takeover_catalog_path).write_text(
        json.dumps({"models": [{"slug": "dup", "priority": 1}, {"slug": "dup", "priority": 2}]}),
        encoding="utf-8")
    with pytest.raises(InvalidCatalog, match="重复"):
        takeover.apply_pending(cfg)
    assert active_path.read_bytes() == before


def test_apply_refuses_catalog_without_import_record(tmp_path):
    """Writing back without ever importing the live catalog is refused."""
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    before = active_path.read_bytes()
    Path(cfg.takeover_catalog_path).write_text(
        json.dumps({"models": [{"slug": "only", "priority": 1}]}), encoding="utf-8")
    with pytest.raises(ConflictError, match="尚未从活跃目录导入"):
        takeover.apply_pending(cfg)
    assert active_path.read_bytes() == before


def test_inspect_reports_broken_files_instead_of_raising(tmp_path):
    """A malformed pending or live catalog is reported, never a traceback."""
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    Path(cfg.takeover_catalog_path).write_text("NOT JSON AT ALL", encoding="utf-8")
    state = takeover.inspect(cfg)
    assert not state.pending_ok and state.pending_error
    assert state.diff is None
    assert "待应用目录结构无效" in takeover.render_state(state)
    with pytest.raises(InvalidCatalog):
        takeover.apply_pending(cfg)

    # A live catalog that no longer parses is a conflict, not a crash.
    cfg2, _u2, active2, _s2 = _sandbox(tmp_path / "second")
    takeover.import_active(cfg2)
    pending2 = json.loads(Path(cfg2.takeover_catalog_path).read_text(encoding="utf-8"))
    pending2["models"][0]["display_name"] = "manager edit"
    Path(cfg2.takeover_catalog_path).write_text(json.dumps(pending2), encoding="utf-8")
    active2.write_text("{ broken", encoding="utf-8")
    state2 = takeover.inspect(cfg2)
    assert "无法解析" in state2.conflict
    assert not state2.writable or state2.conflict


# ---------------------------------------------------------------------------
# safe editing of an existing model
# ---------------------------------------------------------------------------

def test_update_model_preserves_unknown_fields_other_models_and_top_level(tmp_path):
    source = json.dumps(ACTIVE, ensure_ascii=False).encode("utf-8")
    updated = update_model(
        ModelEdit(context_window=131072, max_context_window=131072),
        "deepseek-v4.1-flash", source)
    root = json.loads(updated)
    assert root["schema"] == "user.custom.v7" and root["notes"] == ACTIVE["notes"]
    assert root["models"][0] == ACTIVE["models"][0]          # other model untouched
    assert root["models"][1]["max_context_window"] == 131072
    assert root["models"][1]["default_reasoning_level"] == "max"   # not part of the edit


def test_update_model_keeps_unknown_reasoning_level_fields(tmp_path):
    source = json.dumps(ACTIVE, ensure_ascii=False).encode("utf-8")
    updated = update_model(
        ModelEdit(reasoning=ReasoningSettings(
            supported_efforts=["low", "high", "max"], default_effort="max")),
        "gpt-6-astra", source)
    model = json.loads(updated)["models"][0]
    levels = {lv["effort"]: lv for lv in model["supported_reasoning_levels"]}
    assert levels["high"]["description"] == "provider high"   # provider metadata kept
    assert levels["high"]["budget"] == 200
    assert model["default_reasoning_level"] == "max"
    assert model["provider_metadata"] == {"route": "relay-1"}


def test_update_model_rejects_bad_input(tmp_path):
    source = json.dumps(ACTIVE, ensure_ascii=False).encode("utf-8")
    with pytest.raises(InvalidConfiguration, match="找不到"):
        update_model(ModelEdit(context_window=1), "no-such-model", source)
    with pytest.raises(InvalidConfiguration, match="最大上下文"):
        update_model(ModelEdit(context_window=200000), "gpt-6-astra", source)
    with pytest.raises(InvalidConfiguration, match="正整数"):
        update_model(ModelEdit(context_window=0), "gpt-6-astra", source)
    with pytest.raises(InvalidConfiguration, match="名称"):
        update_model(ModelEdit(display_name="   "), "gpt-6-astra", source)
    with pytest.raises(InvalidConfiguration, match="没有要修改"):
        update_model(ModelEdit(), "gpt-6-astra", source)
    with pytest.raises(InvalidConfiguration):
        update_model(ModelEdit(reasoning=ReasoningSettings(
            supported_efforts=["low"], default_effort="max")), "gpt-6-astra", source)
    # every rejection left the source representation unchanged
    assert json.loads(source) == ACTIVE


# ---------------------------------------------------------------------------
# CLI end-to-end (no Codex runtime involved)
# ---------------------------------------------------------------------------

def _run_cli(config_url: Path, *argv):
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "codex_model_manager", "--config", str(config_url), *argv],
        capture_output=True, text=True, env=env, cwd=str(repo))
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_takeover_flow(tmp_path):
    cfg, cfg_url, active_path, _s = _sandbox(tmp_path)
    original = active_path.read_bytes()

    rc, out, err = _run_cli(cfg_url, "takeover-import")
    assert rc == 0, err
    assert "生效配置引用目录" in out and "待应用目录" in out

    rc, out, err = _run_cli(cfg_url, "edit-model", "gpt-6-astra",
                            "--context-window", "262144", "--max-context-window", "262144")
    assert rc == 0, err
    assert active_path.read_bytes() == original   # editing the pending copy only

    rc, out, err = _run_cli(cfg_url, "takeover-diff")
    assert rc == 0, err
    assert "修改 1" in out and "context_window: 128000 -> 262144" in out
    assert active_path.read_bytes() == original

    rc, out, err = _run_cli(cfg_url, "takeover-apply")
    assert rc == 0, err
    written = json.loads(active_path.read_text(encoding="utf-8"))
    assert written["models"][0]["context_window"] == 262144

    rc, out, err = _run_cli(cfg_url, "takeover-undo")
    assert rc == 0, err
    assert active_path.read_bytes() == original


def test_cli_takeover_apply_refuses_removal_without_confirm(tmp_path):
    cfg, cfg_url, active_path, _s = _sandbox(tmp_path)
    original = active_path.read_bytes()
    rc, _out, err = _run_cli(cfg_url, "takeover-import")
    assert rc == 0, err

    # Drop one model from the pending copy, as a user edit would.
    pending_path = Path(cfg.takeover_catalog_path)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["models"] = [m for m in pending["models"] if m["slug"] != "deepseek-v4.1-flash"]
    pending_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")

    rc, out, err = _run_cli(cfg_url, "takeover-apply")
    assert rc != 0
    assert "deepseek-v4.1-flash" in out + err
    assert active_path.read_bytes() == original

    rc, out, err = _run_cli(cfg_url, "takeover-apply", "--confirm-removals")
    assert rc == 0, err
    written = json.loads(active_path.read_text(encoding="utf-8"))
    assert [m["slug"] for m in written["models"]] == ["gpt-6-astra"]


def test_cli_takeover_apply_is_readonly_without_import(tmp_path):
    cfg, cfg_url, active_path, _s = _sandbox(tmp_path)
    before = active_path.read_bytes()
    rc, out, err = _run_cli(cfg_url, "takeover-apply")
    assert rc != 0
    assert "未写入" in out + err
    assert active_path.read_bytes() == before


def test_cli_explicit_active_is_the_file_actually_written(tmp_path):
    """``--active A`` must never preview A and then silently write config target B."""
    cfg, cfg_url, configured_active, sandbox = _sandbox(tmp_path)
    explicit_active = sandbox / "explicit-active.json"
    explicit_active.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    configured_before = configured_active.read_bytes()

    rc, out, err = _run_cli(
        cfg_url, "takeover-import", "--active", str(explicit_active))
    assert rc == 0, out + err
    rc, out, err = _run_cli(
        cfg_url, "edit-model", "gpt-6-astra",
        "--context-window", "262144", "--max-context-window", "262144")
    assert rc == 0, out + err
    rc, out, err = _run_cli(
        cfg_url, "takeover-apply", "--active", str(explicit_active))
    assert rc == 0, out + err

    assert configured_active.read_bytes() == configured_before
    written = json.loads(explicit_active.read_text(encoding="utf-8"))
    assert written["models"][0]["context_window"] == 262144


def test_cli_apply_refuses_a_different_target_than_was_imported(tmp_path):
    cfg, cfg_url, configured_active, sandbox = _sandbox(tmp_path)
    imported_active = sandbox / "imported-active.json"
    imported_active.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    configured_before = configured_active.read_bytes()
    imported_before = imported_active.read_bytes()

    assert _run_cli(
        cfg_url, "takeover-import", "--active", str(imported_active))[0] == 0
    rc, out, err = _run_cli(cfg_url, "takeover-apply")
    assert rc != 0
    assert "不是同一个文件" in out + err
    assert configured_active.read_bytes() == configured_before
    assert imported_active.read_bytes() == imported_before


def test_cli_edit_model_rejects_arbitrary_catalog_path(tmp_path):
    cfg, cfg_url, _active, sandbox = _sandbox(tmp_path)
    takeover.import_active(cfg)
    cfg.save(str(cfg_url))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(ACTIVE, ensure_ascii=False), encoding="utf-8")
    before = outside.read_bytes()

    rc, out, err = _run_cli(
        cfg_url, "edit-model", "gpt-6-astra", "--catalog", str(outside),
        "--context-window", "262144", "--max-context-window", "262144")
    assert rc != 0
    assert "takeoverCatalogPath" in out + err
    assert outside.read_bytes() == before


def test_apply_persists_undo_record_before_writing(tmp_path):
    cfg, _url, active_path, _sandbox_dir = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "manager edit"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")
    before = active_path.read_bytes()

    def fail_to_persist():
        raise OSError("simulated config save failure")

    with pytest.raises(OSError, match="simulated config save failure"):
        takeover.apply_pending(cfg, persist=fail_to_persist)
    assert active_path.read_bytes() == before
    assert cfg.last_takeover is None


def test_legacy_config_derives_takeover_catalog_path(tmp_path):
    merged = tmp_path / "models.json"
    cfg = AppConfiguration.from_json({
        "customSourcePath": str(tmp_path / "custom.json"),
        "mergedCatalogPath": str(merged),
        "syncLogPath": str(tmp_path / "sync.jsonl"),
        "errorLogPath": str(tmp_path / "error.log"),
        "backupDirectoryPath": str(tmp_path / "backups"),
    })
    assert cfg.takeover_catalog_path == str(tmp_path / "pending-models.json")


def test_demo_pending_catalog_cannot_escape_sandbox(tmp_path):
    cfg, _url, _active, _sandbox_dir = _sandbox(tmp_path)
    outside = tmp_path / "outside" / "pending.json"
    cfg.takeover_catalog_path = str(outside)
    with pytest.raises(InvalidConfiguration, match="沙箱"):
        takeover.import_active(cfg)
    assert not outside.exists()


# ---------------------------------------------------------------------------
# the data service the GUI uses edits the pending copy, never the live catalog
# ---------------------------------------------------------------------------

def test_data_service_edits_and_adds_inside_the_pending_catalog(tmp_path):
    from codex_model_manager.core.custom_models import NewModelDraft
    from codex_model_manager.services.catalog_data_service import CatalogDataService

    cfg, _u, active_path, sandbox = _sandbox(tmp_path)
    original = active_path.read_bytes()
    takeover.import_active(cfg)
    pending = Path(cfg.takeover_catalog_path)
    service = CatalogDataService(cfg.resolved_paths())

    service.update_model_in(ModelEdit(context_window=131072, max_context_window=131072),
                            "deepseek-v4.1-flash", str(pending))
    assert list((sandbox / "backups").glob("pending-models.*.bak")), "edit must back up first"
    edited = json.loads(pending.read_text(encoding="utf-8"))
    assert edited["models"][1]["context_window"] == 131072
    assert edited["schema"] == "user.custom.v7" and edited["notes"] == ACTIVE["notes"]

    # A no-op edit must neither rewrite the file nor add another backup.
    backups = sorted((sandbox / "backups").glob("pending-models.*.bak"))
    service.update_model_in(ModelEdit(display_name=edited["models"][1]["display_name"]),
                            "deepseek-v4.1-flash", str(pending))
    assert sorted((sandbox / "backups").glob("pending-models.*.bak")) == backups

    service.add_model_to(NewModelDraft(
        slug="hosted-model", display_name="Hosted Model", description="from template",
        template_slug="gpt-6-astra", context_window=1000,
    ), str(pending))
    grown = json.loads(pending.read_text(encoding="utf-8"))
    assert [m["slug"] for m in grown["models"]][-1] == "hosted-model"
    assert grown["models"][-1]["provider_metadata"] == {"route": "relay-1"}  # cloned template
    assert grown["notes"] == ACTIVE["notes"]
    assert active_path.read_bytes() == original, "the live catalog is never edited here"


def test_apply_refuses_when_active_catalog_disappeared(tmp_path):
    cfg, _u, active_path, _s = _sandbox(tmp_path)
    takeover.import_active(cfg)
    pending = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    pending["models"][0]["display_name"] = "manager edit"
    Path(cfg.takeover_catalog_path).write_text(json.dumps(pending), encoding="utf-8")
    active_path.unlink()

    with pytest.raises(InvalidConfiguration, match="不存在"):
        takeover.apply_pending(cfg)
    assert not active_path.exists()


def test_create_from_empty_requires_explicit_allow_create(tmp_path):
    """Taking over an empty catalog: creating the live file is an explicit opt-in."""
    cfg, _u, active_path, sandbox = _sandbox(tmp_path, active=None, codex_config=False)
    (sandbox / "config.toml").write_text(
        'model_catalog_json = "active-models.json"\n', encoding="utf-8")
    cfg.codex_config_path = str(sandbox / "config.toml")
    takeover.import_active(cfg, create_empty=True)
    assert not active_path.exists()
    added = json.loads(Path(cfg.takeover_catalog_path).read_text(encoding="utf-8"))
    added["models"].append({"slug": "first-model", "priority": 1, "context_window": 1000})
    Path(cfg.takeover_catalog_path).write_text(json.dumps(added), encoding="utf-8")
    assert not active_path.exists()

    with pytest.raises(InvalidConfiguration, match="不存在"):
        takeover.apply_pending(cfg, allow_create=False)
    assert not active_path.exists()

    result = takeover.apply_pending(cfg, allow_create=True)
    assert result.applied and result.created_file
    assert [m["slug"] for m in json.loads(active_path.read_text(encoding="utf-8"))["models"]] \
        == ["first-model"]

    # Undo removes the file it created, restoring the previous non-existence.
    message = takeover.undo_pending(cfg)
    assert "删除" in message
    assert not active_path.exists()


def test_paths_must_be_configured_and_errors_are_actionable(tmp_path):
    """Unset paths produce a message naming the setting, never a bare traceback."""
    cfg, _u, _a, sandbox = _sandbox(tmp_path, codex_config=False)
    cfg.takeover_catalog_path = None
    with pytest.raises(InvalidConfiguration, match="takeoverCatalogPath"):
        takeover.pending_catalog_path(cfg)
    with pytest.raises(InvalidConfiguration, match="codexConfigPath"):
        takeover.active_catalog_path(cfg)
    # An explicit active path still works without a configured config.toml.
    live = sandbox / "active-models.json"
    assert takeover.active_catalog_path(cfg, str(live)) == str(live)

    # A config.toml that sets nothing is named too, rather than reporting a parse error.
    cfg2, _u2, _a2, sandbox2 = _sandbox(tmp_path / "second", codex_config=False)
    (sandbox2 / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
    cfg2.codex_config_path = str(sandbox2 / "config.toml")
    with pytest.raises(InvalidConfiguration, match="model_catalog_json"):
        takeover.active_catalog_path(cfg2)
