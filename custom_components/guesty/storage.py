"""Persistent cache for Guesty data."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION
from .models import GuestyListing, GuestyReservation


class GuestyStorage:
    """Store Guesty listings, reservations and auth token locally."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize storage for a config entry."""
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}_{entry_id}",
        )

    async def async_load(self) -> dict[str, Any]:
        """Load cached data from disk."""
        data = await self._store.async_load()
        if not data:
            return self._empty_cache()
        return data

    @staticmethod
    def _empty_cache() -> dict[str, Any]:
        """Return an empty cache structure."""
        return {
            "listings": {},
            "reservations": [],
            "access_token": None,
            "token_expires_at": None,
            "last_sync": None,
            "last_listing_sync": None,
            "last_reservation_sync": None,
            "last_full_reservation_sync": None,
            "last_incremental_sync": None,
            "last_error": None,
        }

    async def async_save(self, cache: dict[str, Any]) -> None:
        """Persist cached data to disk."""
        await self._store.async_save(cache)

    @staticmethod
    def listings_from_cache(data: dict[str, Any]) -> dict[str, GuestyListing]:
        """Deserialize listings from cache."""
        return {
            listing_id: GuestyListing.from_dict(listing_data)
            for listing_id, listing_data in (data.get("listings") or {}).items()
        }

    @staticmethod
    def reservations_from_cache(data: dict[str, Any]) -> list[GuestyReservation]:
        """Deserialize reservations from cache."""
        return [
            GuestyReservation.from_dict(item) for item in data.get("reservations") or []
        ]
