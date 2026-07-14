"""Tests for secure guest access branding URLs."""

import pytest

from custom_components.guesty.access_branding import (
    branding_csp_source,
    normalize_branding_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://assets.example.com/logo.png",
        "https://cdn.example.com:8443/brand/logo.svg?v=2&dark=0",
    ],
)
def test_valid_https_branding_url_is_preserved(url: str) -> None:
    """Direct HTTPS image URLs are accepted without remote requests."""
    assert normalize_branding_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://assets.example.com/logo.png",
        "https://user:secret@assets.example.com/logo.png",
        "https://assets.example.com/logo image.png",
        "https://assets.example.com;script-src.example/logo.png",
        "//assets.example.com/logo.png",
    ],
)
def test_unsafe_branding_url_is_rejected(url: str) -> None:
    """Mixed content, credentials, whitespace, and CSP injection are rejected."""
    assert normalize_branding_url(url) is None


def test_csp_source_contains_only_validated_origin() -> None:
    """Queries and paths cannot expand the CSP image allowlist."""
    assert (
        branding_csp_source("https://CDN.example.com:8443/brand/logo.svg?v=2&dark=0")
        == "https://cdn.example.com:8443"
    )
