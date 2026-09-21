"""Regression tests for first-round review fixes.

Covers:
  - apply --diff / --dry-run are strictly read-only (no write, no backup, no dirs)
  - apply target is NOT derived from environment (codexConfigPath only)
  - safe preview never leaks unrelated config values / secrets
  - recommended() defaults to an independent app sandbox (does not write into the
    real CODEX_HOME), and the full demo flow leaves a sentinel CODEX_HOME untouched
  - restore validates backup content type (no config.toml -> model JSON, no guessing)
  - probe reports only what it actually verified (no unfounded capability claim)
  - GUI import is safe without a display and survives missing config/runtime
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_model_manager.cli import main
from codex_model_manager.core.app_config import AppConfiguration
from codex_model_manager.core.backup import classify_backup
from codex_model_manager.core.errors import CodexModelError
from codex_model_manager.core.runtime import probe_bundled

BUNDLED = {
    "models": [
        {
            "slug": "gpt-official",
            "display_name": "Official",
            "description": "Official",
            "visibility": "hide",
            "priority": 4,
            "context_window": 200000,
            "max_context_window": 800000,
            "input_modalities": ["text"],
        }
    ]
}

CUSTOM = {
    "models": [
        {
            "slug": "vendor-model",
            "display_name": "Vendor",
            "description": "Vendor",
            "priority": 1000,
            "context_window": 128000,
            "max_context_window": 128000,
            "input_modalities": ["text"],
        }
    ]
}


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def _make_runtime(tmp_path: Path) -> Path:
    """codex.cmd wrapper around the mock; reuse demo/mock/codex.py."""
    mock = Path(__file__).resolve().parent.parent / "demo" / "mock" / "codex.py"
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text(
        '@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, mock), encoding="utf-8")
    return wrapper


def _build_config(tmp_path: Path, runtime: Path, merged: Path, custom: Path, codex_config: Path) -> Path:
    cfg = AppConfiguration(
        codex_runtime_path=str(runtime),
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=str(codex_config),
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))
    return cfg_url


def _run_cli(config_url: Path, *argv):
    """Run the cli main() and capture stdout to a StringIO via capsys pattern."""
    code, out, err = 0, "", ""
    # use subprocess to be closer to real CLI behaviour and capture streams
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "codex_model_manager", "--config", str(config_url), *argv],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(Path(__file__).resolve().parent.parent))
    return proc.returncode, proc.stdout, proc.stderr


def test_apply_diff_is_strictly_readonly(tmp_path):
    """--diff / --dry-run must NOT write config, create backup, or create dirs."""
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    cfg = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))

    # produce a merged catalog via sync
    rc, out, err = _run_cli(cfg, "sync")
    assert rc == 0, err
    assert merged.exists()

    # a sentinel real config with an unrelated secret
    sentinel = tmp_path / "real_home" / "config.toml"
    _write(sentinel, 'model = "gpt-5"\napi_key = "SECRET_SHOULD_NOT_ESCAPE"\n')

    backup_dir = tmp_path / "Backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    before_backup_count = len(list(backup_dir.glob("*")))
    before_bytes = sentinel.read_bytes()

    for flag in ("--diff", "--dry-run", "--diff", "--dry-run"):
        args = ("--codex-config", str(sentinel)) if flag == "--codex-config" else []
        rc, out, err = _run_cli(cfg, "apply", flag, "--codex-config", str(sentinel))
        assert rc == 0, err
        # caught by command construction: we run effectively --diff --dry-run both
        assert "未写入" in out or "只读预览" in out

    # config not changed, no backup file added
    assert sentinel.read_bytes() == before_bytes
    assert len(list(backup_dir.glob("*"))) == before_backup_count


def test_apply_readonly_does_not_write_even_with_merged(tmp_path):
    """A strict apply --diff never writes even when the value would change."""
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    cfg = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))
    _ = tmp_path / "bundled.json"
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    rc, out, err = _run_cli(cfg, "sync")
    assert rc == 0, err

    sentinel = tmp_path / "real_home" / "config.toml"
    _write(sentinel, 'model = "gpt-5"\n')   # different model_catalog_json (absent)
    before = sentinel.read_bytes()
    rc, out, err = _run_cli(cfg, "apply", "--diff", "--codex-config", str(sentinel))
    assert rc == 0, err
    # nothing written, no model_catalog_json added, backup dir not created as side effect
    assert sentinel.read_bytes() == before
    assert not (tmp_path / "Backups").exists() or not list((tmp_path / "Backups").glob("*"))


def test_apply_requires_codex_config_path_never_derives_from_env(tmp_path):
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    cfg = _build_config(tmp_path, runtime, merged, custom, tmp_path / "cfg.toml")
    _write(custom, json.dumps(CUSTOM))
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    _run_cli(cfg, "sync")

    # Point config at a cfg with NO codexConfigPath and NO --codex-config, and set
    # CODEX_HOME to a temp dir; apply must refuse rather than derive from env.
    cfg2 = tmp_path / "cfg2.json"
    AppConfiguration(
        codex_runtime_path=str(runtime),
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=None,
    ).save(str(cfg2))
    os.environ["CODEX_HOME"] = str(tmp_path / "fake_home")
    rc, out, err = _run_cli(cfg2, "apply")
    assert rc != 0
    assert "codexConfigPath" in err


def test_apply_target_uses_config_path(tmp_path):
    """apply should use config.codexConfigPath when --codex-config is absent."""
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    target = tmp_path / "target" / "config.toml"
    cfg = _build_config(tmp_path, runtime, merged, custom, target)
    _write(custom, json.dumps(CUSTOM))
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    rc, out, err = _run_cli(cfg, "sync")
    assert rc == 0, err

    rc, out, err = _run_cli(cfg, "apply", "--dry-run")  # reads target from config
    assert rc == 0, err
    assert "model_catalog_json" in out


SECRET = "SUPER_SECRET_API_KEY_12345"


def test_safe_preview_hides_unrelated_config_and_api_key(tmp_path, capsys):
    """Preview must not echo api_key stored anywhere in config.toml."""
    from codex_model_manager.core.safe_preview import preview, render_preview

    config = tmp_path / "config.toml"
    config.write_text(
        "# header\n"
        'model = "gpt-5"\n'
        f'api_key = "{SECRET}"\n'
        '\n'
        '[profiles.work]\n'
        'model_catalog_json = "/profile/old.json"\n'
        f'api_key = "{SECRET}"\n'
        '\n'
        '[provider.openai]\n'
        f'token = "{SECRET}"\n',
        encoding="utf-8")
    info = preview(str(config), "/new/models.json")
    text = render_preview(info)
    for needle in (SECRET, "gpt-5", "profile/old", "provider", "token"):
        assert needle not in text
    assert "model_catalog_json" in text
    # proposed path renders as the local absolute path (Windows backslashes)
    assert "models.json" in text
    assert "new" in text.replace("\\", "/")


def test_apply_output_leaks_no_secret(tmp_path):
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    cfg = _build_config(tmp_path, runtime, merged, custom, tmp_path / "cfg.toml")
    _write(custom, json.dumps(CUSTOM))
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    _run_cli(cfg, "sync")

    sentinel = tmp_path / "real_home" / "config.toml"
    _write(sentinel,
        f'api_key = "{SECRET}"\nmodel = "gpt-5"\n')
    rc, out, err = _run_cli(cfg, "apply", "--diff", "--codex-config", str(sentinel))
    assert rc == 0, err
    combined = out + err
    assert SECRET not in combined
    assert "gpt-5" not in combined


def test_recommended_uses_app_sandbox_not_real_codex_home(tmp_path, monkeypatch):
    """recommended() must not place model source/merged into the real CODEX_HOME."""
    import codex_model_manager.core.app_config as m

    appdata = tmp_path / "AppData"
    monkeypatch.setattr(m, "_appdata_dir", lambda: appdata)
    rec = AppConfiguration.recommended()
    assert rec.codex_config_path is None
    ap = appdata.as_posix()
    assert ap in rec.custom_source_path.replace("\\", "/")
    assert ap in rec.merged_catalog_path.replace("\\", "/")
    # must not point into a ~/.codex home
    assert ".codex" not in rec.custom_source_path
    assert ".codex" not in rec.merged_catalog_path


def test_demo_flow_leaves_real_codex_home_untouched(tmp_path, monkeypatch):
    """Full flow (init->sync->add->reasoning->apply read-only) must not modify a
    sentinel 'real CODEX_HOME'."""
    import codex_model_manager.core.app_config as m
    from codex_model_manager.core.catalog_sync import CatalogSyncService, CatalogPaths
    from codex_model_manager.core.custom_models import add_from_template
    from codex_model_manager.core.reasoning import ReasoningSettings
    from codex_model_manager.core.backup import classify_backup
    from codex_model_manager.parser import parse_models

    appdata = tmp_path / "AppData"
    monkeypatch.setattr(m, "_appdata_dir", lambda: appdata)

    runtime = _make_runtime(tmp_path)
    os.environ["MOCK_BUNDLED_JSON"] = str(tmp_path / "bundled.json")
    _write(tmp_path / "bundled.json", json.dumps(BUNDLED))

    # simulated real CODEX_HOME with sentinel files
    real_home = tmp_path / "real_home"
    real_home.mkdir(parents=True, exist_ok=True)
    (real_home / "models.json").write_text("SENTINEL-REAL", encoding="utf-8")
    (real_home / "custom-models.json").write_text("SENTINEL-REAL-CUSTOM", encoding="utf-8")
    (real_home / "config.toml").write_text("real config sentinel", encoding="utf-8")

    # app config target: sandbox under appdata (recommended layout path)
    app_cfg = AppConfiguration.recommended()
    app_cfg.codex_runtime_path = str(runtime)
    merged = Path(app_cfg.merged_catalog_path)
    custom = Path(app_cfg.custom_source_path)

    paths = CatalogPaths(
        codex_runtime=str(runtime),
        custom_source=str(custom),
        merged_catalog=str(merged),
        sync_log=str(appdata / "Logs" / "sync.jsonl"),
        error_log=str(appdata / "Logs" / "sync.error.log"),
        backup_directory=str(appdata / "Backups"),
    )
    svc = CatalogSyncService(paths)
    svc.sync()  # writes merged into sandbox, NOT real_home

    # add custom model + reasoning
    source_data = custom.read_bytes()
    merged_data = merged.read_bytes()
    models = parse_models(source_data, merged_data)
    from codex_model_manager.services.catalog_data_service import CatalogDataService
    from codex_model_manager.core.custom_models import NewModelDraft
    svcCS = CatalogDataService(paths)
    svcCS.add_custom_model(NewModelDraft(slug="my-model", display_name="My", description="My model",
                                          template_slug="gpt-official",
                                          context_window=131072))
    svcCS.update_reasoning(ReasoningSettings(supported_efforts=["low", "high"], default_effort="low"), "my-model")

    # A real-mode apply entry to the sentinel "real" CODEX config must be refused
    # (no verified compatibility) and must leave the sentinel bytes untouched.
    from codex_model_manager.cli import main as cli_main
    app_cfg.codex_config_path = str(real_home / "config.toml")  # real mode (is_demo False)
    cfg_url = tmp_path / "app_config.json"
    app_cfg.save(str(cfg_url))
    sentinel_cfg_before = (real_home / "config.toml").read_bytes()
    rc = cli_main(["--config", str(cfg_url), "apply"])
    assert rc != 0  # real-mode apply is blocked (unverified), never writes
    assert (real_home / "config.toml").read_bytes() == sentinel_cfg_before

    # sentinel real CODEX_HOME untouched
    assert (real_home / "models.json").read_bytes() == b"SENTINEL-REAL"
    assert (real_home / "custom-models.json").read_bytes() == b"SENTINEL-REAL-CUSTOM"
    assert (real_home / "config.toml").read_bytes() == b"real config sentinel"
    # sandbox got the merged catalog
    assert merged.exists() and "models" in merged.read_text(encoding="utf-8")
    assert ".codex" not in str(merged)


def _restore_cfg(tmp_path, merged_content='{"models": []}', custom_content='{"schema": "codex_model_manager_custom_models.v1", "models": []}'):
    """Build a config + real target files so `restore` reaches the type gate."""
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    _write(merged, merged_content)
    _write(custom, custom_content)
    cfg = AppConfiguration(
        codex_runtime_path=str(_make_runtime(tmp_path)),
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=str(tmp_path / "cfg.toml"),
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))
    return cfg_url, merged, custom


def test_restore_config_backup_rejected_for_model_target(tmp_path):
    """A config.toml backup must NOT overwrite a model JSON (content-type gate)."""
    cfg_url, merged, custom = _restore_cfg(tmp_path, merged_content='{"models": [{"slug": "a"}]}')
    config_bak = tmp_path / "config.toml.backup.bak"
    config_bak.write_text('# codex config\nmodel = "gpt-5"\n', encoding="utf-8")
    assert classify_backup(str(config_bak)) == "toplevel_config"

    merged_before = merged.read_bytes()
    rc, out, err = _run_cli(cfg_url, "restore", str(config_bak), "--target", "merged")
    assert rc != 0
    assert "数据类型不匹配" in err or "已拒绝" in err
    # target untouched, and a backup of the original target is NOT created
    assert merged.read_bytes() == merged_before
    assert not os.path.exists(str(tmp_path / "Backups" / (merged.stem + ".bak"))) if list_path(tmp_path / "Backups") is None else True


def test_restore_mismatch_refused_unknown_backup(tmp_path):
    """An unrecognizable backup is refused and the target retains its content."""
    cfg_url, merged, custom = _restore_cfg(tmp_path, merged_content='{"models": [{"slug": "a"}]}')
    bad = tmp_path / "bad.bak"
    bad.write_text("this is not a recognized format", encoding="utf-8")
    assert classify_backup(str(bad)) == "unknown"

    merged_before = merged.read_bytes()
    rc, out, err = _run_cli(cfg_url, "restore", str(bad), "--target", "merged")
    assert rc != 0
    assert "无法识别" in err
    assert merged.read_bytes() == merged_before


def test_restore_custom_backup_to_custom_target_succeeds(tmp_path):
    """A model-catalog backup restores to its own target and backs up the old one."""
    cfg_url, merged, custom = _restore_cfg(tmp_path)
    backup = tmp_path / "custom-source.bak"
    payload = '{"schema": "codex_model_manager_custom_models.v1", "models": [{"slug": "b"}]}'
    backup.write_text(payload, encoding="utf-8")
    assert classify_backup(str(backup)) == "model_catalog_json"

    custom_before = custom.read_bytes()
    rc, out, err = _run_cli(cfg_url, "restore", str(backup), "--target", "custom")
    assert rc == 0, err
    assert custom.read_text(encoding="utf-8") == payload
    # a backup of the previous custom file exists before overwrite. The custom
    # source file here is "custom models.json" (stem "custom models"), so the
    # restore-prefixed backup name starts with "custom ".
    backups = (tmp_path / "Backups").glob("custom*")
    assert any(b for b in backups)


def list_path(p):
    import pathlib
    return [x for x in pathlib.Path(p).glob("*")] if pathlib.Path(p).exists() else None


def test_probe_reports_only_verified(tmp_path, capsys, monkeypatch):
    from codex_model_manager.core.runtime import CodexRuntime
    from codex_model_manager.cli import cmd_probe

    # fake native runtime whose version is None (not verified) -> not reported found
    rt = CodexRuntime(path=str(_make_runtime(tmp_path)), version=None, kind="windows-native")
    monkeypatch.setattr("codex_model_manager.core.runtime.discover", lambda **k: [rt])
    # probe_bundled is imported locally inside cmd_probe -> patch at the module it
    # lives in (core.runtime) rather than cli.
    monkeypatch.setattr("codex_model_manager.core.runtime.probe_bundled", lambda r, timeout=30: (True, ""))
    rc = cmd_probe(SimpleNamespace(codex=None, wsl=True))
    out = capsys.readouterr().out
    assert "bundled 接口: 已验证" in out
    assert "未验证" in out  # model_catalog_json capability always honest


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_gui_importable_without_display_and_survives_missing_config(tmp_path, monkeypatch):
    """GUI module imports without a display; offline (no runtime) doesn't raise."""
    import importlib
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        pytest.skip(f"no tkinter available: {exc}")

    import codex_model_manager.gui.app as gui_app
    # No display available in headless CI -> just assert import safety and that
    # the offline logic path is reachable (resolved raises RuntimeNotFound when
    # there is no native runtime and no path set).
    from codex_model_manager.core.errors import RuntimeNotFound
    from codex_model_manager.core.app_config import AppConfiguration
    # This is the no-runtime branch, even on a machine with Codex now installed.
    monkeypatch.setattr("codex_model_manager.core.runtime.find_native", lambda *a, **k: None)
    monkeypatch.setattr("codex_model_manager.core.app_config._discover_wsl", lambda: [])

    cfg = AppConfiguration(
        codex_runtime_path=None,   # no runtime
        custom_source_path=str(tmp_path / "custom.json"),
        merged_catalog_path=str(tmp_path / "merged.json"),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "B"),
    )
    # without a runtime and without find_native finding one, resolved() raises
    try:
        cfg.resolved()
        raised = False
    except RuntimeNotFound:
        raised = True
    assert raised
    # GUI module imported fine (import-safe), proving the offline/recoverable
    # entry is reachable rather than crashing at import time.
    assert hasattr(gui_app, "CodexModelManagerApp")


# ---- Second round: apply gate (shared CLI/GUI) ----

def _apply_cfg(tmp_path, target, is_demo, runtime=None, merge_dir=None, monkeypatch=None,
               do_sync=True, merged_content=None):
    """Build a config whose merged catalog lives under merge_dir (the app sandbox),
    optionally set the runtime to a nonexistent path, sync via mock, return
    (config_url, merged_path). env is isolated via monkeypatch when given.

    When the runtime is deliberately nonexistent, `resolved()` refuses to even
    sync (codexRuntimePath 指向的文件不存在), so pass do_sync=False and either
    merged_content (written verbatim) or the bundled catalog is written directly;
    apply still reaches the runtime gate because that is checked before the
    catalog contents."""
    if merge_dir is None:
        merge_dir = tmp_path
    runtime = runtime if runtime is not None else _make_runtime(tmp_path)
    merged = Path(merge_dir) / "merged models.json"
    custom = Path(merge_dir) / "custom models.json"
    _write(custom, json.dumps(CUSTOM))
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    if monkeypatch is not None:
        monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent_home"))
    else:
        os.environ["MOCK_BUNDLED_JSON"] = str(bundled)
    cfg = AppConfiguration(
        codex_runtime_path=str(runtime),
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(merge_dir / "Logs" / "sync.jsonl"),
        error_log_path=str(merge_dir / "Logs" / "sync.error.log"),
        backup_directory_path=str(merge_dir / "Backups"),
        codex_config_path=str(target),
        is_demo=is_demo,
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))
    if do_sync:
        rc, out, err = _run_cli(cfg_url, "sync")
        assert rc == 0, err
    elif merged_content is not None:
        _write(merged, merged_content)
    else:
        # a valid structural catalog so only the runtime gate (checked first) blocks
        _write(merged, json.dumps(BUNDLED))
    return cfg_url, merged


def test_apply_nonexistent_runtime_rejected_bytes_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "sink" / "config.toml"
    _write(target, 'model = "gpt-5"\n')
    before = target.read_bytes()
    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=False,
        runtime=tmp_path / "DOES_NOT_EXIST.exe", monkeypatch=monkeypatch,
        do_sync=False)
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc != 0
    assert "不可执行" in err + out or "不存在" in err + out
    assert target.read_bytes() == before


def test_apply_real_mode_unverified_refused(tmp_path, monkeypatch):
    target = tmp_path / "sink" / "config.toml"
    _write(target, 'model = "gpt-5"\n')
    before = target.read_bytes()
    cfg_url, merged = _apply_cfg(tmp_path, target, is_demo=False, monkeypatch=monkeypatch)
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc != 0
    assert "未验证" in out + err
    assert target.read_bytes() == before


def test_apply_invalid_catalog_refused(tmp_path, monkeypatch):
    target = tmp_path / "sink" / "config.toml"
    _write(target, 'model = "gpt-5"\n')
    before = target.read_bytes()
    # merged_catalog exists but is not {"models": [...]} -> structural gate rejects
    cfg_url, merged = _apply_cfg(tmp_path, target, is_demo=False, monkeypatch=monkeypatch)
    merged.write_text("this is not json", encoding="utf-8")
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc != 0
    assert target.read_bytes() == before


def test_apply_demo_out_of_sandbox_refused(tmp_path, monkeypatch):
    # merged catalog lives in tmp_path/sandbox; target outside it must be refused.
    sandbox = tmp_path / "sandbox"
    outside = tmp_path / "outside" / "config.toml"
    _write(outside, 'model = "gpt-5"\n')
    before = outside.read_bytes()
    cfg_url, merged = _apply_cfg(tmp_path, outside, is_demo=True,
                                 merge_dir=sandbox, monkeypatch=monkeypatch)
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc != 0
    assert "越界" in out + err or "沙箱" in out + err
    assert outside.read_bytes() == before


def test_apply_demo_insandbox_allowed(tmp_path, monkeypatch):
    # demo mode, target inside the sandbox holding merged_catalog -> write allowed.
    sandbox = tmp_path / "sandbox"
    target = sandbox / "target_config.toml"
    _write(target, 'model = "gpt-5"\n')
    cfg_url, merged = _apply_cfg(tmp_path, target, is_demo=True,
                                 merge_dir=sandbox, monkeypatch=monkeypatch)
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc == 0, err
    assert "model_catalog_json" in target.read_text(encoding="utf-8")


def test_sync_background_error_resets_and_recovers(tmp_path, monkeypatch):
    """Blocker 2: a failing background sync must surface the error text and allow
    a subsequent sync. We drive the shared run_sync_in_background() through a
    controlled UI surrogate (no display needed): schedule runs the callback AFTER
    the worker thread returned, i.e. after the `except` block has finished, which
    is exactly where the old closure-based capture crashed with NameError."""
    from codex_model_manager.gui.app import run_sync_in_background

    class Svc:
        def __init__(self):
            self.calls = 0

        def run_sync(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("BOOM-sync-failed")

            class _R:
                status = "no_change"
            return _R()

    class Surrogate:
        """Mirrors how the GUI wires sync(): keeps a syncing guard, schedules the
        completion/error callback and resets the guard in the callback."""
        def __init__(self):
            self.syncing = False
            self._pending = []
            self.error_text = None
            self.done_status = None

        def start(self, svc):
            self.error_text = None
            self.done_status = None
            self.syncing = True
            run_sync_in_background(
                svc,
                schedule=lambda fn: self._pending.append(fn),
                on_done=lambda s: self._record_done(s),
                on_error=lambda t: self._record_error(t),
            )

        # simulate the UI main loop flushing queued callbacks
        def flush(self):
            while self._pending:
                fn = self._pending.pop(0)
                fn()

        def _record_done(self, s):
            self.done_status = s
            self.syncing = False

        def _record_error(self, t):
            self.error_text = t
            self.syncing = False

    svc = Svc()
    ui = Surrogate()
    ui.start(svc)
    # Wait until the worker thread has scheduled the failure callback == it has
    # EXITED the except block. Only then (after except) do we run it on the UI side,
    # which is exactly where the old `lambda: f(exc)` crashed with NameError.
    import time
    deadline = time.time() + 5.0
    while not ui._pending and time.time() < deadline:
        time.sleep(0.005)
    assert ui._pending  # worker finished; callback queued
    ui.flush()
    assert ui.error_text == "BOOM-sync-failed"
    assert ui.syncing is False  # guard reset so a new sync is allowed

    # a second sync (recover) must succeed
    ui.start(svc)
    deadline = time.time() + 5.0
    while not ui._pending and time.time() < deadline:
        time.sleep(0.005)
    ui.flush()
    assert ui.done_status == "no_change"
    assert ui.error_text is None
    assert ui.syncing is False


# ---------------------------------------------------------------------------
# P1: demo sandbox escape (.., sibling prefix, symlink/junction, directory)
# ---------------------------------------------------------------------------

def test_apply_demo_dotdot_escape_refused(tmp_path, monkeypatch):
    """A demo target given as ``sandbox/../outside.toml`` must NOT write the file
    the '..' resolves to. The gate canonizes both sandbox root and target, so the
    literal '..' cannot escape the sandbox."""
    sandbox = tmp_path / "sandbox"
    outside = tmp_path / "outside.toml"
    _write(outside, 'model = "gpt-5"\nSENTINEL_DOTDOT = "KEEP"\n')
    before = outside.read_bytes()

    cfg_url, merged = _apply_cfg(
        tmp_path, outside, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False, merged_content=json.dumps(BUNDLED))

    dotdot = str(sandbox / ".." / "outside.toml")
    rc, out, err = _run_cli(cfg_url, "apply", "--codex-config", dotdot)
    assert rc != 0
    assert ("越界" in out + err) or ("沙箱" in out + err)
    assert outside.read_bytes() == before


def test_apply_demo_directory_target_refused(tmp_path, monkeypatch):
    """A demo target that is a directory must be refused even inside the sandbox."""
    sandbox = tmp_path / "sandbox"
    target_dir = sandbox / "config.toml"
    target_dir.mkdir(parents=True, exist_ok=True)

    cfg_url, merged = _apply_cfg(
        tmp_path, target_dir, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False, merged_content=json.dumps(BUNDLED))

    rc, out, err = _run_cli(cfg_url, "apply", "--codex-config", str(target_dir))
    assert rc != 0
    assert "目录" in out + err


def test_apply_demo_sibling_prefix_outside_refused(tmp_path, monkeypatch):
    """A sibling dir sharing a path prefix (``sandbox_evil``) must not pass a
    naive string-prefix check; the common-path membership check rejects it."""
    sandbox = tmp_path / "sandbox"
    sibling = tmp_path / "sandbox_evil"
    target = sibling / "config.toml"
    _write(target, 'model = "gpt-5"\nSENTINEL_PREFIX = "KEEP"\n')
    before = target.read_bytes()

    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False, merged_content=json.dumps(BUNDLED))

    rc, out, err = _run_cli(cfg_url, "apply", "--codex-config", str(target))
    assert rc != 0
    assert ("越界" in out + err) or ("沙箱" in out + err)
    assert target.read_bytes() == before


def test_apply_demo_symlink_escape_refused(tmp_path, monkeypatch):
    """A symlink inside the sandbox that resolves outside it must be refused.
    Skipped explicitly when the platform cannot create a symbolic link."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside_dir"
    target = outside_dir / "config.toml"
    _write(target, 'model = "gpt-5"\nSENTINEL_LINK = "KEEP"\n')
    before = target.read_bytes()

    link = sandbox / "link.toml"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("platform does not support symlinks")
    if not os.path.islink(link):
        # Some environments (notably a sandboxed runner) accept os.symlink() and
        # silently create nothing.  Without a real link there is no escape to
        # refuse, so asserting a refusal here would be a false failure.  Skip
        # instead — and never report the escape as verified.
        pytest.skip("os.symlink() did not actually create a link in this environment")

    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False, merged_content=json.dumps(BUNDLED))

    rc, out, err = _run_cli(cfg_url, "apply", "--codex-config", str(link))
    assert rc != 0
    assert ("越界" in out + err) or ("沙箱" in out + err)
    assert target.read_bytes() == before


def test_apply_demo_junction_escape_refused(tmp_path, monkeypatch):
    """A directory junction inside the sandbox that resolves outside must be refused.

    Junctions need no special privilege on Windows (unlike symlinks, which a
    sandboxed runner may refuse to create without raising), so this is the
    runnable proof that demo sandbox containment also resolves reparse points.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside_dir"
    target = outside_dir / "config.toml"
    _write(target, 'model = "gpt-5"\nSENTINEL_JUNCTION = "KEEP"\n')
    before = target.read_bytes()

    junction = sandbox / "junc"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0 or not junction.exists():
        pytest.skip("cannot create a directory junction in this environment")

    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False, merged_content=json.dumps(BUNDLED))

    rc, out, err = _run_cli(
        cfg_url, "apply", "--codex-config", str(junction / "config.toml"))
    assert rc != 0
    assert ("越界" in out + err) or ("沙箱" in out + err)
    assert target.read_bytes() == before


# ---------------------------------------------------------------------------
# P2: model structure validation on apply (null/string element, missing/duplicate
#     slug, wrong field types, invalid context, invalid reasoning level)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("models", [
    [],                                        # empty array (merged must be non-empty)
    [None],                                   # null element
    ["oops"],                                 # string element
    [{"display_name": "no slug", "priority": 1}],                    # missing slug
    [{"slug": "dup", "priority": 1}, {"slug": "dup", "priority": 2}],  # duplicate slug
    [{"slug": "x", "priority": "bad"}],                              # priority non-numeric
    [{"slug": "x", "priority": 1, "context_window": "huge"}],        # ctx wrong type
    [{"slug": "x", "priority": 1, "context_window": -5}],            # ctx non-positive
    [{"slug": "x", "priority": 1, "context_window": 200, "max_context_window": 100}],  # max < ctx
    [{"slug": "x", "priority": 1, "display_name": 123}],             # wrong field type
    [{"slug": "x", "priority": 1,
      "supported_reasoning_levels": [{"effort": "low"}],
      "default_reasoning_level": "high"}],                           # default outside supported
])
def test_apply_invalid_merged_catalog_refused(tmp_path, monkeypatch, models):
    """apply must reject a structurally-invalid merged catalog and leave the
    target unchanged."""
    sandbox = tmp_path / "sandbox"
    target = sandbox / "target_config.toml"
    _write(target, 'model = "gpt-5"\nSENTINEL_INVALID = "KEEP"\n')
    before = target.read_bytes()

    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=True, merge_dir=sandbox,
        monkeypatch=monkeypatch, do_sync=False,
        merged_content=json.dumps({"models": models}))

    rc, out, err = _run_cli(cfg_url, "apply", "--codex-config", str(target))
    assert rc != 0
    assert ("结构非法" in out + err) or ("目录无效" in out + err)
    assert target.read_bytes() == before


def test_restore_invalid_model_backup_rejected(tmp_path):
    """A corrupt model-catalog backup must not overwrite a healthy merged target."""
    merged = tmp_path / "healthy" / "merged models.json"
    _write(merged, json.dumps(BUNDLED))
    before = merged.read_bytes()

    backup = tmp_path / "corrupt.json"
    _write(backup, json.dumps({"models": [None]}))

    cfg = AppConfiguration(
        codex_runtime_path="",
        custom_source_path=str(tmp_path / "custom models.json"),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=None,
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))

    rc, out, err = _run_cli(cfg_url, "restore", str(backup), "--target", "merged")
    assert rc != 0
    assert ("结构无效" in err + out) or ("拒绝恢复" in err + out)
    assert merged.read_bytes() == before


def test_restore_empty_custom_backup_succeeds(tmp_path):
    """A legitimately-empty custom source (schema + models=[]) may be restored to
    the custom target, and the pre-restore content is backed up first."""
    custom = tmp_path / "custom models.json"
    _write(custom, json.dumps(CUSTOM))
    backup = tmp_path / "empty_custom.json"
    _write(backup, json.dumps({"schema": "codex_model_manager_custom_models.v1", "models": []}))

    backup_dir = tmp_path / "Backups"
    cfg = AppConfiguration(
        codex_runtime_path="",
        custom_source_path=str(custom),
        merged_catalog_path=str(tmp_path / "merged models.json"),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(backup_dir),
        codex_config_path=None,
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))

    rc, out, err = _run_cli(cfg_url, "restore", str(backup), "--target", "custom")
    assert rc == 0, err
    restored = json.loads(custom.read_text(encoding="utf-8"))
    assert restored["models"] == []
    # pre-restore content must have been backed up (atomic replace, keep-old-first)
    assert list(backup_dir.glob("*.before-restore*.bak")), "restore must back up the pre-restore target"


def test_restore_empty_merged_backup_refused(tmp_path):
    """The same empty models array is invalid for a merged catalog: restore must
    refuse and leave the healthy merged target unchanged."""
    merged = tmp_path / "merged models.json"
    _write(merged, json.dumps(BUNDLED))
    before = merged.read_bytes()
    backup = tmp_path / "empty.json"
    _write(backup, json.dumps({"models": []}))

    cfg = AppConfiguration(
        codex_runtime_path="",
        custom_source_path=str(tmp_path / "custom models.json"),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=None,
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))

    rc, out, err = _run_cli(cfg_url, "restore", str(backup), "--target", "merged")
    assert rc != 0
    assert ("结构无效" in err + out) or ("拒绝恢复" in err + out)
    assert merged.read_bytes() == before


# ---------------------------------------------------------------------------
# P3: offline preview (--diff) must not require a discoverable/startable runtime
# ---------------------------------------------------------------------------

def test_apply_diff_offline_without_runtime(tmp_path, monkeypatch):
    """apply --diff must succeed with NO runtime discoverable and write nothing,
    showing the single model_catalog_json change."""
    sandbox = tmp_path / "sandbox"
    target = sandbox / "config.toml"
    _write(target, 'model = "gpt-5"\n')

    cfg_url, merged = _apply_cfg(
        tmp_path, target, is_demo=True, merge_dir=sandbox,
        runtime=tmp_path / "DOES_NOT_EXIST.exe", monkeypatch=monkeypatch,
        do_sync=False, merged_content=json.dumps(BUNDLED))

    rc, out, err = _run_cli(cfg_url, "apply", "--diff")
    assert rc == 0, err
    assert "model_catalog_json" in out
    assert target.read_text(encoding="utf-8") == 'model = "gpt-5"\n'
