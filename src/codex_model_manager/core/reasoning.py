"""Reasoning settings.

Faithful port of Sources/CodexModelManager/Models/ReasoningSettings.swift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

KNOWN_EFFORTS = [
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]


@dataclass
class ReasoningSettings:
    supported_efforts: List[str] = field(default_factory=list)
    default_effort: str = ""

    @classmethod
    def from_model(cls, model):
        levels = model.get("supported_reasoning_levels") or []
        efforts = [
            level.get("effort")
            for level in levels
            if isinstance(level, dict) and level.get("effort")
        ]
        return cls(
            supported_efforts=efforts,
            default_effort=model.get("default_reasoning_level") or "",
        )

    @property
    def available_efforts(self) -> List[str]:
        extra = [e for e in self.supported_efforts if e not in KNOWN_EFFORTS]
        return KNOWN_EFFORTS + extra

    @property
    def is_valid(self) -> bool:
        return (
            bool(self.supported_efforts)
            and all(e.strip() for e in self.supported_efforts)
            and len(set(self.supported_efforts)) == len(self.supported_efforts)
            and self.default_effort in self.supported_efforts
        )

    def set_enabled(self, enabled: bool, effort: str) -> None:
        if enabled and effort not in self.supported_efforts:
            self.supported_efforts.append(effort)
            order = self.available_efforts
            self.supported_efforts.sort(key=lambda e: order.index(e))
        elif not enabled:
            self.supported_efforts = [e for e in self.supported_efforts if e != effort]
        if self.default_effort not in self.supported_efforts:
            self.default_effort = self.supported_efforts[-1] if self.supported_efforts else ""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ReasoningSettings):
            return NotImplemented
        return (
            self.supported_efforts == other.supported_efforts
            and self.default_effort == other.default_effort
        )