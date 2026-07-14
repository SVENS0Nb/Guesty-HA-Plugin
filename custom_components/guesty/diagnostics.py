"""Diagnostics support for Guesty."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ACCESS_TOKEN,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_GUESTY_WEBHOOK_SECRET,
    CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_LOXONE_LISTINGS,
    CONF_LOXONE_MINISERVERS,
    CONF_WEBHOOK_ID,
)
from .data import GuestyConfigEntry

TO_REDACT = {
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_GUESTY_WEBHOOK_SECRET,
    CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID,
    CONF_WEBHOOK_ID,
    "access_token",
    CONF_ACCESS_TOKEN,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GuestyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    data = coordinator.data
    options = dict(entry.options)
    mappings = options.pop(CONF_ACCESS_LOCK_MAPPINGS, {})
    options.pop(CONF_LOXONE_MINISERVERS, None)
    options.pop(CONF_LOXONE_LISTING_MAPPINGS, None)
    options.pop(CONF_LOXONE_LISTINGS, None)
    options.pop(CONF_ACCESS_CUSTOM_FIELD, None)
    mapped_listings = len(mappings) if isinstance(mappings, dict) else 0
    mapped_locks = (
        sum(len(value) for value in mappings.values() if isinstance(value, list))
        if isinstance(mappings, dict)
        else 0
    )

    diagnostics: dict[str, Any] = {
        "config_entry": async_redact_data(entry.data, TO_REDACT),
        "options": options,
        "guest_access": {
            "enabled": bool(entry.options.get(CONF_ACCESS_ENABLED, False)),
            "custom_field_configured": CONF_ACCESS_CUSTOM_FIELD in entry.options,
            "mapped_listings": mapped_listings,
            "mapped_locks": mapped_locks,
        },
        "api": {
            "token_expires_at": client.token_expires_at,
            "rate_limit_remaining": client.last_rate_limit_remaining,
        },
    }
    access_manager = getattr(entry.runtime_data, "access_manager", None)
    if access_manager is not None:
        diagnostics["guest_access"].update(access_manager.diagnostics())
    loxone_manager = getattr(entry.runtime_data, "loxone_manager", None)
    if loxone_manager is not None:
        diagnostics["loxone_pin_access"] = loxone_manager.diagnostics()

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
            "has_last_error": data.last_error is not None,
            "webhook_active": data.webhook_active,
            "listings_count": len(data.listings),
            "reservations_count": len(data.reservations),
        }
        diagnostics["listings"] = [
            {
                "id_hash": hashlib.sha256(listing.id.encode()).hexdigest()[:12],
                "active": listing.active,
                "occupancy": data.occupancy[listing.id].status
                if listing.id in data.occupancy
                else None,
            }
            for listing in data.listings.values()
        ]

    return diagnostics
