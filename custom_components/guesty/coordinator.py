"""Data update coordinator for Guesty."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_LISTING_SYNC_INTERVAL,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_SCAN_INTERVAL,
    CONF_STALE_THRESHOLD_HOURS,
    DEFAULT_LISTING_SYNC_INTERVAL,
    DEFAULT_RESERVATION_DAYS_FUTURE,
    DEFAULT_RESERVATION_DAYS_PAST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    EVENT_OCCUPANCY_CHANGED,
    SYNC_STATUS_DEGRADED,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
)
from .models import (
    GuestyListing,
    GuestyReservation,
    ListingOccupancy,
    calculate_listing_occupancy,
    merge_reservations,
)
from .storage import GuestyStorage

_LOGGER = logging.getLogger(__name__)


def _is_full_reservation_sync_due(last_full_sync: str | None) -> bool:
    """Return whether the daily full reservation sync is due."""
    if not last_full_sync:
        return True
    parsed = dt_util.parse_datetime(last_full_sync)
    if not parsed:
        return True
    try:
        return (dt_util.utcnow() - parsed).total_seconds() >= 86400
    except TypeError:
        return True


@dataclass(slots=True)
class GuestyCoordinatorData:
    """Coordinator data container."""

    listings: dict[str, GuestyListing]
    reservations: list[GuestyReservation]
    occupancy: dict[str, ListingOccupancy]
    last_sync: str | None
    last_listing_sync: str | None
    last_reservation_sync: str | None
    last_full_reservation_sync: str | None
    last_incremental_sync: str | None
    data_stale: bool
    cache_age_minutes: float | None
    sync_status: str
    last_error: str | None
    webhook_active: bool


class GuestyDataUpdateCoordinator(DataUpdateCoordinator[GuestyCoordinatorData]):
    """Fetch and cache Guesty listings and reservations."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GuestyApiClient,
        storage: GuestyStorage,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = entry
        self._client = client
        self._storage = storage
        self._previous_occupancy: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._webhook_active = False
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(
                    CONF_SCAN_INTERVAL,
                    entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                )
            ),
        )

    def set_webhook_active(self, active: bool) -> None:
        """Track whether Guesty webhooks are registered."""
        self._webhook_active = active
        if self.data:
            self._update_data_webhook_flag()

    def _update_data_webhook_flag(self) -> None:
        """Update webhook flag on current data."""
        if not self.data:
            return
        self.async_set_updated_data(
            GuestyCoordinatorData(
                listings=self.data.listings,
                reservations=self.data.reservations,
                occupancy=self.data.occupancy,
                last_sync=self.data.last_sync,
                last_listing_sync=self.data.last_listing_sync,
                last_reservation_sync=self.data.last_reservation_sync,
                last_full_reservation_sync=self.data.last_full_reservation_sync,
                last_incremental_sync=self.data.last_incremental_sync,
                data_stale=self.data.data_stale,
                cache_age_minutes=self.data.cache_age_minutes,
                sync_status=self.data.sync_status,
                last_error=self.data.last_error,
                webhook_active=self._webhook_active,
            )
        )

    async def _async_update_data(self) -> GuestyCoordinatorData:
        """Fetch data from Guesty and merge with cache."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            last_full_sync = cache.get("last_full_reservation_sync")
            full_reservation_sync = _is_full_reservation_sync_due(last_full_sync)
            return await self._async_fetch_data(
                full_reservation_sync=full_reservation_sync
            )

    async def _async_fetch_data(
        self,
        *,
        full_reservation_sync: bool,
        force_listings: bool = False,
    ) -> GuestyCoordinatorData:
        """Fetch and merge Guesty data."""
        days_past = self.config_entry.options.get(
            CONF_RESERVATION_DAYS_PAST, DEFAULT_RESERVATION_DAYS_PAST
        )
        days_future = self.config_entry.options.get(
            CONF_RESERVATION_DAYS_FUTURE, DEFAULT_RESERVATION_DAYS_FUTURE
        )
        listing_interval = self.config_entry.options.get(
            CONF_LISTING_SYNC_INTERVAL, DEFAULT_LISTING_SYNC_INTERVAL
        )
        stale_threshold_hours = self.config_entry.options.get(
            CONF_STALE_THRESHOLD_HOURS, DEFAULT_STALE_THRESHOLD_HOURS
        )

        cache = await self._storage.async_load()
        listings = GuestyStorage.listings_from_cache(cache)
        reservations = GuestyStorage.reservations_from_cache(cache)

        last_sync = cache.get("last_sync")
        last_listing_sync = cache.get("last_listing_sync")
        last_reservation_sync = cache.get("last_reservation_sync")
        last_full_reservation_sync = cache.get("last_full_reservation_sync")
        last_incremental_sync = cache.get("last_incremental_sync")
        last_error: str | None = None
        sync_status = SYNC_STATUS_OK
        api_success = False

        now = dt_util.utcnow()
        should_sync_listings = force_listings or not last_listing_sync
        if last_listing_sync and not should_sync_listings:
            try:
                last_listing_dt = dt_util.parse_datetime(last_listing_sync)
                if (
                    last_listing_dt
                    and (now - last_listing_dt).total_seconds() >= listing_interval
                ):
                    should_sync_listings = True
            except (ValueError, TypeError):
                should_sync_listings = True

        updated_since = None
        if not full_reservation_sync and last_incremental_sync:
            parsed = dt_util.parse_datetime(last_incremental_sync)
            if parsed:
                updated_since = parsed

        try:
            if should_sync_listings:
                listings_result, reservations_result = await asyncio.gather(
                    self._client.async_get_listings(),
                    self._client.async_get_reservations(
                        days_past,
                        days_future,
                        updated_since=None if full_reservation_sync else updated_since,
                    ),
                )
            else:
                listings_result = None
                reservations_result = await self._client.async_get_reservations(
                    days_past,
                    days_future,
                    updated_since=None if full_reservation_sync else updated_since,
                )

            if listings_result is not None:
                listings = {listing.id: listing for listing in listings_result}
                last_listing_sync = now.isoformat()

            if full_reservation_sync or updated_since is None:
                reservations = reservations_result
            else:
                reservations = merge_reservations(
                    reservations,
                    reservations_result,
                    days_past=days_past,
                    days_future=days_future,
                )

            last_sync = now.isoformat()
            last_reservation_sync = now.isoformat()
            if full_reservation_sync:
                last_full_reservation_sync = now.isoformat()
            if updated_since is not None and not full_reservation_sync:
                last_incremental_sync = now.isoformat()
            elif full_reservation_sync or not last_incremental_sync:
                last_incremental_sync = now.isoformat()

            cache.update(
                {
                    "listings": {
                        listing_id: listing.to_dict()
                        for listing_id, listing in listings.items()
                    },
                    "reservations": [
                        reservation.to_dict() for reservation in reservations
                    ],
                    "access_token": self._client.access_token,
                    "token_expires_at": self._client.token_expires_at,
                    "last_sync": last_sync,
                    "last_listing_sync": last_listing_sync,
                    "last_reservation_sync": last_reservation_sync,
                    "last_full_reservation_sync": last_full_reservation_sync,
                    "last_incremental_sync": last_incremental_sync,
                    "last_error": None,
                }
            )
            await self._storage.async_save(cache)
            api_success = True

        except GuestyAuthError as err:
            last_error = str(err)
            sync_status = SYNC_STATUS_ERROR
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except GuestyApiError as err:
            last_error = str(err)
            sync_status = SYNC_STATUS_DEGRADED
            _LOGGER.warning(
                "Guesty API update failed, using cached data if available: %s",
                err,
            )
            if not listings:
                sync_status = SYNC_STATUS_ERROR
                raise UpdateFailed(str(err)) from err
            cache["last_error"] = last_error
            await self._storage.async_save(cache)

        cache_age_minutes = self._calculate_cache_age_minutes(last_sync)
        data_stale = not api_success or (
            cache_age_minutes is not None
            and cache_age_minutes > stale_threshold_hours * 60
        )
        if data_stale and api_success is False:
            sync_status = SYNC_STATUS_DEGRADED
        elif data_stale:
            sync_status = SYNC_STATUS_DEGRADED

        occupancy = self._calculate_occupancy(listings, reservations)
        self._fire_occupancy_events(occupancy)

        return GuestyCoordinatorData(
            listings=listings,
            reservations=reservations,
            occupancy=occupancy,
            last_sync=last_sync,
            last_listing_sync=last_listing_sync,
            last_reservation_sync=last_reservation_sync,
            last_full_reservation_sync=last_full_reservation_sync,
            last_incremental_sync=last_incremental_sync,
            data_stale=data_stale,
            cache_age_minutes=cache_age_minutes,
            sync_status=sync_status,
            last_error=last_error or cache.get("last_error"),
            webhook_active=self._webhook_active,
        )

    async def async_load_cached_data(self) -> GuestyCoordinatorData | None:
        """Load cached data for fast startup."""
        cache = await self._storage.async_load()
        listings = GuestyStorage.listings_from_cache(cache)
        if not listings:
            return None

        reservations = GuestyStorage.reservations_from_cache(cache)
        occupancy = self._calculate_occupancy(listings, reservations)
        cache_age_minutes = self._calculate_cache_age_minutes(cache.get("last_sync"))
        stale_threshold_hours = self.config_entry.options.get(
            CONF_STALE_THRESHOLD_HOURS, DEFAULT_STALE_THRESHOLD_HOURS
        )
        data_stale = (
            cache_age_minutes is not None
            and cache_age_minutes > stale_threshold_hours * 60
        )

        return GuestyCoordinatorData(
            listings=listings,
            reservations=reservations,
            occupancy=occupancy,
            last_sync=cache.get("last_sync"),
            last_listing_sync=cache.get("last_listing_sync"),
            last_reservation_sync=cache.get("last_reservation_sync"),
            last_full_reservation_sync=cache.get("last_full_reservation_sync"),
            last_incremental_sync=cache.get("last_incremental_sync"),
            data_stale=data_stale,
            cache_age_minutes=cache_age_minutes,
            sync_status=SYNC_STATUS_DEGRADED if data_stale else SYNC_STATUS_OK,
            last_error=cache.get("last_error"),
            webhook_active=self._webhook_active,
        )

    async def async_handle_webhook(self, payload: dict[str, Any]) -> None:
        """Handle Guesty webhook notifications."""
        event = (payload.get("event") or payload.get("type") or "").lower()
        reservation = payload.get("reservation") or {}
        reservation_id = reservation.get("_id")

        if event.startswith("reservation") and reservation_id:
            await self._async_apply_reservation_webhook(reservation_id)
            return

        if event.startswith("listing"):
            await self.async_force_full_sync()
            return

        await self.async_request_refresh()

    async def _async_apply_reservation_webhook(self, reservation_id: str) -> None:
        """Refresh a single reservation from a webhook event."""
        try:
            reservation = await self._client.async_get_reservation(reservation_id)
        except (GuestyApiError, GuestyAuthError) as err:
            _LOGGER.warning(
                "Webhook reservation refresh failed, running full sync: %s", err
            )
            await self.async_request_refresh()
            return

        if reservation is None:
            await self.async_request_refresh()
            return

        async with self._refresh_lock:
            cache = await self._storage.async_load()
            reservations = GuestyStorage.reservations_from_cache(cache)
            days_past = self.config_entry.options.get(
                CONF_RESERVATION_DAYS_PAST, DEFAULT_RESERVATION_DAYS_PAST
            )
            days_future = self.config_entry.options.get(
                CONF_RESERVATION_DAYS_FUTURE, DEFAULT_RESERVATION_DAYS_FUTURE
            )
            reservations = merge_reservations(
                reservations,
                [reservation],
                days_past=days_past,
                days_future=days_future,
            )
            listings = GuestyStorage.listings_from_cache(cache)
            now = dt_util.utcnow().isoformat()
            cache["reservations"] = [item.to_dict() for item in reservations]
            cache["last_sync"] = now
            cache["last_reservation_sync"] = now
            cache["last_incremental_sync"] = now
            cache["last_error"] = None
            await self._storage.async_save(cache)

            occupancy = self._calculate_occupancy(listings, reservations)
            self._fire_occupancy_events(occupancy)
            self.async_set_updated_data(
                GuestyCoordinatorData(
                    listings=listings,
                    reservations=reservations,
                    occupancy=occupancy,
                    last_sync=now,
                    last_listing_sync=cache.get("last_listing_sync"),
                    last_reservation_sync=now,
                    last_full_reservation_sync=cache.get("last_full_reservation_sync"),
                    last_incremental_sync=now,
                    data_stale=False,
                    cache_age_minutes=0.0,
                    sync_status=SYNC_STATUS_OK,
                    last_error=None,
                    webhook_active=self._webhook_active,
                )
            )

    async def async_recalculate_occupancy(self) -> None:
        """Recalculate occupancy locally without an API call."""
        if not self.data:
            return

        occupancy = self._calculate_occupancy(
            self.data.listings,
            self.data.reservations,
        )
        self._fire_occupancy_events(occupancy)
        self.async_set_updated_data(
            GuestyCoordinatorData(
                listings=self.data.listings,
                reservations=self.data.reservations,
                occupancy=occupancy,
                last_sync=self.data.last_sync,
                last_listing_sync=self.data.last_listing_sync,
                last_reservation_sync=self.data.last_reservation_sync,
                last_full_reservation_sync=self.data.last_full_reservation_sync,
                last_incremental_sync=self.data.last_incremental_sync,
                data_stale=self.data.data_stale,
                cache_age_minutes=self._calculate_cache_age_minutes(
                    self.data.last_sync
                ),
                sync_status=self.data.sync_status,
                last_error=self.data.last_error,
                webhook_active=self._webhook_active,
            )
        )

    async def async_force_full_sync(self) -> None:
        """Run a full reservation sync."""
        async with self._refresh_lock:
            data = await self._async_fetch_data(
                full_reservation_sync=True,
                force_listings=True,
            )
        self.async_set_updated_data(data)

    def get_listing_reservations(self, listing_id: str) -> list[GuestyReservation]:
        """Return reservations for a listing."""
        if not self.data:
            return []
        return [
            reservation
            for reservation in self.data.reservations
            if reservation.listing_id == listing_id and reservation.is_active_status()
        ]

    def _calculate_occupancy(
        self,
        listings: dict[str, GuestyListing],
        reservations: list[GuestyReservation],
    ) -> dict[str, ListingOccupancy]:
        """Calculate occupancy for all listings."""
        return {
            listing_id: calculate_listing_occupancy(listing, reservations)
            for listing_id, listing in listings.items()
        }

    def _fire_occupancy_events(self, occupancy: dict[str, ListingOccupancy]) -> None:
        """Fire events when occupancy changes."""
        for listing_id, state in occupancy.items():
            previous = self._previous_occupancy.get(listing_id)
            current = state.status
            if previous is not None and previous != current:
                self.hass.bus.async_fire(
                    EVENT_OCCUPANCY_CHANGED,
                    {
                        "listing_id": listing_id,
                        "listing_name": state.listing.display_name,
                        "from": previous,
                        "to": current,
                        "reservation_id": (
                            state.current_reservation.id
                            if state.current_reservation
                            else None
                        ),
                    },
                )
            self._previous_occupancy[listing_id] = current

    @staticmethod
    def _calculate_cache_age_minutes(last_sync: str | None) -> float | None:
        """Return cache age in minutes."""
        if not last_sync:
            return None
        parsed = dt_util.parse_datetime(last_sync)
        if not parsed:
            return None
        delta = dt_util.utcnow() - parsed
        return round(delta.total_seconds() / 60, 1)
