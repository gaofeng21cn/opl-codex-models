"""Shared fixtures: build an isolated mock-Codex environment per test.

The mock Codex is a Python script (demo/mock/codex.py). We generate a
`codex.cmd` wrapper so subprocess can launch it with an argument array on
Windows without any shell-string concatenation.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest

_CLI_LOCK = threading.Lock()


def run_cli_inprocess(config_path: Path | str | None, *argv: str) -> tuple[int, str, str]:
    """Run ``codex_model_manager.cli.main`` in-process and return (rc, stdout, stderr)."""
    from codex_model_manager.cli import main as cli_main

    cli_argv = (
        ["--config", str(config_path), *argv]
        if config_path is not None
        else list(argv)
    )
    with _CLI_LOCK:
        prev_argv = sys.argv[:]
        prev_env = os.environ.copy()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        rc = 0
        try:
            sys.argv = ["codex_model_manager", *cli_argv]
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try:
                    result = cli_main(cli_argv)
                    rc = 0 if result is None else int(result)
                except SystemExit as exc:
                    code = exc.code
                    if code is None:
                        rc = 0
                    elif isinstance(code, int):
                        rc = code
                    else:
                        if code:
                            stderr_buf.write(f"{code}\n")
                        rc = 1
        finally:
            sys.argv = prev_argv
            for key in set(os.environ) - set(prev_env):
                del os.environ[key]
            for key, value in prev_env.items():
                if os.environ.get(key) != value:
                    os.environ[key] = value
        return rc, stdout_buf.getvalue(), stderr_buf.getvalue()

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