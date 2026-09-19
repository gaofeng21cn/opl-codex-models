"""Tests for the compatibility probe, evidence-gated apply, and undo.

Covers the w4-w7 functionality:
  - probe_compatibility produces valid evidence against the mock runtime
  - evidence_to_dict / evidence_from_dict round-trip
  - evidence_valid binds to runtime path/SHA256/kind/distro
  - CLI `probe --verify` stores evidence and succeeds
  - CLI `apply` succeeds in real mode when valid evidence is stored
  - CLI `apply` is refused when evidence is stale (wrong runtime path)
  - CLI `undo` restores the previous value with conflict detection
  - CLI `undo` refuses when the value was externally changed
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_model_manager.core.app_config import AppConfiguration
from codex_model_manager.core.compat_probe import (
    CompatEvidence,
    probe_compatibility,
    evidence_to_dict,
    evidence_from_dict,
    evidence_valid,
)
from codex_model_manager.core.wsl_adapter import make_native_target

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
    mock = Path(__file__).resolve().parent.parent / "demo" / "mock" / "codex.py"
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text(
        '@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, mock), encoding="utf-8")
    return wrapper


def _build_config(tmp_path: Path, runtime: Path, merged: Path, custom: Path,
                  codex_config: Path, is_demo: bool = False) -> Path:
    cfg = AppConfiguration(
        codex_runtime_path=str(runtime),
        custom_source_path=str(custom),
        merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "Logs" / "sync.jsonl"),
        error_log_path=str(tmp_path / "Logs" / "sync.error.log"),
        backup_directory_path=str(tmp_path / "Backups"),
        codex_config_path=str(codex_config),
        is_demo=is_demo,
    )
    cfg_url = tmp_path / "config.json"
    cfg.save(str(cfg_url))
    return cfg_url


def _run_cli(config_url: Path, *argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "codex_model_manager", "--config", str(config_url), *argv],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).resolve().parent.parent))
    return proc.returncode, proc.stdout, proc.stderr


# ---- probe_compatibility: behaviour proof against mock ----

def test_probe_compatibility_succeeds_against_mock(tmp_path, monkeypatch):
    """The probe produces ok=True evidence against a working mock runtime."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    target = make_native_target(str(runtime), str(codex_home))
    evidence = probe_compatibility(target)

    assert evidence.ok is True
    assert evidence.reason == ""
    assert evidence.runtime_kind == "windows-native"
    assert evidence.runtime_version == "mock-codex 9.9.9"
    assert evidence.bundled_model_count == 1
    assert evidence.marker_loaded is True
    assert evidence.marker_absent_from_bundled is True
    assert evidence.missing_catalog_exit_code != 0
    assert evidence.probe_slug.startswith("compat-probe-")


def test_probe_compatibility_fails_when_bundled_missing(tmp_path, monkeypatch):
    """The probe fails when the bundled catalog file does not exist."""
    runtime = _make_runtime(tmp_path)
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")
    # MOCK_BUNDLED_JSON points at a nonexistent file
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(tmp_path / "no_such.json"))

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    target = make_native_target(str(runtime), str(codex_home))
    evidence = probe_compatibility(target)

    assert evidence.ok is False
    assert "bundled" in evidence.reason.lower() or "失败" in evidence.reason


# ---- evidence serialisation ----

def test_evidence_round_trip(tmp_path, monkeypatch):
    """evidence_to_dict -> evidence_from_dict preserves all fields."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    target = make_native_target(str(runtime), str(codex_home))
    evidence = probe_compatibility(target)
    assert evidence.ok

    restored = evidence_from_dict(evidence_to_dict(evidence))
    assert restored is not None
    assert restored.ok == evidence.ok
    assert restored.runtime_path == evidence.runtime_path
    assert restored.runtime_sha256 == evidence.runtime_sha256
    assert restored.runtime_version == evidence.runtime_version
    assert restored.probe_slug == evidence.probe_slug
    assert restored.probe_scheme == evidence.probe_scheme


def test_evidence_from_dict_none_for_bad_data():
    assert evidence_from_dict(None) is None
    assert evidence_from_dict({}) is None
    assert evidence_from_dict("not a dict") is None


# ---- evidence_valid: binding to runtime ----

def test_evidence_valid_for_matching_target(tmp_path, monkeypatch):
    """evidence_valid returns True for the same runtime that produced the evidence."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    target = make_native_target(str(runtime), str(codex_home))
    evidence = probe_compatibility(target)
    assert evidence_valid(evidence, target) is True


def test_evidence_invalid_for_changed_path(tmp_path, monkeypatch):
    """evidence_valid returns False when the runtime path has changed."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    target = make_native_target(str(runtime), str(codex_home))
    evidence = probe_compatibility(target)

    # Build a target with a different (nonexistent) path
    other = make_native_target(str(tmp_path / "different.cmd"), str(codex_home))
    assert evidence_valid(evidence, other) is False


def test_evidence_invalid_for_none():
    assert evidence_valid(None, None) is False


# ---- CLI: probe --verify ----

def test_cli_probe_verify_stores_evidence(tmp_path, monkeypatch):
    """`probe --verify` succeeds and stores evidence in the app config."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc == 0, err
    assert "通过" in out
    assert "已存储脱敏证据" in out

    # Evidence is persisted in the config
    cfg = AppConfiguration.load(str(cfg_url))
    assert cfg.compat_evidence is not None
    assert cfg.compat_evidence["ok"] is True


def test_cli_probe_verify_fails_for_broken_runtime(tmp_path, monkeypatch):
    """`probe --verify` fails when the runtime cannot answer --version."""
    runtime = tmp_path / "nonexistent.cmd"
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc != 0
    assert "失败" in out + err or "无法" in out + err


# ---- CLI: evidence-gated apply ----

def test_cli_apply_succeeds_after_verify(tmp_path, monkeypatch):
    """After `probe --verify` stores valid evidence, `apply` writes for real."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    _write(codex_config, 'model = "gpt-5"\n')
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    # sync to produce a merged catalog
    rc, out, err = _run_cli(cfg_url, "sync")
    assert rc == 0, err

    # verify to store evidence
    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc == 0, err

    # apply should now succeed (evidence is valid)
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc == 0, err
    assert "已备份原 config 并写入" in out
    text = codex_config.read_text(encoding="utf-8")
    assert "model_catalog_json" in text
    # the original model line is preserved (tomlkit)
    assert 'model = "gpt-5"' in text

    # last_apply is recorded
    cfg = AppConfiguration.load(str(cfg_url))
    assert cfg.last_apply is not None
    assert cfg.last_apply["targetConfig"] == str(codex_config)


def test_cli_apply_refused_with_stale_evidence(tmp_path, monkeypatch):
    """Apply is refused when evidence exists but binds to a different runtime."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    _write(codex_config, 'model = "gpt-5"\n')
    before = codex_config.read_bytes()
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "sync")
    assert rc == 0, err

    # Store evidence, then change the runtime path to invalidate it.
    # Use a second mock runtime that EXISTS but differs (path/SHA mismatch).
    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc == 0, err
    runtime2 = tmp_path / "codex2.cmd"
    runtime2.write_text(runtime.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = AppConfiguration.load(str(cfg_url))
    cfg.codex_runtime_path = str(runtime2)
    cfg.save(str(cfg_url))

    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc != 0
    assert "未验证" in out + err or "证据" in out + err
    assert codex_config.read_bytes() == before


# ---- CLI: undo ----

def test_cli_undo_restores_previous_value(tmp_path, monkeypatch):
    """`undo` after `apply` restores the original config (key was absent)."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    _write(codex_config, 'model = "gpt-5"\n')
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "sync")
    assert rc == 0, err
    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc == 0, err
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc == 0, err
    assert "model_catalog_json" in codex_config.read_text(encoding="utf-8")

    rc, out, err = _run_cli(cfg_url, "undo")
    assert rc == 0, err
    text = codex_config.read_text(encoding="utf-8")
    assert "model_catalog_json" not in text
    assert 'model = "gpt-5"' in text

    # last_apply is cleared
    cfg = AppConfiguration.load(str(cfg_url))
    assert cfg.last_apply is None


def test_cli_undo_refuses_on_conflict(tmp_path, monkeypatch):
    """`undo` refuses when the value was externally changed after apply."""
    runtime = _make_runtime(tmp_path)
    bundled = tmp_path / "bundled.json"
    _write(bundled, json.dumps(BUNDLED))
    monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled))
    monkeypatch.setenv("MOCK_CODEX_VERSION", "mock-codex 9.9.9")

    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    _write(codex_config, 'model = "gpt-5"\n')
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "sync")
    assert rc == 0, err
    rc, out, err = _run_cli(cfg_url, "probe", "--verify")
    assert rc == 0, err
    rc, out, err = _run_cli(cfg_url, "apply")
    assert rc == 0, err

    # Externally change the value
    _write(codex_config, 'model_catalog_json = "/somewhere/else.json"\n')

    rc, out, err = _run_cli(cfg_url, "undo")
    assert "冲突" in out + err


def test_cli_undo_no_record(tmp_path, monkeypatch):
    """`undo` without a prior apply reports nothing to undo."""
    runtime = _make_runtime(tmp_path)
    merged = tmp_path / "merged models.json"
    custom = tmp_path / "custom models.json"
    codex_config = tmp_path / "CodexHome" / "config.toml"
    cfg_url = _build_config(tmp_path, runtime, merged, custom, codex_config)
    _write(custom, json.dumps(CUSTOM))

    rc, out, err = _run_cli(cfg_url, "undo")
    assert rc != 0
    assert "无可撤销" in out + err or "没有" in out + err
