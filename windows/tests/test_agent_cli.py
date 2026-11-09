"""The Agent edits the same source as the GUI without applying Codex settings."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_model_manager.core.app_config import AppConfiguration


@pytest.fixture
def catalog(tmp_path):
    custom = tmp_path / "custom.json"
    merged = tmp_path / "merged.json"
    official = {"slug": "official", "display_name": "Official", "description": "Template",
                "priority": 1, "context_window": 128000, "vendor_field": {"keep": True}}
    model = {**official, "slug": "custom", "display_name": "Custom"}
    custom.write_text(json.dumps({"schema": "keep", "models": [model]}))
    merged.write_text(json.dumps({"models": [official, model]}))
    config = AppConfiguration(
        codex_runtime_path="", custom_source_path=str(custom), merged_catalog_path=str(merged),
        sync_log_path=str(tmp_path / "sync.jsonl"), error_log_path=str(tmp_path / "error.log"),
        backup_directory_path=str(tmp_path / "backups"),
    )
    config_path = tmp_path / "config.json"
    config.save(str(config_path))
    return config_path, custom, merged


def run(catalog, *arguments):
    source_root = str(Path(__file__).parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "codex_model_manager", "--config", str(catalog[0]), *arguments],
        env={**os.environ, "PYTHONPATH": source_root}, capture_output=True, text=True,
        encoding="utf-8",  # The CLI stream contract is UTF-8, including on ANSI Windows hosts.
    )
    assert not completed.stderr, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def test_read_preview_edit_and_readback_without_runtime(catalog):
    _, custom, merged = catalog
    merged_before = merged.read_bytes()
    code, before = run(catalog, "list", "--json")
    assert code == 0 and before["schema"] == "codex_models.cli.v1"
    assert before["source_digest"] == hashlib.sha256(custom.read_bytes()).hexdigest()
    assert before["catalog_digest"] == hashlib.sha256(merged_before).hexdigest()
    assert {m["source"] for m in before["models"]} == {"official", "custom"}
    files_before = set(custom.parent.iterdir())
    command = ["add", "new", "--template", "official", "--description", "New model",
               "--context-window", "256000", "--json"]
    code, preview = run(catalog, *command, "--dry-run")
    assert code == 0 and preview["status"] == "preview"
    assert preview["source_digest"] == before["source_digest"]
    assert set(custom.parent.iterdir()) == files_before
    code, saved = run(catalog, *command, "--expected-source-digest", before["source_digest"])
    assert code == 0 and saved["status"] == "source_saved" and saved["sync_required"]
    assert saved["source_digest"] == preview["proposed_source_digest"]
    assert Path(saved["backup_path"]).read_bytes() != custom.read_bytes()
    assert saved["custom_models"][-1]["vendor_field"] == {"keep": True}
    code, after = run(catalog, "list", "--json")
    assert code == 0 and after["source_digest"] == saved["source_digest"]
    assert after["custom_models"][-1]["slug"] == "new"
    assert merged.read_bytes() == merged_before
    code, reasoning = run(catalog, "reasoning", "new", "--efforts", "low,high",
                          "--default", "high", "--json", "--expected-source-digest", after["source_digest"])
    assert code == 0
    assert reasoning["custom_models"][-1]["default_reasoning_level"] == "high"
    code, removed = run(catalog, "remove", "new", "--json",
                        "--expected-source-digest", reasoning["source_digest"])
    assert code == 0 and [m["slug"] for m in removed["custom_models"]] == ["custom"]
    assert merged.read_bytes() == merged_before


def test_stale_and_missing_digest_do_not_write_or_backup(catalog):
    _, custom, _ = catalog
    original = custom.read_bytes()
    for options, error in [([], "invalid_request"), (["--expected-source-digest", "old"], "source_conflict")]:
        code, result = run(catalog, "remove", "custom", "--json", *options)
        assert code == 1 and result["error"]["code"] == error
        assert custom.read_bytes() == original
        assert not (custom.parent / "backups").exists()


def test_same_revision_concurrent_edits_have_one_winner(catalog):
    _, custom, _ = catalog
    digest = hashlib.sha256(custom.read_bytes()).hexdigest()

    def edit(default):
        return run(catalog, "reasoning", "custom", "--efforts", "low,high", "--default", default,
                   "--json", "--expected-source-digest", digest)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["low", "high"]))
    assert sorted(code for code, _ in results) == [0, 1]
    failed = next(value for code, value in results if code)
    assert failed["error"]["code"] == "source_conflict"
    assert len(list((custom.parent / "backups").iterdir())) == 1


def test_json_argument_and_io_errors(catalog):
    code, invalid = run(catalog, "reasoning", "custom", "--json")
    assert code == 2 and invalid["error"]["code"] == "invalid_arguments"
    catalog[1].unlink()
    code, missing = run(catalog, "list", "--json")
    assert code == 2 and missing["error"]["code"] == "io_error"


def test_duplicate_pending_source_model_is_rejected_before_sync(catalog):
    _, custom, _ = catalog
    digest = hashlib.sha256(custom.read_bytes()).hexdigest()
    command = ["add", "pending", "--template", "official", "--description", "Pending model",
               "--context-window", "128000", "--json"]
    code, added = run(catalog, *command, "--expected-source-digest", digest)
    assert code == 0
    code, duplicate = run(catalog, *command, "--expected-source-digest", added["source_digest"])
    assert code == 1 and duplicate["status"] == "error"
    assert [m["slug"] for m in json.loads(custom.read_bytes())["models"]].count("pending") == 1
