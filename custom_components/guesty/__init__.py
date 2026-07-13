"""The Guesty integration."""

from __future__ import annotations

import logging

from homeassistant.components import webhook as ha_webhook
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_TOKEN_EXPIRES_AT,
    CONF_WEBHOOK_ID,
)
from .coordinator import GuestyDataUpdateCoordinator
from .data import GuestyConfigEntry, GuestyRuntimeData
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

    entry.runtime_data = GuestyRuntimeData(coordinator, client, scheduler)

    webhook_id = await async_setup_webhook(hass, entry, coordinator)
    if webhook_id:
        guesty_webhook_id = await async_register_guesty_webhook(
            hass, entry, client, webhook_id
        )
        coordinator.set_webhook_active(guesty_webhook_id is not None)

    def _on_coordinator_update() -> None:
        scheduler.async_schedule()

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

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: GuestyConfigEntry) -> None:
    """Remove the remote Guesty webhook when the config entry is deleted."""
    guesty_webhook_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)
    storage = GuestyStorage(hass, entry.entry_id)
    if guesty_webhook_id:
        cached = await storage.async_load()
        client = GuestyApiClient.from_hass(
            hass,
            entry.data[CONF_CLIENT_ID],
            entry.data[CONF_CLIENT_SECRET],
            cached.get("access_token"),
            cached.get("token_expires_at"),
        )
        try:
            await client.async_unregister_webhook(guesty_webhook_id)
        except (GuestyApiError, GuestyAuthError):
            _LOGGER.warning("Could not remove the Guesty webhook subscription")
    await storage.async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: GuestyConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)
