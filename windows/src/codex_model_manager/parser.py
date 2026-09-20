"""Model representation + parsing of catalogs and sync records.

Faithful to CatalogModel / CatalogParser in the original app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .core.reasoning import ReasoningSettings


@dataclass
class CatalogModel:
    slug: str = ""
    display_name: str = ""
    description: str = ""
    input_modalities: List[str] = field(default_factory=lambda: ["text"])
    supports_original_image_detail: bool = False
    context_window: Optional[int] = None
    max_context_window: Optional[int] = None
    priority: Optional[int] = None
    visibility: Optional[str] = None
    supported_in_api: bool = False
    reasoning: ReasoningSettings = field(default_factory=ReasoningSettings)
    source: str = "official"  # 'official' | 'custom'

    def matches(self, needle: str) -> bool:
        n = needle.lower().strip()
        if not n:
            return True
        return n in self.slug.lower() or n in self.display_name.lower() or n in self.description.lower()


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def parse_models(custom_data: bytes, catalog_data: bytes) -> List[CatalogModel]:
    import json

    def root(data): return json.loads(data.decode("utf-8"))

    custom_root = root(custom_data)
    catalog_root = root(catalog_data)
    custom_objects = custom_root.get("models") or []
    catalog_objects = catalog_root.get("models") or []
    custom_slugs = {m.get("slug") for m in custom_objects if isinstance(m, dict)}

    return [
        model_from_raw(raw, "custom" if raw.get("slug") in custom_slugs else "official")
        for raw in catalog_objects
        if isinstance(raw, dict) and raw.get("slug")
    ]


def model_from_raw(raw: dict, source: str) -> CatalogModel:
    """Build one CatalogModel from a raw catalog object, tagging its `source`.

    The mapping is shared by every reader so the GUI, the CLI and the takeover
    view can never disagree about what a catalog entry means.
    """
    slug = raw.get("slug")
    return CatalogModel(
        slug=str(slug),
        display_name=str(raw.get("display_name") or slug),
        description=str(raw.get("description") or ""),
        input_modalities=raw.get("input_modalities") if isinstance(raw.get("input_modalities"), list) else ["text"],
        supports_original_image_detail=bool(raw.get("supports_image_detail_original") or False),
        context_window=_as_int(raw.get("context_window")),
        max_context_window=_as_int(raw.get("max_context_window")),
        priority=_as_int(raw.get("priority")),
        visibility=raw.get("visibility"),
        supported_in_api=bool(raw.get("supported_in_api") or False),
        reasoning=ReasoningSettings.from_model(raw),
        source=source,
    )


def parse_catalog_models(data: bytes, source: str = "pending") -> List[CatalogModel]:
    """Parse a single catalog document, tagging every model with `source`.

    Used by the takeover view: the pending catalog's entries are all editable
    there, whether a given entry started life as an official or a custom model.
    """
    import json

    root = json.loads(data.decode("utf-8"))
    objects = root.get("models") or []
    return [
        model_from_raw(raw, source)
        for raw in objects
        if isinstance(raw, dict) and raw.get("slug")
    ]


def parse_records(text: str, start: int = 0) -> List[dict]:
    import json

    records = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("status"):
            rec["_index"] = start + i
            records.append(rec)
    # Newest first
    return [dict(r) for r in records[::-1]]


def read_json(path: str) -> dict:
    import json
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))
