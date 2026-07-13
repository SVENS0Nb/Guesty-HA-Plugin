"""Tests for Guesty webhook request and subscription lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.helpers.network import NoURLAvailableError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import webhook as guesty_webhook
from custom_components.guesty.const import (
    CONF_GUESTY_WEBHOOK_ID,
    CONF_WEBHOOK_ID,
    DOMAIN,
)


def _entry(hass, data=None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Guesty", data=data or {})
    entry.add_to_hass(hass)
    return entry


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
    request = SimpleNamespace(
        content_length=None,
        json=AsyncMock(
            return_value={
                "event": "reservation.updated",
                "reservation": {"_id": "65f19af19824d7e6ff848f11"},
            }
        ),
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

    invalid = SimpleNamespace(content_length=None, json=AsyncMock(return_value=[]))
    oversized = SimpleNamespace(
        content_length=guesty_webhook.MAX_WEBHOOK_BODY_BYTES + 1,
        json=AsyncMock(),
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
    request = SimpleNamespace(
        content_length=None,
        json=AsyncMock(return_value={"event": "attacker.event"}),
    )

    response = await handlers[webhook_id](hass, webhook_id, request)

    assert response.status == 202
    coordinator.async_handle_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_remote_subscription_is_reused(hass, monkeypatch) -> None:
    """Reloading Home Assistant does not delete and recreate Guesty's webhook."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: "https://ha.example.test",
    )
    entry = _entry(hass, {CONF_GUESTY_WEBHOOK_ID: "remote-id"})
    client = SimpleNamespace(
        async_ensure_webhook=AsyncMock(return_value="remote-id"),
    )

    result = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert result == "remote-id"
    client.async_ensure_webhook.assert_awaited_once_with(
        "https://ha.example.test/api/webhook/local-id",
        "remote-id",
    )


@pytest.mark.asyncio
async def test_internal_url_is_not_registered_with_guesty(hass, monkeypatch) -> None:
    """Guesty registration requires a publicly reachable Home Assistant URL."""
    monkeypatch.setattr(
        "homeassistant.helpers.network.get_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoURLAvailableError()),
    )
    entry = _entry(hass)
    client = SimpleNamespace(async_ensure_webhook=AsyncMock())

    result = await guesty_webhook.async_register_guesty_webhook(
        hass, entry, client, "local-id"
    )

    assert result is None
    client.async_ensure_webhook.assert_not_awaited()
