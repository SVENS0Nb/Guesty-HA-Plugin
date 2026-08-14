"""Tests for Guesty config-entry setup and unload lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.guesty as guesty_init
from custom_components.guesty import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.guesty.api import GuestyApiError
from custom_components.guesty.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_PIN_CUSTOM_ENABLED,
    CONF_PIN_NATIVE_ENABLED,
    CONF_TOKEN_EXPIRES_AT,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_WEBHOOK_ID,
    DOMAIN,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "keycode_observed",
        "custom_fields_observed",
        "provider_enabled",
        "native_enabled",
        "custom_enabled",
        "pin_read_failed",
        "expected_calls",
    ),
    [
        (False, True, True, True, True, False, 1),
        (True, True, True, True, True, False, 0),
        (False, False, False, True, True, False, 0),
        (True, False, True, False, True, False, 1),
        (False, False, True, True, True, True, 0),
    ],
)
async def test_setup_refreshes_privacy_stripped_pin_sources(
    hass,
    monkeypatch,
    keycode_observed,
    custom_fields_observed,
    provider_enabled,
    native_enabled,
    custom_enabled,
    pin_read_failed,
    expected_calls,
) -> None:
    """PIN providers require one shared full read when cache omitted a source."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={
            CONF_LOXONE_ENABLED: provider_enabled,
            CONF_LOXONE_LISTING_MAPPINGS: {"listing-1": {}},
            CONF_PIN_NATIVE_ENABLED: native_enabled,
            CONF_PIN_CUSTOM_ENABLED: custom_enabled,
        },
    )
    entry.add_to_hass(hass)
    reservation = SimpleNamespace(
        listing_id="listing-1",
        key_code_observed=keycode_observed,
        key_code_read_failed=pin_read_failed,
        custom_fields_observed=custom_fields_observed,
        custom_fields_read_failed=pin_read_failed,
        is_active_status=lambda: True,
    )
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": SimpleNamespace()},
            reservations=[reservation],
        ),
        async_load_cached_data=AsyncMock(return_value=None),
        async_config_entry_first_refresh=AsyncMock(),
        async_force_full_sync=AsyncMock(),
        async_add_listener=MagicMock(return_value=lambda: None),
        set_webhook_active=MagicMock(),
    )
    storage = SimpleNamespace(async_load=AsyncMock(return_value={}))
    scheduler = SimpleNamespace(
        async_schedule=MagicMock(),
        async_unschedule=MagicMock(),
        async_shutdown=AsyncMock(),
    )
    access_manager = SimpleNamespace(
        async_setup=AsyncMock(),
        async_unload=AsyncMock(),
        async_schedule_reconcile=MagicMock(),
    )
    loxone_manager = SimpleNamespace(
        async_setup=AsyncMock(),
        async_unload=AsyncMock(),
        async_schedule_reconcile=MagicMock(),
    )

    monkeypatch.setattr(guesty_init, "GuestyStorage", lambda *_args: storage)
    monkeypatch.setattr(
        guesty_init.GuestyApiClient,
        "from_hass",
        MagicMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        guesty_init, "GuestyDataUpdateCoordinator", lambda *_args: coordinator
    )
    monkeypatch.setattr(
        guesty_init, "GuestyTransitionScheduler", lambda *_args: scheduler
    )
    monkeypatch.setattr(
        guesty_init, "GuestyAccessManager", lambda *_args: access_manager
    )
    monkeypatch.setattr(
        guesty_init, "GuestyLoxoneManager", lambda *_args: loxone_manager
    )
    monkeypatch.setattr(guesty_init, "async_register_access_manager", MagicMock())
    monkeypatch.setattr(
        guesty_init,
        "async_setup_webhook",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    )

    assert await async_setup_entry(hass, entry)

    assert coordinator.async_force_full_sync.await_count == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_webhook_id", ["remote-webhook", None])
async def test_setup_reuses_and_then_removes_transient_token(
    hass, monkeypatch, remote_webhook_id
) -> None:
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
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value={"token_retry_at": 9_876_543_210.0})
    )
    client = SimpleNamespace()
    coordinator = SimpleNamespace(
        data=None,
        async_load_cached_data=AsyncMock(return_value=None),
        async_config_entry_first_refresh=AsyncMock(),
        async_recalculate_occupancy=AsyncMock(),
        async_add_listener=MagicMock(return_value=lambda: None),
        set_webhook_active=MagicMock(),
        async_start_webhook_registration_recovery=MagicMock(),
    )
    scheduler = SimpleNamespace(
        async_schedule=MagicMock(),
        async_unschedule=MagicMock(),
        async_shutdown=AsyncMock(),
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
        AsyncMock(return_value=remote_webhook_id),
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
        9_876_543_210.0,
    )
    assert entry.runtime_data.client is client
    assert entry.runtime_data.ttlock_manager is None
    assert CONF_ACCESS_TOKEN not in entry.data
    assert CONF_TOKEN_EXPIRES_AT not in entry.data
    coordinator.set_webhook_active.assert_called_once_with(
        remote_webhook_id is not None
    )
    if remote_webhook_id is None:
        coordinator.async_start_webhook_registration_recovery.assert_called_once_with(
            "local-webhook"
        )
    else:
        coordinator.async_start_webhook_registration_recovery.assert_not_called()


@pytest.mark.asyncio
async def test_partial_setup_failure_rolls_back_started_resources(
    hass, monkeypatch
) -> None:
    """A failed manager setup leaves no scheduler, task, or public registration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
    )
    entry.add_to_hass(hass)
    storage = SimpleNamespace(async_load=AsyncMock(return_value={}))
    coordinator = SimpleNamespace(
        data=None,
        async_load_cached_data=AsyncMock(return_value=None),
        async_config_entry_first_refresh=AsyncMock(),
        async_shutdown=AsyncMock(),
    )
    scheduler = SimpleNamespace(
        async_schedule=MagicMock(),
        async_unschedule=MagicMock(),
        async_shutdown=AsyncMock(),
    )
    access_manager = SimpleNamespace(
        async_setup=AsyncMock(),
        async_unload=AsyncMock(),
    )
    loxone_manager = SimpleNamespace(
        async_setup=AsyncMock(side_effect=RuntimeError("broken setup")),
        async_unload=AsyncMock(),
    )
    unregister = MagicMock()
    unload_platforms = AsyncMock(return_value=True)

    monkeypatch.setattr(guesty_init, "GuestyStorage", lambda *_args: storage)
    monkeypatch.setattr(
        guesty_init.GuestyApiClient,
        "from_hass",
        MagicMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        guesty_init, "GuestyDataUpdateCoordinator", lambda *_args: coordinator
    )
    monkeypatch.setattr(
        guesty_init, "GuestyTransitionScheduler", lambda *_args: scheduler
    )
    monkeypatch.setattr(
        guesty_init, "GuestyAccessManager", lambda *_args: access_manager
    )
    monkeypatch.setattr(
        guesty_init, "GuestyLoxoneManager", lambda *_args: loxone_manager
    )
    monkeypatch.setattr(guesty_init, "async_unregister_access_manager", unregister)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        unload_platforms,
    )

    with pytest.raises(RuntimeError, match="broken setup"):
        await async_setup_entry(hass, entry)

    scheduler.async_shutdown.assert_awaited_once_with()
    access_manager.async_unload.assert_awaited_once_with()
    loxone_manager.async_unload.assert_awaited_once_with()
    coordinator.async_shutdown.assert_awaited_once_with()
    unregister.assert_not_called()
    unload_platforms.assert_awaited_once_with(entry, guesty_init.PLATFORMS)


@pytest.mark.asyncio
async def test_first_refresh_failure_shuts_down_coordinator(hass, monkeypatch) -> None:
    """A failed initial Guesty fetch cannot leave coordinator work behind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
    )
    entry.add_to_hass(hass)
    storage = SimpleNamespace(async_load=AsyncMock(return_value={}))
    coordinator = SimpleNamespace(
        async_load_cached_data=AsyncMock(return_value=None),
        async_config_entry_first_refresh=AsyncMock(
            side_effect=RuntimeError("refresh failed")
        ),
        async_shutdown=AsyncMock(),
    )
    monkeypatch.setattr(guesty_init, "GuestyStorage", lambda *_args: storage)
    monkeypatch.setattr(
        guesty_init.GuestyApiClient,
        "from_hass",
        MagicMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        guesty_init, "GuestyDataUpdateCoordinator", lambda *_args: coordinator
    )

    with pytest.raises(RuntimeError, match="refresh failed"):
        await async_setup_entry(hass, entry)

    coordinator.async_shutdown.assert_awaited_once_with()


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
    scheduler = SimpleNamespace(
        async_unschedule=MagicMock(),
        async_shutdown=AsyncMock(),
    )
    coordinator = SimpleNamespace(async_shutdown=AsyncMock())
    access_manager = SimpleNamespace(async_unload=AsyncMock())
    loxone_manager = SimpleNamespace(async_unload=AsyncMock())
    ttlock_manager = SimpleNamespace(async_unload=AsyncMock())
    entry.runtime_data = SimpleNamespace(
        scheduler=scheduler,
        coordinator=coordinator,
        access_manager=access_manager,
        loxone_manager=loxone_manager,
        ttlock_manager=ttlock_manager,
    )
    unregister = MagicMock()
    monkeypatch.setattr(guesty_init.ha_webhook, "async_unregister", unregister)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    )

    assert await async_unload_entry(hass, entry)

    scheduler.async_shutdown.assert_awaited_once_with()
    coordinator.async_shutdown.assert_awaited_once_with()
    access_manager.async_unload.assert_awaited_once_with()
    loxone_manager.async_unload.assert_awaited_once_with()
    ttlock_manager.async_unload.assert_awaited_once_with()
    unregister.assert_called_once_with(hass, "local-webhook")


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_failure", [False, True])
async def test_remove_entry_cleans_every_private_and_remote_resource(
    hass, monkeypatch, remote_failure
) -> None:
    """Integration removal is complete even when remote cleanup is best effort."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_GUESTY_WEBHOOK_ID: "remote-webhook",
        },
    )
    entry.add_to_hass(hass)
    storage = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "access_token": "cached-token",
                "token_expires_at": 123.0,
                "token_retry_at": 456.0,
            }
        ),
        async_remove=AsyncMock(),
    )
    access_storage = SimpleNamespace(
        async_load=AsyncMock(
            return_value={
                "records": {
                    "reservation-1": {
                        "field_synced": True,
                        "field_id": "field-1",
                    },
                    "unsynced": {"field_synced": False, "field_id": "field-2"},
                }
            }
        ),
        async_remove=AsyncMock(),
    )
    side_effect = (
        GuestyApiError("temporary cleanup failure") if remote_failure else None
    )
    client = SimpleNamespace(
        async_unregister_webhook=AsyncMock(side_effect=side_effect),
        async_delete_reservation_custom_field=AsyncMock(side_effect=side_effect),
    )
    from_hass = MagicMock(return_value=client)
    remove_loxone = AsyncMock(return_value=not remote_failure)
    remove_ttlock = AsyncMock(return_value=not remote_failure)

    monkeypatch.setattr(guesty_init, "GuestyStorage", lambda *_args: storage)
    monkeypatch.setattr(
        guesty_init, "GuestyAccessStorage", lambda *_args: access_storage
    )
    monkeypatch.setattr(guesty_init.GuestyApiClient, "from_hass", from_hass)
    monkeypatch.setattr(guesty_init, "async_remove_stored_loxone_users", remove_loxone)
    monkeypatch.setattr(
        guesty_init, "async_remove_stored_ttlock_passcodes", remove_ttlock
    )

    await async_remove_entry(hass, entry)

    from_hass.assert_called_once_with(
        hass,
        "client",
        "secret",
        "cached-token",
        123.0,
        456.0,
    )
    client.async_unregister_webhook.assert_awaited_once_with("remote-webhook")
    client.async_delete_reservation_custom_field.assert_awaited_once_with(
        "reservation-1", "field-1"
    )
    storage.async_remove.assert_awaited_once_with()
    access_storage.async_remove.assert_awaited_once_with()
    remove_loxone.assert_awaited_once_with(hass, entry)
    remove_ttlock.assert_awaited_once_with(hass, entry)
