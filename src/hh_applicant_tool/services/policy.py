from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from hh_applicant_tool.utils import json


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

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "must_have": list(self.must_have),
            "nice_to_have": list(self.nice_to_have),
            "avoid": list(self.avoid),
            "dealbreakers": list(self.dealbreakers),
            "excluded_employers": list(self.excluded_employers),
            "excluded_keywords": list(self.excluded_keywords),
            "min_score": self.min_score,
            "cover_letter_style": self.cover_letter_style,
            "cover_letter_language": self.cover_letter_language,
            "force_message": self.force_message,
            "notes": self.notes,
            "skip_blacklisted_employers": self.skip_blacklisted_employers,
        }

    def hash(self) -> str:
        payload = json.dumps(self.to_canonical_dict(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
