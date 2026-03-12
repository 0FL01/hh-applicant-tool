from __future__ import annotations

import re
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TELEGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z0-9_]{3,}",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?[\d][\d\s().-]{8,}[\d]")


def _normalize_telegram_url(value: str) -> str:
    value = value.strip().rstrip(".,);]")
    if value.lower().startswith("http"):
        return value
    return f"https://{value}"


def _normalize_url(value: str) -> str:
    value = value.strip().rstrip(".,);]")
    parsed = urlparse(value)
    if parsed.scheme:
        return value
    return f"https://{value}"


def _normalize_phone(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip())
    digits = re.sub(r"\D", "", compact)
    if len(digits) < 10:
        return ""
    return compact.rstrip(".,;)")


def _extract_signature_name(lines: list[str]) -> str | None:
    closing_markers = {
        "с уважением",
        "с уважением,",
        "best regards",
        "regards",
    }
    for raw_line in reversed(lines[-6:]):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered in closing_markers:
            continue
        if EMAIL_RE.search(line) or TELEGRAM_URL_RE.search(line):
            continue
        if URL_RE.search(line) or _normalize_phone(line):
            continue
        return line
    return None


def extract_contact_details(text: str) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    emails = sorted({match.group(0) for match in EMAIL_RE.finditer(text)})
    telegram_urls = sorted(
        {
            _normalize_telegram_url(match.group(0))
            for match in TELEGRAM_URL_RE.finditer(text)
        }
    )
    all_urls = {
        _normalize_url(match.group(0)) for match in URL_RE.finditer(text)
    }
    urls = sorted(all_urls - set(telegram_urls))
    phones = sorted(
        {
            normalized
            for normalized in (
                _normalize_phone(match.group(0))
                for match in PHONE_RE.finditer(text)
            )
            if normalized
        }
    )
    contact_lines = [
        line
        for line in lines
        if EMAIL_RE.search(line)
        or TELEGRAM_URL_RE.search(line)
        or URL_RE.search(line)
        or _normalize_phone(line)
        or ":" in line
    ]
    signature_name = _extract_signature_name(lines)
    telegram_handles = sorted(
        {
            url.rstrip("/").rsplit("/", 1)[-1]
            for url in telegram_urls
            if "/" in url
        }
    )
    return {
        "signature_name": signature_name,
        "emails": emails,
        "telegram_urls": telegram_urls,
        "telegram_handles": telegram_handles,
        "phones": phones,
        "urls": urls,
        "contact_lines": contact_lines,
    }
