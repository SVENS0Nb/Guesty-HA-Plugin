"""Diagnostics support for Guesty."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

TO_REDACT = {
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_WEBHOOK_ID,
    "access_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    data = coordinator.data

    diagnostics: dict[str, Any] = {
        "config_entry": async_redact_data(entry.data, TO_REDACT),
        "options": dict(entry.options),
        "api": {
            "token_expires_at": client.token_expires_at,
            "rate_limit_remaining": client.last_rate_limit_remaining,
        },
    }

    if data:
        diagnostics["sync"] = {
            "sync_status": data.sync_status,
            "data_stale": data.data_stale,
            "cache_age_minutes": data.cache_age_minutes,
            "last_sync": data.last_sync,
            "last_listing_sync": data.last_listing_sync,
            "last_reservation_sync": data.last_reservation_sync,
            "last_full_reservation_sync": data.last_full_reservation_sync,
            "last_incremental_sync": data.last_incremental_sync,
            "last_error": data.last_error,
            "webhook_active": data.webhook_active,
            "listings_count": len(data.listings),
            "reservations_count": len(data.reservations),
        }
        diagnostics["listings"] = [
            {
                "id": listing.id,
                "title": listing.title,
                "nickname": listing.nickname,
                "active": listing.active,
                "occupancy": data.occupancy[listing.id].status
                if listing.id in data.occupancy
                else None,
            }
            for listing in data.listings.values()
        ]

    return diagnostics
