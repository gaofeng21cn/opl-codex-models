from __future__ import annotations

import json
from pathlib import Path

from scripts.update_reasoning_levels import update


def test_update_reasoning_levels_changes_only_target_model(tmp_path: Path):
    target = tmp_path / "models.json"
    original = {
        "schema": "keep",
        "models": [
            {"slug": "gpt-6-astra", "default_reasoning_level": "low"},
            {"slug": "deepseek-v4.1-flash", "default_reasoning_level": "low",
             "supported_reasoning_levels": [{"effort": "low"}],
             "vendor_field": {"keep": True}},
        ],
    }
    target.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    backup = update(target)

    assert backup.is_file()
    result = json.loads(target.read_text(encoding="utf-8"))
    model = next(m for m in result["models"] if m["slug"] == "deepseek-v4.1-flash")
    assert [x["effort"] for x in model["supported_reasoning_levels"]] == ["low", "high", "max"]
    assert model["default_reasoning_level"] == "high"
    assert model["vendor_field"] == {"keep": True}
    assert result["models"][0] == original["models"][0]


def test_update_reasoning_levels_rejects_missing_or_duplicate_target(tmp_path: Path):
    target = tmp_path / "models.json"
    target.write_text(json.dumps({"models": [{"slug": "other"}]}), encoding="utf-8")
    try:
        update(target)
    except ValueError as exc:
        assert "恰好有一个" in str(exc)
    else:
        raise AssertionError("missing target model must be rejected")
