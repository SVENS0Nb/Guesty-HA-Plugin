"""Tests for the Guesty API client."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import ANY, AsyncMock

import aiohttp
from homeassistant.util import dt as dt_util
import pytest

from custom_components.guesty.api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyNotFoundError,
    GuestyPermissionError,
    GuestyRetryableError,
    is_safe_resource_id,
)
from custom_components.guesty.const import WEBHOOK_SUBSCRIPTION_EVENTS
from custom_components.guesty.models import build_reservation_filters


def _client(*, token: str | None = "token") -> GuestyApiClient:
    """Return a client whose network methods can be mocked."""
    expires = (dt_util.utcnow() + timedelta(hours=1)).timestamp() if token else None
    return GuestyApiClient(object(), "client", "secret", token, expires)


@pytest.mark.asyncio
async def test_network_error_is_retried(monkeypatch) -> None:
    """Transient aiohttp failures use the API retry loop."""
    client = _client()
    request_once = AsyncMock(
        side_effect=[aiohttp.ClientConnectionError("offline"), {"ok": True}]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    result = await client._async_request("GET", "/listings")

    assert result == {"ok": True}
    assert request_once.await_count == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_permanent_api_error_is_not_retried(monkeypatch) -> None:
    """Non-retryable API failures return immediately."""
    client = _client()
    request_once = AsyncMock(side_effect=GuestyApiError("bad request"))
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(GuestyApiError, match="bad request"):
        await client._async_request("GET", "/listings")

    assert request_once.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_header_controls_delay(monkeypatch) -> None:
    """A server-provided retry delay is used by the retry loop."""
    client = _client()
    request_once = AsyncMock(
        side_effect=[GuestyRetryableError("rate limited", 7.0), []]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    await client._async_request("GET", "/listings")

    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_credential_validation_reuses_token_and_fetches_one_listing(
    monkeypatch,
) -> None:
    """Validation does not paginate and exposes its token for first setup."""
    client = _client(token=None)
    ensure_token = AsyncMock()
    request = AsyncMock(return_value={"results": []})

    async def set_token() -> None:
        client._access_token = "validated-token"
        client._token_expires_at = (dt_util.utcnow() + timedelta(hours=1)).timestamp()

    ensure_token.side_effect = set_token
    monkeypatch.setattr(client, "_async_ensure_token", ensure_token)
    monkeypatch.setattr(client, "_async_request", request)

    account_id = await client.async_validate_credentials()

    assert len(account_id) == 64
    assert client.access_token == "validated-token"
    request.assert_awaited_once_with(
        "GET",
        "/listings",
        params={"fields": ANY, "limit": "1"},
    )


@pytest.mark.asyncio
async def test_concurrent_token_checks_create_only_one_token(monkeypatch) -> None:
    """Concurrent first API calls share one OAuth token request."""
    client = _client(token=None)
    calls = 0

    async def refresh_once() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        client._access_token = "shared-token"
        client._token_expires_at = (dt_util.utcnow() + timedelta(hours=1)).timestamp()

    monkeypatch.setattr(client, "_async_refresh_token_once", refresh_once)

    await asyncio.gather(client._async_ensure_token(), client._async_ensure_token())

    assert calls == 1


@pytest.mark.asyncio
async def test_late_unauthorized_response_reuses_newer_token(monkeypatch) -> None:
    """A late 401 response cannot spend another token after a peer refreshed it."""
    client = _client(token="new-token")
    refresh = AsyncMock()
    monkeypatch.setattr(client, "_async_refresh_token_locked", refresh)

    await client._async_ensure_token(
        force_refresh=True,
        invalid_token="old-token",
    )

    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_pagination_detects_repeated_pages(monkeypatch) -> None:
    """A broken API cursor cannot loop until memory is exhausted."""
    client = _client()
    repeated_page = [{"_id": str(index)} for index in range(100)]
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(side_effect=[repeated_page, repeated_page]),
    )

    with pytest.raises(GuestyApiError, match="did not advance"):
        await client._async_paginate("/reservations")


def test_resource_ids_are_restricted_to_safe_path_segments() -> None:
    """Webhook-controlled IDs cannot alter an API URL path."""
    assert is_safe_resource_id("65f19af19824d7e6ff848f11")
    assert not is_safe_resource_id("../webhooks")
    assert not is_safe_resource_id("id/other")
    assert not is_safe_resource_id(None)


def test_rate_limit_headers_use_lowest_available_window() -> None:
    """Diagnostics report the most constrained Guesty rate window."""
    client = _client()
    client._capture_rate_limit_headers(
        {
            "X-RateLimit-Remaining-Second": "8",
            "X-RateLimit-Remaining-Minute": "42",
            "X-RateLimit-Remaining-Hour": "100",
        }
    )
    assert client.last_rate_limit_remaining == 8


def test_paginated_results_ignore_non_objects() -> None:
    """Malformed items cannot crash listing or reservation parsing."""
    assert GuestyApiClient._normalize_results(
        {"results": [{"_id": "valid"}, "invalid", None]}
    ) == [{"_id": "valid"}]


def test_permission_and_invalid_json_errors_are_safe() -> None:
    """Public API errors do not need raw response bodies."""
    assert str(GuestyPermissionError("Permission denied (403)")) == (
        "Permission denied (403)"
    )
    with pytest.raises(GuestyApiError, match="Invalid JSON"):
        GuestyApiClient._parse_response_body("not json")


def test_targeted_reservation_filters_limit_new_listing_traffic() -> None:
    """New listings can retrieve reservations without scanning the whole account."""
    filters = build_reservation_filters(
        30,
        365,
        listing_ids={"listing-2", "listing-1"},
    )

    assert {
        "operator": "$in",
        "field": "listingId",
        "value": ["listing-1", "listing-2"],
    } in filters


@pytest.mark.asyncio
async def test_webhook_registration_uses_only_documented_events(monkeypatch) -> None:
    """Compatibility-only event names cannot make registration fail."""
    client = _client()
    request = AsyncMock(return_value={"_id": "webhook-1"})
    monkeypatch.setattr(client, "_async_request", request)

    assert await client.async_register_webhook("https://ha.example.test/hook") == (
        "webhook-1"
    )
    request.assert_awaited_once_with(
        "POST",
        "/webhooks",
        json_body={
            "url": "https://ha.example.test/hook",
            "events": list(WEBHOOK_SUBSCRIPTION_EVENTS),
        },
    )


@pytest.mark.asyncio
async def test_stale_remote_webhook_is_detected(monkeypatch) -> None:
    """A deleted remote subscription is not treated as active forever."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=[{"_id": "other-webhook"}]),
    )

    assert not await client.async_webhook_matches(
        "webhook-1", "https://ha.example.test/hook"
    )


@pytest.mark.asyncio
async def test_deleted_reservation_returns_none(monkeypatch) -> None:
    """A 404 can remove a deleted reservation from the local cache."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(side_effect=GuestyNotFoundError("not found")),
    )

    assert await client.async_get_reservation("reservation-1") is None


@pytest.mark.asyncio
async def test_custom_field_name_is_resolved_once_from_account(monkeypatch) -> None:
    """Users can configure the Guesty display name instead of an opaque id."""
    client = _client()
    request = AsyncMock(
        side_effect=[
            {"_id": "account-1"},
            [
                {
                    "_id": "65fab102a5284d73c6206db0",
                    "displayName": "Door access link",
                    "variable": "door_access_link",
                }
            ],
        ]
    )
    monkeypatch.setattr(client, "_async_request", request)

    assert await client.async_resolve_custom_field("{{door_access_link}}") == (
        "65fab102a5284d73c6206db0"
    )


@pytest.mark.asyncio
async def test_reservation_custom_field_uses_v3_endpoint(monkeypatch) -> None:
    """Door links never use Guesty's retired reservation field endpoint."""
    client = _client()
    request = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_async_request", request)

    await client.async_update_reservation_custom_field(
        "reservation-1",
        "65fab102a5284d73c6206db0",
        "https://ha.test/access",
    )

    request.assert_awaited_once_with(
        "PUT",
        "/reservations-v3/reservation-1/custom-fields",
        json_body={
            "customFields": [
                {
                    "fieldId": "65fab102a5284d73c6206db0",
                    "value": "https://ha.test/access",
                }
            ]
        },
    )
