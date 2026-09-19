"""Shared fixtures: build an isolated mock-Codex environment per test.

The mock Codex is a Python script (demo/mock/codex.py). We generate a
`codex.cmd` wrapper so subprocess can launch it with an argument array on
Windows without any shell-string concatenation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

MOCK = Path(__file__).resolve().parent.parent / "demo" / "mock"


def make_runtime(tmp_path: Path) -> Path:
    """Create a codex.cmd wrapper that runs the mock Python script."""
    wrapper = tmp_path / "codex.cmd"
    python = sys.executable
    mock_py = MOCK / "codex.py"
    wrapper.write_text(
        '@echo off\r\n"{}" "{}" %*\r\n'.format(python, mock_py),
        encoding="utf-8",
    )
    return wrapper


@pytest.fixture
def mock_runtime(tmp_path, monkeypatch):
    """Return a helper that binds the mock runtime to CatalogPaths.

    Builds the mock runtime once; each call writes a fresh bundled catalog file
    and MOCK_* env vars, then returns a CatalogPaths-style dict plus the bundled
    file path and the env that the sync will run under.
    """
    runtime = make_runtime(tmp_path)

    def build(
        bundled: dict | None = None,
        version: str = "mock-codex 9.9.9",
        exit_code: str | None = None,
        sleep: str | None = None,
        custom_models: dict | None = None,
        visibility: dict | None = None,
        overrides: dict | None = None,
        base=None,
    ):
        import uuid

        base = base or (tmp_path / f"env-{uuid.uuid4().hex}")
        base.mkdir(parents=True, exist_ok=True)
        bundled_file = base / "bundled.json"
        if version is not None:
            monkeypatch.setenv("MOCK_CODEX_VERSION", version)
        if bundled is not None:
            bundled_file.write_text(json.dumps(bundled, ensure_ascii=False), encoding="utf-8")
            monkeypatch.setenv("MOCK_BUNDLED_JSON", str(bundled_file))
        if exit_code is not None:
            monkeypatch.setenv("MOCK_EXIT_CODE", exit_code)
        elif sleep is not None:
            # ensure a leftover simulated-failure flag can't swallow the sleep
            monkeypatch.setenv("MOCK_EXIT_CODE", "0")
        if sleep is not None:
            monkeypatch.setenv("MOCK_SLEEP_SECONDS", sleep)
        custom = base / "custom 模型.json"
        if custom_models is not None:
            custom.write_text(json.dumps(custom_models, ensure_ascii=False), encoding="utf-8")
        paths = {
            "codex_runtime": str(runtime),
            "custom_source": str(custom),
            "merged_catalog": str(base / "合并 models.json"),
            "sync_log": str(base / "Logs" / "sync.jsonl"),
            "error_log": str(base / "Logs" / "sync.error.log"),
            "backup_directory": str(base / "Backups"),
            "visibility_overrides": visibility or {},
            "model_overrides": overrides or {},
        }
        return paths, bundled_file

    return build