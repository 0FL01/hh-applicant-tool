from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

CoverLetterMode = Literal["auto", "none", "text", "template", "llm"]


@dataclass(frozen=True)
class CoverLetterRequest:
    mode: CoverLetterMode = "auto"
    text: str | None = None
    template_text: str | None = None


@dataclass(frozen=True)
class CoverLetterResult:
    source: Literal["provided", "template", "llm", "none"]
    text: str
    preview: str | None
    sha256: str | None


def preview_text(value: str, limit: int = 240) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def hash_text(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def render_template(template: str, values: dict[str, Any]) -> str:
    return template.format_map(_SafeFormatDict(values))


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"
