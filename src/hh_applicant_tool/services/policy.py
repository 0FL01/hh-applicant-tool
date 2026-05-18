from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VacancyPolicy:
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    dealbreakers: list[str] = field(default_factory=list)
    excluded_employers: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    min_score: float = 0.7
    cover_letter_style: str = "short"
    cover_letter_language: str = "ru"
    force_message: bool = False
    notes: str = ""
    skip_blacklisted_employers: bool = True

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any] | None,
    ) -> VacancyPolicy:
        if not data:
            return cls()
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in allowed})
