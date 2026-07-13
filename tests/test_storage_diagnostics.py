"""Tests for resilient cache loading and privacy-safe diagnostics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty.const import (
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DOMAIN,
)
from custom_components.guesty.diagnostics import async_get_config_entry_diagnostics
from custom_components.guesty.models import GuestyListing
from custom_components.guesty.storage import GuestyStorage


def test_invalid_cache_records_are_skipped() -> None:
    """One malformed cache record cannot break integration startup."""
    listings = GuestyStorage.listings_from_cache(
        {
            "listings": {
                "valid": {
                    "id": "valid",
                    "title": "Valid",
                    "nickname": None,
                    "active": True,
                },
                "invalid": {"title": "Missing ID"},
            }
        }
    )
    reservations = GuestyStorage.reservations_from_cache(
        {"reservations": [{"id": "missing-fields"}, "invalid"]}
    )

    assert list(listings) == ["valid"]
    assert reservations == []


@pytest.mark.asyncio
async def test_non_mapping_cache_is_reset(hass) -> None:
    """A corrupted top-level cache is replaced by an empty structure."""
    storage = GuestyStorage(hass, "entry")
    storage._store.async_load = AsyncMock(return_value=[])

    cache = await storage.async_load()

    assert cache["listings"] == {}
    assert cache["reservations"] == []


@pytest.mark.asyncio
async def test_diagnostics_hash_listing_ids_and_omit_private_text(hass) -> None:
    """Exported diagnostics contain counts but no property names or raw errors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "private-client", CONF_CLIENT_SECRET: "private-secret"},
        options={
            CONF_ACCESS_ENABLED: True,
            CONF_ACCESS_CUSTOM_FIELD: "private-field-id",
            CONF_ACCESS_LOCK_MAPPINGS: {
                "private-listing-id": [
                    {"entity_id": "lock.private_door", "name": "Private door"}
                ]
            },
        },
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="private-listing-id",
        title="Private address",
        nickname="Private nickname",
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    data = SimpleNamespace(
        sync_status="degraded",
        data_stale=True,
        cache_age_minutes=10,
        last_sync=None,
        last_listing_sync=None,
        last_reservation_sync=None,
        last_full_reservation_sync=None,
        last_incremental_sync=None,
        last_error="legacy response body with private data",
        webhook_active=False,
        listings={listing.id: listing},
        reservations=[],
        occupancy={},
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        client=SimpleNamespace(
            token_expires_at=1234,
            last_rate_limit_remaining=8,
        ),
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = str(diagnostics)

    assert diagnostics["sync"]["has_last_error"] is True
    assert diagnostics["listings"][0]["id_hash"] != listing.id
    assert "Private address" not in serialized
    assert "Private nickname" not in serialized
    assert "legacy response body" not in serialized
    assert "private-secret" not in serialized
    assert "private-field-id" not in serialized
    assert "lock.private_door" not in serialized
    assert diagnostics["guest_access"]["mapped_locks"] == 1
