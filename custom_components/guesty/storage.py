"""Persistent cache for Guesty data."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION
from .models import GuestyListing, GuestyReservation

_LOGGER = logging.getLogger(__name__)


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
        if not isinstance(data, dict):
            return self._empty_cache()
        cache = self._empty_cache()
        cache.update(data)
        if not isinstance(cache.get("listings"), dict):
            cache["listings"] = {}
        if not isinstance(cache.get("reservations"), list):
            cache["reservations"] = []
        return cache

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

    async def async_remove(self) -> None:
        """Delete cached Guesty data from disk."""
        await self._store.async_remove()

    @staticmethod
    def listings_from_cache(data: dict[str, Any]) -> dict[str, GuestyListing]:
        """Deserialize listings from cache."""
        listings: dict[str, GuestyListing] = {}
        raw_listings = data.get("listings")
        if not isinstance(raw_listings, dict):
            return listings
        for listing_id, listing_data in raw_listings.items():
            if not isinstance(listing_data, dict):
                continue
            try:
                listing = GuestyListing.from_dict(listing_data)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignoring an invalid cached Guesty listing")
                continue
            listings[str(listing_id)] = listing
        return listings

    @staticmethod
    def reservations_from_cache(data: dict[str, Any]) -> list[GuestyReservation]:
        """Deserialize reservations from cache."""
        reservations: list[GuestyReservation] = []
        raw_reservations = data.get("reservations")
        if not isinstance(raw_reservations, list):
            return reservations
        for item in raw_reservations:
            if not isinstance(item, dict):
                continue
            try:
                reservations.append(GuestyReservation.from_dict(item))
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignoring an invalid cached Guesty reservation")
        return reservations
