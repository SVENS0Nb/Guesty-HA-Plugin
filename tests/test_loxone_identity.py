"""Regression cases for Loxone's distinct identity fields and cleanup."""

from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

import pytest

from custom_components.guesty.loxone_api import LoxoneApiClient, LoxoneApiError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {"uuid": "a", "userid": "guesty-test", "uniqueUserId": "foreign"},
        {"uuid": "a", "uniqueUserId": "guesty-test", "isAdmin": True},
        {"uuid": "a", "uniqueUserId": "foreign"},
    ],
)
async def test_recovery_rejects_conflicting_or_privileged_identity(monkeypatch, record):
    api = client()
    monkeypatch.setattr(
        api,
        "_async_request",
        AsyncMock(side_effect=[({"uuid": "a"}, 200), (record, 200)]),
    )
    with pytest.raises(LoxoneApiError):
        await api.async_find_user_by_userid("guesty-test")


@pytest.mark.asyncio
async def test_legacy_scan_never_truncates_to_a_false_absence(monkeypatch):
    api = client()
    request = AsyncMock(
        side_effect=[({}, 200), ([{"uuid": f"u-{i}"} for i in range(129)], 200)]
    )
    monkeypatch.setattr(api, "_async_request", request)
    with pytest.raises(LoxoneApiError, match="scan limit"):
        await api.async_find_user_by_userid("guesty-test")
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_empty_lookup_requires_successful_full_legacy_scan(monkeypatch):
    api = client()
    monkeypatch.setattr(
        api,
        "_async_request",
        AsyncMock(
            side_effect=[
                ({}, 200),
                ([{"uuid": "a", "isAdmin": False}], 200),
                LoxoneApiError("read failed"),
            ]
        ),
    )
    with pytest.raises(LoxoneApiError):
        await api.async_find_user_by_userid("guesty-test")


@pytest.mark.asyncio
async def test_delete_readback_must_confirm_absence(monkeypatch):
    api = client()
    monkeypatch.setattr(api, "async_get_user", AsyncMock(return_value={"uuid": "a"}))
    monkeypatch.setattr(api, "_async_request", AsyncMock(return_value=({}, 200)))
    with pytest.raises(LoxoneApiError, match="confirm deletion"):
        await api.async_delete_user("a")


@pytest.mark.asyncio
async def test_http_500_backend_failure_is_not_absence(monkeypatch):
    api, session = transport_client(
        [(500, {"LL": {"Code": "500", "value": {"error": "backend failure"}}})]
    )
    # Avoid transport retry sleeps; neither decoding nor a list may prove absence.
    from custom_components.guesty import loxone_api

    monkeypatch.setattr(loxone_api, "LOXONE_MAX_RETRIES", 0)
    with pytest.raises(LoxoneApiError):
        await api.async_get_user("a")
    assert session.get.call_count == 1


def client():
    return LoxoneApiClient(object(), "https://controller.example.test", "svc", "pw")


@pytest.mark.asyncio
async def test_legacy_scan_does_not_read_administrator_details(monkeypatch):
    api = client()
    request = AsyncMock(
        side_effect=[
            ({"uuid": ""}, 200),
            (
                [
                    {"uuid": "admin", "isAdmin": True},
                    {"uuid": "guest", "isAdmin": False},
                ],
                200,
            ),
            ({"uuid": "guest", "userid": "guesty-test", "uniqueUserId": ""}, 200),
        ]
    )
    monkeypatch.setattr(api, "_async_request", request)
    assert (await api.async_find_user_by_userid("guesty-test"))["uuid"] == "guest"
    assert all(c.args[0] != "getuser/admin" for c in request.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {"uuid": "unknown"},
        {"uuid": "admin", "isAdmin": True, "uniqueUserId": "guesty-test"},
    ],
)
async def test_legacy_scan_rejects_unknown_role_or_admin_collision(monkeypatch, row):
    api = client()
    monkeypatch.setattr(
        api, "_async_request", AsyncMock(side_effect=[({}, 200), ([row], 200)])
    )
    with pytest.raises(LoxoneApiError):
        await api.async_find_user_by_userid("guesty-test")


@pytest.mark.asyncio
async def test_legacy_recovery_enumerates_after_empty_unique_lookup(monkeypatch):
    api = client()
    request = AsyncMock(
        side_effect=[
            ({"uuid": "", "name": ""}, 200),
            ([{"uuid": "legacy", "isAdmin": False}], 200),
            ({"uuid": "legacy", "userid": "guesty-test", "uniqueUserId": ""}, 200),
        ]
    )
    monkeypatch.setattr(api, "_async_request", request)
    result = await api.async_find_user_by_userid("guesty-test")
    assert result["uuid"] == "legacy"
    assert request.await_count == 3


@pytest.mark.asyncio
async def test_recovery_rejects_duplicate_legacy_markers(monkeypatch):
    api = client()
    request = AsyncMock(
        side_effect=[
            ({"uuid": ""}, 200),
            ([{"uuid": "a", "isAdmin": False}, {"uuid": "b", "isAdmin": False}], 200),
            ({"uuid": "a", "userid": "guesty-test"}, 200),
            ({"uuid": "b", "userid": "guesty-test"}, 200),
        ]
    )
    monkeypatch.setattr(api, "_async_request", request)
    with pytest.raises(LoxoneApiError, match="ambiguous"):
        await api.async_find_user_by_userid("guesty-test")


@pytest.mark.asyncio
async def test_unique_lookup_accepts_unique_field_without_legacy_marker(monkeypatch):
    api = client()
    monkeypatch.setattr(
        api,
        "_async_request",
        AsyncMock(
            side_effect=[
                ({"uuid": "existing"}, 200),
                (
                    {
                        "uuid": "existing",
                        "uniqueUserId": "guesty-test",
                        "userid": "nfc-id",
                    },
                    200,
                ),
            ]
        ),
    )
    assert (await api.async_find_user_by_userid("guesty-test"))["uuid"] == "existing"


@pytest.mark.asyncio
async def test_update_preserves_nfc_and_writes_unique_identity(monkeypatch):
    api = client()
    monkeypatch.setattr(
        api,
        "async_get_user",
        AsyncMock(
            return_value={
                "uuid": "existing",
                "uniqueUserId": "guesty-test",
                "userid": "nfc-id",
            }
        ),
    )
    request = AsyncMock(
        return_value=({"uuid": "existing", "uniqueUserId": "guesty-test"}, 200)
    )
    monkeypatch.setattr(api, "_async_request", request)
    await api.async_add_or_update_user(
        user_uuid="existing",
        name="Managed test",
        user_id="guesty-test",
        group_uuids=["guest-group"],
        valid_from=datetime.fromisoformat("2026-11-13T10:00:00+00:00"),
        valid_until=datetime.fromisoformat("2026-11-14T10:00:00+00:00"),
    )
    payload = json.loads(unquote(request.await_args.args[0].split("/", 1)[1]))
    assert payload["uniqueUserId"] == "guesty-test"
    assert "userid" not in payload
    assert "keycodes" not in payload


@pytest.mark.asyncio
async def test_absence_requires_complete_valid_user_list(monkeypatch):
    api = client()
    monkeypatch.setattr(
        api,
        "_async_request",
        AsyncMock(
            side_effect=[
                ({}, 404),
                ({"partial": []}, 200),
            ]
        ),
    )
    with pytest.raises(LoxoneApiError):
        await api.async_get_user("missing")


@pytest.mark.asyncio
async def test_cleanup_skips_delete_when_authoritatively_absent(monkeypatch):
    api = client()
    request = AsyncMock(side_effect=[({}, 404), ([{"uuid": "other"}], 200)])
    monkeypatch.setattr(api, "_async_request", request)
    await api.async_delete_user("missing")
    assert [c.args[0] for c in request.await_args_list] == [
        "getuser/missing",
        "getuserlist2",
    ]


def transport_client(responses):
    session = MagicMock()
    contexts = []
    for status, value in responses:
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        response = SimpleNamespace(
            content=SimpleNamespace(read=AsyncMock(side_effect=[body, b""])),
            charset="utf-8",
            status=status,
            request_info=None,
            history=(),
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=None)
        contexts.append(context)
    session.get.side_effect = contexts
    return LoxoneApiClient(
        session, "https://controller.example.test", "svc", "pw"
    ), session


@pytest.mark.asyncio
async def test_http_500_unknown_user_is_verified_without_delete():
    api, session = transport_client(
        [
            (500, {"LL": {"Code": "500", "value": {"error": "unknown user"}}}),
            (200, {"LL": {"Code": "200", "value": []}}),
        ]
    )
    await api.async_delete_user("missing")
    assert session.get.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not found", {"LL": {"Code": "404", "value": {}}}])
async def test_generic_http_404_never_proves_user_absence(body):
    api, _ = transport_client(
        [(404, body), (200, {"LL": {"Code": "200", "value": [{"uuid": "missing"}]}})]
    )
    with pytest.raises(LoxoneApiError):
        await api.async_get_user("missing")
