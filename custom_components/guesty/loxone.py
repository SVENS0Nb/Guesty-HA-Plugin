"""Reservation-driven, time-limited Loxone PIN provisioning."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
    GuestyKeyCodeWriteResult,
    GuestyKeyCodeUnavailableError,
    GuestyNotFoundError,
    GuestyPermissionError,
    GuestyRetryableError,
    KEYCODE_WRITE_ROUTE_V2,
    KEYCODE_WRITE_ROUTE_V3,
    KEYCODE_WRITE_ROUTES,
    is_safe_resource_id,
)
from .const import (
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_LATE_MINUTES,
    CONF_CLIENT_ID,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_GUESTY_CODE_SUFFIXES,
    CONF_LOXONE_CODE_PREFIX,
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
    CONF_PIN_CUSTOM_FIELD,
    CONF_PIN_CUSTOM_ENABLED,
    CONF_PIN_NATIVE_ENABLED,
    CONF_PIN_OFFLINE_PROVISIONING,
    CONF_SCAN_INTERVAL,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    DEFAULT_ACCESS_EARLY_MINUTES,
    DEFAULT_ACCESS_LATE_MINUTES,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_GUESTY_CODE_SUFFIX,
    DEFAULT_LOXONE_CODE_PREFIX,
    DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
    DEFAULT_PIN_CUSTOM_FIELD,
    DEFAULT_PIN_CUSTOM_ENABLED,
    DEFAULT_PIN_NATIVE_ENABLED,
    DEFAULT_PIN_OFFLINE_PROVISIONING,
    DEFAULT_SCAN_INTERVAL,
    GUESTY_CODE_SUFFIX_MAX_LENGTH,
    LEGACY_CONF_LOXONE_CUSTOM_FIELD,
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
from .models import GuestyListing, GuestyReservation, reservation_log_marker

_LOGGER = logging.getLogger(__name__)

LOXONE_STORAGE_KEY = "guesty_loxone"
_GUESTY_KEYCODE_WRITE_BATCH_SIZE = 2
_GUESTY_KEYCODE_QUEUE_DELAY_SECONDS = 30
_GUESTY_MIN_WRITE_SPACING_SECONDS = 0
_GUESTY_RATE_LIMIT_RESERVE = 4
_GUESTY_REQUESTS_PER_WRITE_SLOT = 4
_GUESTY_WRITE_ATTEMPTS_KEY = "guesty_write_attempts"
_LEGACY_GUESTY_KEYCODE_WRITE_ROUTE_KEY = "guesty_keycode_write_route"
_GUESTY_SYNC_QUEUED = "guesty_sync_queued"
_WEBHOOK_PIN_FIRST_WRITE_DELAY = timedelta(minutes=1)
_WEBHOOK_PIN_FAST_RETRY_WINDOW = timedelta(minutes=5)
_WEBHOOK_PIN_RETRY_INTERVAL = timedelta(minutes=1)
_WEBHOOK_PIN_RECEIVED_AT_KEY = "webhook_pin_received_at"
_WEBHOOK_PIN_FIRST_WRITE_AT_KEY = "webhook_pin_first_write_at"
_WEBHOOK_PIN_FAST_RETRY_UNTIL_KEY = "webhook_pin_fast_retry_until"
_WEBHOOK_PIN_FAST_FAILURES_KEY = "webhook_pin_fast_failures"
_GUESTY_KEYCODE_SOURCE = "notes.keyCode"
_GUESTY_NATIVE_WRITE_ROUTE_KEY = "native_write_route"
_GUESTY_NATIVE_OPERATION = "guesty_native"
_GUESTY_CUSTOM_OPERATION = "guesty_custom"
_PIN_FIELD_RESOLVE_OPERATION = "pin_field_resolve"
_PIN_STATE_SCHEMA_VERSION_KEY = "pin_state_schema_version"
_PIN_STATE_SCHEMA_VERSION = 2
_RESOLVED_PIN_FIELD_KEY = "resolved_pin_field"
_GUESTY_RETRY_STATE_VERSION_KEY = "guesty_retry_state_version"
_GUESTY_RETRY_STATE_VERSION = 4
_GUESTY_CLIENT_FINGERPRINT_KEY = "guesty_client_fingerprint"
_LEGACY_CUSTOM_FIELD_ERRORS = {
    "custom_field_unavailable",
    "guesty_custom_field_rejected",
    "guesty_reservation_or_field_not_found",
}
_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
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


class _GuestyWriteDeferred(Exception):
    """Signal that the persistent global Guesty write limit is full."""

    def __init__(self, retry_at: datetime) -> None:
        """Initialize the internal control-flow signal."""
        super().__init__("Guesty Keycode write deferred")
        self.retry_at = retry_at


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
            return {"records": {}}
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        else:
            data["records"] = {
                str(reservation_id): record
                for reservation_id, record in data["records"].items()
                if isinstance(reservation_id, str) and isinstance(record, dict)
            }
            for record in data["records"].values():
                if record.get("last_error") in _LEGACY_CUSTOM_FIELD_ERRORS:
                    record.pop("last_error", None)
                    record.pop("guesty_retry_at", None)
                    record.pop("guesty_retry_count", None)
        # Versions 1.8.x through 2.1.x cached a reservation custom-field
        # definition here. Native notes.keyCode needs no account-level lookup.
        data.pop("resolved_field", None)
        return data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Persist state."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Delete all local state."""
        await self._store.async_remove()


class GuestyLoxoneManager:
    """Synchronize Guesty's PIN mirrors with short-lived lock credentials."""

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

    @property
    def _ttlock_mappings(self) -> dict[str, list[int]]:
        """Return TTLock mappings used to select shared Guesty PIN bookings."""
        raw = self.entry.options.get(CONF_TTLOCK_LISTING_MAPPINGS, {})
        return raw if isinstance(raw, dict) else {}

    @property
    def _pin_listing_ids(self) -> set[str]:
        """Return listings served by at least one enabled PIN provider."""
        listing_ids: set[str] = set()
        if self.entry.options.get(CONF_LOXONE_ENABLED, False):
            listing_ids.update(self._mappings)
        if self.entry.options.get(CONF_TTLOCK_ENABLED, False):
            listing_ids.update(self._ttlock_mappings)
        return listing_ids

    def _listing_uses_loxone(self, listing_id: str) -> bool:
        """Return whether this listing retains the existing Loxone behavior."""
        return bool(
            self.entry.options.get(CONF_LOXONE_ENABLED, False)
            and listing_id in self._mappings
        )

    @property
    def _native_pin_enabled(self) -> bool:
        """Return whether native Guesty Keycode participates in PIN sync."""
        return bool(
            self.entry.options.get(
                CONF_PIN_NATIVE_ENABLED,
                DEFAULT_PIN_NATIVE_ENABLED,
            )
        )

    @property
    def _custom_pin_enabled(self) -> bool:
        """Return whether the configurable Guesty PIN field participates."""
        return bool(
            self.entry.options.get(
                CONF_PIN_CUSTOM_ENABLED,
                DEFAULT_PIN_CUSTOM_ENABLED,
            )
        )

    async def async_setup(self) -> None:
        """Load private state and start one reconciliation pass."""
        self._data = await self._storage.async_load()
        recovered_retries, state_changed = self._migrate_guesty_retry_state()
        state_changed = self._migrate_dual_source_state() or state_changed
        state_changed = self._clear_disabled_pin_source_state() or state_changed
        if state_changed:
            await self._storage.async_save(self._data)
        if recovered_retries:
            _LOGGER.warning(
                "Rescheduled persisted Guesty Keycode retries "
                "count=%s operation=native_keycode_write reason=state_migration",
                recovered_retries,
            )
        else:
            self._log_persisted_guesty_retry_summary()
        self.async_schedule_reconcile()

    def _migrate_dual_source_state(self) -> bool:
        """Upgrade one-source PIN records without rotating confirmed codes."""
        raw_version = self._data.get(_PIN_STATE_SCHEMA_VERSION_KEY, 0)
        version = (
            raw_version
            if isinstance(raw_version, int) and not isinstance(raw_version, bool)
            else 0
        )
        if version >= _PIN_STATE_SCHEMA_VERSION:
            return False

        for record in self._records.values():
            old_last_error = record.get("last_error")
            code = record.get("code")
            if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
                continue
            suffix = record.get("guesty_suffix")
            display = f"{code}{suffix}" if isinstance(suffix, str) else code
            if self._parse_guesty_code(display) != code:
                display = code
            record.setdefault("guesty_display_value", display)

            if record.get("field_synced") is True:
                field_id = record.get("field_id")
                if field_id == _GUESTY_KEYCODE_SOURCE:
                    record.setdefault("native_baseline_value", display)
                    record.setdefault("native_synced", True)
                elif is_safe_resource_id(field_id):
                    record.setdefault("custom_baseline_value", display)
                    record.setdefault("custom_synced", True)
                    record.setdefault("custom_field_id", field_id)

            # Current releases used the aggregate Guesty retry exclusively for
            # notes.keyCode. Preserve that backoff under the native source.
            for suffix_key in ("retry_at", "retry_count"):
                old_key = f"guesty_{suffix_key}"
                new_key = f"{_GUESTY_NATIVE_OPERATION}_{suffix_key}"
                if old_key in record and new_key not in record:
                    record[new_key] = record[old_key]
            if (
                isinstance(record.get("last_error"), str)
                and record.get("last_error") != _GUESTY_SYNC_QUEUED
            ):
                record.setdefault("native_last_error", record["last_error"])
            self._refresh_guesty_aggregate_state(record)
            if old_last_error == _GUESTY_SYNC_QUEUED and not record.get("field_synced"):
                record["last_error"] = _GUESTY_SYNC_QUEUED

        self._data[_PIN_STATE_SCHEMA_VERSION_KEY] = _PIN_STATE_SCHEMA_VERSION
        return True

    def _clear_disabled_pin_source_state(self) -> bool:
        """Stop disabled source retries without discarding confirmed baselines."""
        if self._native_pin_enabled and self._custom_pin_enabled:
            return False
        changed = False
        for record in self._records.values():
            before = dict(record)
            for enabled, source, operation in (
                (self._native_pin_enabled, "native", _GUESTY_NATIVE_OPERATION),
                (self._custom_pin_enabled, "custom", _GUESTY_CUSTOM_OPERATION),
            ):
                if enabled:
                    continue
                self._clear_retry(record, operation)
                record.pop(f"{source}_last_error", None)
            self._refresh_guesty_aggregate_state(record)
            changed = record != before or changed
        return changed

    def _guesty_client_fingerprint(self) -> str | None:
        """Return a private stable marker for credential-change recovery."""
        client_id = self.entry.data.get(CONF_CLIENT_ID)
        if not isinstance(client_id, str) or not client_id.strip():
            return None
        return hashlib.sha256(client_id.strip().encode()).hexdigest()

    def _migrate_guesty_retry_state(self) -> tuple[int, bool]:
        """Recover stale retry state once and after Guesty credential changes."""
        raw_version = self._data.get(_GUESTY_RETRY_STATE_VERSION_KEY, 0)
        version = (
            raw_version
            if isinstance(raw_version, int) and not isinstance(raw_version, bool)
            else 0
        )
        current_fingerprint = self._guesty_client_fingerprint()
        previous_fingerprint = self._data.get(_GUESTY_CLIENT_FINGERPRINT_KEY)
        credentials_changed = bool(
            current_fingerprint
            and isinstance(previous_fingerprint, str)
            and previous_fingerprint != current_fingerprint
        )
        recovered = 0
        state_changed = (
            self._data.pop(_LEGACY_GUESTY_KEYCODE_WRITE_ROUTE_KEY, None) is not None
        )
        for record in self._records.values():
            source_operations = (
                ("guesty", "last_error"),
                (_GUESTY_NATIVE_OPERATION, "native_last_error"),
                (_GUESTY_CUSTOM_OPERATION, "custom_last_error"),
            )
            for operation, error_key in source_operations:
                if self._retry_at(record, operation) is None:
                    continue
                last_error = record.get(error_key)
                if operation == "guesty" and not isinstance(last_error, str):
                    last_error = record.get("last_error")
                legacy_reasonless_retry = not last_error
                stale_native_404 = (
                    operation in {"guesty", _GUESTY_NATIVE_OPERATION}
                    and version < _GUESTY_RETRY_STATE_VERSION
                    and last_error
                    in {
                        "guesty_reservation_not_found",
                        "guesty_keycode_rejected",
                        "guesty_keycode_endpoint_unavailable",
                    }
                )
                if (
                    stale_native_404
                    and last_error == "guesty_keycode_endpoint_unavailable"
                ):
                    record[_GUESTY_NATIVE_WRITE_ROUTE_KEY] = KEYCODE_WRITE_ROUTE_V2
                if not (
                    legacy_reasonless_retry or stale_native_404 or credentials_changed
                ):
                    continue
                self._clear_retry(record, operation)
                record[error_key] = _GUESTY_SYNC_QUEUED
                record["last_error"] = _GUESTY_SYNC_QUEUED
                recovered += 1
                state_changed = True

        if version != _GUESTY_RETRY_STATE_VERSION:
            self._data[_GUESTY_RETRY_STATE_VERSION_KEY] = _GUESTY_RETRY_STATE_VERSION
            state_changed = True
        if (
            current_fingerprint is not None
            and previous_fingerprint != current_fingerprint
        ):
            self._data[_GUESTY_CLIENT_FINGERPRINT_KEY] = current_fingerprint
            state_changed = True
        return recovered, state_changed

    def _guesty_retry_summary(self) -> tuple[dict[str, int], datetime | None]:
        """Return bounded safe retry counts and the next retry time."""
        counts: dict[str, int] = {}
        next_retry: datetime | None = None
        enabled_operations = []
        if self._native_pin_enabled:
            enabled_operations.append((_GUESTY_NATIVE_OPERATION, "native_last_error"))
        if self._custom_pin_enabled:
            enabled_operations.append((_GUESTY_CUSTOM_OPERATION, "custom_last_error"))
        for record in self._records.values():
            source_retry_seen = False
            for operation, error_key in enabled_operations:
                retry_at = self._retry_at(record, operation)
                if retry_at is None:
                    continue
                source_retry_seen = True
                raw_reason = record.get(error_key)
                if raw_reason == _GUESTY_SYNC_QUEUED:
                    continue
                reason = (
                    raw_reason
                    if isinstance(raw_reason, str)
                    and re.fullmatch(r"[a-z0-9_]{1,64}", raw_reason)
                    else "unknown"
                )
                counts[reason] = counts.get(reason, 0) + 1
                next_retry = self._earlier(next_retry, retry_at)
            if source_retry_seen:
                continue
            retry_at = self._retry_at(record, "guesty")
            raw_reason = record.get("last_error")
            if retry_at is None or raw_reason == _GUESTY_SYNC_QUEUED:
                continue
            reason = (
                raw_reason
                if isinstance(raw_reason, str)
                and re.fullmatch(r"[a-z0-9_]{1,64}", raw_reason)
                else "unknown"
            )
            counts[reason] = counts.get(reason, 0) + 1
            next_retry = self._earlier(next_retry, retry_at)
        return counts, next_retry

    def _log_persisted_guesty_retry_summary(self) -> None:
        """Make a restored silent retry state visible after startup."""
        counts, next_retry = self._guesty_retry_summary()
        if not counts:
            return
        reasons = ",".join(
            f"{reason}:{count}" for reason, count in sorted(counts.items())
        )
        _LOGGER.warning(
            "Guesty Keycode synchronization is waiting for persisted retries "
            "count=%s reasons=%s next_retry_at=%s",
            sum(counts.values()),
            reasons,
            next_retry.isoformat() if next_retry is not None else "unknown",
        )

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
        self._task = None
        self._pending = False
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
        finally:
            self._task = None
            if self._pending and not self._unloaded:
                self.async_schedule_reconcile()

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
            self._guesty_writes_remaining = self._guesty_write_budget(now)
            next_run: datetime | None = None
            errors: list[str] = []
            data = self._coordinator.data
            data_stale = data is None or bool(getattr(data, "data_stale", False))
            self._clear_disabled_pin_source_state()

            eligible: dict[str, GuestyReservation] = {}
            invalid_active_reservation_ids: set[str] = set()
            listings: dict[str, GuestyListing] = {}
            pin_listing_ids = self._pin_listing_ids
            if pin_listing_ids and data is not None:
                listings = dict(data.listings)
                for reservation in data.reservations:
                    if (
                        not reservation.is_active_status()
                        or reservation.listing_id not in pin_listing_ids
                        or reservation.listing_id not in data.listings
                    ):
                        continue
                    try:
                        _start, end = self._access_window(
                            reservation,
                            data.listings[reservation.listing_id],
                        )
                    except (TypeError, ValueError):
                        invalid_active_reservation_ids.add(reservation.id)
                        continue
                    if end > now:
                        eligible[reservation.id] = reservation

            offline_provisioning = bool(
                self.entry.options.get(
                    CONF_PIN_OFFLINE_PROVISIONING,
                    DEFAULT_PIN_OFFLINE_PROVISIONING,
                )
            )
            if data_stale and offline_provisioning:
                for reservation_id, stored_record in self._records.items():
                    if reservation_id in eligible or stored_record.get("retired"):
                        continue
                    listing_id = stored_record.get("listing_id")
                    if listing_id not in pin_listing_ids:
                        continue
                    raw_reservation = stored_record.get("reservation_snapshot")
                    raw_listing = stored_record.get("listing_snapshot")
                    if not isinstance(raw_reservation, dict) or not isinstance(
                        raw_listing, dict
                    ):
                        continue
                    try:
                        restored_reservation = GuestyReservation.from_dict(
                            raw_reservation
                        )
                        restored_listing = GuestyListing.from_dict(raw_listing)
                    except (KeyError, TypeError, ValueError):
                        continue
                    stored_end = dt_util.parse_datetime(
                        str(stored_record.get("access_end", ""))
                    )
                    if stored_end is None or stored_end <= now:
                        continue
                    eligible[reservation_id] = restored_reservation
                    listings[listing_id] = restored_listing

            custom_field_id: str | None = None
            custom_field_error: str | None = None
            if eligible and not data_stale and self._custom_pin_enabled:
                reference = self._pin_custom_field_reference()
                if self._data.get("pin_field_retry_reference") != reference:
                    self._clear_retry(self._data, _PIN_FIELD_RESOLVE_OPERATION)
                    self._data["pin_field_retry_reference"] = reference
                    self._data.pop("pin_field_last_error", None)
                resolve_retry_at = self._retry_at(
                    self._data,
                    _PIN_FIELD_RESOLVE_OPERATION,
                )
                if resolve_retry_at is not None and resolve_retry_at > now:
                    custom_field_error = str(
                        self._data.get(
                            "pin_field_last_error",
                            "guesty_custom_field_unavailable",
                        )
                    )
                    next_run = self._earlier(next_run, resolve_retry_at)
                    errors.append(custom_field_error)
                else:
                    try:
                        custom_field_id = await self._async_pin_custom_field_id()
                    except (GuestyApiError, GuestyAuthError) as err:
                        custom_field_error = (
                            "guesty_custom_field_unavailable"
                            if isinstance(err, GuestyNotFoundError)
                            else self._guesty_error_reason(err)
                        )
                        self._data["pin_field_last_error"] = custom_field_error
                        self._record_retry_failure(
                            self._data,
                            _PIN_FIELD_RESOLVE_OPERATION,
                            now,
                        )
                        resolve_retry_at = self._retry_at(
                            self._data,
                            _PIN_FIELD_RESOLVE_OPERATION,
                        )
                        if resolve_retry_at is not None:
                            next_run = self._earlier(next_run, resolve_retry_at)
                        errors.append(custom_field_error)
                    else:
                        self._clear_retry(self._data, _PIN_FIELD_RESOLVE_OPERATION)
                        self._data.pop("pin_field_last_error", None)
            elif not self._custom_pin_enabled:
                self._clear_retry(self._data, _PIN_FIELD_RESOLVE_OPERATION)
                self._data.pop("pin_field_last_error", None)

            for reservation_id in list(self._records):
                if reservation_id in eligible:
                    continue
                if reservation_id in invalid_active_reservation_ids:
                    # Invalid live timing cannot authorize access, but it also
                    # is not proof that the booking was cancelled. Preserve an
                    # existing private record until a later valid snapshot can
                    # safely reconcile or retire it.
                    errors.append("invalid_reservation_time")
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
                    and listing_id in pin_listing_ids
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
                    listings[item.listing_id],
                    now,
                ),
            ):
                listing = listings[reservation.listing_id]
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

                record = self._records.setdefault(reservation.id, {})
                if data_stale:
                    stored_start = dt_util.parse_datetime(
                        str(record.get("access_start", ""))
                    )
                    stored_end = dt_util.parse_datetime(
                        str(record.get("access_end", ""))
                    )
                    offline_ready = bool(
                        offline_provisioning
                        and stored_start is not None
                        and stored_end is not None
                        and stored_end > now
                        and record.get("field_synced")
                        and isinstance(record.get("code"), str)
                        and record.get("offline_snapshot_confirmed_at")
                    )
                    if not offline_ready:
                        next_run = self._earlier(next_run, end)
                        continue
                    start, end = stored_start, stored_end
                    record["offline_snapshot_in_use"] = True
                else:
                    record["listing_id"] = reservation.listing_id
                    record["access_start"] = start.isoformat()
                    record["access_end"] = end.isoformat()
                    record["offline_snapshot_confirmed_at"] = now.isoformat()
                    record["reservation_snapshot"] = reservation.to_dict(
                        include_guest_details=bool(
                            self.entry.options.get(
                                CONF_EXPOSE_GUEST_DETAILS,
                                DEFAULT_EXPOSE_GUEST_DETAILS,
                            )
                        )
                    )
                    record["listing_snapshot"] = listing.to_dict()
                    record.pop("offline_snapshot_in_use", None)
                    try:
                        (
                            sync_errors,
                            sync_next_run,
                        ) = await self._async_sync_guesty_fields(
                            reservation,
                            record,
                            now,
                            custom_field_id,
                            custom_field_error=custom_field_error,
                        )
                        errors.extend(sync_errors)
                        if sync_next_run is not None:
                            next_run = self._earlier(next_run, sync_next_run)
                    except _GuestyWriteDeferred as err:
                        next_run = self._earlier(next_run, err.retry_at)
                    except (RuntimeError, ValueError) as err:
                        record["last_error"] = "code_generation_failed"
                        errors.append(type(err).__name__)
                    except (LoxoneApiError, LoxoneAuthError) as err:
                        errors.append(type(err).__name__)
                if record.get("conflict"):
                    errors.append(str(record.get("last_error") or "keycode_conflict"))

                if not self._listing_uses_loxone(reservation.listing_id):
                    # TTLock-only listings still use the exact same Guesty PIN
                    # lifecycle. Remove a previously configured Loxone user,
                    # then leave provider delivery to the TTLock manager.
                    if record.get("user_uuid"):
                        try:
                            await self._async_delete_remote_user(record)
                        except (LoxoneApiError, LoxoneAuthError) as err:
                            self._record_retry_failure(record, "cleanup", now)
                            retry_at = self._retry_at(record, "cleanup")
                            if retry_at:
                                next_run = self._earlier(next_run, retry_at)
                            errors.append(type(err).__name__)
                        else:
                            self._clear_retry(record, "cleanup")
                    next_run = self._earlier(next_run, end)
                    guesty_retry_at = self._retry_at(record, "guesty")
                    if guesty_retry_at:
                        next_run = self._earlier(next_run, guesty_retry_at)
                    continue

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
                    # A reservation can be moved back outside the provisioning
                    # lead after its user was already created. Keeping that user
                    # would preserve the former validity window until the new
                    # provisioning time and could grant access for a stay that
                    # no longer exists. Remove only the remote delivery state;
                    # the authoritative Guesty code remains stable for the later
                    # recreation.
                    if record.get("user_uuid"):
                        cleanup_retry_at = self._retry_at(record, "cleanup")
                        if cleanup_retry_at is None or cleanup_retry_at <= now:
                            try:
                                await self._async_delete_remote_user(record)
                            except (LoxoneApiError, LoxoneAuthError) as err:
                                self._record_retry_failure(record, "cleanup", now)
                                record["last_error"] = type(err).__name__
                                errors.append(type(err).__name__)
                            else:
                                self._clear_retry(record, "cleanup")
                        cleanup_retry_at = self._retry_at(record, "cleanup")
                        if cleanup_retry_at:
                            next_run = self._earlier(next_run, cleanup_retry_at)
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
                                await self._async_provision(
                                    reservation,
                                    record,
                                    groups,
                                    start,
                                    end,
                                )
                            except LoxoneCodeConflictError:
                                record["conflict"] = True
                                record["last_error"] = "code_conflict"
                                self._record_retry_failure(record, "loxone", now)
                                errors.append("code_conflict")
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

    def _recent_guesty_write_attempts(self, now: datetime) -> list[datetime]:
        """Return and normalize recent globally rate-limited write attempts."""
        raw_attempts = self._data.get(_GUESTY_WRITE_ATTEMPTS_KEY, [])
        if not isinstance(raw_attempts, list):
            raw_attempts = []

        cutoff = now - timedelta(seconds=_GUESTY_KEYCODE_QUEUE_DELAY_SECONDS)
        future_limit = now + timedelta(seconds=_GUESTY_KEYCODE_QUEUE_DELAY_SECONDS)
        attempts: list[datetime] = []
        for value in raw_attempts[-32:]:
            if not isinstance(value, str):
                continue
            try:
                parsed = dt_util.parse_datetime(value)
            except (TypeError, ValueError):
                continue
            if (
                parsed is None
                or parsed.utcoffset() is None
                or parsed <= cutoff
                or parsed > future_limit
            ):
                continue
            attempts.append(parsed)
        attempts.sort()
        normalized = [attempt.isoformat() for attempt in attempts]
        if normalized:
            self._data[_GUESTY_WRITE_ATTEMPTS_KEY] = normalized
        else:
            self._data.pop(_GUESTY_WRITE_ATTEMPTS_KEY, None)
        return attempts

    def _guesty_write_budget(self, now: datetime) -> int:
        """Return the global write allowance while reserving API capacity."""
        recent_attempts = self._recent_guesty_write_attempts(now)
        if (
            recent_attempts
            and recent_attempts[-1]
            + timedelta(seconds=_GUESTY_MIN_WRITE_SPACING_SECONDS)
            > now
        ):
            return 0
        global_capacity = max(
            0,
            _GUESTY_KEYCODE_WRITE_BATCH_SIZE - len(recent_attempts),
        )
        remaining = getattr(self._client, "last_rate_limit_remaining", None)
        if not isinstance(remaining, int):
            return global_capacity
        # Every externally budgeted PUT can require up to three bounded
        # confirmation reads. Charge that complete envelope before the write so
        # route discovery and eventual consistency cannot consume the reserve
        # needed by normal reservation and webhook traffic.
        capacity = max(
            0,
            (remaining - _GUESTY_RATE_LIMIT_RESERVE) // _GUESTY_REQUESTS_PER_WRITE_SLOT,
        )
        return min(global_capacity, capacity)

    def _next_guesty_write_at(self, now: datetime) -> datetime:
        """Return the earliest time another global write slot is available."""
        attempts = self._recent_guesty_write_attempts(now)
        retry_at = now
        if len(attempts) >= _GUESTY_KEYCODE_WRITE_BATCH_SIZE:
            retry_at = attempts[-_GUESTY_KEYCODE_WRITE_BATCH_SIZE] + timedelta(
                seconds=_GUESTY_KEYCODE_QUEUE_DELAY_SECONDS
            )
        if attempts:
            retry_at = max(
                retry_at,
                attempts[-1] + timedelta(seconds=_GUESTY_MIN_WRITE_SPACING_SECONDS),
            )

        remaining = getattr(self._client, "last_rate_limit_remaining", None)
        if (
            isinstance(remaining, int)
            and remaining < _GUESTY_RATE_LIMIT_RESERVE + _GUESTY_REQUESTS_PER_WRITE_SLOT
        ):
            raw_interval = self.entry.options.get(
                CONF_SCAN_INTERVAL,
                self.entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
            try:
                interval = max(
                    _GUESTY_KEYCODE_QUEUE_DELAY_SECONDS,
                    int(raw_interval),
                )
            except (TypeError, ValueError):
                interval = DEFAULT_SCAN_INTERVAL
            retry_at = max(retry_at, now + timedelta(seconds=interval))
        return retry_at

    async def _async_reserve_guesty_write_slots(
        self,
        now: datetime,
        count: int,
    ) -> bool:
        """Persist Guesty write attempts before making network requests."""
        attempts = self._recent_guesty_write_attempts(now)
        if (
            count < 1
            or len(attempts) + count > _GUESTY_KEYCODE_WRITE_BATCH_SIZE
            or (
                attempts
                and attempts[-1] + timedelta(seconds=_GUESTY_MIN_WRITE_SPACING_SECONDS)
                > now
            )
        ):
            return False
        attempts.extend([now] * count)
        self._data[_GUESTY_WRITE_ATTEMPTS_KEY] = [
            attempt.isoformat() for attempt in attempts
        ]
        # Persist before every possible PUT. A timeout or restart after Guesty
        # accepted a request must still consume the shared traffic allowance.
        await self._storage.async_save(self._data)
        return True

    async def _async_refund_guesty_write_slots(
        self,
        now: datetime,
        count: int,
    ) -> None:
        """Release pre-reserved fallback slots that were not used."""
        if count <= 0:
            return
        attempts = self._recent_guesty_write_attempts(now)
        if count >= len(attempts):
            self._data.pop(_GUESTY_WRITE_ATTEMPTS_KEY, None)
        else:
            self._data[_GUESTY_WRITE_ATTEMPTS_KEY] = [
                attempt.isoformat() for attempt in attempts[:-count]
            ]
        await self._storage.async_save(self._data)

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

    def _pin_custom_field_reference(self) -> str:
        """Return the configured shared reservation PIN field reference."""
        configured = self.entry.options.get(CONF_PIN_CUSTOM_FIELD)
        if configured is None:
            configured = self.entry.options.get(
                LEGACY_CONF_LOXONE_CUSTOM_FIELD,
                DEFAULT_PIN_CUSTOM_FIELD,
            )
        reference = str(configured).strip()
        if not reference:
            reference = DEFAULT_PIN_CUSTOM_FIELD
        return reference

    async def _async_pin_custom_field_id(self) -> str:
        """Resolve and privately cache the configured PIN custom field."""
        reference = self._pin_custom_field_reference()
        cached = self._data.get(_RESOLVED_PIN_FIELD_KEY)
        if (
            isinstance(cached, dict)
            and cached.get("reference") == reference
            and is_safe_resource_id(cached.get("id"))
        ):
            return cached["id"]
        field_id = await self._client.async_resolve_custom_field(reference)
        self._data[_RESOLVED_PIN_FIELD_KEY] = {
            "reference": reference,
            "id": field_id,
        }
        await self._storage.async_save(self._data)
        return field_id

    @staticmethod
    def _clean_guesty_value(value: Any) -> str | None:
        """Normalize one optional Guesty display value."""
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    async def _async_custom_field_observation(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        field_id: str,
    ) -> tuple[bool, str | None]:
        """Observe the configured field without duplicating normal API reads."""
        field_changed = record.get("custom_field_id") != field_id
        if reservation.custom_fields_observed:
            return True, self._clean_guesty_value(
                reservation.custom_fields.get(field_id)
            )

        source_version = reservation.last_updated_at
        if (
            not field_changed
            and "custom_baseline_value" in record
            and isinstance(source_version, str)
            and record.get("custom_source_last_updated_at") == source_version
        ):
            # The disk cache intentionally strips sensitive values. An
            # unchanged reservation version lets the private baseline stand
            # until the next fresh shared poll.
            return False, None

        value = await self._client.async_get_reservation_custom_field(
            reservation.id,
            field_id,
        )
        reservation.custom_fields[field_id] = value
        reservation.custom_fields_observed = True
        return True, self._clean_guesty_value(value)

    def _canonical_display_value(
        self,
        record: Mapping[str, Any],
        listing_id: str,
    ) -> str | None:
        """Return the stable Guesty-facing value retained in private state."""
        if record.get("replacement_pending"):
            confirmed = record.get("guesty_confirmed_code")
            if isinstance(confirmed, str) and _CODE_PATTERN.fullmatch(confirmed):
                return self._guesty_code_value(confirmed, listing_id)
        code = record.get("code")
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            return None
        display = record.get("guesty_display_value")
        if isinstance(display, str) and self._parse_guesty_code(display) == code:
            return display
        return self._guesty_code_value(code, listing_id)

    def _select_dual_source_value(
        self,
        reservation: GuestyReservation,
        record: Mapping[str, Any],
        *,
        native_observed: bool,
        native_value: str | None,
        custom_observed: bool,
        custom_value: str | None,
    ) -> str:
        """Choose one deterministic value, with native Keycode as tie-breaker."""
        canonical = self._canonical_display_value(record, reservation.listing_id)
        native_has_baseline = "native_baseline_value" in record
        custom_has_baseline = "custom_baseline_value" in record
        native_changed = bool(
            native_observed
            and native_has_baseline
            and native_value != record.get("native_baseline_value")
        )
        custom_changed = bool(
            custom_observed
            and custom_has_baseline
            and custom_value != record.get("custom_baseline_value")
        )

        if native_changed and custom_changed:
            if native_value and native_value == custom_value:
                return native_value
            # When both mirrors were edited during the same Guesty version,
            # native Keycode is the explicit product-level tie-breaker. An
            # empty native value cannot delete an established PIN, so retain
            # the private canonical value before considering the custom field.
            return (
                native_value
                or canonical
                or custom_value
                or self._new_code_value(reservation.listing_id)
            )
        if native_changed:
            return (
                native_value
                or canonical
                or custom_value
                or self._new_code_value(reservation.listing_id)
            )
        if custom_changed:
            return (
                custom_value
                or canonical
                or native_value
                or self._new_code_value(reservation.listing_id)
            )

        populated = [
            value
            for observed, value in (
                (native_observed, native_value),
                (custom_observed, custom_value),
            )
            if observed and value is not None
        ]
        if populated and all(value == populated[0] for value in populated):
            return populated[0]
        if len(populated) == 1:
            return populated[0]
        if len(populated) > 1:
            # A failed or queued stale mirror must not supersede a successful
            # manual edit merely because native Keycode is the normal
            # tie-breaker. Outside that narrowly proven propagation state,
            # native Keycode wins every unexplained mismatch.
            matching = [value == canonical for value in populated]
            if canonical is not None and matching.count(True) == 1:
                mismatching_index = matching.index(False)
                mismatching_source = "native" if mismatching_index == 0 else "custom"
                if record.get(f"{mismatching_source}_synced") is False or record.get(
                    f"{mismatching_source}_last_error"
                ):
                    # A previously failed or queued mirror still contains its
                    # old confirmed baseline; it cannot become a new manual
                    # edit merely because its propagation retry is pending.
                    return canonical
            if native_observed and native_value is not None:
                return native_value
            return (
                custom_value
                or canonical
                or self._new_code_value(reservation.listing_id)
            )
        if canonical is not None:
            return canonical
        return self._new_code_value(reservation.listing_id)

    def _new_code_value(self, listing_id: str) -> str:
        """Generate one unique PIN and return its Guesty-facing value."""
        code = self._generate_code()
        self._last_generated += 1
        return self._guesty_code_value(code, listing_id)

    def _reservation_webhook_received_at(self, reservation_id: str) -> datetime | None:
        """Return one recent coordinator webhook timestamp when available."""
        getter = getattr(self._coordinator, "reservation_webhook_received_at", None)
        if not callable(getter):
            return None
        received_at = getter(reservation_id)
        if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
            return None
        return received_at

    @staticmethod
    def _stored_datetime(record: Mapping[str, Any], key: str) -> datetime | None:
        """Parse one timezone-aware private-state timestamp."""
        value = record.get(key)
        if not isinstance(value, str):
            return None
        try:
            parsed = dt_util.parse_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed is None or parsed.utcoffset() is None:
            return None
        return parsed

    def _next_webhook_pin_retry_at(
        self,
        record: Mapping[str, Any],
        now: datetime,
    ) -> datetime | None:
        """Return the next minute boundary inside a new-booking fast window."""
        received_at = self._stored_datetime(record, _WEBHOOK_PIN_RECEIVED_AT_KEY)
        retry_until = self._stored_datetime(
            record,
            _WEBHOOK_PIN_FAST_RETRY_UNTIL_KEY,
        )
        if received_at is None or retry_until is None or now >= retry_until:
            return None
        elapsed = max(0, (now - received_at).total_seconds())
        interval_seconds = _WEBHOOK_PIN_RETRY_INTERVAL.total_seconds()
        boundary = int(elapsed // interval_seconds) + 1
        retry_at = received_at + _WEBHOOK_PIN_RETRY_INTERVAL * boundary
        return retry_at if retry_at <= retry_until else None

    def _clear_webhook_pin_fast_state(self, record: dict[str, Any]) -> None:
        """Clear the temporary webhook publication schedule after completion."""
        for key in (
            _WEBHOOK_PIN_RECEIVED_AT_KEY,
            _WEBHOOK_PIN_FIRST_WRITE_AT_KEY,
            _WEBHOOK_PIN_FAST_RETRY_UNTIL_KEY,
            _WEBHOOK_PIN_FAST_FAILURES_KEY,
        ):
            record.pop(key, None)

    def _webhook_pin_sources_synced(
        self,
        record: Mapping[str, Any],
        desired: str,
    ) -> bool:
        """Return whether every enabled Guesty mirror confirms the staged PIN."""
        enabled_sources = []
        if self._native_pin_enabled:
            enabled_sources.append("native")
        if self._custom_pin_enabled:
            enabled_sources.append("custom")
        return bool(enabled_sources) and all(
            record.get(f"{source}_synced") is True
            and record.get(f"{source}_baseline_value") == desired
            for source in enabled_sources
        )

    async def _async_stage_webhook_pin(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        *,
        native_observed: bool,
        native_value: str | None,
        custom_observed: bool,
        custom_value: str | None,
    ) -> datetime | None:
        """Persist a new webhook PIN now while deferring its first Guesty PUT."""
        if self._canonical_display_value(record, reservation.listing_id) is not None:
            return self._stored_datetime(record, _WEBHOOK_PIN_FIRST_WRITE_AT_KEY)
        received_at = self._reservation_webhook_received_at(reservation.id)
        if received_at is None:
            return None
        observed_sources = [
            (native_observed, native_value)
            for enabled in (self._native_pin_enabled,)
            if enabled
        ] + [
            (custom_observed, custom_value)
            for enabled in (self._custom_pin_enabled,)
            if enabled
        ]
        if any(observed and value is not None for observed, value in observed_sources):
            return None
        # Match the normal first-code safety gate: at least one configured
        # mirror must have been read successfully before a private PIN is born.
        if not any(observed for observed, _value in observed_sources):
            return None

        desired = self._new_code_value(reservation.listing_id)
        await self._async_adopt_canonical_value(
            record,
            desired,
            reservation.listing_id,
        )
        first_write_at = received_at + _WEBHOOK_PIN_FIRST_WRITE_DELAY
        retry_until = received_at + _WEBHOOK_PIN_FAST_RETRY_WINDOW
        record[_WEBHOOK_PIN_RECEIVED_AT_KEY] = received_at.isoformat()
        record[_WEBHOOK_PIN_FIRST_WRITE_AT_KEY] = first_write_at.isoformat()
        record[_WEBHOOK_PIN_FAST_RETRY_UNTIL_KEY] = retry_until.isoformat()
        record[_WEBHOOK_PIN_FAST_FAILURES_KEY] = 0
        for enabled, source, operation in (
            (self._native_pin_enabled, "native", _GUESTY_NATIVE_OPERATION),
            (self._custom_pin_enabled, "custom", _GUESTY_CUSTOM_OPERATION),
        ):
            if not enabled:
                continue
            record[f"{source}_synced"] = False
            record[f"{source}_last_error"] = _GUESTY_SYNC_QUEUED
            self._clear_retry(record, operation)
            record[f"{operation}_retry_at"] = first_write_at.isoformat()
        self._refresh_guesty_aggregate_state(record)
        await self._storage.async_save(self._data)
        _LOGGER.info(
            "Staged Guesty reservation PIN after webhook marker=%s "
            "first_write_in_seconds=%s",
            self._reservation_marker(reservation.id),
            max(0, int((first_write_at - now).total_seconds())),
        )
        return first_write_at

    def _stored_repair_values(
        self,
        record: Mapping[str, Any],
        listing_id: str,
    ) -> list[str]:
        """Return prior confirmed values suitable for repairing Guesty input."""
        canonical = self._canonical_display_value(record, listing_id)
        raw_candidates: list[Any] = []
        if record.get("last_error") == "guesty_duplicate_keycode":
            # Releases through v2.4.3 retained the rejected duplicate in the
            # aggregate code fields. The per-source baselines still contain
            # the last safe value and must therefore be preferred once.
            raw_candidates.extend(
                (
                    record.get("native_baseline_value"),
                    record.get("custom_baseline_value"),
                )
            )
        raw_candidates.extend(
            (
                canonical,
                record.get("native_baseline_value"),
                record.get("custom_baseline_value"),
            )
        )
        values: list[str] = []
        for candidate in raw_candidates:
            code = self._parse_guesty_code(candidate)
            if code is None:
                continue
            value = self._guesty_code_value(code, listing_id)
            if value not in values:
                values.append(value)
        return values

    def _repair_guesty_value(
        self,
        record: Mapping[str, Any],
        reservation: GuestyReservation,
        *,
        rejected_code: str | None,
        custom_field_id: str | None,
    ) -> tuple[str, bool]:
        """Restore the last safe PIN, or generate one when none exists yet."""
        for value in self._stored_repair_values(record, reservation.listing_id):
            code = self._parse_guesty_code(value)
            if code is None or code == rejected_code:
                continue
            if not self._code_is_used_elsewhere(
                code,
                reservation.id,
                custom_field_id=custom_field_id,
            ):
                return value, False
        return self._new_code_value(reservation.listing_id), True

    async def _async_adopt_canonical_value(
        self,
        record: dict[str, Any],
        value: str,
        listing_id: str,
    ) -> None:
        """Persist one valid unique Guesty value and invalidate provider caches."""
        code = self._parse_guesty_code(value)
        if code is None:
            raise ValueError("Invalid Guesty PIN display value")
        changed = record.get("code") != code
        display_changed = record.get("guesty_display_value") != value
        record["code"] = code
        record["guesty_display_value"] = value
        record["guesty_confirmed_code"] = code
        record["guesty_suffix"] = self._guesty_code_suffix(listing_id)
        if changed:
            record["code_set"] = False
            record.pop("provisioned_at", None)
            record.pop("repair_confirmation_pending", None)
            self._clear_retry(record, "loxone")
        if changed or display_changed:
            for key in (
                "replacement_pending",
                "replacement_rejected_code",
            ):
                record.pop(key, None)
        record.pop("conflict", None)
        if record.get("last_error") in {
            "guesty_pin_sources_changed",
            "guesty_pin_sources_mismatch",
            "guesty_duplicate_keycode",
            "invalid_existing_keycode",
            "guesty_keycode_removed",
        }:
            record.pop("last_error", None)
        await self._storage.async_save(self._data)

    def _refresh_guesty_aggregate_state(self, record: dict[str, Any]) -> None:
        """Maintain backward-compatible aggregate status from both mirrors."""
        enabled_sources = []
        if self._native_pin_enabled:
            enabled_sources.append(("native", _GUESTY_NATIVE_OPERATION))
        if self._custom_pin_enabled:
            enabled_sources.append(("custom", _GUESTY_CUSTOM_OPERATION))
        retry_times = [
            retry
            for _source, operation in enabled_sources
            if (retry := self._retry_at(record, operation)) is not None
        ]
        if retry_times:
            record["guesty_retry_at"] = min(retry_times).isoformat()
            record["guesty_retry_count"] = max(
                int(record.get(f"{operation}_retry_count", 0) or 0)
                for _source, operation in enabled_sources
            )
        else:
            self._clear_retry(record, "guesty")

        source_confirmed = any(
            record.get(f"{source}_synced") for source, _op in enabled_sources
        )
        repair_confirmation_pending = bool(
            record.get("repair_confirmation_pending")
            and self._confirmed_guesty_code(record) == record.get("code")
        )
        record["field_synced"] = bool(
            not record.get("conflict")
            and (source_confirmed or repair_confirmation_pending)
            and isinstance(record.get("code"), str)
        )
        if record.get("conflict"):
            return
        source_errors = [
            record.get(f"{source}_last_error") for source, _op in enabled_sources
        ]
        real_error = next(
            (
                value
                for value in source_errors
                if isinstance(value, str) and value != _GUESTY_SYNC_QUEUED
            ),
            None,
        )
        if real_error is not None:
            record["last_error"] = real_error
        elif retry_times and not source_confirmed:
            record["last_error"] = _GUESTY_SYNC_QUEUED
        elif record.get("last_error") == _GUESTY_SYNC_QUEUED or str(
            record.get("last_error", "")
        ).startswith("guesty_"):
            record.pop("last_error", None)

    def _queue_source_write(
        self,
        record: dict[str, Any],
        now: datetime,
        operation: str,
        source: str,
    ) -> datetime:
        """Queue one mirror write under the shared persistent traffic limit."""
        retry_at = self._next_guesty_write_at(now)
        fast_retry_at = self._next_webhook_pin_retry_at(record, now)
        if fast_retry_at is not None:
            # A fresh webhook gets one attempt per minute, never an extra
            # sub-minute burst. The global limiter may still postpone this
            # boundary when Guesty headroom is exhausted.
            retry_at = max(retry_at, fast_retry_at)
        if retry_at <= now:
            retry_at = now + timedelta(seconds=_GUESTY_MIN_WRITE_SPACING_SECONDS)
        current = self._retry_at(record, operation)
        failure_count = record.get(f"{operation}_retry_count")
        has_failure_backoff = bool(
            isinstance(failure_count, int)
            and not isinstance(failure_count, bool)
            and failure_count > 0
            and record.get(f"{source}_last_error") != _GUESTY_SYNC_QUEUED
        )
        if has_failure_backoff:
            record[f"{operation}_retry_at"] = max(
                retry_at,
                current or retry_at,
            ).isoformat()
        elif current is None or current <= now:
            self._clear_retry(record, operation)
            record[f"{source}_last_error"] = _GUESTY_SYNC_QUEUED
            record[f"{operation}_retry_at"] = retry_at.isoformat()
            self._last_queued += 1
        else:
            retry_at = current
        self._refresh_guesty_aggregate_state(record)
        return retry_at

    def _record_guesty_mirror_failure_retry(
        self,
        record: dict[str, Any],
        operation: str,
        now: datetime,
    ) -> None:
        """Use minute retries for a fresh webhook, then normal backoff."""
        fast_retry_at = self._next_webhook_pin_retry_at(record, now)
        if fast_retry_at is None:
            self._record_retry_failure(record, operation, now)
            return
        self._clear_retry(record, operation)
        record[f"{operation}_retry_at"] = fast_retry_at.isoformat()
        try:
            failures = max(int(record.get(_WEBHOOK_PIN_FAST_FAILURES_KEY, 0)), 0)
        except (TypeError, ValueError):
            failures = 0
        record[_WEBHOOK_PIN_FAST_FAILURES_KEY] = min(failures + 1, 5)

    async def _async_write_native_mirror(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        value: str,
        now: datetime,
    ) -> None:
        """Write and confirm the native Guesty Keycode mirror."""
        self._guesty_writes_remaining = min(
            self._guesty_writes_remaining,
            self._guesty_write_budget(now),
        )
        if self._guesty_writes_remaining <= 0:
            raise _GuestyWriteDeferred(
                self._queue_source_write(
                    record,
                    now,
                    _GUESTY_NATIVE_OPERATION,
                    "native",
                )
            )
        preferred_route = record.get(_GUESTY_NATIVE_WRITE_ROUTE_KEY)
        if preferred_route not in KEYCODE_WRITE_ROUTES:
            preferred_route = reservation.key_code_route
        if preferred_route not in KEYCODE_WRITE_ROUTES:
            preferred_route = KEYCODE_WRITE_ROUTE_V3

        allow_v2_fallback = bool(
            preferred_route == KEYCODE_WRITE_ROUTE_V3
            and self._guesty_writes_remaining >= 2
        )
        reserved_slots = 2 if allow_v2_fallback else 1
        if not await self._async_reserve_guesty_write_slots(now, reserved_slots):
            self._guesty_writes_remaining = 0
            raise _GuestyWriteDeferred(
                self._queue_source_write(
                    record,
                    now,
                    _GUESTY_NATIVE_OPERATION,
                    "native",
                )
            )
        self._guesty_writes_remaining = max(
            0,
            self._guesty_writes_remaining - reserved_slots,
        )
        try:
            if preferred_route == KEYCODE_WRITE_ROUTE_V3 and allow_v2_fallback:
                # These are the client's defaults; omitting them preserves
                # compatibility with older test doubles and API clients.
                result = await self._client.async_update_reservation_key_code(
                    reservation.id,
                    value,
                )
            else:
                result = await self._client.async_update_reservation_key_code(
                    reservation.id,
                    value,
                    preferred_route=preferred_route,
                    allow_v2_fallback=False,
                )
        except GuestyKeyCodeUnavailableError:
            # The API client only raises this after proving that the exact
            # reservation exists in v3 while its notes updater returns 404.
            # Cache the required v2 backing model for the bounded next retry.
            record[_GUESTY_NATIVE_WRITE_ROUTE_KEY] = KEYCODE_WRITE_ROUTE_V2
            self._last_guesty_writes += reserved_slots
            raise _GuestyWriteDeferred(
                self._queue_source_write(
                    record,
                    now,
                    _GUESTY_NATIVE_OPERATION,
                    "native",
                )
            )
        except Exception:
            # Unknown outcomes stay conservatively charged. A timeout after an
            # accepted request must not permit more than two PUTs per window.
            self._last_guesty_writes += reserved_slots
            raise

        actual_attempts = (
            result.attempts
            if isinstance(result, GuestyKeyCodeWriteResult)
            and 1 <= result.attempts <= reserved_slots
            else 1
        )
        confirmed_route = (
            result.route
            if isinstance(result, GuestyKeyCodeWriteResult)
            and result.route in KEYCODE_WRITE_ROUTES
            else preferred_route
        )
        unused_slots = reserved_slots - actual_attempts
        if unused_slots:
            await self._async_refund_guesty_write_slots(now, unused_slots)
            self._guesty_writes_remaining += unused_slots
        self._last_guesty_writes += actual_attempts
        record[_GUESTY_NATIVE_WRITE_ROUTE_KEY] = confirmed_route
        reservation.key_code = value
        reservation.key_code_observed = True
        reservation.key_code_route = confirmed_route
        record["native_baseline_value"] = value
        record["native_synced"] = True
        record.pop("native_last_error", None)
        self._clear_retry(record, _GUESTY_NATIVE_OPERATION)
        _LOGGER.info(
            "Guesty reservation PIN mirror synchronized marker=%s source=native",
            self._reservation_marker(reservation.id),
        )

    async def _async_write_custom_mirror(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        field_id: str,
        value: str,
        now: datetime,
    ) -> None:
        """Write and confirm the configurable Guesty custom-field mirror."""
        self._guesty_writes_remaining = min(
            self._guesty_writes_remaining,
            self._guesty_write_budget(now),
        )
        if self._guesty_writes_remaining <= 0:
            raise _GuestyWriteDeferred(
                self._queue_source_write(
                    record,
                    now,
                    _GUESTY_CUSTOM_OPERATION,
                    "custom",
                )
            )
        if not await self._async_reserve_guesty_write_slots(now, 1):
            self._guesty_writes_remaining = 0
            raise _GuestyWriteDeferred(
                self._queue_source_write(
                    record,
                    now,
                    _GUESTY_CUSTOM_OPERATION,
                    "custom",
                )
            )
        self._guesty_writes_remaining = max(0, self._guesty_writes_remaining - 1)
        try:
            await self._client.async_update_reservation_custom_field(
                reservation.id,
                field_id,
                value,
            )
        finally:
            self._last_guesty_writes += 1
        reservation.custom_fields[field_id] = value
        reservation.custom_fields_observed = True
        record["custom_field_id"] = field_id
        record["custom_baseline_value"] = value
        record["custom_synced"] = True
        record.pop("custom_last_error", None)
        self._clear_retry(record, _GUESTY_CUSTOM_OPERATION)
        _LOGGER.info(
            "Guesty reservation PIN mirror synchronized marker=%s source=custom",
            self._reservation_marker(reservation.id),
        )

    async def _async_sync_one_mirror(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        *,
        source: str,
        operation: str,
        observed: bool,
        current_value: str | None,
        desired_value: str,
        field_id: str | None,
        now: datetime,
    ) -> tuple[str | None, datetime | None]:
        """Synchronize one Guesty mirror and return an error and retry time."""
        if source == "native" and self._retry_at(record, operation) is None:
            legacy_retry = self._retry_at(record, "guesty")
            if legacy_retry is not None:
                record[f"{operation}_retry_at"] = legacy_retry.isoformat()
                legacy_count = record.get("guesty_retry_count")
                if isinstance(legacy_count, int) and not isinstance(legacy_count, bool):
                    record[f"{operation}_retry_count"] = legacy_count
                legacy_error = record.get("last_error")
                if isinstance(legacy_error, str):
                    record["native_last_error"] = legacy_error
        if (
            source == "native"
            and not observed
            and (
                (
                    record.get("field_synced") is True
                    and record.get("field_id") == _GUESTY_KEYCODE_SOURCE
                )
                or record.get("replacement_pending") is True
            )
            and self._confirmed_guesty_code(record)
            == self._parse_guesty_code(desired_value)
        ):
            record["native_baseline_value"] = desired_value
            record["native_synced"] = True
            return None, None
        if (
            not observed
            and record.get(f"{source}_synced") is True
            and record.get(f"{source}_baseline_value") == desired_value
        ):
            # Sparse cached reservation projections intentionally omit secrets.
            # A confirmed private baseline is authoritative until a later fresh
            # response explicitly observes that source.
            return None, self._retry_at(record, operation)
        if current_value == desired_value:
            record[f"{source}_baseline_value"] = desired_value
            record[f"{source}_synced"] = True
            record.pop(f"{source}_last_error", None)
            self._clear_retry(record, operation)
            return None, None
        record[f"{source}_synced"] = False
        if source == "custom" and field_id is None:
            record["custom_synced"] = False
            return "guesty_custom_field_unavailable", None
        retry_at = self._retry_at(record, operation)
        if retry_at is not None and retry_at > now:
            return None, retry_at
        if self._guesty_writes_remaining <= 0:
            return None, self._queue_source_write(
                record,
                now,
                operation,
                source,
            )
        try:
            if source == "native":
                await self._async_write_native_mirror(
                    reservation,
                    record,
                    desired_value,
                    now,
                )
            else:
                assert field_id is not None
                await self._async_write_custom_mirror(
                    reservation,
                    record,
                    field_id,
                    desired_value,
                    now,
                )
        except _GuestyWriteDeferred as err:
            return None, err.retry_at
        except (GuestyApiError, GuestyAuthError) as err:
            reason = self._guesty_error_reason(err)
            if source == "custom" and isinstance(err, GuestyNotFoundError):
                self._data.pop(_RESOLVED_PIN_FIELD_KEY, None)
            record[f"{source}_last_error"] = reason
            record[f"{source}_synced"] = False
            self._record_guesty_mirror_failure_retry(record, operation, now)
            retry_at = self._retry_at(record, operation)
            if self._guesty_error_stops_write_batch(err):
                self._guesty_writes_remaining = 0
            self._log_guesty_mirror_failure(
                reservation,
                source,
                operation,
                record,
                err,
                now,
            )
            return reason, retry_at
        return None, None

    async def _async_sync_guesty_fields(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        now: datetime,
        custom_field_id: str | None,
        *,
        custom_field_error: str | None,
    ) -> tuple[list[str], datetime | None]:
        """Reconcile native and custom Guesty PIN mirrors without ping-pong."""
        native_enabled = self._native_pin_enabled
        custom_enabled = self._custom_pin_enabled
        for enabled, source, operation in (
            (native_enabled, "native", _GUESTY_NATIVE_OPERATION),
            (custom_enabled, "custom", _GUESTY_CUSTOM_OPERATION),
        ):
            if enabled:
                continue
            self._clear_retry(record, operation)
            record.pop(f"{source}_last_error", None)
        if not native_enabled and not custom_enabled:
            record["field_synced"] = False
            record["last_error"] = "pin_source_not_configured"
            await self._storage.async_save(self._data)
            return ["pin_source_not_configured"], None

        native_read_failed = bool(
            native_enabled
            and reservation.key_code_read_failed
            and not (
                reservation.key_code_route == KEYCODE_WRITE_ROUTE_V2
                and self._clean_guesty_value(reservation.key_code) is not None
            )
        )
        native_observed = bool(
            native_enabled and reservation.key_code_observed and not native_read_failed
        )
        native_value = (
            self._clean_guesty_value(reservation.key_code) if native_observed else None
        )
        if (
            native_observed
            and reservation.key_code_route in KEYCODE_WRITE_ROUTES
            and record.get(_GUESTY_NATIVE_WRITE_ROUTE_KEY) not in KEYCODE_WRITE_ROUTES
        ):
            # An explicit native read can seed an unknown route. A route
            # learned from an actual write/404 probe remains stronger than an
            # empty alternate-model projection. This is private
            # per-reservation state, never an account-wide assumption.
            record[_GUESTY_NATIVE_WRITE_ROUTE_KEY] = reservation.key_code_route
        if (
            native_enabled
            and not reservation.key_code_read_failed
            and record.get("native_last_error")
            == "guesty_native_keycode_read_unavailable"
        ):
            record.pop("native_last_error", None)
        custom_observed = False
        custom_value: str | None = None
        errors: list[str] = []
        next_run: datetime | None = None
        if native_read_failed:
            if record.get("native_synced") is not True:
                record["native_synced"] = False
            record["native_last_error"] = "guesty_native_keycode_read_unavailable"
            errors.append("guesty_native_keycode_read_unavailable")
        custom_read_failed = bool(
            custom_enabled
            and reservation.custom_fields_read_failed
            and not reservation.custom_fields_observed
        )

        if custom_enabled and custom_field_id is not None:
            if custom_read_failed:
                if record.get("custom_synced") is not True:
                    record["custom_synced"] = False
                custom_field_error = "guesty_custom_field_read_unavailable"
                record["custom_last_error"] = custom_field_error
                errors.append(custom_field_error)
            else:
                retry_at = self._retry_at(record, _GUESTY_CUSTOM_OPERATION)
                if retry_at is not None and retry_at > now:
                    custom_field_error = str(
                        record.get("custom_last_error")
                        or "guesty_custom_field_unavailable"
                    )
                    next_run = self._earlier(next_run, retry_at)
                else:
                    try:
                        (
                            custom_observed,
                            custom_value,
                        ) = await self._async_custom_field_observation(
                            reservation,
                            record,
                            custom_field_id,
                        )
                    except (GuestyApiError, GuestyAuthError) as err:
                        custom_field_error = self._guesty_error_reason(err)
                        if isinstance(err, GuestyNotFoundError):
                            self._data.pop(_RESOLVED_PIN_FIELD_KEY, None)
                        record["custom_synced"] = False
                        record["custom_last_error"] = custom_field_error
                        self._record_retry_failure(
                            record, _GUESTY_CUSTOM_OPERATION, now
                        )
                        retry_at = self._retry_at(record, _GUESTY_CUSTOM_OPERATION)
                        if retry_at is not None:
                            next_run = self._earlier(next_run, retry_at)
                        errors.append(custom_field_error)

        if (
            (native_read_failed or custom_read_failed)
            and not any(
                observed and value is not None
                for observed, value in (
                    (native_observed, native_value),
                    (custom_observed, custom_value),
                )
            )
            and self._canonical_display_value(record, reservation.listing_id) is None
        ):
            # An unread source could already contain a manually chosen PIN.
            # Without either a healthy populated mirror or a confirmed private
            # baseline, generating a replacement would risk overwriting it.
            self._refresh_guesty_aggregate_state(record)
            await self._storage.async_save(self._data)
            return errors, next_run

        webhook_first_write_at = await self._async_stage_webhook_pin(
            reservation,
            record,
            now,
            native_observed=native_observed,
            native_value=native_value,
            custom_observed=custom_observed,
            custom_value=custom_value,
        )
        if webhook_first_write_at is not None and now < webhook_first_write_at:
            record["source_last_updated_at"] = reservation.last_updated_at
            if custom_field_id is not None:
                record["custom_field_id"] = custom_field_id
                record["custom_source_last_updated_at"] = reservation.last_updated_at
            self._refresh_guesty_aggregate_state(record)
            await self._storage.async_save(self._data)
            return errors, self._earlier(next_run, webhook_first_write_at)

        generated_during_repair = False
        desired = self._select_dual_source_value(
            reservation,
            record,
            native_observed=native_observed,
            native_value=native_value,
            custom_observed=custom_observed,
            custom_value=custom_value,
        )
        configured_suffix = self._guesty_code_suffix(reservation.listing_id)
        code = self._parse_guesty_code(desired)
        if code is None:
            desired, generated_during_repair = self._repair_guesty_value(
                record,
                reservation,
                rejected_code=None,
                custom_field_id=custom_field_id,
            )
            code = self._parse_guesty_code(desired)
            _LOGGER.info(
                "Restoring Guesty reservation PIN mirrors marker=%s "
                "reason=invalid_value",
                self._reservation_marker(reservation.id),
            )
        if code is None:  # Defensive: generated and stored values are validated.
            raise ValueError("Could not repair invalid Guesty PIN")
        # The configurable suffix is presentation metadata for the guest, not
        # part of the six-digit provider PIN. Normalize both mirrors without
        # ever rotating that PIN.
        desired = f"{code}{configured_suffix}"
        if self._code_is_used_elsewhere(
            code,
            reservation.id,
            custom_field_id=custom_field_id,
        ):
            desired, generated_during_repair = self._repair_guesty_value(
                record,
                reservation,
                rejected_code=code,
                custom_field_id=custom_field_id,
            )
            repaired_code = self._parse_guesty_code(desired)
            if repaired_code is None:
                raise ValueError("Could not repair duplicate Guesty PIN")
            code = repaired_code
            desired = f"{code}{configured_suffix}"
            _LOGGER.info(
                "Restoring Guesty reservation PIN mirrors marker=%s "
                "reason=duplicate_value",
                self._reservation_marker(reservation.id),
            )

        if (
            record.get("replacement_pending") is True
            and not native_observed
            and self._confirmed_guesty_code(record) == code
        ):
            record["native_baseline_value"] = desired
            record["native_synced"] = True

        confirmed_repair_value = bool(
            not generated_during_repair
            and (
                (record.get("field_synced") is True and record.get("code") == code)
                or any(
                    self._parse_guesty_code(record.get(f"{source}_baseline_value"))
                    == code
                    for source in ("native", "custom")
                )
            )
            and any(
                observed and current != desired
                for observed, current in (
                    (native_observed, native_value),
                    (custom_observed, custom_value),
                )
            )
        )
        await self._async_adopt_canonical_value(
            record,
            desired,
            reservation.listing_id,
        )
        if confirmed_repair_value:
            # The guest may already rely on this previously confirmed code.
            # A queued or failed Guesty repair must not revoke healthy provider
            # access while the same stable value is being republished.
            record["repair_confirmation_pending"] = True
        record["source_last_updated_at"] = reservation.last_updated_at
        if custom_field_id is not None:
            record["custom_field_id"] = custom_field_id
            record["custom_source_last_updated_at"] = reservation.last_updated_at
        if custom_field_error is not None:
            record["custom_synced"] = False
            record["custom_last_error"] = custom_field_error

        # Keep the established native route first. A failed native write does
        # not prevent the custom mirror from succeeding on a later write slot.
        generated_new_value = bool(
            generated_during_repair
            or (
                not native_value
                and not custom_value
                and not any(
                    f"{source}_baseline_value" in record
                    for source, enabled in (
                        ("native", native_enabled),
                        ("custom", custom_enabled),
                    )
                    if enabled
                )
            )
        )
        mirrors: list[tuple[str, str, bool, str | None, str | None]] = []
        if native_enabled:
            mirrors.append(
                (
                    "native",
                    _GUESTY_NATIVE_OPERATION,
                    native_observed,
                    native_value if native_observed else None,
                    None,
                )
            )
        if custom_enabled:
            mirrors.append(
                (
                    "custom",
                    _GUESTY_CUSTOM_OPERATION,
                    custom_observed,
                    custom_value if custom_observed else None,
                    custom_field_id,
                )
            )
        for source, operation, observed, current, field_id in mirrors:
            if source == "native" and native_read_failed:
                # Fresh dates/statuses remain usable when the optional Keycode
                # read fails, but an unobserved native field must never be
                # generated or overwritten blindly. The independent custom
                # mirror may continue and the next coordinator refresh retries
                # the read without a separate traffic loop.
                continue
            if source == "custom" and custom_read_failed:
                continue
            if (
                source == "custom"
                and custom_field_id is not None
                and custom_field_error is None
                and native_enabled
                and record.get("native_synced") is True
                and generated_new_value
            ):
                # Give another newly observed reservation its first confirmed
                # Guesty mirror before backfilling this newly generated value.
                # On later passes reservations are still processed by current
                # stay and then nearest check-in, so a distant or already ended
                # booking cannot hold back a nearer redundancy write.
                record["custom_synced"] = False
                retry_at = self._queue_source_write(
                    record,
                    now,
                    operation,
                    source,
                )
                next_run = self._earlier(next_run, retry_at)
                continue
            error, retry_at = await self._async_sync_one_mirror(
                reservation,
                record,
                source=source,
                operation=operation,
                observed=observed,
                current_value=current,
                desired_value=desired,
                field_id=field_id,
                now=now,
            )
            if error is not None:
                errors.append(error)
            if retry_at is not None:
                next_run = self._earlier(next_run, retry_at)

        if any(
            record.get(f"{source}_synced") is True
            and record.get(f"{source}_baseline_value") == desired
            for source, _operation, _observed, _current, _field_id in mirrors
        ):
            record.pop("repair_confirmation_pending", None)
        if self._webhook_pin_sources_synced(record, desired):
            self._clear_webhook_pin_fast_state(record)
        self._refresh_guesty_aggregate_state(record)
        await self._storage.async_save(self._data)
        return errors, next_run

    @staticmethod
    def _guesty_error_reason(error: Exception) -> str:
        """Return a stable privacy-safe reason for UI and diagnostics."""
        if isinstance(error, GuestyAuthError):
            return "guesty_authentication_failed"
        if isinstance(error, GuestyPermissionError):
            return "guesty_permission_denied"
        if isinstance(error, GuestyKeyCodeUnavailableError):
            return "guesty_keycode_endpoint_unavailable"
        if isinstance(error, GuestyNotFoundError):
            return "guesty_reservation_not_found"
        if isinstance(error, GuestyRetryableError):
            return "guesty_temporarily_unavailable"
        return "guesty_keycode_rejected"

    @staticmethod
    def _guesty_error_log_context(error: Exception) -> tuple[int | str, str, str]:
        """Return bounded, privacy-safe Guesty HTTP fields for logs."""
        raw_status = getattr(error, "status_code", None)
        status: int | str = (
            raw_status
            if isinstance(raw_status, int)
            and not isinstance(raw_status, bool)
            and 100 <= raw_status <= 599
            else "unknown"
        )
        raw_endpoint = getattr(error, "endpoint", None)
        endpoint = (
            raw_endpoint
            if isinstance(raw_endpoint, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", raw_endpoint)
            else "unknown"
        )
        raw_request_id = getattr(error, "request_id", None)
        request_id = (
            raw_request_id
            if isinstance(raw_request_id, str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", raw_request_id)
            else "unavailable"
        )
        return status, endpoint, request_id

    def _log_guesty_mirror_failure(
        self,
        reservation: GuestyReservation,
        source: str,
        operation: str,
        record: Mapping[str, Any],
        error: Exception,
        now: datetime,
    ) -> None:
        """Log one privacy-safe mirror failure without exposing the PIN."""
        status, endpoint, request_id = self._guesty_error_log_context(error)
        retry_at = self._retry_at(record, operation)
        retry_in_seconds = (
            max(0, int((retry_at - now).total_seconds())) if retry_at is not None else 0
        )
        retry_count = record.get(f"{operation}_retry_count", 0)
        rate_limit_remaining = getattr(
            self._client,
            "last_rate_limit_remaining",
            None,
        )
        _LOGGER.warning(
            "Guesty reservation PIN mirror synchronization failed "
            "marker=%s source=%s operation=%s reason=%s endpoint=%s http_status=%s "
            "request_id=%s retry_count=%s retry_in_seconds=%s "
            "rate_limit_remaining=%s",
            self._reservation_marker(reservation.id),
            source,
            (
                "native_keycode_write"
                if source == "native"
                else "reservation_custom_field_write"
            ),
            self._guesty_error_reason(error),
            endpoint,
            status,
            request_id,
            retry_count,
            retry_in_seconds,
            (
                rate_limit_remaining
                if isinstance(rate_limit_remaining, int)
                else "unknown"
            ),
        )

    @staticmethod
    def _guesty_error_stops_write_batch(error: Exception) -> bool:
        """Return whether one failure predicts failure for all later writes."""
        # A reservation-specific 404 may affect a stale, imported, grouped, or
        # otherwise non-writable record while unrelated reservations remain
        # writable. Its failed request already consumed one write-budget slot;
        # do not let it starve the rest of the bounded batch.
        return not isinstance(
            error,
            (GuestyKeyCodeUnavailableError, GuestyNotFoundError),
        )

    @staticmethod
    def _reservation_marker(reservation_id: str) -> str:
        """Return a non-reversible marker for safe operational logging."""
        return reservation_log_marker(reservation_id)

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
        record.pop("external_rejected_codes", None)
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
        if not prefix.isascii() or not prefix.isdigit() or not 1 <= len(prefix) <= 2:
            raise ValueError("Invalid Loxone code prefix")
        existing = {
            record.get("code")
            for record in self._records.values()
            if isinstance(record, dict)
        }
        for record in self._records.values():
            rejected = (
                record.get("external_rejected_codes", [])
                if isinstance(record, dict)
                else []
            )
            if isinstance(rejected, list):
                existing.update(
                    code
                    for code in rejected
                    if isinstance(code, str) and _CODE_PATTERN.fullmatch(code)
                )
        data = self._coordinator.data
        if data is not None:
            existing.update(
                code
                for reservation in data.reservations
                if (code := self._parse_guesty_code(reservation.key_code)) is not None
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

    def _code_is_used_elsewhere(
        self,
        code: str,
        reservation_id: str,
        *,
        custom_field_id: str | None = None,
    ) -> bool:
        """Resolve duplicate ownership without rotating the established owner."""
        local_owners = sorted(
            other_id
            for other_id, record in self._records.items()
            if isinstance(record, dict)
            and record.get("code") == code
            and not record.get("retired")
            and record.get("last_error") != "guesty_duplicate_keycode"
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
                and (
                    self._parse_guesty_code(reservation.key_code) == code
                    or (
                        custom_field_id is not None
                        and self._parse_guesty_code(
                            reservation.custom_fields.get(custom_field_id)
                        )
                        == code
                    )
                )
            )
            if data is not None
            else []
        )
        return len(remote_owners) > 1 and reservation_id != remote_owners[0]

    def _guesty_code_suffix(self, listing_id: str) -> str:
        """Return one validated display-only suffix for a Guesty listing."""
        configured = self.entry.options.get(CONF_GUESTY_CODE_SUFFIXES, {})
        value = configured.get(listing_id) if isinstance(configured, dict) else None
        if not isinstance(value, str):
            return DEFAULT_GUESTY_CODE_SUFFIX
        suffix = value.strip()
        if (
            len(suffix) > GUESTY_CODE_SUFFIX_MAX_LENGTH
            or any(character.isdigit() for character in suffix)
            or any(not character.isprintable() for character in suffix)
        ):
            return DEFAULT_GUESTY_CODE_SUFFIX
        return suffix

    def _guesty_code_value(self, code: Any, listing_id: str) -> str:
        """Format the Guesty-facing value without changing the provider PIN."""
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            return ""
        return f"{code}{self._guesty_code_suffix(listing_id)}"

    @staticmethod
    def _confirmed_guesty_code(record: Mapping[str, Any]) -> str | None:
        """Return the PIN that Guesty has already confirmed at least once."""
        confirmed = record.get("guesty_confirmed_code")
        if isinstance(confirmed, str) and _CODE_PATTERN.fullmatch(confirmed):
            return confirmed
        code = record.get("code")
        if (
            record.get("field_id") == _GUESTY_KEYCODE_SOURCE
            and record.get("field_synced") is True
            and isinstance(code, str)
            and _CODE_PATTERN.fullmatch(code)
        ):
            return code
        return None

    @staticmethod
    def _parse_guesty_code(value: Any) -> str | None:
        """Extract a six-digit PIN from a bounded display-suffixed value."""
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not 6 <= len(normalized) <= 6 + GUESTY_CODE_SUFFIX_MAX_LENGTH:
            return None
        code = normalized[:6]
        suffix = normalized[6:]
        if not _CODE_PATTERN.fullmatch(code):
            return None
        if any(character.isdigit() for character in suffix):
            return None
        if any(not character.isprintable() for character in suffix):
            return None
        return code

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

    def reservation_pin_snapshot(self, reservation_id: str) -> dict[str, Any]:
        """Return private PIN state to another in-process access provider."""
        record = self._records.get(reservation_id)
        if not isinstance(record, dict) or record.get("retired"):
            return {}
        guesty_blocked = record.get("last_error") in {
            "guesty_keycode_changed",
            "guesty_keycode_removed",
            "guesty_duplicate_keycode",
            "invalid_existing_keycode",
            "invalid_local_keycode",
            "guesty_pin_sources_changed",
            "guesty_pin_sources_mismatch",
            "source_change_cleanup_failed",
        }
        return {
            "code": record.get("code"),
            "field_synced": bool(record.get("field_synced") and not guesty_blocked),
            "access_start": record.get("access_start"),
            "access_end": record.get("access_end"),
        }

    def reservation_access_window(
        self,
        reservation: GuestyReservation,
        listing: GuestyListing,
    ) -> tuple[datetime, datetime]:
        """Share the exact existing access-offset calculation with providers."""
        return self._access_window(reservation, listing)

    def offline_reservation_snapshots(
        self,
    ) -> list[tuple[GuestyReservation, GuestyListing]]:
        """Return validated, previously confirmed schedules to PIN providers."""
        if not self.entry.options.get(
            CONF_PIN_OFFLINE_PROVISIONING,
            DEFAULT_PIN_OFFLINE_PROVISIONING,
        ):
            return []
        snapshots: list[tuple[GuestyReservation, GuestyListing]] = []
        for record in self._records.values():
            if (
                record.get("retired")
                or not record.get("offline_snapshot_confirmed_at")
                or not record.get("field_synced")
                or not isinstance(record.get("code"), str)
            ):
                continue
            raw_reservation = record.get("reservation_snapshot")
            raw_listing = record.get("listing_snapshot")
            if not isinstance(raw_reservation, dict) or not isinstance(
                raw_listing, dict
            ):
                continue
            try:
                reservation = GuestyReservation.from_dict(raw_reservation)
                listing = GuestyListing.from_dict(raw_listing)
            except (KeyError, TypeError, ValueError):
                continue
            snapshots.append((reservation, listing))
        return snapshots

    async def async_rotate_external_conflict(
        self,
        reservation_id: str,
        rejected_code: str,
    ) -> bool:
        """Reject provider-driven PIN changes after Guesty confirmation."""
        del reservation_id, rejected_code
        return False

    def listing_status_snapshot(self, listing_id: str) -> dict[str, Any]:
        """Return privacy-safe Guesty Keycode and Loxone PIN status."""
        pin_configured = listing_id in self._pin_listing_ids
        loxone_configured = self._listing_uses_loxone(listing_id)
        if not pin_configured:
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
                    "cleanup_pending"
                    if loxone_configured and listing_cleanup_pending
                    else "no_reservation"
                    if loxone_configured
                    else "not_configured"
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
            "loxone_status": (
                "scheduled"
                if loxone_configured and now < provision_at
                else "pending"
                if loxone_configured
                else "not_configured"
            ),
            "access_start": start,
            "access_end": end,
            "provision_at": provision_at,
            "reservation_status": reservation.status,
            "data_stale": bool(getattr(data, "data_stale", False)),
            "field_synced": False,
            "loxone_user_created": False,
            "native_keycode_enabled": self._native_pin_enabled,
            "custom_field_enabled": self._custom_pin_enabled,
            "custom_field_configured": bool(self._pin_custom_field_reference()),
            "custom_field_resolved": False,
        }
        if not isinstance(record, dict) or record.get("retired"):
            return snapshot

        last_error = record.get("last_error")
        guesty_conflict = bool(record.get("conflict")) and last_error in {
            "guesty_keycode_changed",
            "guesty_keycode_removed",
            "guesty_duplicate_keycode",
            "invalid_existing_keycode",
            "invalid_local_keycode",
            "guesty_pin_sources_changed",
            "guesty_pin_sources_mismatch",
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
        elif field_synced:
            snapshot["guesty_status"] = "synced"
        elif (
            guesty_retry_at is not None and last_error != _GUESTY_SYNC_QUEUED
        ) or last_error in {
            "code_generation_failed",
            "source_change_cleanup_failed",
        }:
            snapshot["guesty_status"] = "error"
        elif last_error == "pin_source_not_configured":
            snapshot["guesty_status"] = "error"
        snapshot["native_keycode_synced"] = bool(
            self._native_pin_enabled and record.get("native_synced")
        )
        snapshot["custom_field_synced"] = bool(
            self._custom_pin_enabled and record.get("custom_synced")
        )
        snapshot["custom_field_resolved"] = bool(
            self._custom_pin_enabled and record.get("custom_field_id")
        )
        snapshot["offline_snapshot_available"] = bool(
            record.get("offline_snapshot_confirmed_at")
        )

        remote_ready = bool(record.get("user_uuid") and record.get("code_set"))
        snapshot["loxone_user_created"] = remote_ready
        if not loxone_configured:
            snapshot["loxone_status"] = "not_configured"
        elif listing_cleanup_pending or (
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
        retry_counts, next_retry = self._guesty_retry_summary()
        native_routes = {
            route
            for record in records.values()
            if isinstance(record, dict)
            and (route := record.get(_GUESTY_NATIVE_WRITE_ROUTE_KEY))
            in KEYCODE_WRITE_ROUTES
        }
        native_route = (
            next(iter(native_routes))
            if len(native_routes) == 1
            else ("mixed" if native_routes else "automatic")
        )
        return {
            "enabled": bool(self.entry.options.get(CONF_LOXONE_ENABLED, False)),
            "native_keycode_enabled": self._native_pin_enabled,
            "custom_field_enabled": self._custom_pin_enabled,
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
            "guesty_keycode_write_route": native_route,
            "queued_during_last_reconcile": self._last_queued,
            "local_records": len(records),
            "native_keycodes_synced": sum(
                1
                for record in records.values()
                if self._native_pin_enabled
                and isinstance(record, dict)
                and (
                    record.get("native_synced")
                    or (
                        record.get("field_synced")
                        and record.get("field_id") == _GUESTY_KEYCODE_SOURCE
                    )
                )
            ),
            "native_keycodes_pending": sum(
                1
                for record in records.values()
                if self._native_pin_enabled
                and isinstance(record, dict)
                and not record.get("retired")
                and not record.get("native_synced")
            ),
            "native_keycodes_queued": sum(
                1
                for record in records.values()
                if self._native_pin_enabled
                and isinstance(record, dict)
                and (
                    record.get("native_last_error") == _GUESTY_SYNC_QUEUED
                    or (
                        "native_last_error" not in record
                        and record.get("last_error") == _GUESTY_SYNC_QUEUED
                    )
                )
            ),
            "native_keycode_failures": sum(
                1
                for record in records.values()
                if self._native_pin_enabled
                and isinstance(record, dict)
                and self._retry_at(record, _GUESTY_NATIVE_OPERATION) is not None
                and record.get("native_last_error") != _GUESTY_SYNC_QUEUED
            ),
            "custom_fields_synced": sum(
                1
                for record in records.values()
                if self._custom_pin_enabled
                and isinstance(record, dict)
                and record.get("custom_synced")
            ),
            "custom_fields_pending": sum(
                1
                for record in records.values()
                if self._custom_pin_enabled
                and isinstance(record, dict)
                and not record.get("retired")
                and not record.get("custom_synced")
            ),
            "offline_snapshots_available": sum(
                1
                for record in records.values()
                if isinstance(record, dict)
                and bool(record.get("offline_snapshot_confirmed_at"))
                and bool(record.get("field_synced"))
            ),
            "native_keycode_error_counts": retry_counts,
            "next_native_keycode_retry_at": (
                next_retry.isoformat() if next_retry is not None else None
            ),
            "retry_state_version": self._data.get(
                _GUESTY_RETRY_STATE_VERSION_KEY,
                0,
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
    """Best-effort remove managed users, then erase local credentials."""
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
    # Every managed user is already time-limited with expirationAction=delete.
    # Once the config entry is gone there is no safe owner that could retry
    # orphan tombstones after a restart, so never retain Miniserver credentials.
    await storage.async_remove()
    return cleanup_complete and not records
