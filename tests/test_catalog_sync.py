"""Sync merge logic (ports and extends the original Swift tests)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from codex_model_manager.core.catalog_sync import CatalogPaths, CatalogSyncService
from codex_model_manager.core.errors import InvalidCatalog, ProcessFailed
from codex_model_manager.core.overrides import ModelFieldOverrides
from codex_model_manager.core.process_runner import run_process


def _m(paths, overrides=None, visibility=None):
    return CatalogPaths(
        codex_runtime=paths["codex_runtime"],
        custom_source=paths["custom_source"],
        merged_catalog=paths["merged_catalog"],
        sync_log=paths["sync_log"],
        error_log=paths["error_log"],
        backup_directory=paths["backup_directory"],
        visibility_overrides=visibility or paths["visibility_overrides"],
        model_overrides=overrides if overrides is not None else paths["model_overrides"],
    )


def _read(path):
    return json.loads(open(path, encoding="utf-8").read())


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
            "supports_reasoning_summaries": True,
        }
    ]
}


def test_merge_matches_original_semantics(mock_runtime):
    paths, _ = mock_runtime(bundled=BUNDLED, custom_models=CUSTOM)
    paths["visibility_overrides"] = {"gpt-official": "list", "vendor-model": "hide"}
    service = CatalogSyncService(_m(paths))

    assert service.sync_and_append_log().status == "updated"
    assert service.sync_and_append_log().status == "no_change"
    models = _read(paths["merged_catalog"])["models"]
    assert [m["slug"] for m in models] == ["gpt-official", "vendor-model"]
    assert models[1]["priority"] == 5          # custom = ceiling + index + 1
    assert models[0]["supports_parallel_tool_calls"] is True
    assert models[1]["supports_parallel_tool_calls"] is True
    assert models[0]["visibility"] == "list"
    assert models[1]["visibility"] == "hide"
    assert models[0]["max_context_window"] == 800000
    assert _read(paths["custom_source"]) == CUSTOM  # custom source untouched
    assert len(open(paths["sync_log"], encoding="utf-8").read().splitlines()) == 2


def test_overlap_is_rejected(mock_runtime):
    bundled = {"models": [{"slug": "gpt-official", "priority": 1}]}
    custom = {"models": [{"slug": "gpt-official", "priority": 2}]}
    paths, _ = mock_runtime(bundled=bundled, custom_models=custom)
    with pytest.raises(InvalidCatalog, match="重名"):
        CatalogSyncService(_m(paths)).sync()


def test_unknown_fields_preserved(mock_runtime):
    bundled = {
        "models": [{
            "slug": "gpt-official", "priority": 1,
            "future_metadata": {"version": 2, "nested": [1, 2, 3]},
            "provider_extra": {"x": 1},
        }]
    }
    custom = {
        "models": [{"slug": "vendor", "priority": 5, "wire_api": "responses", "supports_tools": True}]
    }
    paths, _ = mock_runtime(bundled=bundled, custom_models=custom)
    CatalogSyncService(_m(paths)).sync()
    models = _read(paths["merged_catalog"])["models"]
    assert models[0]["future_metadata"] == {"version": 2, "nested": [1, 2, 3]}
    assert models[1]["wire_api"] == "responses"
    assert models[1]["supports_tools"] is True


def test_field_overrides_follow_upstream_and_can_be_removed(mock_runtime):
    official = {
        "slug": "gpt-official", "priority": 1, "visibility": "hide",
        "context_window": 200000, "max_context_window": 800000,
        "input_modalities": ["text"], "future_metadata": {"version": 1},
        "supports_reasoning_summaries": False, "supports_parallel_tool_calls": False,
    }
    custom = {"models": [{"slug": "vendor-model", "context_window": 128000, "max_context_window": 128000}]}
    write_upstream = lambda p, m: Path(p).write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    paths, bundled_file = mock_runtime(
        bundled={"models": [official]}, custom_models=custom,
        overrides={
            "gpt-official": ModelFieldOverrides(context_window=393216),
            "vendor-model": ModelFieldOverrides(context_window=999),
            "future-model": ModelFieldOverrides(context_window=123),
        },
    )
    service = CatalogSyncService(_m(paths))
    result = service.sync()
    m0 = _read(paths["merged_catalog"])["models"][0]
    assert m0["context_window"] == 393216
    assert m0["visibility"] == "hide"  # no visibility override configured here
    assert _read(paths["merged_catalog"])["models"][1]["context_window"] == 128000
    record = json.loads(result.record_data)
    assert record["inactive_model_overrides"] == ["future-model", "vendor-model"]
    assert service.sync().status == "no_change"

    # upstream changes; non-overridden fields follow, override keeps context
    write_upstream(bundled_file, {
        "models": [{
            **official, "context_window": 300000, "max_context_window": 1000000,
            "input_modalities": ["text", "image"], "future_metadata": {"version": 2},
            "default_reasoning_level": "max",
        }]
    })
    assert service.sync().status == "updated"
    m0 = _read(paths["merged_catalog"])["models"][0]
    assert m0["context_window"] == 393216
    assert m0["max_context_window"] == 1000000
    assert m0["future_metadata"] == {"version": 2}

    # max-only override
    paths2, bundled2 = mock_runtime(
        bundled={"models": [official]}, custom_models=custom,
        overrides={"gpt-official": ModelFieldOverrides(max_context_window=500000)},
    )
    CatalogSyncService(_m(paths2)).sync()
    m = _read(paths2["merged_catalog"])["models"][0]
    assert m["context_window"] == 200000
    assert m["max_context_window"] == 500000

    # both overridden, then invalid value leaves merged untouched
    paths3, bundled3 = mock_runtime(
        bundled={"models": [official]}, custom_models=custom,
        overrides={"gpt-official": ModelFieldOverrides(context_window=393216, max_context_window=393216)},
    )
    CatalogSyncService(_m(paths3)).sync()
    assert _read(paths3["merged_catalog"])["models"][0]["context_window"] == 393216
    before = open(paths3["merged_catalog"], encoding="utf-8").read()
    for ov in (ModelFieldOverrides(context_window=0), ModelFieldOverrides(max_context_window=1)):
        with pytest.raises(Exception):
            CatalogSyncService(_m(paths3, overrides={"gpt-official": ov})).sync()
        assert open(paths3["merged_catalog"], encoding="utf-8").read() == before

    # cleared overrides -> full upstream
    paths4, _ = mock_runtime(bundled={"models": [official]}, custom_models=custom)
    CatalogSyncService(_m(paths4)).sync()
    m = _read(paths4["merged_catalog"])["models"][0]
    assert m["context_window"] == 200000
    assert m["max_context_window"] == 800000


def test_bundled_failure_keeps_existing_merged(mock_runtime):
    official = {"slug": "gpt-official", "priority": 1, "context_window": 100, "max_context_window": 200}
    paths, bundled_file = mock_runtime(bundled={"models": [official]}, custom_models={"models": []})
    service = CatalogSyncService(_m(paths))
    assert service.sync().status == "updated"
    before = open(paths["merged_catalog"], encoding="utf-8").read()

    # Point MOCK_BUNDLED_JSON at a missing file => mock writes nothing => parse error
    fd, missing = tempfile.mkstemp(suffix=".missing.json")
    os.close(fd)
    os.unlink(missing)
    saved = os.environ.get("MOCK_BUNDLED_JSON")
    os.environ["MOCK_BUNDLED_JSON"] = missing
    try:
        with pytest.raises(Exception):
            service.sync()
    finally:
        if saved is None:
            os.environ.pop("MOCK_BUNDLED_JSON", None)
        else:
            os.environ["MOCK_BUNDLED_JSON"] = saved
    assert open(paths["merged_catalog"], encoding="utf-8").read() == before


def test_process_failure_and_timeout(mock_runtime):
    paths, _ = mock_runtime(bundled=BUNDLED, custom_models=CUSTOM, exit_code="1")
    with pytest.raises(ProcessFailed):
        CatalogSyncService(_m(paths)).sync()

    paths2, _ = mock_runtime(bundled=BUNDLED, custom_models=CUSTOM, sleep="3")
    with pytest.raises(ProcessFailed, match="未完成|超时"):
        run_process(paths2["codex_runtime"], ["debug", "models", "--bundled"], timeout=1.0)


def test_chinese_and_space_paths(mock_runtime, tmp_path):
    """A real sync must succeed when every path holds Chinese chars and spaces."""
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix="我的 模型 目录 "))
    paths, bundled = mock_runtime(bundled=BUNDLED, custom_models=CUSTOM, base=base)
    service = CatalogSyncService(_m(paths))
    result = service.sync()
    assert result.status in ("updated", "no_change")
    merged = _read(paths["merged_catalog"])
    slugs = {m["slug"] for m in merged["models"]}
    assert {"gpt-official", "vendor-model"} <= slugs
    assert Path(paths["merged_catalog"]).exists()