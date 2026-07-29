"""Diagnostics support for Guesty."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LATE_MINUTES,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ACCESS_TOKEN,
    CONF_GUESTY_WEBHOOK_ID,
    CONF_GUESTY_WEBHOOK_SECRET,
    CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID,
    CONF_LISTING_SYNC_INTERVAL,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_PROVISION_LEAD_MINUTES,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_SCAN_INTERVAL,
    CONF_STALE_THRESHOLD_HOURS,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
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

SAFE_OPTION_KEYS = frozenset(
    {
        CONF_SCAN_INTERVAL,
        CONF_LISTING_SYNC_INTERVAL,
        CONF_RESERVATION_DAYS_PAST,
        CONF_RESERVATION_DAYS_FUTURE,
        CONF_STALE_THRESHOLD_HOURS,
        CONF_EXPOSE_GUEST_DETAILS,
        CONF_ACCESS_ENABLED,
        CONF_ACCESS_EARLY_MINUTES,
        CONF_ACCESS_LATE_MINUTES,
        CONF_LOXONE_ENABLED,
        CONF_LOXONE_PROVISION_LEAD_MINUTES,
        CONF_TTLOCK_ENABLED,
        CONF_TTLOCK_PROVISION_LEAD_MINUTES,
    }
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GuestyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    data = coordinator.data
    # Diagnostics are deliberately opt-in rather than copy-and-redact. A newly
    # added option is private until explicitly reviewed and added here.
    options = {
        key: entry.options[key] for key in SAFE_OPTION_KEYS if key in entry.options
    }
    mappings = entry.options.get(CONF_ACCESS_LOCK_MAPPINGS, {})
    rate_limit_windows = getattr(client, "rate_limit_remaining_by_window", {})
    if not isinstance(rate_limit_windows, dict):
        rate_limit_windows = {}
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
            "rate_limit_remaining_by_window": dict(rate_limit_windows),
        },
    }
    access_manager = getattr(entry.runtime_data, "access_manager", None)
    if access_manager is not None:
        diagnostics["guest_access"].update(access_manager.diagnostics())
    loxone_manager = getattr(entry.runtime_data, "loxone_manager", None)
    if loxone_manager is not None:
        diagnostics["loxone_pin_access"] = loxone_manager.diagnostics()
    ttlock_manager = getattr(entry.runtime_data, "ttlock_manager", None)
    if ttlock_manager is not None:
        diagnostics["ttlock_pin_access"] = ttlock_manager.diagnostics()

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
