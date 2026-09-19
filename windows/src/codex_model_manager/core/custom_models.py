"""Custom model source editor.

Faithful port of Sources/CodexModelManager/Services/CustomModelEditor.swift.
The custom-models source is JSON (schema "codex_model_manager_custom_models.v1").
Unknown fields are preserved; only the editable fields are touched.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from .errors import InvalidCatalog, InvalidConfiguration
from .reasoning import ReasoningSettings

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_JSON_OPTIONS = {
    "indent": 2,
    "sort_keys": True,
    "ensure_ascii": False,
}


def _dumps(value) -> bytes:
    return json.dumps(value, **_JSON_OPTIONS).encode("utf-8") + b"\n"


def _loads(data: bytes) -> Dict:
    try:
        root = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCatalog(f"模型源 JSON 无法解析：{exc}") from exc
    if not isinstance(root, dict):
        raise InvalidCatalog("模型源顶层必须是 JSON 对象")
    return root


def is_valid_slug(slug: str) -> bool:
    return SLUG_RE.match(slug) is not None


def normalize_slug(slug: str) -> str:
    return slug.strip().lower()


def _apply_reasoning(settings: ReasoningSettings, model: Dict) -> None:
    if not settings.is_valid:
        raise InvalidConfiguration(
            "请至少选择一个推理档位，并将默认档位设为其中之一。"
        )
    existing = model.get("supported_reasoning_levels") or []
    existing = existing if isinstance(existing, list) else []
    new_levels: List[Dict] = []
    for effort in settings.supported_efforts:
        prior = next(
            (lv for lv in existing if isinstance(lv, dict) and lv.get("effort") == effort),
            None,
        )
        if prior is not None:
            new_levels.append(dict(prior))
        else:
            new_levels.append({"effort": effort, "description": f"Reasoning effort: {effort}"})
    model["supported_reasoning_levels"] = new_levels
    model["default_reasoning_level"] = settings.default_effort


class NewModelDraft:
    def __init__(
        self,
        slug: str = "",
        display_name: str = "",
        description: str = "",
        template_slug: str = "",
        context_window: int = 0,
        supports_image: bool = False,
        supports_original_image_detail: bool = False,
        reasoning: Optional[ReasoningSettings] = None,
    ):
        self.slug = slug
        self.display_name = display_name
        self.description = description
        self.template_slug = template_slug
        self.context_window = context_window
        self.supports_image = supports_image
        self.supports_original_image_detail = supports_original_image_detail
        self.reasoning = reasoning

    @property
    def normalized_slug(self) -> str:
        return normalize_slug(self.slug)

    @property
    def normalized_display_name(self) -> str:
        return " ".join(self.display_name.split())

    @property
    def normalized_description(self) -> str:
        return " ".join(self.description.split())


def add_from_template(
    draft: NewModelDraft,
    source_data: bytes,
    catalog_data: Optional[bytes] = None,
    existing_slugs: Optional[set] = None,
) -> bytes:
    slug = draft.normalized_slug
    if not is_valid_slug(slug):
        raise InvalidConfiguration(f"模型标识无效：{draft.slug}")
    if existing_slugs is not None and slug in existing_slugs:
        raise InvalidConfiguration(f"模型标识已存在：{slug}")

    display_name = draft.normalized_display_name
    description = draft.normalized_description
    if not draft.template_slug or draft.context_window <= 0 or not display_name or not description:
        raise InvalidConfiguration("请选择模板并填写标识、名称、描述和上下文大小。")

    root = _loads(source_data)
    models = root.get("models")
    if not isinstance(models, list):
        raise InvalidConfiguration("custom-models 的 models 必须是数组")

    template_data = catalog_data if catalog_data is not None else source_data
    template_root = _loads(template_data)
    templates = template_root.get("models")
    if not isinstance(templates, list):
        raise InvalidCatalog("模板目录 models 必须是数组")
    new_model: Optional[Dict] = next(
        (m for m in templates if isinstance(m, dict) and m.get("slug") == draft.template_slug),
        None,
    )
    if new_model is None:
        raise InvalidConfiguration("找不到所选模板模型")

    new_model = dict(new_model)
    priorities = [
        m.get("priority") for m in models if isinstance(m, dict) and isinstance(m.get("priority"), int)
    ]
    next_priority = (max(priorities) + 1 if priorities else 1000)

    new_model["slug"] = slug
    new_model["visibility"] = "list"
    new_model["display_name"] = display_name
    new_model["description"] = description
    new_model["context_window"] = draft.context_window
    new_model["max_context_window"] = draft.context_window
    new_model["input_modalities"] = ["text", "image"] if draft.supports_image else ["text"]
    new_model["supports_image_detail_original"] = bool(
        draft.supports_image and draft.supports_original_image_detail
    )
    if "supports_reasoning_summaries" not in new_model:
        new_model["supports_reasoning_summaries"] = True
    new_model["priority"] = next_priority
    if draft.reasoning is not None:
        _apply_reasoning(draft.reasoning, new_model)

    models = list(models) + [new_model]
    root["models"] = models
    return _dumps(root)


def update_reasoning(settings: ReasoningSettings, slug: str, source_data: bytes) -> bytes:
    root = _loads(source_data)
    models = root.get("models")
    if not isinstance(models, list):
        raise InvalidCatalog("模型源 models 必须是数组")
    index = next(
        (i for i, m in enumerate(models) if isinstance(m, dict) and m.get("slug") == slug),
        None,
    )
    if index is None:
        raise InvalidConfiguration("只能编辑自定义模型源中已有模型的推理配置。")
    model = dict(models[index])
    _apply_reasoning(settings, model)
    models[index] = model
    root["models"] = models
    return _dumps(root)