"""Data update coordinator for Guesty."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyAuthError,
    GuestyKeyCodeReadResult,
    GuestyPermissionError,
    is_guesty_object_id,
    is_safe_resource_id,
)
from .const import (
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_LISTING_SYNC_INTERVAL,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_PIN_CUSTOM_ENABLED,
    CONF_PIN_NATIVE_ENABLED,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_SCAN_INTERVAL,
    CONF_STALE_THRESHOLD_HOURS,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_LISTING_SYNC_INTERVAL,
    DEFAULT_PIN_CUSTOM_ENABLED,
    DEFAULT_PIN_NATIVE_ENABLED,
    DEFAULT_RESERVATION_DAYS_FUTURE,
    DEFAULT_RESERVATION_DAYS_PAST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    EVENT_OCCUPANCY_CHANGED,
    INACTIVE_RESERVATION_STATUSES,
    SYNC_STATUS_DEGRADED,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
    WEBHOOK_DEBOUNCE_SECONDS,
    WEBHOOK_EVENTS,
    WEBHOOK_INACTIVE_LISTING_SYNC_INTERVAL,
    WEBHOOK_REGISTRATION_RETRY_BASE_SECONDS,
    WEBHOOK_REGISTRATION_RETRY_MAX_SECONDS,
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
_PIN_ENRICHMENT_RETRY_KEY = "pin_enrichment_retry_needed"
_RECENT_WEBHOOK_RETENTION = timedelta(minutes=10)
_WEBHOOK_QUEUE_KEY = "pending_reservation_webhooks"
_WEBHOOK_QUEUE_MAX_ITEMS = 1000
_WEBHOOK_FAST_RETRY_MINUTES = 5
_WEBHOOK_QUEUE_RETENTION = timedelta(days=7)
_WEBHOOK_REASON_NOT_VISIBLE = "reservation_not_visible"
_WEBHOOK_REASON_API_UNAVAILABLE = "api_unavailable"
_WEBHOOK_REASON_PIN_UNAVAILABLE = "pin_projection_unavailable"


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
        self._recent_reservation_webhooks: dict[str, datetime] = {}
        self._webhook_batch_task: asyncio.Task[None] | None = None
        self._webhook_registration_task: asyncio.Task[None] | None = None
        self._webhook_queue_changed = asyncio.Event()
        self._last_webhook_received_at: str | None = None
        self._last_webhook_processed_at: str | None = None
        self._last_webhook_failure_reason: str | None = None
        self._oldest_pending_webhook_at: str | None = None
        self._pending_webhook_count = 0
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
        if self._webhook_active == active:
            return
        self._webhook_active = active
        if self.data:
            self._update_data_webhook_flag()

    def _update_cached_auth_state(self, cache: dict[str, Any]) -> None:
        """Persist the shared token and OAuth cooldown without exposing it."""
        cache.update(
            {
                "access_token": getattr(self._client, "access_token", None),
                "token_expires_at": getattr(self._client, "token_expires_at", None),
                "token_retry_at": getattr(self._client, "token_retry_at", None),
            }
        )

    @callback
    def _async_clear_recovered_reauthentication(self) -> None:
        """Abort stale reauth flows only after a successful live API sync."""
        try:
            active_flows = list(
                self.config_entry.async_get_active_flows(
                    self.hass,
                    {SOURCE_REAUTH},
                )
            )
            for flow in active_flows:
                flow_id = flow.get("flow_id")
                if isinstance(flow_id, str):
                    self.hass.config_entries.flow.async_abort(flow_id)
            if active_flows:
                _LOGGER.info(
                    "Guesty API recovered; cleared stale reauthentication repair"
                )
        except Exception:  # Defensive cleanup must never break a healthy sync.
            _LOGGER.exception("Could not clear recovered Guesty reauthentication")

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

    def async_start_webhook_registration_recovery(self, webhook_id: str) -> None:
        """Own periodic webhook registration recovery and health checks."""
        if self._unloaded:
            return
        task = self._webhook_registration_task
        if task is not None and not task.done():
            return
        self._webhook_registration_task = (
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_recover_webhook_registration(webhook_id),
                "guesty_recover_webhook_registration",
            )
        )

    async def _async_recover_webhook_registration(self, webhook_id: str) -> None:
        """Recover and periodically verify push delivery until unload."""
        from .webhook import async_register_guesty_webhook

        delay = (
            WEBHOOK_REGISTRATION_RETRY_MAX_SECONDS
            if self._webhook_active
            else WEBHOOK_REGISTRATION_RETRY_BASE_SECONDS
        )
        try:
            while not self._unloaded:
                await asyncio.sleep(delay)
                if self._unloaded:
                    return
                was_active = self._webhook_active
                try:
                    guesty_webhook_id = await async_register_guesty_webhook(
                        self.hass,
                        self.config_entry,
                        self._client,
                        webhook_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # Defensive background-task boundary.
                    _LOGGER.exception(
                        "Unexpected Guesty webhook registration recovery failure"
                    )
                    guesty_webhook_id = None
                if guesty_webhook_id is not None:
                    self.set_webhook_active(True)
                    delay = WEBHOOK_REGISTRATION_RETRY_MAX_SECONDS
                else:
                    self.set_webhook_active(False)
                    delay = (
                        WEBHOOK_REGISTRATION_RETRY_BASE_SECONDS
                        if was_active
                        else min(
                            max(delay, WEBHOOK_REGISTRATION_RETRY_BASE_SECONDS) * 2,
                            WEBHOOK_REGISTRATION_RETRY_MAX_SECONDS,
                        )
                    )
        finally:
            self._webhook_registration_task = None

    async def _async_update_data(self) -> GuestyCoordinatorData:
        """Fetch data from Guesty and merge with cache."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            self._restore_webhook_queue_state(cache)
            last_full_sync = cache.get("last_full_reservation_sync")
            full_reservation_sync = bool(
                cache.get(_PIN_ENRICHMENT_RETRY_KEY)
                or _is_full_reservation_sync_due(last_full_sync)
            )
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
        expose_guest_details = bool(
            self.config_entry.options.get(
                CONF_EXPOSE_GUEST_DETAILS,
                DEFAULT_EXPOSE_GUEST_DETAILS,
            )
        )
        if not expose_guest_details and GuestyStorage.strip_guest_details(cache):
            await self._storage.async_save(cache)
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
            pin_read_kwargs = self._reservation_pin_read_kwargs()
            if should_sync_listings:
                listings_result, reservations_result = await asyncio.gather(
                    self._client.async_get_listings(),
                    self._client.async_get_reservations(
                        days_past,
                        days_future,
                        updated_since=None if full_reservation_sync else updated_since,
                        **pin_read_kwargs,
                    ),
                )
            else:
                listings_result = None
                reservations_result = await self._client.async_get_reservations(
                    days_past,
                    days_future,
                    updated_since=None if full_reservation_sync else updated_since,
                    **pin_read_kwargs,
                )

            await self._async_try_enrich_native_keycodes(reservations_result)
            pin_enrichment_failed = any(
                reservation.key_code_read_failed
                or reservation.custom_fields_read_failed
                for reservation in reservations_result
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
                    "reservations": self._reservations_for_cache(reservations),
                    "last_sync": last_sync,
                    "last_listing_sync": last_listing_sync,
                    "last_reservation_sync": last_reservation_sync,
                    "last_full_reservation_sync": last_full_reservation_sync,
                    "last_incremental_sync": last_incremental_sync,
                    "last_error": None,
                }
            )
            if pin_enrichment_failed:
                cache[_PIN_ENRICHMENT_RETRY_KEY] = True
            elif full_reservation_sync:
                cache.pop(_PIN_ENRICHMENT_RETRY_KEY, None)
            self._update_cached_auth_state(cache)
            await self._storage.async_save(cache)
            api_success = True
            self._async_clear_recovered_reauthentication()

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
            self._update_cached_auth_state(cache)
            cache["last_error"] = last_error
            await self._storage.async_save(cache)
            if not listings:
                sync_status = SYNC_STATUS_ERROR
                raise UpdateFailed(str(err)) from err

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
        self._restore_webhook_queue_state(cache)
        if not self.config_entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        ) and GuestyStorage.strip_guest_details(cache):
            await self._storage.async_save(cache)
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
            now = dt_util.utcnow()
            cutoff = now - _RECENT_WEBHOOK_RETENTION
            self._recent_reservation_webhooks = {
                item_id: received_at
                for item_id, received_at in self._recent_reservation_webhooks.items()
                if received_at >= cutoff
            }
            inactive_hint = (
                self._reservation_status_from_webhook(payload)
                in INACTIVE_RESERVATION_STATUSES
            )
            await self._async_persist_reservation_webhook(
                reservation_id,
                now,
                inactive_hint=inactive_hint,
            )
            self._recent_reservation_webhooks[reservation_id] = now
            self._pending_reservation_ids.add(reservation_id)
            if inactive_hint:
                # A signed inactive status is sufficient to revoke local
                # access immediately. The durable queue still confirms the
                # final Guesty state and cannot lose cleanup on restart.
                await self._async_remove_reservation_from_cache(reservation_id)
        else:
            listing_id = self._listing_id_from_webhook(payload)
            key = listing_id if is_safe_resource_id(listing_id) else "unknown"
            # Keep only the newest event for a listing during the debounce window.
            self._pending_listing_payloads[key] = payload

        self._ensure_webhook_batch_task()

    def reservation_webhook_received_at(self, reservation_id: str) -> datetime | None:
        """Return the recent verified handoff time for one reservation event."""
        received_at = self._recent_reservation_webhooks.get(reservation_id)
        if received_at is None:
            return None
        now = dt_util.utcnow()
        if received_at < now - _RECENT_WEBHOOK_RETENTION or received_at > now:
            self._recent_reservation_webhooks.pop(reservation_id, None)
            return None
        return received_at

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

    @staticmethod
    def _reservation_status_from_webhook(payload: dict[str, Any]) -> str | None:
        """Extract one normalized reservation status from supported payloads."""
        candidates: list[Any] = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
            nested = data.get("reservation")
            if isinstance(nested, dict):
                candidates.append(nested)
        reservation = payload.get("reservation")
        if isinstance(reservation, dict):
            candidates.append(reservation)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            status = candidate.get("status")
            if isinstance(status, str) and status.strip():
                return status.strip().lower().replace(" ", "_")
        return None

    @staticmethod
    def _webhook_queue(cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return a defensively validated persistent reservation queue."""
        raw_queue = cache.get(_WEBHOOK_QUEUE_KEY)
        if not isinstance(raw_queue, dict):
            return {}
        queue: dict[str, dict[str, Any]] = {}
        for reservation_id, raw_record in raw_queue.items():
            if not is_safe_resource_id(reservation_id) or not isinstance(
                raw_record, dict
            ):
                continue
            received_at = raw_record.get("received_at")
            next_attempt_at = raw_record.get("next_attempt_at")
            if not isinstance(received_at, str) or not isinstance(next_attempt_at, str):
                continue
            try:
                received = dt_util.parse_datetime(received_at)
                next_attempt = dt_util.parse_datetime(next_attempt_at)
            except (TypeError, ValueError):
                continue
            if (
                received is None
                or next_attempt is None
                or received.utcoffset() is None
                or next_attempt.utcoffset() is None
            ):
                continue
            attempt_count = raw_record.get("attempt_count", 0)
            if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
                attempt_count = 0
            generation = raw_record.get("generation", 1)
            if not isinstance(generation, int) or isinstance(generation, bool):
                generation = 1
            record: dict[str, Any] = {
                "received_at": received.isoformat(),
                "next_attempt_at": next_attempt.isoformat(),
                "attempt_count": max(0, min(attempt_count, 100000)),
                "generation": max(1, min(generation, 1000000000)),
                "inactive_hint": bool(raw_record.get("inactive_hint", False)),
            }
            reason = raw_record.get("last_reason")
            if reason in {
                _WEBHOOK_REASON_NOT_VISIBLE,
                _WEBHOOK_REASON_API_UNAVAILABLE,
                _WEBHOOK_REASON_PIN_UNAVAILABLE,
            }:
                record["last_reason"] = reason
            queue[reservation_id] = record
        return queue

    def _sync_webhook_queue_diagnostics(
        self,
        cache: dict[str, Any],
        queue: dict[str, dict[str, Any]],
    ) -> None:
        """Update privacy-safe queue diagnostics from private state."""
        self._pending_webhook_count = len(queue)
        received_values = sorted(
            record["received_at"]
            for record in queue.values()
            if isinstance(record.get("received_at"), str)
        )
        self._oldest_pending_webhook_at = (
            received_values[0] if received_values else None
        )
        for cache_key, attribute in (
            ("last_webhook_received_at", "_last_webhook_received_at"),
            ("last_webhook_processed_at", "_last_webhook_processed_at"),
            ("last_webhook_failure_reason", "_last_webhook_failure_reason"),
        ):
            value = cache.get(cache_key)
            setattr(self, attribute, value if isinstance(value, str) else None)

    def _restore_webhook_queue_state(self, cache: dict[str, Any]) -> None:
        """Restore durable webhook work and start its owned worker."""
        queue = self._webhook_queue(cache)
        self._sync_webhook_queue_diagnostics(cache, queue)
        if queue and not self._unloaded:
            now = dt_util.utcnow()
            cutoff = now - _RECENT_WEBHOOK_RETENTION
            for reservation_id, record in queue.items():
                received_at = dt_util.parse_datetime(record["received_at"])
                if (
                    received_at is not None
                    and received_at.utcoffset() is not None
                    and cutoff <= received_at <= now
                ):
                    self._recent_reservation_webhooks[reservation_id] = received_at
            self._pending_reservation_ids.update(queue)
            self._ensure_webhook_batch_task()

    async def _async_persist_reservation_webhook(
        self,
        reservation_id: str,
        received_at: datetime,
        *,
        inactive_hint: bool,
    ) -> None:
        """Persist a verified reservation event before acknowledging it."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            queue = self._webhook_queue(cache)
            existing = queue.get(reservation_id)
            if existing is None and len(queue) >= _WEBHOOK_QUEUE_MAX_ITEMS:
                # Preserve the oldest work. Guesty will retry this newly
                # rejected handoff because the exception prevents HTTP 202.
                raise RuntimeError("Guesty webhook queue is full")
            first_received = (
                existing.get("received_at") if isinstance(existing, dict) else None
            )
            previous_generation = (
                int(existing.get("generation", 1)) if isinstance(existing, dict) else 0
            )
            queue[reservation_id] = {
                "received_at": (
                    first_received
                    if isinstance(first_received, str)
                    else received_at.isoformat()
                ),
                "next_attempt_at": received_at.isoformat(),
                "attempt_count": 0,
                "generation": previous_generation + 1,
                "inactive_hint": bool(
                    inactive_hint or (existing or {}).get("inactive_hint", False)
                ),
            }
            cache[_WEBHOOK_QUEUE_KEY] = queue
            cache["last_webhook_received_at"] = received_at.isoformat()
            await self._storage.async_save(cache)
            self._sync_webhook_queue_diagnostics(cache, queue)
            self._webhook_queue_changed.set()

    def _next_webhook_retry_at(
        self,
        record: dict[str, Any],
        now: datetime,
    ) -> datetime:
        """Return the next bounded eventual-consistency retry."""
        attempts = int(record.get("attempt_count", 0))
        received_at = dt_util.parse_datetime(str(record.get("received_at", "")))
        if received_at is not None and attempts <= _WEBHOOK_FAST_RETRY_MINUTES:
            boundary = received_at + timedelta(minutes=max(attempts, 1))
            if boundary > now:
                return boundary
        normal_seconds = int(
            self.config_entry.options.get(
                CONF_SCAN_INTERVAL,
                self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        return now + timedelta(seconds=max(normal_seconds, 60))

    async def _async_update_webhook_queue_result(
        self,
        reservation_id: str,
        *,
        expected_generation: int,
        success: bool,
        reason: str | None,
    ) -> None:
        """Atomically complete or reschedule one webhook item."""
        async with self._refresh_lock:
            cache = await self._storage.async_load()
            queue = self._webhook_queue(cache)
            record = queue.get(reservation_id)
            if record is None:
                self._sync_webhook_queue_diagnostics(cache, queue)
                return
            if int(record.get("generation", 1)) != expected_generation:
                # A newer verified event arrived while this targeted read was
                # in flight. Its generation owns the queue item and must be
                # replayed immediately instead of being completed or delayed
                # by the older result.
                self._sync_webhook_queue_diagnostics(cache, queue)
                return
            now = dt_util.utcnow()
            if success:
                queue.pop(reservation_id, None)
                cache["last_webhook_processed_at"] = now.isoformat()
                cache.pop("last_webhook_failure_reason", None)
            else:
                record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
                record["next_attempt_at"] = self._next_webhook_retry_at(
                    record,
                    now,
                ).isoformat()
                if reason in {
                    _WEBHOOK_REASON_NOT_VISIBLE,
                    _WEBHOOK_REASON_API_UNAVAILABLE,
                    _WEBHOOK_REASON_PIN_UNAVAILABLE,
                }:
                    record["last_reason"] = reason
                    cache["last_webhook_failure_reason"] = reason
            if queue:
                cache[_WEBHOOK_QUEUE_KEY] = queue
            else:
                cache.pop(_WEBHOOK_QUEUE_KEY, None)
            await self._storage.async_save(cache)
            self._sync_webhook_queue_diagnostics(cache, queue)

    async def _async_process_webhook_batches(self) -> None:
        """Drain durable webhook work without losing bursts or restarts."""
        try:
            while not self._unloaded:
                await asyncio.sleep(WEBHOOK_DEBOUNCE_SECONDS)
                listing_payloads = list(self._pending_listing_payloads.values())
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

                cache = await self._storage.async_load()
                queue = self._webhook_queue(cache)
                self._sync_webhook_queue_diagnostics(cache, queue)
                if not queue:
                    self._pending_reservation_ids.clear()
                    if not self._pending_listing_payloads:
                        return
                    continue

                now = dt_util.utcnow()
                expired_ids: list[str] = []
                due: list[tuple[bool, datetime, str, int]] = []
                next_due: datetime | None = None
                for reservation_id, record in queue.items():
                    received_at = dt_util.parse_datetime(record["received_at"])
                    attempt_at = dt_util.parse_datetime(record["next_attempt_at"])
                    if received_at is None or attempt_at is None:
                        expired_ids.append(reservation_id)
                        continue
                    if received_at < now - _WEBHOOK_QUEUE_RETENTION:
                        expired_ids.append(reservation_id)
                        continue
                    if attempt_at <= now:
                        due.append(
                            (
                                not bool(record.get("inactive_hint", False)),
                                received_at,
                                reservation_id,
                                int(record.get("generation", 1)),
                            )
                        )
                    elif next_due is None or attempt_at < next_due:
                        next_due = attempt_at

                for reservation_id in expired_ids:
                    await self._async_update_webhook_queue_result(
                        reservation_id,
                        expected_generation=int(
                            queue[reservation_id].get("generation", 1)
                        ),
                        success=True,
                        reason=None,
                    )
                    self._pending_reservation_ids.discard(reservation_id)

                if due:
                    # Inactive hints first, then the oldest verified handoff.
                    # Every reservation keeps its targeted read; bursts never
                    # collapse into an incremental query that can miss it.
                    due.sort()
                    for (
                        _active_sort,
                        _received,
                        reservation_id,
                        generation,
                    ) in due:
                        if self._unloaded:
                            return
                        try:
                            (
                                success,
                                reason,
                            ) = await self._async_apply_reservation_webhook(
                                reservation_id
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # Defensive background-task boundary.
                            _LOGGER.exception(
                                "Unexpected targeted Guesty webhook refresh failure"
                            )
                            success = False
                            reason = _WEBHOOK_REASON_API_UNAVAILABLE
                        await self._async_update_webhook_queue_result(
                            reservation_id,
                            expected_generation=generation,
                            success=success,
                            reason=reason,
                        )
                        if success:
                            self._pending_reservation_ids.discard(reservation_id)
                    continue

                if self._pending_listing_payloads:
                    continue
                if next_due is None:
                    return
                # A newly persisted webhook wakes this shared waiter
                # immediately. The timeout keeps unload and time-based retry
                # boundaries responsive without a waiter per reservation.
                delay = max(0.0, min((next_due - now).total_seconds(), 60.0))
                try:
                    await asyncio.wait_for(
                        self._webhook_queue_changed.wait(),
                        timeout=delay,
                    )
                except TimeoutError:
                    pass
                finally:
                    self._webhook_queue_changed.clear()
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
        self._recent_reservation_webhooks.clear()
        self._webhook_queue_changed.set()
        for task in (
            self._webhook_batch_task,
            self._webhook_registration_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._webhook_batch_task = None
        self._webhook_registration_task = None

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
                        **self._reservation_pin_read_kwargs(
                            listing_ids=added_listing_ids
                        ),
                    )
                except (GuestyApiError, GuestyAuthError) as err:
                    _LOGGER.warning(
                        "Could not immediately load reservations for new listings: %s",
                        err,
                    )
                else:
                    await self._async_try_enrich_native_keycodes(listing_reservations)
                    if any(
                        reservation.key_code_read_failed
                        or reservation.custom_fields_read_failed
                        for reservation in listing_reservations
                    ):
                        cache[_PIN_ENRICHMENT_RETRY_KEY] = True
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
                    "reservations": self._reservations_for_cache(reservations),
                }
            )
            self._update_cached_auth_state(cache)
            if use_api_fallback:
                # Only a complete listing read proves the account-wide listing
                # snapshot fresh. A targeted payload must not postpone the
                # periodic safety scan for unrelated missed events.
                cache["last_listing_sync"] = now
            await self._storage.async_save(cache)
            self._async_set_targeted_data_from_cache(cache)

    async def _async_apply_reservation_webhook(
        self, reservation_id: str
    ) -> tuple[bool, str | None]:
        """Refresh one webhook reservation and report durable queue outcome."""
        pin_observation_incomplete = False
        try:
            reservation = await self._client.async_get_reservation(reservation_id)
            if reservation is not None:
                pin_listing_ids = self._pin_provider_listing_ids()
                if (
                    reservation.is_active_status()
                    and reservation.listing_id in pin_listing_ids
                ):
                    try:
                        enriched = await self._client.async_get_reservation(
                            reservation_id,
                            include_key_code=self.config_entry.options.get(
                                CONF_PIN_NATIVE_ENABLED,
                                DEFAULT_PIN_NATIVE_ENABLED,
                            ),
                            include_custom_fields=self.config_entry.options.get(
                                CONF_PIN_CUSTOM_ENABLED,
                                DEFAULT_PIN_CUSTOM_ENABLED,
                            ),
                        )
                    except (GuestyApiError, GuestyAuthError) as err:
                        pin_observation_incomplete = True
                        if self.config_entry.options.get(
                            CONF_PIN_NATIVE_ENABLED,
                            DEFAULT_PIN_NATIVE_ENABLED,
                        ):
                            reservation.key_code_read_failed = True
                        if self.config_entry.options.get(
                            CONF_PIN_CUSTOM_ENABLED,
                            DEFAULT_PIN_CUSTOM_ENABLED,
                        ):
                            reservation.custom_fields_read_failed = True
                        _LOGGER.warning(
                            "Guesty optional targeted PIN enrichment failed; "
                            "continuing with the reservation update: %s",
                            err,
                        )
                    else:
                        if enriched is not None:
                            reservation = enriched
                        else:
                            pin_observation_incomplete = True
                            if self.config_entry.options.get(
                                CONF_PIN_NATIVE_ENABLED,
                                DEFAULT_PIN_NATIVE_ENABLED,
                            ):
                                reservation.key_code_read_failed = True
                            if self.config_entry.options.get(
                                CONF_PIN_CUSTOM_ENABLED,
                                DEFAULT_PIN_CUSTOM_ENABLED,
                            ):
                                reservation.custom_fields_read_failed = True
                if not await self._async_try_enrich_native_keycodes([reservation]):
                    pin_observation_incomplete = True
        except (GuestyApiError, GuestyAuthError) as err:
            _LOGGER.warning(
                "Webhook reservation refresh failed; scheduling targeted retry: %s",
                err,
            )
            return False, _WEBHOOK_REASON_API_UNAVAILABLE

        if reservation is None:
            # A new reservation can remain unreadable for several minutes.
            # Preserve any prior safe snapshot unless the signed payload
            # already supplied an inactive status hint.
            return False, _WEBHOOK_REASON_NOT_VISIBLE

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
            cache["reservations"] = self._reservations_for_cache(reservations)
            if reservation.key_code_read_failed or (
                reservation.custom_fields_read_failed
            ):
                cache[_PIN_ENRICHMENT_RETRY_KEY] = True
            self._update_cached_auth_state(cache)
            await self._storage.async_save(cache)
            self._async_set_targeted_data_from_cache(
                cache,
                reservation_overrides={reservation.id: reservation},
            )
        incomplete_sources = bool(
            pin_observation_incomplete
            or reservation.key_code_read_failed
            or (
                reservation.custom_fields_read_failed
                and not reservation.custom_fields_projection_omitted
            )
        )
        if reservation.is_active_status() and incomplete_sources:
            return False, _WEBHOOK_REASON_PIN_UNAVAILABLE
        return True, None

    def webhook_diagnostics(self) -> dict[str, Any]:
        """Return privacy-safe durable webhook queue diagnostics."""
        return {
            "pending_reservations": self._pending_webhook_count,
            "oldest_pending_received_at": self._oldest_pending_webhook_at,
            "last_received_at": self._last_webhook_received_at,
            "last_processed_at": self._last_webhook_processed_at,
            "last_failure_reason": self._last_webhook_failure_reason,
        }

    def _pin_provider_listing_ids(self) -> set[str]:
        """Return listings mapped to either PIN delivery provider."""
        listing_ids: set[str] = set()
        options = self.config_entry.options
        if options.get(CONF_LOXONE_ENABLED, False):
            mappings = options.get(CONF_LOXONE_LISTING_MAPPINGS, {})
            if isinstance(mappings, dict):
                listing_ids.update(
                    value for value in mappings if is_safe_resource_id(value)
                )
        if options.get(CONF_TTLOCK_ENABLED, False):
            mappings = options.get(CONF_TTLOCK_LISTING_MAPPINGS, {})
            if isinstance(mappings, dict):
                listing_ids.update(
                    value for value in mappings if is_safe_resource_id(value)
                )
        return listing_ids

    def _native_keycode_listing_ids(self) -> set[str]:
        """Return mapped listings whose enabled PIN source needs native Keycodes."""
        if not self.config_entry.options.get(
            CONF_PIN_NATIVE_ENABLED,
            DEFAULT_PIN_NATIVE_ENABLED,
        ):
            return set()
        return self._pin_provider_listing_ids()

    def _reservation_pin_read_kwargs(
        self,
        *,
        listing_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Return minimal optional V2 PIN projection arguments."""
        mapped = self._pin_provider_listing_ids()
        if listing_ids is not None:
            mapped.intersection_update(listing_ids)
        if not mapped:
            return {}
        options = self.config_entry.options
        include_key_code = bool(
            options.get(CONF_PIN_NATIVE_ENABLED, DEFAULT_PIN_NATIVE_ENABLED)
        )
        include_custom_fields = bool(
            options.get(CONF_PIN_CUSTOM_ENABLED, DEFAULT_PIN_CUSTOM_ENABLED)
        )
        if not include_key_code and not include_custom_fields:
            return {}
        return {
            "pin_listing_ids": mapped,
            "include_key_code": include_key_code,
            "include_custom_fields": include_custom_fields,
        }

    async def _async_try_enrich_native_keycodes(
        self,
        reservations: list[GuestyReservation],
    ) -> bool:
        """Enrich native V3 Keycodes without blocking fresh base data."""
        try:
            await self._async_enrich_native_keycodes(reservations)
        except (GuestyApiError, GuestyAuthError) as err:
            mapped_listing_ids = self._native_keycode_listing_ids()
            for reservation in reservations:
                if (
                    reservation.listing_id in mapped_listing_ids
                    and reservation.is_active_status()
                    and reservation.key_code_route != "v2"
                ):
                    reservation.key_code_read_failed = True
            _LOGGER.warning(
                "Guesty optional v3 Keycode enrichment failed; continuing with "
                "fresh reservation data: %s",
                err,
            )
            return False
        return True

    async def _async_enrich_native_keycodes(
        self,
        reservations: list[GuestyReservation],
    ) -> None:
        """Overlay authoritative Reservations v3 Keycodes on shared results."""
        mapped_listing_ids = self._native_keycode_listing_ids()
        if not mapped_listing_ids:
            return
        targets = {
            reservation.id
            for reservation in reservations
            if reservation.listing_id in mapped_listing_ids
            and reservation.is_active_status()
            and is_guesty_object_id(reservation.id)
        }
        if not targets:
            return

        read_result = await self._client.async_get_reservation_key_codes(targets)
        if isinstance(read_result, GuestyKeyCodeReadResult):
            key_codes = read_result.key_codes
            sparse_ids = read_result.sparse_ids
        else:
            # Preserve compatibility with simple third-party/test clients that
            # implemented the older mapping-only interface.
            key_codes = read_result
            sparse_ids = frozenset()
        for reservation in reservations:
            if reservation.id not in key_codes:
                if reservation.id in sparse_ids and reservation.key_code_v2_observed:
                    # Guesty returns legacy/channel reservations through V3 but
                    # omits their V3-only notes container. Paired with a
                    # successful V2 Keycode projection for the exact same ID,
                    # this identifies the V2 backing model even when the empty
                    # top-level Keycode itself was omitted.
                    reservation.key_code_read_failed = False
                    reservation.key_code_observed = True
                    reservation.key_code_route = "v2"
                    continue
                if reservation.id in targets and reservation.key_code_route != "v2":
                    # A missing reservation or notes container is sparse, not
                    # proof that the native field is empty. Keep this source
                    # fail-closed until the next shared full enrichment.
                    reservation.key_code_read_failed = True
                continue
            reservation.key_code_read_failed = False
            if (
                key_codes[reservation.id] is None
                and reservation.key_code_route == "v2"
                and reservation.key_code
            ):
                # A channel reservation may be readable through v3 while its
                # native Keycode belongs exclusively to the v2 backing model.
                # An empty v3 notes projection cannot erase a populated,
                # explicitly observed top-level v2 Keycode.
                continue
            reservation.key_code = key_codes[reservation.id]
            reservation.key_code_observed = True
            reservation.key_code_route = "v3"

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
            cache.update(
                {
                    "reservations": self._reservations_for_cache(remaining),
                }
            )
            await self._storage.async_save(cache)
            self._async_set_targeted_data_from_cache(cache)

    def _async_set_targeted_data_from_cache(
        self,
        cache: dict[str, Any],
        *,
        reservation_overrides: dict[str, GuestyReservation] | None = None,
    ) -> None:
        """Publish targeted data without resetting global reservation freshness."""
        listings = GuestyStorage.listings_from_cache(cache)
        reservations = GuestyStorage.reservations_from_cache(cache)
        if reservation_overrides:
            reservations = [
                reservation_overrides.get(item.id, item) for item in reservations
            ]
        occupancy = self._calculate_occupancy(listings, reservations)
        self._fire_occupancy_events(occupancy)
        last_sync = cache.get("last_sync")
        cache_age_minutes = self._calculate_cache_age_minutes(last_sync)
        stale_threshold_hours = self.config_entry.options.get(
            CONF_STALE_THRESHOLD_HOURS,
            DEFAULT_STALE_THRESHOLD_HOURS,
        )
        age_is_stale = cache_age_minutes is None or (
            cache_age_minutes > stale_threshold_hours * 60
        )
        previous = self.data
        last_error = (
            previous.last_error
            if previous is not None and previous.last_error is not None
            else cache.get("last_error")
        )
        data_stale = bool(
            age_is_stale or last_error or (previous is not None and previous.data_stale)
        )
        sync_status = (
            previous.sync_status
            if previous is not None
            else (SYNC_STATUS_DEGRADED if data_stale else SYNC_STATUS_OK)
        )
        if data_stale and sync_status == SYNC_STATUS_OK:
            sync_status = SYNC_STATUS_DEGRADED
        self.async_set_updated_data(
            GuestyCoordinatorData(
                listings=listings,
                reservations=reservations,
                occupancy=occupancy,
                last_sync=last_sync,
                last_listing_sync=cache.get("last_listing_sync"),
                last_reservation_sync=cache.get("last_reservation_sync"),
                last_full_reservation_sync=cache.get("last_full_reservation_sync"),
                last_incremental_sync=cache.get("last_incremental_sync"),
                data_stale=data_stale,
                cache_age_minutes=cache_age_minutes,
                sync_status=sync_status,
                last_error=last_error,
                webhook_active=self._webhook_active,
            )
        )

    def _reservations_for_cache(
        self, reservations: list[GuestyReservation]
    ) -> list[dict[str, Any]]:
        """Serialize reservations without persisting opted-out guest details."""
        include_guest_details = bool(
            self.config_entry.options.get(
                CONF_EXPOSE_GUEST_DETAILS,
                DEFAULT_EXPOSE_GUEST_DETAILS,
            )
        )
        return [
            reservation.to_dict(include_guest_details=include_guest_details)
            for reservation in reservations
        ]

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
