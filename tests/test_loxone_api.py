"""Tests for the Loxone user-management client."""

from __future__ import annotations

from datetime import datetime
import json
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest

from custom_components.guesty.loxone_api import (
    LoxoneApiClient,
    LoxoneApiError,
    LoxoneCodeConflictError,
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
