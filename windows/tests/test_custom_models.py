"""Custom model editor rules (ports of CustomModelEditorTests)."""

from __future__ import annotations

import json

import pytest

from codex_model_manager.core.custom_models import (
    NewModelDraft,
    add_from_template,
    update_reasoning,
)
from codex_model_manager.core.errors import InvalidConfiguration
from codex_model_manager.core.reasoning import ReasoningSettings


def _src(models):
    return json.dumps({"schema": "future.v2", "models": models}).encode("utf-8")


def test_adding_clones_template_and_overrides_editable(mock_runtime):
    source = _src([{
        "slug": "template-model", "display_name": "Template", "description": "Template description",
        "context_window": 100000, "max_context_window": 100000, "input_modalities": ["text"],
        "supports_image_detail_original": False, "priority": 50, "wire_api": "responses",
        "supports_tools": True,
        "supported_reasoning_levels": [{"effort": "high", "description": "Provider description"}],
        "default_reasoning_level": "high",
    }])
    draft = NewModelDraft(
        slug=" Vendor-Vision ", display_name=" Vendor Vision ", description=" Hosted multimodal model ",
        template_slug="template-model", context_window=262144, supports_image=True,
        supports_original_image_detail=True,
        reasoning=ReasoningSettings(supported_efforts=["low", "high", "max"], default_effort="max"),
    )
    updated = add_from_template(draft, source, existing_slugs={"template-model"})
    root = json.loads(updated)
    models = root["models"]
    added = models[-1]
    assert len(models) == 2
    assert added["slug"] == "vendor-vision"
    assert added["display_name"] == "Vendor Vision"
    assert added["description"] == "Hosted multimodal model"
    assert added["context_window"] == 262144 and added["max_context_window"] == 262144
    assert added["input_modalities"] == ["text", "image"]
    assert added["supports_image_detail_original"] is True
    assert added["priority"] == 51
    assert added["wire_api"] == "responses"
    assert added["supports_tools"] is True
    assert [lv["effort"] for lv in added["supported_reasoning_levels"]] == ["low", "high", "max"]
    assert added["supported_reasoning_levels"][1]["description"] == "Provider description"
    assert added["default_reasoning_level"] == "max"
    assert models[0]["default_reasoning_level"] == "high"


def test_adding_rejects_duplicate_and_invalid(mock_runtime):
    source = _src([{"slug": "template-model", "priority": 1}])
    draft = NewModelDraft(
        slug="template-model", display_name="Duplicate", description="d",
        template_slug="template-model", context_window=1000,
    )
    with pytest.raises(InvalidConfiguration, match="已存在"):
        add_from_template(draft, source, existing_slugs={"template-model"})
    draft.slug = "Invalid Slug"
    with pytest.raises(InvalidConfiguration):
        add_from_template(draft, source, existing_slugs=set())


def test_adding_clones_official_template_into_empty_source(mock_runtime):
    source = _src([])
    catalog = json.dumps({"models": [{
        "slug": "gpt-official", "visibility": "hide", "display_name": "GPT Official",
        "description": "Official template", "context_window": 200000, "max_context_window": 800000,
        "input_modalities": ["text", "image"], "supports_image_detail_original": True,
        "priority": 1, "wire_api": "responses",
        "supported_reasoning_levels": [{"effort": "high", "description": "High"}],
        "default_reasoning_level": "high",
    }]}).encode("utf-8")
    draft = NewModelDraft(
        slug="hosted-model", display_name="Hosted Model", description="Hosted model",
        template_slug="gpt-official", context_window=128000,
    )
    updated = add_from_template(draft, source, catalog_data=catalog, existing_slugs={"gpt-official"})
    model = json.loads(updated)["models"][0]
    assert model["slug"] == "hosted-model"
    assert model["wire_api"] == "responses"
    assert model["priority"] == 1000
    assert model["visibility"] == "list"
    assert model["default_reasoning_level"] == "high"


def test_edit_reasoning_preserves_other_and_unknown(mock_runtime):
    target = {
        "slug": "vendor-model", "input_modalities": ["text", "image"], "context_window": 1048576,
        "provider_metadata": {"tools": ["custom-function"]},
        "supported_reasoning_levels": [
            {"effort": "high", "description": "Provider high", "budget": 200},
            {"effort": "future", "description": "Future provider effort", "budget": 400},
        ],
        "default_reasoning_level": "high",
    }
    other = {"slug": "other", "default_reasoning_level": "medium"}
    source = _src([target, other])
    settings = ReasoningSettings(supported_efforts=["low", "high", "future"], default_effort="future")
    updated = update_reasoning(settings, "vendor-model", source)
    root = json.loads(updated)
    models = root["models"]
    assert root["schema"] == "future.v2"
    assert models[1] == other
    remaining = {k: v for k, v in models[0].items() if k not in ("supported_reasoning_levels", "default_reasoning_level")}
    expected = {k: v for k, v in target.items() if k not in ("supported_reasoning_levels", "default_reasoning_level")}
    assert remaining == expected
    assert [lv["effort"] for lv in models[0]["supported_reasoning_levels"]] == settings.supported_efforts
    assert models[0]["supported_reasoning_levels"][2] == target["supported_reasoning_levels"][1]  # unknown level kept
    assert models[0]["default_reasoning_level"] == "future"


def test_edit_rejects_invalid_and_outside_source(mock_runtime):
    source = _src([{"slug": "vendor-model"}])
    cases = [
        ReasoningSettings(),
        ReasoningSettings(supported_efforts=["low", "high"], default_effort="max"),
        ReasoningSettings(supported_efforts=["high", "high"], default_effort="high"),
    ]
    for settings in cases:
        with pytest.raises(InvalidConfiguration):
            update_reasoning(settings, "vendor-model", source)
    with pytest.raises(InvalidConfiguration):
        update_reasoning(ReasoningSettings(supported_efforts=["high"], default_effort="high"),
                         "gpt-official", source)


def test_removing_default_selects_remaining(mock_runtime):
    s = ReasoningSettings(supported_efforts=["low", "high", "max"], default_effort="high")
    s.set_enabled(False, "high")
    assert s.default_effort == "max"
    s.set_enabled(False, "max")
    assert s.default_effort == "low"
    s.set_enabled(False, "low")
    assert not s.is_valid
    s.set_enabled(True, "high")
    assert s.default_effort == "high" and s.is_valid