"""TOML config editor: comment/other-config preservation, corrupt-input safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_model_manager.core.config_editor import set_model_catalog


def test_preserves_comments_toplevel_and_profile(tmp_path):
    config = tmp_path / "config.toml"
    original = (
        "# top comment\n"
        'model = "gpt-5"\n'
        'model_catalog_json = "/old/models.json"\n'
        "\n"
        "[profiles.work]\n"
        'model = "profile-model"\n'
        'model_catalog_json = "/profile/models.json"\n'
        "\n"
        "# keep this\n"
        "sandbox_mode = \"read_only\"\n"
    )
    config.write_text(original, encoding="utf-8")
    catalog = tmp_path / "new models.json"
    backup_dir = tmp_path / "Backups"

    set_model_catalog(str(catalog), str(config), str(backup_dir))
    updated = config.read_text(encoding="utf-8")

    assert '# top comment' in updated
    assert 'model = "gpt-5"' in updated
    assert f'model_catalog_json = "{catalog}"' in updated.replace("\\", "/") or 'model_catalog_json = "C:' in updated
    # profile table untouched
    assert '[profiles.work]' in updated
    assert 'model_catalog_json = "/profile/models.json"' in updated
    assert 'model = "profile-model"' in updated
    assert '# keep this' in updated
    assert 'sandbox_mode = "read_only"' in updated
    # one backup created
    backups = list((backup_dir).glob("*"))
    assert len(backups) == 1


def test_adds_key_when_absent_and_second_call_no_backup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('# hello\nmodel = "gpt-5"\n', encoding="utf-8")
    catalog = tmp_path / "models.json"
    backup_dir = tmp_path / "Backups"
    set_model_catalog(str(catalog), str(config), str(backup_dir))
    first = config.read_text(encoding="utf-8")
    assert 'model_catalog_json' in first
    assert '# hello' in first
    n1 = len(list(backup_dir.glob("*")))
    set_model_catalog(str(catalog), str(config), str(backup_dir))
    n2 = len(list(backup_dir.glob("*")))
    assert n1 == n2  # no new backup when unchanged


def test_corrupt_config_is_not_clobbered(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("this is not = [ valid toml", encoding="utf-8")
    catalog = tmp_path / "models.json"
    backup_dir = tmp_path / "Backups"
    with pytest.raises(Exception, match="无法解析"):
        set_model_catalog(str(catalog), str(config), str(backup_dir))
    # original preserved byte-for-byte
    assert config.read_text(encoding="utf-8") == "this is not = [ valid toml"