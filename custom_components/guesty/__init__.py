"""The Guesty integration."""

from __future__ import annotations

import logging

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import GuestyApiClient
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from .coordinator import GuestyDataUpdateCoordinator
from .scheduler import GuestyTransitionScheduler
from .storage import GuestyStorage
from .webhook import async_register_guesty_webhook, async_setup_webhook

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Guesty from a config entry."""
    storage = GuestyStorage(hass, entry.entry_id)
    cached = await storage.async_load()

    client = GuestyApiClient.from_hass(
        hass,
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
        cached.get("access_token"),
        cached.get("token_expires_at"),
    )

    coordinator = GuestyDataUpdateCoordinator(hass, entry, client, storage)
    cached_data = await coordinator.async_load_cached_data()
    if cached_data:
        coordinator.async_set_updated_data(cached_data)

    await coordinator.async_config_entry_first_refresh()

    async def _on_transition() -> None:
        await coordinator.async_recalculate_occupancy()
        scheduler.async_schedule()

    scheduler = GuestyTransitionScheduler(hass, coordinator, _on_transition)
    scheduler.async_schedule()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
        "scheduler": scheduler,
        "sensor_listing_ids": set(),
        "calendar_listing_ids": set(),
    }

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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    scheduler: GuestyTransitionScheduler | None = entry_data.get("scheduler")
    if scheduler:
        scheduler.async_unschedule()

    client: GuestyApiClient | None = entry_data.get("client")
    guesty_webhook_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)
    if client and guesty_webhook_id:
        try:
            await client.async_unregister_webhook(guesty_webhook_id)
        except Exception:
            _LOGGER.debug("Could not unregister Guesty webhook on unload")

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)
