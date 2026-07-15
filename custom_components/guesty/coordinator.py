"""Data update coordinator for Guesty."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyAuthError,
    GuestyPermissionError,
    is_safe_resource_id,
)
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
    WEBHOOK_DEBOUNCE_SECONDS,
    WEBHOOK_EVENTS,
    WEBHOOK_INACTIVE_LISTING_SYNC_INTERVAL,
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
INCREMENTAL_SYNC_OVERLAP = timedelta(minutes=5)


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
        self._pending_reservation_ids: set[str] = set()
        self._pending_listing_payloads: dict[str, dict[str, Any]] = {}
        self._webhook_batch_task: asyncio.Task[None] | None = None
        self._unloaded = False
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
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
        if not self._webhook_active:
            listing_interval = min(
                listing_interval,
                WEBHOOK_INACTIVE_LISTING_SYNC_INTERVAL,
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
                updated_since = parsed - INCREMENTAL_SYNC_OVERLAP

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

        except (GuestyAuthError, GuestyPermissionError) as err:
            last_error = str(err)
            sync_status = SYNC_STATUS_ERROR
            raise ConfigEntryAuthFailed(str(err)) from err
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
        """Queue a Guesty webhook for a traffic-efficient near-real-time update."""
        if self._unloaded:
            return
        event = (payload.get("event") or payload.get("type") or "").lower()
        if event not in WEBHOOK_EVENTS:
            _LOGGER.debug("Ignoring unsupported Guesty webhook event %r", event)
            return

        if event.startswith("reservation."):
            reservation_id = self._reservation_id_from_webhook(payload)
            if not is_safe_resource_id(reservation_id):
                _LOGGER.warning(
                    "Ignoring Guesty reservation webhook without a valid id"
                )
                return
            self._pending_reservation_ids.add(reservation_id)
        else:
            listing_id = self._listing_id_from_webhook(payload)
            key = listing_id if is_safe_resource_id(listing_id) else "unknown"
            # Keep only the newest event for a listing during the debounce window.
            self._pending_listing_payloads[key] = payload

        self._ensure_webhook_batch_task()

    def _ensure_webhook_batch_task(self) -> None:
        """Own exactly one batch worker without creating per-event waiters."""
        if self._unloaded:
            return
        task = self._webhook_batch_task
        if task is None or task.done():
            self._webhook_batch_task = self.hass.async_create_task(
                self._async_process_webhook_batches(),
                "guesty_process_webhook_batch",
            )

    async def _async_process_webhook_batches(self) -> None:
        """Collapse webhook bursts without losing changes that arrive mid-sync."""
        try:
            while not self._unloaded and (
                self._pending_reservation_ids or self._pending_listing_payloads
            ):
                await asyncio.sleep(WEBHOOK_DEBOUNCE_SECONDS)
                reservation_ids = set(self._pending_reservation_ids)
                listing_payloads = list(self._pending_listing_payloads.values())
                self._pending_reservation_ids.clear()
                self._pending_listing_payloads.clear()

                if listing_payloads:
                    try:
                        await self._async_apply_listing_webhooks(listing_payloads)
                    except (GuestyApiError, GuestyAuthError) as err:
                        _LOGGER.warning(
                            "Listing webhook refresh failed; using polling fallback: %s",
                            err,
                        )
                        await self.async_refresh()

                if len(reservation_ids) == 1:
                    await self._async_apply_reservation_webhook(reservation_ids.pop())
                elif reservation_ids:
                    # One filtered incremental query is cheaper than one request per
                    # reservation during bulk edits or Guesty retry bursts.
                    await self.async_refresh()
        finally:
            self._webhook_batch_task = None
            # Close the race where a payload arrives after the loop checks its
            # condition but before the worker marks itself finished.
            if self._pending_reservation_ids or self._pending_listing_payloads:
                self._ensure_webhook_batch_task()

    async def async_shutdown(self) -> None:
        """Cancel webhook work so reloads and shutdowns cannot leak API tasks."""
        self._unloaded = True
        self._pending_reservation_ids.clear()
        self._pending_listing_payloads.clear()
        task = self._webhook_batch_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._webhook_batch_task = None

    @staticmethod
    def _reservation_id_from_webhook(payload: dict[str, Any]) -> str | None:
        """Extract a reservation id from supported Guesty payload shapes."""
        reservation = payload.get("reservation")
        if isinstance(reservation, dict):
            value = reservation.get("_id") or reservation.get("id")
            if isinstance(value, str):
                return value

        direct_id = payload.get("reservationId") or payload.get("reservation_id")
        if isinstance(direct_id, str):
            return direct_id

        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("reservation")
            if isinstance(nested, dict):
                value = nested.get("_id") or nested.get("id")
                if isinstance(value, str):
                    return value
            value = data.get("reservationId") or data.get("_id") or data.get("id")
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _listing_data_from_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Extract listing data from supported Guesty payload shapes."""
        listing = payload.get("listing")
        if isinstance(listing, dict):
            return listing
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("listing")
            if isinstance(nested, dict):
                return nested
            if data.get("_id") or data.get("id"):
                return data
        return None

    @classmethod
    def _listing_id_from_webhook(cls, payload: dict[str, Any]) -> str | None:
        """Extract a listing id from a Guesty webhook."""
        listing = cls._listing_data_from_webhook(payload)
        if listing:
            value = listing.get("_id") or listing.get("id")
            if isinstance(value, str):
                return value
        value = payload.get("listingId") or payload.get("listing_id")
        return value if isinstance(value, str) else None

    async def _async_apply_listing_webhooks(
        self,
        payloads: list[dict[str, Any]],
    ) -> None:
        """Apply listing payloads directly and fetch only data that is missing."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            listings = GuestyStorage.listings_from_cache(cache)
            reservations = GuestyStorage.reservations_from_cache(cache)
            previous_listing_ids = set(listings)
            use_api_fallback = False

            for payload in payloads:
                event = (payload.get("event") or payload.get("type") or "").lower()
                listing_data = self._listing_data_from_webhook(payload)
                listing_id = self._listing_id_from_webhook(payload)
                if not is_safe_resource_id(listing_id):
                    use_api_fallback = True
                    continue

                if event == "listing.removed":
                    listings.pop(listing_id, None)
                    continue

                if listing_data is None:
                    use_api_fallback = True
                    continue
                try:
                    listings[listing_id] = GuestyListing.from_api(
                        listing_data,
                        fallback=listings.get(listing_id),
                    )
                except (KeyError, TypeError, ValueError):
                    use_api_fallback = True

            if use_api_fallback:
                listing_results = await self._client.async_get_listings()
                listings = {listing.id: listing for listing in listing_results}

            added_listing_ids = set(listings) - previous_listing_ids
            if added_listing_ids:
                days_past = self.config_entry.options.get(
                    CONF_RESERVATION_DAYS_PAST, DEFAULT_RESERVATION_DAYS_PAST
                )
                days_future = self.config_entry.options.get(
                    CONF_RESERVATION_DAYS_FUTURE, DEFAULT_RESERVATION_DAYS_FUTURE
                )
                try:
                    listing_reservations = await self._client.async_get_reservations(
                        days_past,
                        days_future,
                        listing_ids=added_listing_ids,
                    )
                except (GuestyApiError, GuestyAuthError) as err:
                    _LOGGER.warning(
                        "Could not immediately load reservations for new listings: %s",
                        err,
                    )
                else:
                    reservations = merge_reservations(
                        reservations,
                        listing_reservations,
                        days_past=days_past,
                        days_future=days_future,
                    )

            # Removed listings must disappear from occupancy and calendar data
            # immediately, including their cached reservations.
            reservations = [
                reservation
                for reservation in reservations
                if reservation.listing_id in listings
            ]
            now = dt_util.utcnow().isoformat()
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
                    "last_sync": now,
                    "last_listing_sync": now,
                    "last_error": None,
                }
            )
            await self._storage.async_save(cache)
            self._async_set_fresh_data_from_cache(cache)

    async def _async_apply_reservation_webhook(self, reservation_id: str) -> None:
        """Refresh a single reservation from a webhook event."""
        try:
            reservation = await self._client.async_get_reservation(reservation_id)
        except (GuestyApiError, GuestyAuthError) as err:
            _LOGGER.warning(
                "Webhook reservation refresh failed; running incremental sync: %s",
                err,
            )
            await self.async_refresh()
            return

        if reservation is None:
            await self._async_remove_reservation_from_cache(reservation_id)
            # A 404 can mean a deletion or a short Guesty consistency delay.
            # An immediate incremental pass safely covers both cases.
            await self.async_refresh()
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
            now = dt_util.utcnow().isoformat()
            cache["reservations"] = [item.to_dict() for item in reservations]
            cache["last_sync"] = now
            cache["last_reservation_sync"] = now
            cache["last_error"] = None
            await self._storage.async_save(cache)
            self._async_set_fresh_data_from_cache(
                cache,
                reservation_overrides={reservation.id: reservation},
            )

    async def _async_remove_reservation_from_cache(self, reservation_id: str) -> None:
        """Remove a reservation Guesty reports as no longer existing."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            reservations = GuestyStorage.reservations_from_cache(cache)
            remaining = [
                reservation
                for reservation in reservations
                if reservation.id != reservation_id
            ]
            if len(remaining) == len(reservations):
                return
            now = dt_util.utcnow().isoformat()
            cache.update(
                {
                    "reservations": [item.to_dict() for item in remaining],
                    "last_sync": now,
                    "last_reservation_sync": now,
                    "last_error": None,
                }
            )
            await self._storage.async_save(cache)
            self._async_set_fresh_data_from_cache(cache)

    def _async_set_fresh_data_from_cache(
        self,
        cache: dict[str, Any],
        *,
        reservation_overrides: dict[str, GuestyReservation] | None = None,
    ) -> None:
        """Publish targeted data while keeping access codes out of disk storage."""
        listings = GuestyStorage.listings_from_cache(cache)
        reservations = GuestyStorage.reservations_from_cache(cache)
        if reservation_overrides:
            reservations = [
                reservation_overrides.get(item.id, item) for item in reservations
            ]
        occupancy = self._calculate_occupancy(listings, reservations)
        self._fire_occupancy_events(occupancy)
        self.async_set_updated_data(
            GuestyCoordinatorData(
                listings=listings,
                reservations=reservations,
                occupancy=occupancy,
                last_sync=cache.get("last_sync"),
                last_listing_sync=cache.get("last_listing_sync"),
                last_reservation_sync=cache.get("last_reservation_sync"),
                last_full_reservation_sync=cache.get("last_full_reservation_sync"),
                last_incremental_sync=cache.get("last_incremental_sync"),
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
        for removed_listing_id in set(self._previous_occupancy) - set(occupancy):
            self._previous_occupancy.pop(removed_listing_id, None)
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
