from __future__ import annotations

from typing import Any

from hh_applicant_tool.services import CoverLetterRequest, SearchFilters
from hh_applicant_tool.services.policy import VacancyPolicy


def parse_filters(data: dict[str, Any] | None) -> SearchFilters:
    if not data:
        return SearchFilters()
    allowed = SearchFilters.__dataclass_fields__
    return SearchFilters(**{k: v for k, v in data.items() if k in allowed})


def parse_policy(data: dict[str, Any] | None) -> VacancyPolicy:
    return VacancyPolicy.from_mapping(data)


def parse_cover_letter(data: dict[str, Any] | None) -> CoverLetterRequest:
    if not data:
        return CoverLetterRequest()
    allowed = CoverLetterRequest.__dataclass_fields__
    return CoverLetterRequest(
        **{k: v for k, v in data.items() if k in allowed}
    )
