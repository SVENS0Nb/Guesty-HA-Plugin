"""Tests for Guesty webhook request and subscription lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.helpers.network import NoURLAvailableError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import webhook as guesty_webhook
from custom_components.guesty.api import GuestyNotFoundError
from custom_components.guesty.const import (
    CONF_GUESTY_WEBHOOK_ID,
    CONF_GUESTY_WEBHOOK_SECRET,
    CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

_SIGNING_KEY = b"0123456789abcdef0123456789abcdef"
TEST_SECRET = "whsec_" + base64.b64encode(_SIGNING_KEY).decode()


def _entry(hass, data=None) -> MockConfigEntry:
    entry_data = {CONF_GUESTY_WEBHOOK_SECRET: TEST_SECRET}
    entry_data.update(data or {})
    entry = MockConfigEntry(domain=DOMAIN, title="Guesty", data=entry_data)
    entry.add_to_hass(hass)
    return entry


def _signed_request(payload, *, message_id="msg-1", timestamp=None):
    """Return a request carrying a valid Standard Webhooks signature."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"{message_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(
        hmac.new(_SIGNING_KEY, signed, hashlib.sha256).digest()
    ).decode()
    return SimpleNamespace(
        content_length=len(body),
        read=AsyncMock(return_value=body),
        headers={
            "webhook-id": message_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": f"v1,{signature}",
        },
    )


def _capture_registry(monkeypatch) -> dict[str, object]:
    handlers: dict[str, object] = {}
    monkeypatch.setattr(guesty_webhook.webhook, "async_generate_id", lambda: "id")
    monkeypatch.setattr(
        guesty_webhook.webhook,
        "async_unregister",
        lambda hass, webhook_id: handlers.pop(webhook_id, None),
    )

    def register(
        hass,
        domain,
        name,
        webhook_id,
        handler,
        *,
        allowed_methods=None,
    ) -> None:
        handlers[webhook_id] = handler

    monkeypatch.setattr(guesty_webhook.webhook, "async_register", register)
    return handlers


@pytest.mark.asyncio
async def test_reload_rebinds_and_acknowledges_before_processing(
    hass, monkeypatch
) -> None:
    """The stable endpoint immediately queues work on the latest coordinator."""
    handlers = _capture_registry(monkeypatch)
    entry = _entry(hass)
    old_coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    new_coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())

    webhook_id = await guesty_webhook.async_setup_webhook(hass, entry, old_coordinator)
    await guesty_webhook.async_setup_webhook(hass, entry, new_coordinator)
    request = _signed_request(
        {
            "event": "reservation.updated.v2",
            "data": {"reservation": {"id": "65f19af19824d7e6ff848f11"}},
        }
    )

    response = await handlers[webhook_id](hass, webhook_id, request)
    assert response.status == 202
    await hass.async_block_till_done()

    assert entry.data[CONF_WEBHOOK_ID] == "id"
    old_coordinator.async_handle_webhook.assert_not_awaited()
    new_coordinator.async_handle_webhook.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_and_oversized_payloads_are_rejected(hass, monkeypatch) -> None:
    """Malformed request bodies are rejected without background work."""
    handlers = _capture_registry(monkeypatch)
    entry = _entry(hass)
    coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    webhook_id = await guesty_webhook.async_setup_webhook(hass, entry, coordinator)

    invalid = _signed_request([])
    oversized = SimpleNamespace(
        content_length=guesty_webhook.MAX_WEBHOOK_BODY_BYTES + 1,
        read=AsyncMock(),
        headers={},
    )

    invalid_response = await handlers[webhook_id](hass, webhook_id, invalid)
    oversized_response = await handlers[webhook_id](hass, webhook_id, oversized)

    assert invalid_response.status == 400
    assert oversized_response.status == 413
    coordinator.async_handle_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_event_is_acknowledged_without_work(hass, monkeypatch) -> None:
    """An allowlist prevents arbitrary events from creating API load."""
    handlers = _capture_registry(monkeypatch)
    entry = _entry(hass)
    coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    webhook_id = await guesty_webhook.async_setup_webhook(hass, entry, coordinator)
    request = _signed_request({"event": "attacker.event"})

    response = await handlers[webhook_id](hass, webhook_id, request)

    assert response.status == 202
    coordinator.async_handle_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_stale_and_replayed_signatures_are_rejected(
    hass, monkeypatch
) -> None:
    """Spoofed, stale, and replayed payloads cannot create API work."""
    handlers = _capture_registry(monkeypatch)
    entry = _entry(hass)
    coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    webhook_id = await guesty_webhook.async_setup_webhook(hass, entry, coordinator)
    payload = {
        "event": "reservation.updated.v2",
        "data": {"reservationId": "reservation-1"},
    }

    invalid = _signed_request(payload, message_id="invalid")
    invalid.headers["webhook-signature"] = "v1,not-valid"
    stale = _signed_request(
        payload, message_id="stale", timestamp=int(time.time()) - 600
    )
    valid = _signed_request(payload, message_id="valid")

    assert (await handlers[webhook_id](hass, webhook_id, invalid)).status == 401
    assert (await handlers[webhook_id](hass, webhook_id, stale)).status == 401
    assert (await handlers[webhook_id](hass, webhook_id, valid)).status == 202
    assert (await handlers[webhook_id](hass, webhook_id, valid)).status == 202
    coordinator.async_handle_webhook.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_remote_subscription_is_reused(hass, monkeypatch) -> None:
    """An unchanged subscription reuses its stored secret without extra traffic."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: "https://ha.example.test",
    )
    entry = _entry(hass, {CONF_GUESTY_WEBHOOK_ID: "remote-id"})
    client = SimpleNamespace(
        async_ensure_webhook=AsyncMock(return_value="remote-id"),
        async_get_webhook_secret=AsyncMock(return_value=TEST_SECRET),
    )

    result = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert result == "remote-id"
    client.async_ensure_webhook.assert_awaited_once_with(
        "https://ha.example.test/api/webhook/local-id",
        "remote-id",
    )
    client.async_get_webhook_secret.assert_not_awaited()
    assert entry.data[CONF_GUESTY_WEBHOOK_SECRET] == TEST_SECRET


@pytest.mark.asyncio
async def test_legacy_subscription_without_secret_is_recreated_once(
    hass, monkeypatch
) -> None:
    """A pre-signature webhook is replaced and receives a verifiable secret."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: "https://ha.example.test",
    )
    entry = _entry(
        hass,
        {
            CONF_GUESTY_WEBHOOK_ID: "legacy-id",
            CONF_GUESTY_WEBHOOK_SECRET: "",
        },
    )
    client = SimpleNamespace(
        async_ensure_webhook=AsyncMock(return_value="legacy-id"),
        async_get_webhook_secret=AsyncMock(
            side_effect=[GuestyNotFoundError("not found"), TEST_SECRET]
        ),
        async_unregister_webhook=AsyncMock(),
        async_register_webhook=AsyncMock(return_value="signed-id"),
    )

    result = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert result == "signed-id"
    client.async_unregister_webhook.assert_awaited_once_with("legacy-id")
    client.async_register_webhook.assert_awaited_once_with(
        "https://ha.example.test/api/webhook/local-id"
    )
    assert client.async_get_webhook_secret.await_count == 2
    assert entry.data[CONF_GUESTY_WEBHOOK_ID] == "signed-id"
    assert entry.data[CONF_GUESTY_WEBHOOK_SECRET] == TEST_SECRET
    assert CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID not in entry.data


@pytest.mark.asyncio
async def test_failed_secret_migration_never_recreates_in_a_loop(
    hass, monkeypatch
) -> None:
    """A persistent Guesty 404 keeps polling without repeated webhook creation."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: "https://ha.example.test",
    )
    entry = _entry(
        hass,
        {
            CONF_GUESTY_WEBHOOK_ID: "legacy-id",
            CONF_GUESTY_WEBHOOK_SECRET: "",
        },
    )
    client = SimpleNamespace(
        async_ensure_webhook=AsyncMock(side_effect=["legacy-id", "signed-id"]),
        async_get_webhook_secret=AsyncMock(
            side_effect=[
                GuestyNotFoundError("not found"),
                GuestyNotFoundError("still not found"),
                GuestyNotFoundError("still not found"),
            ]
        ),
        async_unregister_webhook=AsyncMock(),
        async_register_webhook=AsyncMock(return_value="signed-id"),
    )

    first = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )
    second = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert first is None
    assert second is None
    client.async_unregister_webhook.assert_awaited_once_with("legacy-id")
    client.async_register_webhook.assert_awaited_once()
    assert entry.data[CONF_GUESTY_WEBHOOK_ID] == "signed-id"
    assert entry.data[CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID] == "signed-id"
    assert not entry.data.get(CONF_GUESTY_WEBHOOK_SECRET)


@pytest.mark.asyncio
async def test_internal_url_is_not_registered_with_guesty(hass, monkeypatch) -> None:
    """Guesty registration requires a publicly reachable Home Assistant URL."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoURLAvailableError()),
    )
    entry = _entry(hass)
    client = SimpleNamespace(
        async_ensure_webhook=AsyncMock(),
        async_get_webhook_secret=AsyncMock(),
    )

    result = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert result is None
    client.async_ensure_webhook.assert_not_awaited()
    client.async_get_webhook_secret.assert_not_awaited()
