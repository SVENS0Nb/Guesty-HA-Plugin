"""Validation helpers for optional guest access page branding."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
MAX_BRANDING_URL_LENGTH = 2048


def normalize_branding_url(value: Any) -> str | None:
    """Return a safe absolute HTTPS image URL or None for empty/invalid input."""
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None
    if len(url) > MAX_BRANDING_URL_LENGTH or any(
        character.isspace() or ord(character) < 0x20 for character in url
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or not _HOST_PATTERN.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    expected_netloc = hostname if port is None else f"{hostname}:{port}"
    if parsed.netloc.casefold() != expected_netloc.casefold():
        return None
    return url


def branding_csp_source(url: str) -> str:
    """Return the validated URL's origin for a CSP img-src directive."""
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("Branding URL has no hostname")
    port = parsed.port
    return f"https://{hostname.lower()}{f':{port}' if port is not None else ''}"
