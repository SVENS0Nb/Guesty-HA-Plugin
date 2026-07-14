"""Tests for Guesty config-entry setup and unload lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.guesty as guesty_init
from custom_components.guesty import async_setup_entry, async_unload_entry
from custom_components.guesty.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_TOKEN_EXPIRES_AT,
    CONF_WEBHOOK_ID,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_setup_reuses_and_then_removes_transient_token(hass, monkeypatch) -> None:
    """The validation token reaches runtime storage without remaining in entry data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_ACCESS_TOKEN: "validation-token",
            CONF_TOKEN_EXPIRES_AT: 123456.0,
        },
    )
    entry.add_to_hass(hass)
    storage = SimpleNamespace(async_load=AsyncMock(return_value={}))
    client = SimpleNamespace()
    coordinator = SimpleNamespace(
        data=None,
        async_load_cached_data=AsyncMock(return_value=None),
        async_config_entry_first_refresh=AsyncMock(),
        async_recalculate_occupancy=AsyncMock(),
        async_add_listener=MagicMock(return_value=lambda: None),
        set_webhook_active=MagicMock(),
    )
    scheduler = SimpleNamespace(
        async_schedule=MagicMock(),
        async_unschedule=MagicMock(),
    )
    from_hass = MagicMock(return_value=client)

    monkeypatch.setattr(guesty_init, "GuestyStorage", lambda hass, entry_id: storage)
    monkeypatch.setattr(guesty_init.GuestyApiClient, "from_hass", from_hass)
    monkeypatch.setattr(
        guesty_init, "GuestyDataUpdateCoordinator", lambda *args: coordinator
    )
    monkeypatch.setattr(
        guesty_init, "GuestyTransitionScheduler", lambda *args: scheduler
    )
    monkeypatch.setattr(
        guesty_init,
        "async_setup_webhook",
        AsyncMock(return_value="local-webhook"),
    )
    monkeypatch.setattr(
        guesty_init,
        "async_register_guesty_webhook",
        AsyncMock(return_value="remote-webhook"),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    )

    assert await async_setup_entry(hass, entry)

    from_hass.assert_called_once_with(
        hass,
        "client",
        "secret",
        "validation-token",
        123456.0,
    )
    assert entry.runtime_data.client is client
    assert CONF_ACCESS_TOKEN not in entry.data
    assert CONF_TOKEN_EXPIRES_AT not in entry.data
    coordinator.set_webhook_active.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_unload_only_removes_local_webhook(hass, monkeypatch) -> None:
    """Home Assistant reloads keep the remote Guesty subscription intact."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_WEBHOOK_ID: "local-webhook",
        },
    )
    entry.add_to_hass(hass)
    scheduler = SimpleNamespace(async_unschedule=MagicMock())
    coordinator = SimpleNamespace(async_shutdown=AsyncMock())
    entry.runtime_data = SimpleNamespace(scheduler=scheduler, coordinator=coordinator)
    unregister = MagicMock()
    monkeypatch.setattr(guesty_init.ha_webhook, "async_unregister", unregister)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )

    assert await async_unload_entry(hass, entry)

    scheduler.async_unschedule.assert_called_once_with()
    coordinator.async_shutdown.assert_awaited_once_with()
    unregister.assert_called_once_with(hass, "local-webhook")
