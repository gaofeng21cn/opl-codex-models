"""Field-level overrides for official models.

Faithful port of Sources/CodexModelCore/ModelFieldOverrides.swift. Only the
current/max context window may be overridden; every other official field still
follows the freshly loaded catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from .errors import InvalidConfiguration

OVERRIDE_FIELDS = ("context_window", "max_context_window")


@dataclass
class ModelFieldOverrides:
    context_window: Optional[int] = None
    max_context_window: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return self.context_window is None and self.max_context_window is None

    @property
    def fields(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        if self.context_window is not None:
            result["context_window"] = self.context_window
        if self.max_context_window is not None:
            result["max_context_window"] = self.max_context_window
        return result

    def validate(self) -> None:
        for _, value in self.fields.items():
            if not isinstance(value, int) or value <= 0:
                raise InvalidConfiguration("本机覆盖的上下文必须是正整数。")
        if (
            self.context_window is not None
            and self.max_context_window is not None
            and self.context_window > self.max_context_window
        ):
            raise InvalidConfiguration("当前上下文不能大于最大上下文。")

    def applying(self, model: Dict) -> Dict:
        self.validate()
        result = dict(model)
        for field_name, value in self.fields.items():
            result[field_name] = value
        if not self.is_empty:
            current = result.get("context_window")
            maximum = result.get("max_context_window")
            if (
                isinstance(current, int)
                and isinstance(maximum, int)
                and current > maximum
            ):
                raise InvalidConfiguration(
                    "本机覆盖与官方目录合并后，当前上下文大于最大上下文。请调整该模型的覆盖值。"
                )
        return result

    def to_json(self) -> Dict[str, int]:
        return self.fields

    @classmethod
    def from_json(cls, data: Dict) -> "ModelFieldOverrides":
        return cls(
            context_window=data.get("context_window"),
            max_context_window=data.get("max_context_window"),
        )


def validate_override_map(overrides: Dict[str, ModelFieldOverrides]) -> None:
    for _slug, override in overrides.items():
        override.validate()