"""The Guesty integration."""

from __future__ import annotations

import logging

from homeassistant.components import webhook as ha_webhook
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .access import (
    GuestyAccessManager,
    GuestyAccessStorage,
    async_register_access_manager,
    async_unregister_access_manager,
)
from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_TOKEN_EXPIRES_AT,
    CONF_WEBHOOK_ID,
)
from .coordinator import GuestyDataUpdateCoordinator
from .data import GuestyConfigEntry, GuestyRuntimeData
from .loxone import GuestyLoxoneManager, async_remove_stored_loxone_users
from .scheduler import GuestyTransitionScheduler
from .storage import GuestyStorage
from .webhook import async_register_guesty_webhook, async_setup_webhook

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]


async def async_setup_entry(hass: HomeAssistant, entry: GuestyConfigEntry) -> bool:
    """Set up Guesty from a config entry."""
    storage = GuestyStorage(hass, entry.entry_id)
    cached = await storage.async_load()

    client = GuestyApiClient.from_hass(
        hass,
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
        entry.data.get(CONF_ACCESS_TOKEN) or cached.get("access_token"),
        entry.data.get(CONF_TOKEN_EXPIRES_AT) or cached.get("token_expires_at"),
    )

    coordinator = GuestyDataUpdateCoordinator(hass, entry, client, storage)
    cached_data = await coordinator.async_load_cached_data()
    if cached_data:
        coordinator.async_set_updated_data(cached_data)

    await coordinator.async_config_entry_first_refresh()

    # The general cache intentionally strips Keycodes and custom-field values.
    # When Loxone is enabled after a restart, perform one shared full read so
    # native Keycodes and optional migration values are available from the
    # normal reservation response without one private request per booking.
    mappings = entry.options.get(CONF_LOXONE_LISTING_MAPPINGS, {})
    mapped_listing_ids = set(mappings) if isinstance(mappings, dict) else set()
    if (
        entry.options.get(CONF_LOXONE_ENABLED, False)
        and coordinator.data is not None
        and any(
            reservation.is_active_status()
            and reservation.listing_id in mapped_listing_ids
            and not reservation.key_code_observed
            for reservation in coordinator.data.reservations
        )
    ):
        await coordinator.async_force_full_sync()

    if CONF_ACCESS_TOKEN in entry.data or CONF_TOKEN_EXPIRES_AT in entry.data:
        hass.config_entries.async_update_entry(
            entry,
            data={
                key: value
                for key, value in entry.data.items()
                if key not in {CONF_ACCESS_TOKEN, CONF_TOKEN_EXPIRES_AT}
            },
        )

    async def _on_transition() -> None:
        await coordinator.async_recalculate_occupancy()
        scheduler.async_schedule()

    scheduler = GuestyTransitionScheduler(hass, coordinator, _on_transition)
    scheduler.async_schedule()

    access_manager = GuestyAccessManager(hass, entry, client, coordinator)
    async_register_access_manager(hass, access_manager)
    await access_manager.async_setup()

    loxone_manager = GuestyLoxoneManager(hass, entry, client, coordinator)
    await loxone_manager.async_setup()

    entry.runtime_data = GuestyRuntimeData(
        coordinator, client, scheduler, access_manager, loxone_manager
    )

    webhook_id = await async_setup_webhook(hass, entry, coordinator)
    if webhook_id:
        guesty_webhook_id = await async_register_guesty_webhook(
            hass, entry, client, webhook_id
        )
        coordinator.set_webhook_active(guesty_webhook_id is not None)

    def _on_coordinator_update() -> None:
        scheduler.async_schedule()
        access_manager.async_schedule_reconcile()
        loxone_manager.async_schedule_reconcile()

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GuestyConfigEntry) -> bool:
    """Unload a config entry."""
    entry.runtime_data.scheduler.async_unschedule()

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        ha_webhook.async_unregister(hass, webhook_id)
    await entry.runtime_data.coordinator.async_shutdown()

    access_manager = getattr(entry.runtime_data, "access_manager", None)
    if access_manager is not None:
        await access_manager.async_unload()
    async_unregister_access_manager(hass, entry.entry_id)

    loxone_manager = getattr(entry.runtime_data, "loxone_manager", None)
    if loxone_manager is not None:
        await loxone_manager.async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: GuestyConfigEntry) -> None:
    """Remove the remote Guesty webhook when the config entry is deleted."""
    guesty_webhook_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)
    storage = GuestyStorage(hass, entry.entry_id)
    access_storage = GuestyAccessStorage(hass, entry.entry_id)
    cached = await storage.async_load()
    access_data = await access_storage.async_load()
    access_records = access_data.get("records", {})
    synced_access_records = [
        (reservation_id, record.get("field_id"))
        for reservation_id, record in access_records.items()
        if isinstance(record, dict)
        and record.get("field_synced")
        and isinstance(record.get("field_id"), str)
    ]

    if guesty_webhook_id or synced_access_records:
        client = GuestyApiClient.from_hass(
            hass,
            entry.data[CONF_CLIENT_ID],
            entry.data[CONF_CLIENT_SECRET],
            cached.get("access_token"),
            cached.get("token_expires_at"),
        )
    if guesty_webhook_id:
        try:
            await client.async_unregister_webhook(guesty_webhook_id)
        except (GuestyApiError, GuestyAuthError):
            _LOGGER.warning("Could not remove the Guesty webhook subscription")
    for reservation_id, field_id in synced_access_records:
        try:
            await client.async_delete_reservation_custom_field(reservation_id, field_id)
        except (GuestyApiError, GuestyAuthError):
            _LOGGER.warning("Could not clear a Guesty door access field during removal")
    await storage.async_remove()
    await access_storage.async_remove()
    if not await async_remove_stored_loxone_users(hass, entry):
        _LOGGER.error(
            "One or more managed Loxone users could not be removed; "
            "code-free cleanup records were retained"
        )


async def _async_update_listener(hass: HomeAssistant, entry: GuestyConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)
