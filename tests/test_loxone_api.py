"""Tests for the Loxone user-management client."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

import pytest

from custom_components.guesty import loxone_api
from custom_components.guesty._http import ResponseTooLargeError
from custom_components.guesty.loxone_api import (
    LoxoneApiClient,
    LoxoneApiError,
    LoxoneAuthError,
    LoxoneCodeConflictError,
    loxone_server_id,
    loxone_timestamp,
    normalize_loxone_url,
)


def _client() -> LoxoneApiClient:
    return LoxoneApiClient(object(), "https://loxone.example.test/proxy/", "svc", "pw")


def test_loxone_url_requires_https_and_preserves_proxy_path() -> None:
    """Credentials are allowed only through a clean TLS URL."""
    assert normalize_loxone_url(" https://loxone.test/ha/ ") == (
        "https://loxone.test/ha"
    )
    with pytest.raises(ValueError):
        normalize_loxone_url("http://loxone.test")
    with pytest.raises(ValueError):
        normalize_loxone_url("https://user:pw@loxone.test")


def test_loxone_epoch_conversion() -> None:
    """Loxone validity timestamps use seconds since 2009 UTC."""
    assert loxone_timestamp(datetime.fromisoformat("2009-01-01T00:01:00+00:00")) == 60
    assert loxone_timestamp(datetime.fromisoformat("2009-01-01T00:01:00")) == 60


def test_loxone_server_id_is_stable_and_credentials_are_required() -> None:
    """Connection identity is non-secret and empty service accounts fail early."""
    assert loxone_server_id("https://loxone.test/", " Service ") == loxone_server_id(
        "https://loxone.test", "service"
    )
    with pytest.raises(ValueError, match="username and password"):
        LoxoneApiClient(object(), "https://loxone.test", "", "")


@pytest.mark.asyncio
async def test_loxone_response_reader_joins_fragmented_json() -> None:
    """A Loxone response may arrive in several network reads."""
    content = SimpleNamespace(
        read=AsyncMock(
            side_effect=[
                b'{"LL":{"Code":"200",',
                b'"value":"{\\"ok\\":true}"}}',
                b"",
            ]
        )
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
    session.get.return_value = request_context
    client = LoxoneApiClient(session, "https://loxone.example.test", "svc", "pw")

    assert await client._async_request("test") == ({"ok": True}, 200)
    assert content.read.await_count == 3


@pytest.mark.asyncio
async def test_group_list_excludes_privileged_and_builtin_groups(monkeypatch) -> None:
    """Guest mappings expose normal groups only, never administrator groups."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(
            return_value=(
                [
                    {
                        "uuid": "normal",
                        "name": "Guests",
                        "type": 0,
                        "userRights": 1,
                    },
                    {
                        "uuid": "manager",
                        "name": "Managers",
                        "type": 0,
                        "userRights": 0x100,
                    },
                    {
                        "uuid": "config",
                        "name": "Config",
                        "type": 0,
                        "userRights": 0x4,
                    },
                    {"uuid": "missing-rights", "name": "Unsafe", "type": 0},
                    {
                        "uuid": "admin",
                        "name": "All Access",
                        "type": 4,
                        "userRights": 0xFFFFFF,
                    },
                    {"uuid": "all", "name": "All", "type": 2, "userRights": 0},
                ],
                200,
            )
        ),
    )

    assert await client.async_get_groups() == [{"uuid": "normal", "name": "Guests"}]


@pytest.mark.asyncio
async def test_group_list_rejects_non_list_response(monkeypatch) -> None:
    """A malformed group response cannot silently remove authorization choices."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=({"not": "a list"}, 200)),
    )

    with pytest.raises(LoxoneApiError, match="invalid group list"):
        await client.async_get_groups()


@pytest.mark.asyncio
async def test_get_user_distinguishes_absent_and_ambiguous_results(monkeypatch) -> None:
    """Only explicit absence is safe to treat as an idempotent deletion."""
    client = _client()
    request = AsyncMock(return_value=({"uuid": "user-1"}, 200))
    monkeypatch.setattr(client, "_async_request", request)
    assert await client.async_get_user("user-1") == {"uuid": "user-1"}

    request.return_value = ("not-an-object", 200)
    assert await client.async_get_user("user-1") is None

    request.return_value = ({"error": "unknown user"}, 500)
    assert await client.async_get_user("user-1") is None

    request.return_value = ({"error": "backend failed"}, 500)
    with pytest.raises(LoxoneApiError, match="verify whether"):
        await client.async_get_user("user-1")

    request.side_effect = LoxoneApiError("missing", 404)
    assert await client.async_get_user("user-1") is None


@pytest.mark.asyncio
async def test_find_user_rejects_malformed_or_mismatched_lookup(monkeypatch) -> None:
    """Ambiguous-create recovery adopts only an exact stable userid match."""
    client = _client()
    request = AsyncMock(return_value=(None, 200))
    monkeypatch.setattr(client, "_async_request", request)
    assert await client.async_find_user_by_userid("guesty-1") is None

    request.return_value = ("invalid", 200)
    with pytest.raises(LoxoneApiError, match="invalid userid lookup"):
        await client.async_find_user_by_userid("guesty-1")

    request.return_value = ({"name": "missing uuid"}, 200)
    assert await client.async_find_user_by_userid("guesty-1") is None

    request.return_value = ({"uuid": "user-1"}, 200)
    monkeypatch.setattr(
        client,
        "async_get_user",
        AsyncMock(return_value={"uuid": "user-1", "userid": "someone-else"}),
    )
    assert await client.async_find_user_by_userid("guesty-1") is None


@pytest.mark.asyncio
async def test_user_payload_contains_timespan_groups_and_auto_delete(
    monkeypatch,
) -> None:
    """Managed users receive the exact bounded-access properties."""
    client = _client()
    request = AsyncMock(return_value=({"uuid": "user-uuid"}, 200))
    monkeypatch.setattr(client, "_async_request", request)

    result = await client.async_add_or_update_user(
        user_uuid=None,
        name="Guesty Test",
        user_id="guesty-123",
        group_uuids=["group-1", "group-2"],
        valid_from=datetime.fromisoformat("2026-07-20T13:00:00+00:00"),
        valid_until=datetime.fromisoformat("2026-07-22T09:00:00+00:00"),
    )

    assert result == "user-uuid"
    command = request.await_args.args[0]
    payload = json.loads(unquote(command.split("/", 1)[1]))
    assert payload["userState"] == 4
    assert payload["expirationAction"] == 1
    assert payload["usergroups"] == ["group-1", "group-2"]
    assert payload["validUntil"] > payload["validFrom"]
    assert request.await_args.kwargs["retry_transport"] is False


@pytest.mark.asyncio
async def test_existing_user_update_can_retry_transport(monkeypatch) -> None:
    """Only UUID-addressed Loxone updates are safe for transport retries."""
    client = _client()
    request = AsyncMock(return_value=({"uuid": "user-uuid"}, 200))
    monkeypatch.setattr(client, "_async_request", request)

    await client.async_add_or_update_user(
        user_uuid="user-uuid",
        name="Guesty Test",
        user_id="guesty-123",
        group_uuids=["group-1"],
        valid_from=datetime.fromisoformat("2026-07-20T13:00:00+00:00"),
        valid_until=datetime.fromisoformat("2026-07-22T09:00:00+00:00"),
    )

    assert request.await_args.kwargs["retry_transport"] is True


@pytest.mark.asyncio
async def test_user_create_requires_returned_uuid(monkeypatch) -> None:
    """An ambiguous create remains pending when no UUID is confirmed."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=({}, 200)),
    )

    with pytest.raises(LoxoneApiError, match="user UUID"):
        await client.async_add_or_update_user(
            user_uuid=None,
            name="Guesty Test",
            user_id="guesty-123",
            group_uuids=["group-1"],
            valid_from=datetime.fromisoformat("2026-07-20T13:00:00+00:00"),
            valid_until=datetime.fromisoformat("2026-07-22T09:00:00+00:00"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("result_code", [201, 409])
async def test_non_unique_access_codes_are_never_accepted(
    monkeypatch, result_code
) -> None:
    """Both Loxone collision results fail closed."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=(None, result_code)),
    )

    with pytest.raises(LoxoneCodeConflictError):
        await client.async_set_access_code("user-uuid", "712345")


@pytest.mark.asyncio
async def test_access_code_rejects_non_ascii_digits() -> None:
    """Loxone receives only the ASCII keypad digits used by the integration."""
    with pytest.raises(ValueError):
        await _client().async_set_access_code("user-uuid", "٧١٢٣٤٥")


@pytest.mark.asyncio
async def test_user_recovery_uses_direct_userid_lookup(monkeypatch) -> None:
    """Crash recovery uses two bounded requests instead of an N+1 user scan."""
    client = _client()
    request = AsyncMock(
        side_effect=[
            ({"uuid": "user-uuid", "name": "Guest"}, 200),
            ({"uuid": "user-uuid", "userid": "guesty-123"}, 200),
        ]
    )
    monkeypatch.setattr(client, "_async_request", request)

    result = await client.async_find_user_by_userid("guesty-123")

    assert result == {"uuid": "user-uuid", "userid": "guesty-123"}
    assert request.await_args_list[0].args == ("checkuserid/guesty-123",)
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_delete_rejects_ambiguous_loxone_500(monkeypatch) -> None:
    """A generic result 500 cannot erase the local cleanup record."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=({"error": "internal failure"}, 500)),
    )

    with pytest.raises(LoxoneApiError, match="confirm deletion"):
        await client.async_delete_user("user-uuid")


@pytest.mark.asyncio
async def test_delete_accepts_explicit_unknown_user(monkeypatch) -> None:
    """An explicit absent-user response keeps deletion idempotent."""
    client = _client()
    monkeypatch.setattr(
        client,
        "_async_request",
        AsyncMock(return_value=({"error": "unknown user"}, 500)),
    )

    await client.async_delete_user("user-uuid")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, b"{}", LoxoneAuthError),
        (400, b"{}", LoxoneApiError),
        (200, b'{"LL":{"Code":"429","value":null}}', LoxoneAuthError),
    ],
)
async def test_request_classifies_http_and_envelope_failures(
    status, body, error_type
) -> None:
    """HTTP and Loxone authorization failures remain distinguishable."""
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
    session.get.return_value = context
    client = LoxoneApiClient(session, "https://loxone.test", "svc", "secret")

    with pytest.raises(error_type):
        await client._async_request("test")


@pytest.mark.asyncio
async def test_non_idempotent_request_does_not_retry_transport_failure() -> None:
    """An ambiguous create never becomes a duplicate through hidden retries."""
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get.return_value = context
    client = LoxoneApiClient(session, "https://loxone.test", "svc", "secret")

    with pytest.raises(LoxoneApiError, match="Could not connect"):
        await client._async_request("create", retry_transport=False)

    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_oversized_loxone_response_fails_closed(monkeypatch) -> None:
    """The hard response limit prevents unbounded memory use."""
    response = SimpleNamespace(
        content=object(),
        charset="utf-8",
        status=200,
        request_info=None,
        history=(),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get.return_value = context
    monkeypatch.setattr(
        loxone_api,
        "async_read_limited",
        AsyncMock(side_effect=ResponseTooLargeError("too large")),
    )

    with pytest.raises(LoxoneApiError, match="size limit"):
        await LoxoneApiClient(
            session, "https://loxone.test", "svc", "secret"
        )._async_request("test")


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "[]",
        '{"LL":{"Code":"invalid","value":null}}',
    ],
)
def test_decode_response_rejects_malformed_envelopes(body) -> None:
    """Malformed controller responses cannot be mistaken for success."""
    with pytest.raises(LoxoneApiError):
        LoxoneApiClient._decode_response(body)


def test_decode_response_preserves_non_json_string_value() -> None:
    """Plain-text Loxone values remain available to explicit classifiers."""
    assert LoxoneApiClient._decode_response(
        '{"LL":{"Code":"200","value":"plain text"}}'
    ) == ("plain text", 200)
