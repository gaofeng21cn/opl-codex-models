"""Backup/restore semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_model_manager.core.backup import atomic_write, backup_file, restore_file


def test_backup_restore_roundtrip_keeps_current_first(tmp_path):
    target = tmp_path / "models.json"
    target.write_text('{"old":1}')
    backup_dir = tmp_path / "Backups"
    b = backup_file(str(target), str(backup_dir), prefix="models")
    assert b and Path(b).exists()
    # simulate a bad overwrite
    atomic_write(str(target), b'{"corrupt":2}')
    assert target.read_text(encoding="utf-8") == '{"corrupt":2}'
    before = restore_file(b, str(target), str(backup_dir), prefix="models")
    assert target.read_text(encoding="utf-8") == '{"old":1}'
    # pre-restore state was preserved so a wrong restore is reversible
    assert before and Path(before).exists()
    assert Path(before).read_text(encoding="utf-8") == '{"corrupt":2}'


def test_failed_write_preserves_old_data(tmp_path):
    target = tmp_path / "merged.json"
    target.write_text("ORIGINAL")
    backup_dir = tmp_path / "Backups"

    # atomic_write into a path whose parent is a file => write must fail
    blocking = tmp_path / "blocker"
    blocking.write_text("x")
    bad_path = blocking / "sub" / "file.json"
    with pytest.raises(Exception):
        atomic_write(str(bad_path), b"DATA")
    # original merged file untouched
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # no leftover temp file in target dir
    leftovers = [p for p in tmp_path.glob("*.tmp.*")] + list(tmp_path.glob(".*tmp*"))
    assert leftovers == []


def test_restore_missing_backup_raises(tmp_path):
    target = tmp_path / "x.json"
    with pytest.raises(FileNotFoundError):
        restore_file(str(tmp_path / "nope.bak"), str(target), str(tmp_path / "B"), prefix="x")


def test_backup_missing_source_returns_none(tmp_path):
    assert backup_file(str(tmp_path / "absent.json"), str(tmp_path / "B"), prefix="x") is None