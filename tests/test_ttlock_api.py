"""Tests for the TTLock Open Platform client."""

from __future__ import annotations

from datetime import datetime
import hashlib
from unittest.mock import AsyncMock

import pytest

from custom_components.guesty.ttlock_api import (
    TTLockApiClient,
    TTLockCodeConflictError,
    TTLockGatewayError,
    ttlock_api_base_url,
    ttlock_milliseconds,
)


def _client() -> TTLockApiClient:
    return TTLockApiClient(
        object(),
        region="eu",
        client_id="client",
        client_secret="secret",
        username="owner@example.com",
        access_token="access",
        refresh_token="refresh",
    )


def test_ttlock_regions_are_allow_listed() -> None:
    """The config cannot turn TTLock requests into arbitrary SSRF targets."""
    assert ttlock_api_base_url("eu") == "https://euapi.ttlock.com"
    with pytest.raises(ValueError):
        ttlock_api_base_url("https://attacker.invalid")


def test_ttlock_uses_unix_milliseconds() -> None:
    """Validity timestamps preserve exact booking minutes in UTC."""
    assert (
        ttlock_milliseconds(datetime.fromisoformat("2026-07-20T12:34:00+00:00"))
        == 1784550840000
    )


def test_invalid_token_expiration_fails_safe_to_refresh() -> None:
    """A damaged private timestamp cannot crash authenticated requests."""
    client = _client()
    client.token_expires_at = "not-a-timestamp"

    assert client._token_needs_refresh() is True


@pytest.mark.asyncio
async def test_authentication_hashes_password_and_retains_only_tokens(
    monkeypatch,
) -> None:
    """The TTLock App password is never retained after OAuth exchange."""
    client = _client()
    request = AsyncMock(
        return_value={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7776000,
        }
    )
    monkeypatch.setattr(client, "_async_token_request", request)

    await client.async_authenticate("owner@example.com", "SensitivePassword")

    form = request.await_args.args[0]
    assert form["client_id"] == "client"
    assert form["client_secret"] == "secret"
    assert form["username"] == "owner@example.com"
    assert (
        form["password"]
        == hashlib.md5(  # noqa: S324
            b"SensitivePassword", usedforsecurity=False
        ).hexdigest()
    )
    assert client.access_token == "new-access"
    assert client.refresh_token == "new-refresh"
    assert not hasattr(client, "password")


@pytest.mark.asyncio
async def test_authenticated_api_uses_official_camel_case_parameter_names(
    monkeypatch,
) -> None:
    """OAuth snake-case and Cloud API camel-case parameter names stay distinct."""
    client = _client()
    request = AsyncMock(return_value={"list": []})
    monkeypatch.setattr(client, "_async_http_post", request)

    await client.async_list_locks()

    path, form = request.await_args.args
    assert path == "/v3/lock/list"
    assert form["clientId"] == "client"
    assert form["accessToken"] == "access"
    assert isinstance(form["date"], int)
    assert "client_id" not in form


@pytest.mark.asyncio
async def test_add_change_and_delete_use_gateway_operations(monkeypatch) -> None:
    """All passcode mutations explicitly request gateway delivery type 2."""
    client = _client()
    request = AsyncMock(
        side_effect=[
            {"keyboardPwdId": 1234},
            {"errcode": 0},
            {"errcode": 0},
        ]
    )
    monkeypatch.setattr(client, "_async_api_request", request)
    start = datetime.fromisoformat("2026-07-20T13:00:00+00:00")
    end = datetime.fromisoformat("2026-07-22T09:00:00+00:00")

    password_id = await client.async_add_passcode(
        lock_id=42,
        code="712345",
        name="Guesty-ABC",
        valid_from=start,
        valid_until=end,
    )
    await client.async_change_passcode(
        lock_id=42,
        password_id=password_id,
        code="712346",
        name="Guesty-ABC",
        valid_from=start,
        valid_until=end,
    )
    await client.async_delete_passcode(lock_id=42, password_id=password_id)

    assert password_id == 1234
    assert request.await_args_list[0].args[1]["addType"] == 2
    assert request.await_args_list[0].kwargs["retry_transport"] is False
    assert request.await_args_list[1].args[1]["changeType"] == 2
    assert request.await_args_list[2].args[1]["deleteType"] == 2


def test_ttlock_error_classification_is_fail_closed() -> None:
    """Known gateway and collision responses are never accepted as success."""
    client = _client()
    with pytest.raises(TTLockGatewayError):
        client._raise_api_error({"errmsg": "gateway offline"}, -2012)
    with pytest.raises(TTLockCodeConflictError):
        client._raise_api_error({"errmsg": "Passcode already exists"}, 1)
