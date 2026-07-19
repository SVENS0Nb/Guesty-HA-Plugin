"""Reservation-driven, time-limited Loxone PIN provisioning."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
import hashlib
import logging
import re
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyAuthError,
    GuestyNotFoundError,
    GuestyPermissionError,
    GuestyRetryableError,
    is_safe_resource_id,
)
from .const import (
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_LATE_MINUTES,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_LOXONE_CODE_PREFIX,
    CONF_LOXONE_CUSTOM_FIELD,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_GROUP_UUIDS,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_LOXONE_MINISERVERS,
    CONF_LOXONE_PROVISION_LEAD_MINUTES,
    CONF_LOXONE_SERVER_ID,
    CONF_LOXONE_SERVER_GROUPS,
    CONF_LOXONE_SERVER_PASSWORD,
    CONF_LOXONE_SERVER_URL,
    CONF_LOXONE_SERVER_USERNAME,
    DEFAULT_ACCESS_EARLY_MINUTES,
    DEFAULT_ACCESS_LATE_MINUTES,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_LOXONE_CODE_PREFIX,
    DEFAULT_LOXONE_CUSTOM_FIELD,
    DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
    LOXONE_ACCESS_CODE_LENGTH,
    LOXONE_RETRY_BASE_SECONDS,
    LOXONE_RETRY_MAX_SECONDS,
    LOXONE_STORAGE_VERSION,
)
from .coordinator import GuestyDataUpdateCoordinator
from .loxone_api import (
    LoxoneApiClient,
    LoxoneApiError,
    LoxoneAuthError,
    LoxoneCodeConflictError,
)
from .models import GuestyListing, GuestyReservation

_LOGGER = logging.getLogger(__name__)

LOXONE_STORAGE_KEY = "guesty_loxone"
_MAX_CODE_ROTATIONS_PER_RECONCILE = 3
_GUESTY_FIELD_WRITE_BATCH_SIZE = 2
_GUESTY_FIELD_QUEUE_DELAY_SECONDS = 30
_GUESTY_SYNC_QUEUED = "guesty_sync_queued"
_CODE_PATTERN = re.compile(r"^\d{6}$")
_SERVER_SNAPSHOT_KEY = "server_snapshot"
_SERVER_SNAPSHOT_FIELDS = (
    CONF_LOXONE_SERVER_URL,
    CONF_LOXONE_SERVER_USERNAME,
    CONF_LOXONE_SERVER_PASSWORD,
)
_WEAK_CODES = {
    "000000",
    "111111",
    "222222",
    "333333",
    "444444",
    "555555",
    "666666",
    "777777",
    "888888",
    "999999",
    "012345",
    "123456",
    "234567",
    "345678",
    "456789",
    "987654",
    "876543",
    "765432",
    "654321",
    "543210",
}


class GuestyLoxoneStorage:
    """Store PINs separately, privately, and with atomic writes."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the private store."""
        self._store = Store(
            hass,
            LOXONE_STORAGE_VERSION,
            f"{LOXONE_STORAGE_KEY}_{entry_id}",
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> dict[str, Any]:
        """Load a validated top-level state object."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {"records": {}, "resolved_field": {}}
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        else:
            data["records"] = {
                str(reservation_id): record
                for reservation_id, record in data["records"].items()
                if isinstance(reservation_id, str) and isinstance(record, dict)
            }
        if not isinstance(data.get("resolved_field"), dict):
            data["resolved_field"] = {}
        return data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Persist state."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Delete all local state."""
        await self._store.async_remove()


class GuestyLoxoneManager:
    """Synchronize a Guesty reservation field with short-lived Loxone users."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GuestyApiClient,
        coordinator: GuestyDataUpdateCoordinator,
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self._client = client
        self._coordinator = coordinator
        self._storage = GuestyLoxoneStorage(hass, entry.entry_id)
        self._data: dict[str, Any] = {"records": {}}
        self._clients: dict[str, LoxoneApiClient] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self._unloaded = False
        self._cancel_timer: Callable[[], None] | None = None
        self._last_reconcile_at: str | None = None
        self._last_result = "never"
        self._last_error: str | None = None
        self._last_generated = 0
        self._last_rotated = 0
        self._last_provisioned = 0
        self._last_deleted = 0
        self._last_guesty_writes = 0
        self._last_queued = 0
        self._guesty_writes_remaining = 0
        self._listeners: set[Callable[[], None]] = set()

    @property
    def _records(self) -> dict[str, dict[str, Any]]:
        """Return validated reservation records."""
        records = self._data.setdefault("records", {})
        return records if isinstance(records, dict) else {}

    @property
    def _servers(self) -> dict[str, dict[str, Any]]:
        """Return configured Miniserver records keyed by stable id."""
        raw = self.entry.options.get(CONF_LOXONE_MINISERVERS, [])
        if not isinstance(raw, list):
            return {}
        return {
            item[CONF_LOXONE_SERVER_ID]: item
            for item in raw
            if isinstance(item, dict)
            and isinstance(item.get(CONF_LOXONE_SERVER_ID), str)
        }

    @property
    def _mappings(self) -> dict[str, dict[str, Any]]:
        """Return valid per-listing Loxone mappings."""
        raw = self.entry.options.get(CONF_LOXONE_LISTING_MAPPINGS, {})
        return raw if isinstance(raw, dict) else {}

    async def async_setup(self) -> None:
        """Load private state and start one reconciliation pass."""
        self._data = await self._storage.async_load()
        # Version 1.8.0 could put every booking into an independent exponential
        # retry after a bulk custom-field migration exhausted Guesty's request
        # allowance. Convert those reason-less retries into the bounded queue so
        # the nearest stays recover immediately after upgrading.
        recovered_retry = False
        for record in self._records.values():
            if (
                not record.get("field_synced")
                and self._retry_at(record, "guesty") is not None
                and not record.get("last_error")
            ):
                self._clear_retry(record, "guesty")
                record["last_error"] = _GUESTY_SYNC_QUEUED
                recovered_retry = True
        if recovered_retry:
            await self._storage.async_save(self._data)
        self.async_schedule_reconcile()

    async def async_unload(self) -> None:
        """Stop timers and background work."""
        self._unloaded = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._clients.clear()
        self._listeners.clear()

    def async_schedule_reconcile(self) -> None:
        """Debounce Guesty push/poll changes into one Loxone pass."""
        if self._unloaded:
            return
        self._pending = True
        if self._task and not self._task.done():
            return
        self._task = self.hass.async_create_task(
            self._async_reconcile_loop(),
            "guesty_loxone_reconcile",
        )

    async def _async_reconcile_loop(self) -> None:
        """Process updates arriving while a previous pass is running."""
        try:
            while self._pending and not self._unloaded:
                self._pending = False
                await asyncio.sleep(0.5)
                try:
                    await self.async_reconcile()
                except Exception as err:  # Defensive task boundary.
                    self._last_result = "error"
                    self._last_error = type(err).__name__
                    _LOGGER.exception("Unexpected Loxone PIN synchronization failure")
                    self._notify_listeners()
        except asyncio.CancelledError:
            raise

    async def async_reconcile(self) -> None:
        """Reconcile cached Guesty reservations without any extra listing poll."""
        async with self._lock:
            now = dt_util.utcnow()
            self._last_generated = 0
            self._last_rotated = 0
            self._last_provisioned = 0
            self._last_deleted = 0
            self._last_guesty_writes = 0
            self._last_queued = 0
            self._guesty_writes_remaining = self._guesty_write_budget()
            next_run: datetime | None = None
            errors: list[str] = []
            data = self._coordinator.data
            data_stale = data is None or bool(getattr(data, "data_stale", False))

            eligible: dict[str, GuestyReservation] = {}
            if self.entry.options.get(CONF_LOXONE_ENABLED, False) and data is not None:
                eligible = {
                    reservation.id: reservation
                    for reservation in data.reservations
                    if reservation.is_active_status()
                    and reservation.listing_id in self._mappings
                    and reservation.listing_id in data.listings
                }

            field_id: str | None = None
            if eligible and not data_stale:
                try:
                    field_id = await self._async_custom_field_id()
                except (GuestyApiError, GuestyAuthError) as err:
                    errors.append(type(err).__name__)
                else:
                    self._hydrate_observed_custom_fields(
                        eligible.values(),
                        field_id,
                    )

            for reservation_id in list(self._records):
                if reservation_id in eligible:
                    continue
                # An unavailable Guesty API cannot prove a cancellation. Keep
                # the code-free/future record until a fresh pass, unless the
                # feature or its listing mapping was intentionally disabled.
                record = self._records.get(reservation_id, {})
                listing_id = (
                    record.get("listing_id") if isinstance(record, dict) else None
                )
                stored_end = (
                    dt_util.parse_datetime(str(record.get("access_end", "")))
                    if isinstance(record, dict)
                    else None
                )
                if (
                    data_stale
                    and self.entry.options.get(CONF_LOXONE_ENABLED, False)
                    and listing_id in self._mappings
                    and not record.get("retired")
                    and (stored_end is None or stored_end > now)
                ):
                    if stored_end is not None:
                        next_run = self._earlier(next_run, stored_end)
                    continue
                cleanup_retry_at = self._retry_at(record, "cleanup")
                if cleanup_retry_at and cleanup_retry_at > now:
                    next_run = self._earlier(next_run, cleanup_retry_at)
                    continue
                try:
                    await self._async_retire(reservation_id)
                except (LoxoneApiError, LoxoneAuthError) as err:
                    self._record_retry_failure(record, "cleanup", now)
                    retry_at = self._retry_at(record, "cleanup")
                    if retry_at:
                        next_run = self._earlier(next_run, retry_at)
                    errors.append(type(err).__name__)

            for reservation in sorted(
                eligible.values(),
                key=lambda item: self._reservation_sync_order(
                    item,
                    data.listings[item.listing_id],
                    now,
                ),
            ):
                listing = data.listings[reservation.listing_id]
                record = self._records.get(reservation.id, {})
                try:
                    start, end = self._access_window(reservation, listing)
                except (TypeError, ValueError):
                    errors.append("invalid_reservation_time")
                    continue

                if end <= now:
                    reservation.key_code = None
                    cleanup_retry_at = self._retry_at(record, "cleanup")
                    if cleanup_retry_at and cleanup_retry_at > now:
                        next_run = self._earlier(next_run, cleanup_retry_at)
                        continue
                    try:
                        await self._async_retire(reservation.id)
                    except (LoxoneApiError, LoxoneAuthError) as err:
                        self._record_retry_failure(record, "cleanup", now)
                        retry_at = self._retry_at(record, "cleanup")
                        if retry_at:
                            next_run = self._earlier(next_run, retry_at)
                        errors.append(type(err).__name__)
                    continue

                if data_stale:
                    next_run = self._earlier(next_run, end)
                    continue

                record = self._records.setdefault(reservation.id, {})
                record["listing_id"] = reservation.listing_id
                record["access_start"] = start.isoformat()
                record["access_end"] = end.isoformat()
                if field_id is None:
                    record["last_error"] = "custom_field_unavailable"
                    continue
                try:
                    field_changed = await self._async_observe_custom_field(
                        reservation,
                        record,
                        field_id,
                    )
                    if (
                        self._guesty_writes_remaining <= 0
                        and not self._retry_is_deferred(record, "guesty", now)
                        and self._custom_field_write_required(
                            reservation,
                            record,
                        )
                    ):
                        retry_at = self._queue_custom_field_write(record, now)
                        next_run = self._earlier(next_run, retry_at)
                        continue
                    await self._async_ensure_key_code(
                        reservation,
                        record,
                        now,
                        field_id,
                        field_changed=field_changed,
                    )
                except (GuestyApiError, GuestyAuthError) as err:
                    reason = self._guesty_error_reason(err)
                    record["last_error"] = reason
                    self._guesty_writes_remaining = 0
                    self._record_retry_failure(record, "guesty", now)
                    retry_at = self._retry_at(record, "guesty")
                    if retry_at:
                        next_run = self._earlier(next_run, retry_at)
                    errors.append(reason)
                    _LOGGER.warning(
                        "Guesty reservation code synchronization failed for %s: %s",
                        self._reservation_marker(reservation.id),
                        reason,
                    )
                except (RuntimeError, ValueError) as err:
                    record["last_error"] = "code_generation_failed"
                    errors.append(type(err).__name__)
                except (LoxoneApiError, LoxoneAuthError) as err:
                    errors.append(type(err).__name__)
                if record.get("conflict"):
                    errors.append(str(record.get("last_error") or "keycode_conflict"))

                mapping = self._mappings.get(reservation.listing_id, {})
                server_id = mapping.get(CONF_LOXONE_SERVER_ID)
                groups = mapping.get(CONF_LOXONE_GROUP_UUIDS)
                configured_server = self._servers.get(server_id)
                allowed_groups = (
                    {
                        item.get("uuid")
                        for item in configured_server.get(CONF_LOXONE_SERVER_GROUPS, [])
                        if isinstance(item, dict) and isinstance(item.get("uuid"), str)
                    }
                    if isinstance(configured_server, dict)
                    else set()
                )
                if (
                    not isinstance(server_id, str)
                    or configured_server is None
                    or not isinstance(groups, list)
                    or not groups
                    or not all(isinstance(item, str) for item in groups)
                    or not set(groups).issubset(allowed_groups)
                ):
                    record["last_error"] = "invalid_mapping"
                    errors.append("invalid_mapping")
                    continue

                old_server_id = record.get("server_id")
                if old_server_id and old_server_id != server_id:
                    cleanup_retry_at = self._retry_at(record, "cleanup")
                    if cleanup_retry_at and cleanup_retry_at > now:
                        next_run = self._earlier(next_run, cleanup_retry_at)
                        continue
                    try:
                        await self._async_delete_remote_user(record)
                    except (LoxoneApiError, LoxoneAuthError) as err:
                        self._record_retry_failure(record, "cleanup", now)
                        retry_at = self._retry_at(record, "cleanup")
                        if retry_at:
                            next_run = self._earlier(next_run, retry_at)
                        errors.append(type(err).__name__)
                        continue
                    self._clear_retry(record, "cleanup")
                record["server_id"] = server_id
                record[_SERVER_SNAPSHOT_KEY] = self._server_snapshot(configured_server)

                lead = timedelta(
                    minutes=int(
                        self.entry.options.get(
                            CONF_LOXONE_PROVISION_LEAD_MINUTES,
                            DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
                        )
                    )
                )
                provision_at = start - lead
                if now < provision_at:
                    next_run = self._earlier(next_run, provision_at)
                else:
                    if (
                        record.get("conflict")
                        and record.get("last_error") == "code_conflict"
                        and not self._retry_is_deferred(record, "loxone", now)
                    ):
                        record["conflict"] = False
                    if record.get("field_synced") and not record.get("conflict"):
                        if not self._retry_is_deferred(record, "loxone", now):
                            try:
                                await self._async_provision_with_collision_rotation(
                                    reservation,
                                    record,
                                    groups,
                                    start,
                                    end,
                                    now,
                                    field_id,
                                )
                            except LoxoneCodeConflictError:
                                record["conflict"] = True
                                record["last_error"] = "code_conflict"
                                self._record_retry_failure(record, "loxone", now)
                                errors.append("code_conflict")
                            except (GuestyApiError, GuestyAuthError) as err:
                                reason = self._guesty_error_reason(err)
                                self._record_retry_failure(record, "guesty", now)
                                record["last_error"] = reason
                                self._guesty_writes_remaining = 0
                                errors.append(reason)
                            except (LoxoneApiError, LoxoneAuthError) as err:
                                self._record_retry_failure(record, "loxone", now)
                                record["last_error"] = type(err).__name__
                                errors.append(type(err).__name__)
                    retry_at = self._retry_at(record, "loxone")
                    if retry_at:
                        next_run = self._earlier(next_run, retry_at)
                next_run = self._earlier(next_run, end)

                guesty_retry_at = self._retry_at(record, "guesty")
                if guesty_retry_at:
                    next_run = self._earlier(next_run, guesty_retry_at)
                    if record.get("last_error") != _GUESTY_SYNC_QUEUED:
                        errors.append(
                            str(record.get("last_error") or "guesty_sync_retry_pending")
                        )
                cleanup_retry_at = self._retry_at(record, "cleanup")
                if cleanup_retry_at:
                    next_run = self._earlier(next_run, cleanup_retry_at)

            await self._storage.async_save(self._data)
            self._schedule_at(next_run)
            self._last_reconcile_at = now.isoformat()
            self._last_result = "ok" if not errors else "partial"
            self._last_error = errors[0] if errors else None
            self._notify_listeners()

    async def _async_custom_field_id(self) -> str:
        """Resolve and cache the configurable Guesty reservation field."""
        reference = str(
            self.entry.options.get(CONF_LOXONE_CUSTOM_FIELD)
            or DEFAULT_LOXONE_CUSTOM_FIELD
        ).strip()
        if not reference:
            raise GuestyApiError("Guesty PIN custom field is not configured")

        cached = self._data.get("resolved_field")
        if (
            isinstance(cached, dict)
            and cached.get("reference") == reference
            and is_safe_resource_id(cached.get("id"))
        ):
            return cached["id"]

        field_id = await self._client.async_resolve_custom_field(reference)
        self._data["resolved_field"] = {"reference": reference, "id": field_id}
        await self._storage.async_save(self._data)
        return field_id

    def _guesty_write_budget(self) -> int:
        """Return a small write budget while reserving normal API capacity."""
        remaining = getattr(self._client, "last_rate_limit_remaining", None)
        if not isinstance(remaining, int):
            return _GUESTY_FIELD_WRITE_BATCH_SIZE
        # A verified write normally uses one PUT and one GET. Keep several
        # requests available for reservation/webhook traffic and still allow a
        # single probe after Guesty's allowance has reset.
        capacity = max(1, (remaining - 4) // 2)
        return min(_GUESTY_FIELD_WRITE_BATCH_SIZE, capacity)

    def _reservation_sync_order(
        self,
        reservation: GuestyReservation,
        listing: GuestyListing,
        now: datetime,
    ) -> tuple[int, datetime, str]:
        """Prioritize current and nearest stays during a bulk migration."""
        try:
            start, _end = self._access_window(reservation, listing)
        except (TypeError, ValueError):
            start = datetime.max.replace(tzinfo=dt_util.UTC)
        return (0 if start <= now else 1, start, reservation.id)

    def _custom_field_write_required(
        self,
        reservation: GuestyReservation,
        record: Mapping[str, Any],
    ) -> bool:
        """Return whether reconciliation is expected to write Guesty."""
        if not reservation.key_code_observed:
            return False
        remote_code = (
            reservation.key_code.strip()
            if isinstance(reservation.key_code, str)
            else None
        )
        local_code = record.get("code")
        if record.get("replacement_pending"):
            return remote_code != local_code
        if remote_code:
            return not _CODE_PATTERN.fullmatch(
                remote_code
            ) or self._code_is_used_elsewhere(remote_code, reservation.id)
        return True

    def _queue_custom_field_write(
        self,
        record: dict[str, Any],
        now: datetime,
    ) -> datetime:
        """Queue one unsynchronized field without exponential error backoff."""
        self._clear_retry(record, "guesty")
        retry_at = now + timedelta(seconds=_GUESTY_FIELD_QUEUE_DELAY_SECONDS)
        record["guesty_retry_at"] = retry_at.isoformat()
        record["last_error"] = _GUESTY_SYNC_QUEUED
        self._last_queued += 1
        return retry_at

    @staticmethod
    def _guesty_error_reason(error: Exception) -> str:
        """Return a stable privacy-safe reason for UI and diagnostics."""
        if isinstance(error, GuestyAuthError):
            return "guesty_authentication_failed"
        if isinstance(error, GuestyPermissionError):
            return "guesty_permission_denied"
        if isinstance(error, GuestyNotFoundError):
            return "guesty_reservation_or_field_not_found"
        if isinstance(error, GuestyRetryableError):
            return "guesty_temporarily_unavailable"
        return "guesty_custom_field_rejected"

    @staticmethod
    def _reservation_marker(reservation_id: str) -> str:
        """Return a non-reversible marker for safe operational logging."""
        return hashlib.sha256(reservation_id.encode()).hexdigest()[:12]

    @staticmethod
    def _hydrate_observed_custom_fields(
        reservations: Collection[GuestyReservation],
        field_id: str,
    ) -> None:
        """Select the configured value from already-fetched reservation data."""
        for reservation in reservations:
            if not reservation.custom_fields_observed:
                continue
            value = reservation.custom_fields.get(field_id)
            reservation.key_code = str(value).strip() if value is not None else None
            reservation.key_code_observed = True

    async def _async_observe_custom_field(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        field_id: str,
    ) -> bool:
        """Observe the authoritative field, reusing the shared reservation fetch."""
        field_changed = record.get("field_id") != field_id
        if not reservation.key_code_observed:
            source_version = reservation.last_updated_at
            if (
                not field_changed
                and record.get("field_synced")
                and isinstance(source_version, str)
                and record.get("source_last_updated_at") == source_version
                and not record.get("replacement_pending")
            ):
                # The normal Guesty poll strips private values before disk cache.
                # An unchanged reservation version means the private stored code
                # is still current, so no per-reservation API request is needed.
                return False

            value = await self._client.async_get_reservation_custom_field(
                reservation.id,
                field_id,
            )
            reservation.key_code = str(value).strip() if value is not None else None
            reservation.key_code_observed = True

        record["source_last_updated_at"] = reservation.last_updated_at
        return field_changed

    async def _async_ensure_key_code(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        field_id: str,
        *,
        field_changed: bool,
    ) -> None:
        """Use a valid unique Guesty custom-field code or replace a duplicate."""
        local_code = record.get("code")
        remote_observed = bool(reservation.key_code_observed)
        remote_code = (
            reservation.key_code.strip()
            if isinstance(reservation.key_code, str)
            else None
        )

        # Cached reservations deliberately do not contain custom-field codes.
        # Absence is authoritative only when this reservation field was read;
        # otherwise preserving the private record prevents unnecessary churn.
        if not remote_observed and not record.get("replacement_pending"):
            return

        if record.get("replacement_pending"):
            rejected_code = record.get("replacement_rejected_code")
            if (
                remote_code
                and _CODE_PATTERN.fullmatch(remote_code)
                and remote_code not in {local_code, rejected_code}
                and not self._code_is_used_elsewhere(remote_code, reservation.id)
            ):
                await self._async_adopt_guesty_code(record, remote_code, field_id)
                return
            await self._async_publish_replacement_code(
                reservation,
                record,
                now,
                field_id,
            )
            return

        if remote_code:
            if not _CODE_PATTERN.fullmatch(remote_code):
                await self._async_replace_invalid_or_missing_guesty_code(
                    reservation,
                    record,
                    now,
                    field_id,
                    rejected_code=remote_code,
                )
                return
            if self._code_is_used_elsewhere(remote_code, reservation.id):
                await self._async_rotate_duplicate_code(
                    reservation,
                    record,
                    now,
                    field_id,
                    rejected_code=remote_code,
                )
                return
            if local_code != remote_code:
                await self._async_adopt_guesty_code(record, remote_code, field_id)
                return
            record["field_synced"] = True
            record["field_id"] = field_id
            self._clear_retry(record, "guesty")
            last_error = record.get("last_error")
            if isinstance(last_error, str) and (
                last_error.startswith("guesty_")
                or last_error == "invalid_existing_keycode"
            ):
                record.pop("conflict", None)
                record.pop("last_error", None)
            return

        if local_code is not None and field_changed:
            if not isinstance(local_code, str) or not _CODE_PATTERN.fullmatch(
                local_code
            ):
                local_code = self._generate_code()
                record["code"] = local_code
                self._last_rotated += 1
            record["field_synced"] = False
            await self._async_write_custom_field(
                reservation,
                record,
                field_id,
                local_code,
            )
            return

        if local_code is not None:
            await self._async_replace_invalid_or_missing_guesty_code(
                reservation,
                record,
                now,
                field_id,
                rejected_code=None,
            )
            return

        if local_code is None:
            legacy_code = (
                reservation.legacy_key_code.strip()
                if isinstance(reservation.legacy_key_code, str)
                else None
            )
            if (
                legacy_code
                and _CODE_PATTERN.fullmatch(legacy_code)
                and not self._code_is_used_elsewhere(legacy_code, reservation.id)
            ):
                local_code = legacy_code
            else:
                local_code = self._generate_code()
                self._last_generated += 1
            record["code"] = local_code
            record["field_synced"] = False
            record["created_at"] = now.isoformat()
            record.pop("conflict", None)
            record.pop("last_error", None)
            await self._storage.async_save(self._data)

        if not isinstance(local_code, str) or not _CODE_PATTERN.fullmatch(local_code):
            record["conflict"] = True
            record["last_error"] = "invalid_local_keycode"
            return
        if record.get("field_synced"):
            return
        if self._retry_is_deferred(record, "guesty", now):
            return

        await self._async_write_custom_field(
            reservation,
            record,
            field_id,
            local_code,
        )
        record.pop("last_error", None)
        self._clear_retry(record, "guesty")
        await self._storage.async_save(self._data)

    async def _async_write_custom_field(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        field_id: str,
        code: str,
    ) -> None:
        """Write and locally confirm the authoritative reservation field."""
        if not _CODE_PATTERN.fullmatch(code):
            raise ValueError("Guesty reservation access code must contain six digits")
        self._guesty_writes_remaining = max(0, self._guesty_writes_remaining - 1)
        self._last_guesty_writes += 1
        await self._client.async_update_reservation_custom_field(
            reservation.id,
            field_id,
            code,
        )
        reservation.key_code = code
        reservation.key_code_observed = True
        reservation.custom_fields[field_id] = code
        reservation.custom_fields_observed = True
        record["field_id"] = field_id
        record["field_synced"] = True
        record["source_last_updated_at"] = reservation.last_updated_at
        self._clear_retry(record, "guesty")
        last_error = record.get("last_error")
        if isinstance(last_error, str) and last_error.startswith("guesty_"):
            record.pop("last_error", None)
        await self._storage.async_save(self._data)

    async def _async_replace_invalid_or_missing_guesty_code(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        field_id: str,
        *,
        rejected_code: str | None,
    ) -> None:
        """Fail closed, then replace an explicitly empty or invalid Guesty code."""
        if self._retry_is_deferred(record, "cleanup", now):
            record["conflict"] = True
            record["last_error"] = "source_change_cleanup_failed"
            return
        try:
            await self._async_delete_remote_user(record)
        except (LoxoneApiError, LoxoneAuthError):
            record["conflict"] = True
            record["last_error"] = "source_change_cleanup_failed"
            self._record_retry_failure(record, "cleanup", now)
            await self._storage.async_save(self._data)
            raise
        self._clear_retry(record, "cleanup")
        await self._async_rotate_duplicate_code(
            reservation,
            record,
            now,
            field_id,
            rejected_code=rejected_code,
        )

    async def _async_adopt_guesty_code(
        self,
        record: dict[str, Any],
        remote_code: str,
        field_id: str,
    ) -> None:
        """Adopt one valid, unique Guesty code and mark Loxone for update."""
        record["code"] = remote_code
        record["field_synced"] = True
        record["field_id"] = field_id
        record["code_set"] = False
        for key in (
            "provisioned_at",
            "conflict",
            "last_error",
            "replacement_pending",
            "replacement_rejected_code",
        ):
            record.pop(key, None)
        self._clear_retry(record, "guesty")
        self._clear_retry(record, "loxone")
        await self._storage.async_save(self._data)

    async def _async_rotate_duplicate_code(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        field_id: str,
        *,
        rejected_code: str | None,
    ) -> None:
        """Generate, persist, and publish a replacement for a duplicate code."""
        replacement = self._generate_code()
        if rejected_code is not None and replacement == rejected_code:
            raise RuntimeError("Generated replacement matched the rejected code")
        record["code"] = replacement
        record["field_synced"] = False
        record["code_set"] = False
        record["replacement_pending"] = True
        if rejected_code is None:
            record.pop("replacement_rejected_code", None)
        else:
            record["replacement_rejected_code"] = rejected_code
        record.pop("provisioned_at", None)
        record.pop("conflict", None)
        record.pop("last_error", None)
        self._clear_retry(record, "guesty")
        self._clear_retry(record, "loxone")
        self._last_rotated += 1
        await self._storage.async_save(self._data)
        await self._async_publish_replacement_code(
            reservation,
            record,
            now,
            field_id,
        )

    async def _async_publish_replacement_code(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        field_id: str,
    ) -> None:
        """Finish a crash-safe Guesty write for a generated replacement code."""
        code = record.get("code")
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            raise ValueError("Invalid pending replacement code")
        if reservation.key_code != code:
            if self._retry_is_deferred(record, "guesty", now):
                return
            await self._async_write_custom_field(
                reservation,
                record,
                field_id,
                code,
            )
        record["field_synced"] = True
        record["field_id"] = field_id
        record.pop("replacement_pending", None)
        record.pop("replacement_rejected_code", None)
        record.pop("last_error", None)
        self._clear_retry(record, "guesty")
        await self._storage.async_save(self._data)

    async def _async_provision_with_collision_rotation(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        groups: list[str],
        start: datetime,
        end: datetime,
        now: datetime,
        field_id: str,
    ) -> None:
        """Provision Loxone, rotating Guesty immediately after code collisions."""
        for attempt in range(_MAX_CODE_ROTATIONS_PER_RECONCILE):
            try:
                await self._async_provision(
                    reservation,
                    record,
                    groups,
                    start,
                    end,
                )
                return
            except LoxoneCodeConflictError:
                rejected_code = record.get("code")
                if not isinstance(rejected_code, str):
                    raise
                await self._async_rotate_duplicate_code(
                    reservation,
                    record,
                    now,
                    field_id,
                    rejected_code=rejected_code,
                )
                if attempt == _MAX_CODE_ROTATIONS_PER_RECONCILE - 1:
                    raise

    async def _async_provision(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        groups: list[str],
        start: datetime,
        end: datetime,
    ) -> None:
        """Create/update one Loxone user only near its access window."""
        code = record.get("code")
        server_id = record.get("server_id")
        if not isinstance(code, str) or not isinstance(server_id, str):
            return
        client = self._loxone_client(server_id)
        user_id = self._user_id(reservation.id)
        display_name = self._display_name(reservation)
        fingerprint = self._fingerprint(
            server_id,
            display_name,
            groups,
            start,
            end,
        )
        user_uuid = record.get("user_uuid")
        if not isinstance(user_uuid, str):
            user_uuid = None

        if record.get("collision_cleanup_pending") and user_uuid is not None:
            await client.async_delete_user(user_uuid)
            for key in ("user_uuid", "fingerprint", "code_set"):
                record.pop(key, None)
            record.pop("collision_cleanup_pending", None)
            await self._storage.async_save(self._data)
            raise LoxoneCodeConflictError("Cleaned up a non-unique Loxone code")

        if user_uuid is None and record.get("create_started"):
            recovered = await client.async_find_user_by_userid(user_id)
            if isinstance(recovered, dict) and isinstance(recovered.get("uuid"), str):
                user_uuid = recovered["uuid"]
                record["user_uuid"] = user_uuid
                await self._storage.async_save(self._data)

        if user_uuid is None or record.get("fingerprint") != fingerprint:
            record["create_started"] = True
            await self._storage.async_save(self._data)
            try:
                user_uuid = await client.async_add_or_update_user(
                    user_uuid=user_uuid,
                    name=display_name,
                    user_id=user_id,
                    group_uuids=groups,
                    valid_from=start,
                    valid_until=end,
                )
            except LoxoneApiError as err:
                # expirationAction may already have removed a user before an
                # updated reservation arrives. Recover by stable userid before
                # creating a replacement without changing the current Guesty PIN.
                if user_uuid is None or err.code != 500:
                    raise
                record.pop("user_uuid", None)
                record.pop("fingerprint", None)
                await self._storage.async_save(self._data)
                recovered = await client.async_find_user_by_userid(user_id)
                recovered_uuid = (
                    recovered.get("uuid") if isinstance(recovered, dict) else None
                )
                user_uuid = await client.async_add_or_update_user(
                    user_uuid=(
                        recovered_uuid if isinstance(recovered_uuid, str) else None
                    ),
                    name=display_name,
                    user_id=user_id,
                    group_uuids=groups,
                    valid_from=start,
                    valid_until=end,
                )
            record["user_uuid"] = user_uuid
            record["fingerprint"] = fingerprint
            record["code_set"] = False
            await self._storage.async_save(self._data)

        if not record.get("code_set"):
            try:
                await client.async_set_access_code(user_uuid, code)
            except LoxoneCodeConflictError:
                record["collision_cleanup_pending"] = True
                await self._storage.async_save(self._data)
                await client.async_delete_user(user_uuid)
                record.pop("user_uuid", None)
                record.pop("fingerprint", None)
                record["code_set"] = False
                record.pop("collision_cleanup_pending", None)
                await self._storage.async_save(self._data)
                raise
            record["code_set"] = True
        record["provisioned_at"] = dt_util.utcnow().isoformat()
        record.pop("last_error", None)
        self._clear_retry(record, "loxone")
        self._last_provisioned += 1

    async def _async_retire(self, reservation_id: str) -> None:
        """Remove the plaintext PIN first, then delete the remote user."""
        record = self._records.get(reservation_id)
        if not isinstance(record, dict):
            return
        record.pop("code", None)
        record["retired"] = True
        await self._storage.async_save(self._data)
        await self._async_delete_remote_user(record)
        self._records.pop(reservation_id, None)
        await self._storage.async_save(self._data)
        self._last_deleted += 1

    async def _async_delete_remote_user(self, record: dict[str, Any]) -> None:
        """Delete a remote user, retaining a code-free tombstone on failure."""
        user_uuid = record.get("user_uuid")
        server_id = record.get("server_id")
        if isinstance(user_uuid, str) and isinstance(server_id, str):
            snapshot = record.get(_SERVER_SNAPSHOT_KEY)
            await self._loxone_client(
                server_id,
                snapshot if isinstance(snapshot, dict) else None,
            ).async_delete_user(user_uuid)
        for key in (
            "user_uuid",
            "fingerprint",
            "code_set",
            "create_started",
            "provisioned_at",
            "collision_cleanup_pending",
        ):
            record.pop(key, None)

    def _loxone_client(
        self,
        server_id: str,
        server_fallback: Mapping[str, Any] | None = None,
    ) -> LoxoneApiClient:
        """Return one shared Loxone client per configured server."""
        if server_id in self._clients:
            return self._clients[server_id]
        server = self._servers.get(server_id) or server_fallback
        if not isinstance(server, dict):
            raise LoxoneApiError("Configured Loxone Miniserver no longer exists")
        try:
            client = LoxoneApiClient.from_hass(
                self.hass,
                server[CONF_LOXONE_SERVER_URL],
                server[CONF_LOXONE_SERVER_USERNAME],
                server[CONF_LOXONE_SERVER_PASSWORD],
            )
        except (KeyError, TypeError, ValueError) as err:
            raise LoxoneApiError("Invalid Loxone Miniserver configuration") from err
        self._clients[server_id] = client
        return client

    @staticmethod
    def _server_snapshot(server: Mapping[str, Any]) -> dict[str, str]:
        """Persist only connection fields needed to delete an orphaned user."""
        snapshot: dict[str, str] = {}
        for key in _SERVER_SNAPSHOT_FIELDS:
            value = server.get(key)
            if isinstance(value, str):
                snapshot[key] = value
        return snapshot

    def _generate_code(self) -> str:
        """Generate a strong local six-digit code in the configured namespace."""
        prefix = str(
            self.entry.options.get(
                CONF_LOXONE_CODE_PREFIX,
                DEFAULT_LOXONE_CODE_PREFIX,
            )
        )
        if not prefix.isdigit() or not 1 <= len(prefix) <= 2:
            raise ValueError("Invalid Loxone code prefix")
        existing = {
            record.get("code")
            for record in self._records.values()
            if isinstance(record, dict)
        }
        data = self._coordinator.data
        if data is not None:
            existing.update(
                reservation.key_code.strip()
                for reservation in data.reservations
                if isinstance(reservation.key_code, str)
                and _CODE_PATTERN.fullmatch(reservation.key_code.strip())
            )
        suffix_length = LOXONE_ACCESS_CODE_LENGTH - len(prefix)
        capacity = 10**suffix_length
        start = secrets.randbelow(capacity)
        for offset in range(capacity):
            suffix = str((start + offset) % capacity).zfill(suffix_length)
            code = f"{prefix}{suffix}"
            if code not in existing and code not in _WEAK_CODES:
                return code
        raise RuntimeError("Could not allocate an unused Loxone access code")

    def _code_is_used_elsewhere(self, code: str, reservation_id: str) -> bool:
        """Resolve duplicate ownership without rotating the established owner."""
        local_owners = sorted(
            other_id
            for other_id, record in self._records.items()
            if isinstance(record, dict)
            and record.get("code") == code
            and not record.get("retired")
        )
        if local_owners:
            return reservation_id not in local_owners or (
                len(local_owners) > 1 and reservation_id != local_owners[0]
            )
        data = self._coordinator.data
        remote_owners = (
            sorted(
                reservation.id
                for reservation in data.reservations
                if reservation.is_active_status()
                and isinstance(reservation.key_code, str)
                and reservation.key_code.strip() == code
            )
            if data is not None
            else []
        )
        return len(remote_owners) > 1 and reservation_id != remote_owners[0]

    def _display_name(self, reservation: GuestyReservation) -> str:
        """Return the opted-in guest name or a privacy-safe booking reference."""
        if self.entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        ):
            guest = " ".join((reservation.guest_name or "Gast").split())[:40]
            marker = hashlib.sha256(reservation.id.encode()).hexdigest()[:8]
            return f"Guesty {guest} [{marker}]"
        booking_id = " ".join(reservation.id.split())[:48]
        return f"Guesty Buchung {booking_id}"

    def _access_window(
        self,
        reservation: GuestyReservation,
        listing: GuestyListing,
    ) -> tuple[datetime, datetime]:
        """Return the configured access window for one reservation."""
        start = reservation.check_in_datetime(listing) - timedelta(
            minutes=int(
                self.entry.options.get(
                    CONF_ACCESS_EARLY_MINUTES,
                    DEFAULT_ACCESS_EARLY_MINUTES,
                )
            )
        )
        end = reservation.check_out_datetime(listing) + timedelta(
            minutes=int(
                self.entry.options.get(
                    CONF_ACCESS_LATE_MINUTES,
                    DEFAULT_ACCESS_LATE_MINUTES,
                )
            )
        )
        return start, end

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Listen for completed Guesty/Loxone reconciliation passes."""
        self._listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._listeners.discard(listener)

        return _remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Refresh status entities after local or remote state changes."""
        for listener in tuple(self._listeners):
            listener()

    def listing_status_snapshot(self, listing_id: str) -> dict[str, Any]:
        """Return privacy-safe Guesty custom-field and Loxone PIN status."""
        if (
            not self.entry.options.get(CONF_LOXONE_ENABLED, False)
            or listing_id not in self._mappings
        ):
            return {
                "guesty_status": "not_configured",
                "loxone_status": "not_configured",
            }

        data = self._coordinator.data
        if data is None:
            return {
                "guesty_status": "error",
                "loxone_status": "error",
                "data_stale": True,
            }

        now = dt_util.utcnow()
        listing = data.listings.get(listing_id)
        candidates: list[tuple[int, datetime, datetime, GuestyReservation]] = []
        if listing is not None:
            for reservation in data.reservations:
                if (
                    reservation.listing_id != listing_id
                    or not reservation.is_active_status()
                ):
                    continue
                try:
                    start, end = self._access_window(reservation, listing)
                except (TypeError, ValueError):
                    continue
                if end <= now:
                    continue
                priority = 0 if start <= now < end else 1
                candidates.append((priority, start, end, reservation))

        listing_cleanup_pending = any(
            isinstance(record, dict)
            and record.get("listing_id") == listing_id
            and (
                record.get("retired")
                or record.get("collision_cleanup_pending")
                or record.get("cleanup_retry_at")
            )
            for record in self._records.values()
        )
        if not candidates:
            return {
                "guesty_status": "no_reservation",
                "loxone_status": (
                    "cleanup_pending" if listing_cleanup_pending else "no_reservation"
                ),
                "data_stale": bool(getattr(data, "data_stale", False)),
            }

        def _candidate_issue_priority(
            item: tuple[int, datetime, datetime, GuestyReservation],
        ) -> int:
            """Surface any booking error before a healthy current/next booking."""
            record = self._records.get(item[3].id)
            if not isinstance(record, dict):
                return 2
            last_error = record.get("last_error")
            if (
                last_error
                in {
                    "code_generation_failed",
                    "custom_field_unavailable",
                    "invalid_mapping",
                    "source_change_cleanup_failed",
                }
                or (
                    self._retry_at(record, "guesty") is not None
                    and last_error != _GUESTY_SYNC_QUEUED
                )
                or self._retry_at(record, "loxone") is not None
                or self._retry_at(record, "cleanup") is not None
            ):
                return 0
            if record.get("conflict") or record.get("collision_cleanup_pending"):
                return 1
            return 2

        _priority, start, end, reservation = min(
            candidates,
            key=lambda item: (
                _candidate_issue_priority(item),
                item[0],
                item[1],
                item[3].id,
            ),
        )
        lead = timedelta(
            minutes=int(
                self.entry.options.get(
                    CONF_LOXONE_PROVISION_LEAD_MINUTES,
                    DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
                )
            )
        )
        provision_at = start - lead
        record = self._records.get(reservation.id)
        snapshot: dict[str, Any] = {
            "guesty_status": "pending",
            "loxone_status": "scheduled" if now < provision_at else "pending",
            "access_start": start,
            "access_end": end,
            "provision_at": provision_at,
            "reservation_status": reservation.status,
            "data_stale": bool(getattr(data, "data_stale", False)),
            "field_synced": False,
            "loxone_user_created": False,
        }
        if not isinstance(record, dict) or record.get("retired"):
            return snapshot

        last_error = record.get("last_error")
        guesty_conflict = bool(record.get("conflict")) and last_error in {
            "guesty_keycode_changed",
            "invalid_existing_keycode",
            "invalid_local_keycode",
        }
        field_synced = bool(record.get("field_synced"))
        snapshot["field_synced"] = field_synced
        guesty_retry_at = self._retry_at(record, "guesty")
        if guesty_retry_at is not None:
            snapshot["retry_at"] = guesty_retry_at
        if isinstance(last_error, str) and last_error != _GUESTY_SYNC_QUEUED:
            snapshot["error_reason"] = last_error
        if guesty_conflict:
            snapshot["guesty_status"] = "conflict"
        elif (
            guesty_retry_at is not None and last_error != _GUESTY_SYNC_QUEUED
        ) or last_error in {
            "code_generation_failed",
            "custom_field_unavailable",
            "source_change_cleanup_failed",
        }:
            snapshot["guesty_status"] = "error"
        elif field_synced:
            snapshot["guesty_status"] = "synced"

        remote_ready = bool(record.get("user_uuid") and record.get("code_set"))
        snapshot["loxone_user_created"] = remote_ready
        if listing_cleanup_pending or (
            record.get("collision_cleanup_pending")
            or record.get("retired")
            or self._retry_at(record, "cleanup") is not None
        ):
            snapshot["loxone_status"] = "cleanup_pending"
        elif last_error == "code_conflict":
            snapshot["loxone_status"] = "conflict"
        elif (
            last_error == "invalid_mapping"
            or self._retry_at(record, "loxone") is not None
        ):
            snapshot["loxone_status"] = "error"
        elif remote_ready:
            snapshot["loxone_status"] = "provisioned"
        return snapshot

    @staticmethod
    def _user_id(reservation_id: str) -> str:
        """Return a stable, non-secret NFC permission identifier."""
        return f"guesty-{hashlib.sha256(reservation_id.encode()).hexdigest()[:20]}"

    @staticmethod
    def _fingerprint(
        server_id: str,
        name: str,
        groups: list[str],
        start: datetime,
        end: datetime,
    ) -> str:
        """Fingerprint every remote property that requires an update."""
        value = "\0".join(
            (server_id, name, *sorted(groups), start.isoformat(), end.isoformat())
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def diagnostics(self) -> dict[str, Any]:
        """Return a privacy-safe operational summary without PINs or names."""
        records = self._records
        return {
            "enabled": bool(self.entry.options.get(CONF_LOXONE_ENABLED, False)),
            "configured_miniservers": len(self._servers),
            "mapped_listings": len(self._mappings),
            "last_reconcile_at": self._last_reconcile_at,
            "last_reconcile_result": self._last_result,
            "has_last_error": self._last_error is not None,
            "last_error": self._last_error,
            "generated_during_last_reconcile": self._last_generated,
            "rotated_during_last_reconcile": self._last_rotated,
            "provisioned_during_last_reconcile": self._last_provisioned,
            "deleted_during_last_reconcile": self._last_deleted,
            "guesty_writes_during_last_reconcile": self._last_guesty_writes,
            "queued_during_last_reconcile": self._last_queued,
            "local_records": len(records),
            "custom_field_codes_synced": sum(
                1
                for record in records.values()
                if isinstance(record, dict) and record.get("field_synced")
            ),
            "custom_field_codes_pending": sum(
                1
                for record in records.values()
                if isinstance(record, dict)
                and not record.get("retired")
                and not record.get("field_synced")
            ),
            "custom_field_codes_queued": sum(
                1
                for record in records.values()
                if isinstance(record, dict)
                and record.get("last_error") == _GUESTY_SYNC_QUEUED
            ),
            "custom_field_code_failures": sum(
                1
                for record in records.values()
                if isinstance(record, dict)
                and self._retry_at(record, "guesty") is not None
                and record.get("last_error") != _GUESTY_SYNC_QUEUED
            ),
            "remote_users": sum(
                1
                for record in records.values()
                if isinstance(record, dict) and record.get("user_uuid")
            ),
            "conflicts": sum(
                1
                for record in records.values()
                if isinstance(record, dict) and record.get("conflict")
            ),
        }

    def _schedule_at(self, moment: datetime | None) -> None:
        """Schedule the next exact provisioning, retry, or checkout transition."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if moment is None or self._unloaded:
            return
        now = dt_util.utcnow()
        if moment <= now:
            moment = now + timedelta(seconds=1)

        @callback
        def _run(_now: datetime) -> None:
            self._cancel_timer = None
            self.async_schedule_reconcile()

        self._cancel_timer = async_track_point_in_utc_time(self.hass, _run, moment)

    @staticmethod
    def _earlier(current: datetime | None, candidate: datetime) -> datetime:
        """Return the earlier datetime."""
        return candidate if current is None or candidate < current else current

    @staticmethod
    def _retry_is_deferred(
        record: Mapping[str, Any], operation: str, now: datetime
    ) -> bool:
        """Return whether a persistent operation backoff is active."""
        retry_at = GuestyLoxoneManager._retry_at(record, operation)
        return retry_at is not None and retry_at > now

    @staticmethod
    def _retry_at(record: Mapping[str, Any], operation: str) -> datetime | None:
        """Parse one retry timestamp."""
        value = record.get(f"{operation}_retry_at")
        if not isinstance(value, str):
            return None
        try:
            return dt_util.parse_datetime(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _record_retry_failure(
        record: dict[str, Any], operation: str, now: datetime
    ) -> None:
        """Persist bounded exponential backoff."""
        count_key = f"{operation}_retry_count"
        try:
            count = min(max(int(record.get(count_key, 0)), 0) + 1, 20)
        except (TypeError, ValueError):
            count = 1
        delay = min(
            LOXONE_RETRY_BASE_SECONDS * (2 ** (count - 1)),
            LOXONE_RETRY_MAX_SECONDS,
        )
        record[count_key] = count
        record[f"{operation}_retry_at"] = (now + timedelta(seconds=delay)).isoformat()

    @staticmethod
    def _clear_retry(record: dict[str, Any], operation: str) -> None:
        """Clear operation backoff after success."""
        record.pop(f"{operation}_retry_count", None)
        record.pop(f"{operation}_retry_at", None)


async def async_remove_stored_loxone_users(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Remove managed users and retain retryable tombstones after failures."""
    storage = GuestyLoxoneStorage(hass, entry.entry_id)
    data = await storage.async_load()
    records = data.get("records", {})
    raw_servers = entry.options.get(CONF_LOXONE_MINISERVERS, [])
    servers = (
        {
            item.get(CONF_LOXONE_SERVER_ID): item
            for item in raw_servers
            if isinstance(item, dict)
            and isinstance(item.get(CONF_LOXONE_SERVER_ID), str)
        }
        if isinstance(raw_servers, list)
        else {}
    )
    clients: dict[str, LoxoneApiClient] = {}
    cleanup_complete = True
    if isinstance(records, dict):
        # Never retain PIN plaintext after the integration is removed, even if
        # one Miniserver is currently unreachable.
        for record in records.values():
            if isinstance(record, dict):
                record.pop("code", None)
                record["retired"] = True
        await storage.async_save(data)

        for reservation_id, record in list(records.items()):
            if not isinstance(record, dict):
                continue
            user_uuid = record.get("user_uuid")
            server_id = record.get("server_id")
            snapshot = record.get(_SERVER_SNAPSHOT_KEY)
            server = servers.get(server_id) or (
                snapshot if isinstance(snapshot, dict) else None
            )
            if not isinstance(user_uuid, str):
                records.pop(reservation_id, None)
                await storage.async_save(data)
                continue
            if not isinstance(server_id, str) or not isinstance(server, dict):
                cleanup_complete = False
                continue
            try:
                client = clients.get(server_id)
                if client is None:
                    client = LoxoneApiClient.from_hass(
                        hass,
                        server[CONF_LOXONE_SERVER_URL],
                        server[CONF_LOXONE_SERVER_USERNAME],
                        server[CONF_LOXONE_SERVER_PASSWORD],
                    )
                    clients[server_id] = client
                await client.async_delete_user(user_uuid)
            except (KeyError, TypeError, ValueError, LoxoneApiError):
                cleanup_complete = False
                _LOGGER.warning(
                    "Could not remove a managed Loxone guest during integration removal"
                )
            else:
                records.pop(reservation_id, None)
                await storage.async_save(data)
    if cleanup_complete and not records:
        await storage.async_remove()
    else:
        await storage.async_save(data)
    return cleanup_complete and not records
