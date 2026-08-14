"""Tests for the TTLock Open Platform client."""

from __future__ import annotations

from datetime import datetime
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.guesty.ttlock_api import (
    TTLockAuthError,
    TTLockApiError,
    TTLockApiClient,
    TTLockCodeConflictError,
    TTLockGatewayError,
    TTLockRateLimitError,
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

    naive = datetime.fromisoformat("2026-07-20T12:34:00")
    assert ttlock_milliseconds(naive) == 1784550840000


def test_ttlock_rejects_missing_application_and_app_login_credentials() -> None:
    """Both Open Platform and one-time App credentials fail closed."""
    with pytest.raises(ValueError, match="client ID"):
        TTLockApiClient(
            object(),
            region="eu",
            client_id="",
            client_secret="secret",
        )


@pytest.mark.asyncio
async def test_ttlock_rejects_empty_app_login() -> None:
    """An empty TTLock App account never reaches the OAuth endpoint."""
    client = _client()
    client._async_token_request = AsyncMock()

    with pytest.raises(TTLockAuthError, match="username and password"):
        await client.async_authenticate("", "")

    client._async_token_request.assert_not_awaited()


def test_invalid_token_expiration_fails_safe_to_refresh() -> None:
    """A damaged private timestamp cannot crash authenticated requests."""
    client = _client()
    client.token_expires_at = "not-a-timestamp"

    assert client._token_needs_refresh() is True


@pytest.mark.asyncio
async def test_ttlock_response_reader_joins_fragmented_json() -> None:
    """A TTLock response may arrive in several network reads."""
    content = SimpleNamespace(
        read=AsyncMock(side_effect=[b'{"errcode":', b'0,"success":true}', b""])
    )
    response = SimpleNamespace(
        content=content,
        charset="utf-8",
        status=200,
        request_info=None,
        history=(),
    )
    request_context = MagicMock()
    request_context.__aenter__ = AsyncMock(return_value=response)
    request_context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = request_context
    client = TTLockApiClient(
        session,
        region="eu",
        client_id="client",
        client_secret="secret",
        access_token="access",
    )

    assert await client._async_http_post("/test", {}) == {
        "errcode": 0,
        "success": True,
    }
    assert content.read.await_count == 3


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
async def test_refresh_rotates_tokens_and_snapshot(monkeypatch) -> None:
    """The serialized refresh flow retains the replacement refresh token."""
    client = _client()
    request = AsyncMock(
        return_value={
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": "invalid",
        }
    )
    monkeypatch.setattr(client, "_async_token_request", request)

    await client.async_refresh_access_token()

    assert request.await_args.args[0]["refresh_token"] == "refresh"
    assert client.token_snapshot()["access_token"] == "rotated-access"
    assert client.token_snapshot()["refresh_token"] == "rotated-refresh"
    assert client.token_snapshot()["token_expires_at"]


@pytest.mark.asyncio
async def test_refresh_without_refresh_token_fails_before_network(monkeypatch) -> None:
    """A missing refresh token is a repairable authentication failure."""
    client = _client()
    client.refresh_token = ""
    request = AsyncMock()
    monkeypatch.setattr(client, "_async_token_request", request)

    with pytest.raises(TTLockAuthError, match="refresh token is unavailable"):
        await client.async_refresh_access_token()

    request.assert_not_awaited()


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
async def test_passcode_list_uses_bounded_pagination_and_filters_items(
    monkeypatch,
) -> None:
    """Passcode reads stop at the advertised page and ignore malformed rows."""
    client = _client()
    first_page = [{"keyboardPwdId": item} for item in range(100)]
    request = AsyncMock(
        side_effect=[
            {"list": first_page, "pages": 2},
            {"list": [{"keyboardPwdId": 101}, "invalid"], "pages": 2},
        ]
    )
    monkeypatch.setattr(client, "_async_api_request", request)

    result = await client.async_list_passcodes(42)

    assert len(result) == 101
    assert request.await_count == 2
    assert request.await_args_list[1].args[1]["pageNo"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["locks", "passcodes"])
async def test_ttlock_list_operations_reject_invalid_payloads(
    monkeypatch, operation
) -> None:
    """Malformed remote lists cannot be treated as empty authorization state."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_api_request",
        AsyncMock(return_value={"list": "not-a-list"}),
    )

    with pytest.raises(TTLockApiError, match="invalid"):
        if operation == "locks":
            await client.async_list_locks()
        else:
            await client.async_list_passcodes(42)


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


@pytest.mark.asyncio
async def test_add_requires_a_confirmed_passcode_id(monkeypatch) -> None:
    """An ambiguous create response remains recoverable by the manager."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_api_request",
        AsyncMock(return_value={"errcode": 0}),
    )
    start = datetime.fromisoformat("2026-07-20T13:00:00+00:00")
    end = datetime.fromisoformat("2026-07-22T09:00:00+00:00")

    with pytest.raises(TTLockApiError, match="passcode ID"):
        await client.async_add_passcode(
            lock_id=42,
            code="712345",
            name="Guesty-ABC",
            valid_from=start,
            valid_until=end,
        )


@pytest.mark.asyncio
async def test_change_and_delete_require_explicit_ttlock_success(
    monkeypatch,
) -> None:
    """Ambiguous HTTP-200 mutation responses are never accepted as success."""
    client = _client()
    request = AsyncMock(return_value={"description": "gateway result unavailable"})
    monkeypatch.setattr(client, "_async_api_request", request)
    start = datetime.fromisoformat("2026-07-20T13:00:00+00:00")
    end = datetime.fromisoformat("2026-07-22T09:00:00+00:00")

    with pytest.raises(TTLockApiError, match="did not include an error code"):
        await client.async_change_passcode(
            lock_id=42,
            password_id=1234,
            code="712345",
            name="Guesty-ABC",
            valid_from=start,
            valid_until=end,
        )
    with pytest.raises(TTLockApiError, match="did not include an error code"):
        await client.async_delete_passcode(lock_id=42, password_id=1234)

    assert request.await_count == 2


@pytest.mark.asyncio
async def test_api_auth_error_refreshes_once_then_replays(monkeypatch) -> None:
    """An expired TTLock token is refreshed once without a retry loop."""
    client = _client()
    post = AsyncMock(
        side_effect=[
            {"errcode": 10003},
            {"errcode": 0, "list": []},
        ]
    )
    refresh = AsyncMock()
    monkeypatch.setattr(client, "_async_http_post", post)
    monkeypatch.setattr(client, "async_refresh_access_token", refresh)

    assert await client.async_list_locks() == []
    refresh.assert_awaited_once_with()
    assert post.await_count == 2


@pytest.mark.asyncio
async def test_api_requires_access_token_and_object_response(monkeypatch) -> None:
    """Missing authentication and malformed JSON envelopes fail explicitly."""
    client = _client()
    client.access_token = ""
    client.refresh_token = ""
    with pytest.raises(TTLockAuthError, match="access token is unavailable"):
        await client.async_list_locks()

    client.access_token = "access"
    monkeypatch.setattr(client, "_async_http_post", AsyncMock(return_value=[]))
    with pytest.raises(TTLockApiError, match="invalid API response"):
        await client.async_list_locks()


@pytest.mark.asyncio
async def test_oauth_response_requires_token_object(monkeypatch) -> None:
    """OAuth responses without an access token never enter private storage."""
    client = _client()
    monkeypatch.setattr(client, "_async_http_post", AsyncMock(return_value=[]))
    with pytest.raises(TTLockAuthError, match="invalid OAuth response"):
        await client._async_token_request({})

    monkeypatch.setattr(
        client,
        "_async_http_post",
        AsyncMock(return_value={"errcode": 10003}),
    )
    with pytest.raises(TTLockAuthError, match="rejected") as raised:
        await client._async_token_request({})
    assert raised.value.code == 10003


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, b"{}", TTLockAuthError),
        (429, b"{}", TTLockRateLimitError),
        (400, b"{}", TTLockApiError),
        (200, b"not-json", TTLockApiError),
        (200, b"[]", TTLockApiError),
    ],
)
async def test_http_post_classifies_remote_failures(status, body, error_type) -> None:
    """HTTP, rate-limit, authentication, and JSON failures stay distinct."""
    content = SimpleNamespace(read=AsyncMock(side_effect=[body, b""]))
    response = SimpleNamespace(
        content=content,
        charset="utf-8",
        status=status,
        request_info=None,
        history=(),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = context
    client = TTLockApiClient(
        session,
        region="eu",
        client_id="client",
        client_secret="secret",
    )

    with pytest.raises(error_type):
        await client._async_http_post("/test", {})


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"errmsg": "denied"}, TTLockAuthError),
        ({"errmsg": "too many"}, TTLockRateLimitError),
        ({"errmsg": "other"}, TTLockApiError),
    ],
)
def test_ttlock_remaining_error_codes_are_classified(payload, error_type) -> None:
    """Every nonzero error reaches a stable exception class."""
    code = (
        10003
        if error_type is TTLockAuthError
        else 30006
        if error_type is TTLockRateLimitError
        else 99
    )
    with pytest.raises(error_type):
        _client()._raise_api_error(payload, code)


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "", "refresh_token": "refresh"},
        {"access_token": "access", "refresh_token": ""},
    ],
)
def test_token_response_requires_both_rotating_tokens(payload) -> None:
    """A partial OAuth success cannot replace a valid stored session."""
    with pytest.raises(TTLockAuthError):
        _client()._apply_token_response(payload)


@pytest.mark.asyncio
async def test_ttlock_rejects_non_ascii_passcode_digits() -> None:
    """TTLock receives only ASCII keypad digits."""
    start = datetime.fromisoformat("2026-07-20T13:00:00+00:00")
    end = datetime.fromisoformat("2026-07-22T09:00:00+00:00")
    with pytest.raises(ValueError):
        await _client().async_add_passcode(
            lock_id=42,
            code="٧١٢٣٤٥",
            name="Guesty-ABC",
            valid_from=start,
            valid_until=end,
        )


def test_ttlock_error_classification_is_fail_closed() -> None:
    """Known gateway and collision responses are never accepted as success."""
    client = _client()
    with pytest.raises(TTLockGatewayError):
        client._raise_api_error({"errmsg": "gateway offline"}, -2012)
    with pytest.raises(TTLockCodeConflictError):
        client._raise_api_error({"errmsg": "Passcode already exists"}, 1)
