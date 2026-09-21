"""Runtime discovery + config-dir (CODEX_HOME / Windows vs WSL)."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from codex_model_manager.core import runtime as rt


def _write_magic(path: Path, magic: bytes):
    path.write_bytes(magic + b"\x00payload")


def test_classify_windows_pe(tmp_path):
    p = tmp_path / "codex.exe"
    _write_magic(p, b"MZ")
    assert rt.classify(str(p)) == "windows-native"


def test_classify_wsl_elf(tmp_path):
    p = tmp_path / "codex"
    _write_magic(p, b"\x7fELF")
    assert rt.classify(str(p)) == "wsl"


def test_codex_home_env(monkeypatch, tmp_path):
    assert os.environ.get("CODEX_HOME") is None or True
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex home"))
    assert rt.codex_home() == tmp_path / "codex home"


def test_codex_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "expanduser", lambda self: Path(os.path.expanduser("~")), raising=False)
    h = rt.codex_home()
    assert h.name == ".codex"


def test_discover_prefers_user_path_and_native(monkeypatch, tmp_path):
    # build a fake native binary
    fake = tmp_path / "codex.exe"
    _write_magic(fake, b"MZ")
    monkeypatch.setattr(rt, "_version", lambda p: "1.0.0")
    found = rt.discover(user_path=str(fake))
    assert found and found[0].kind == "windows-native"
    monkeypatch.setattr(rt, "_version", lambda p: None)
    assert rt.discover(user_path=str(fake)) == []