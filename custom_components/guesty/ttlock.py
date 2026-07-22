"""Reservation-driven, time-limited TTLock passcode provisioning."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
import hashlib
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GuestyApiError, GuestyAuthError
from .const import (
    CONF_TTLOCK_ACCESS_TOKEN,
    CONF_TTLOCK_ACCOUNT,
    CONF_TTLOCK_CLIENT_ID,
    CONF_TTLOCK_CLIENT_SECRET,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    CONF_TTLOCK_LOCK_ID,
    CONF_TTLOCK_LOCK_IDS,
    CONF_TTLOCK_LOCKS,
    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
    CONF_TTLOCK_REFRESH_TOKEN,
    CONF_TTLOCK_REGION,
    CONF_TTLOCK_TOKEN_EXPIRES_AT,
    CONF_TTLOCK_USERNAME,
    DEFAULT_TTLOCK_PROVISION_LEAD_MINUTES,
    TTLOCK_MAX_LOCKS_PER_LISTING,
    TTLOCK_RETRY_BASE_SECONDS,
    TTLOCK_RETRY_MAX_SECONDS,
    TTLOCK_STORAGE_VERSION,
)
from .coordinator import GuestyDataUpdateCoordinator
from .loxone import GuestyLoxoneManager
from .models import GuestyReservation
from .ttlock_api import (
    TTLockApiClient,
    TTLockApiError,
    TTLockAuthError,
    TTLockCodeConflictError,
    TTLockGatewayError,
    TTLockOperationPendingError,
    TTLockRateLimitError,
)

_LOGGER = logging.getLogger(__name__)

TTLOCK_STORAGE_KEY = "guesty_ttlock"
_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_ACCOUNT_SNAPSHOT_KEY = "account_snapshot"
_TOKEN_ACCOUNT_KEY = "account_key"
_REMOTE_VERIFY_INTERVAL = timedelta(minutes=30)
_REMOTE_PENDING_RETRY = timedelta(seconds=30)
_CONFLICT_ROTATION_WINDOW = timedelta(hours=1)
_MAX_CONFLICT_ROTATIONS_PER_WINDOW = 3
_REMOTE_NORMAL_STATUS = 1
_REMOTE_INVALID_STATUSES = {2, 5, 7, 9}
_REMOTE_PENDING_STATUSES = {3, 4, 6, 8}


class GuestyTTLockStorage:
    """Store remote passcode IDs and refreshed tokens privately."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the private store."""
        self._store = Store(
            hass,
            TTLOCK_STORAGE_VERSION,
            f"{TTLOCK_STORAGE_KEY}_{entry_id}",
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> dict[str, Any]:
        """Load validated state."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {"records": {}, "tokens": {}}
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        else:
            data["records"] = {
                str(reservation_id): record
                for reservation_id, record in data["records"].items()
                if isinstance(reservation_id, str) and isinstance(record, dict)
            }
        if not isinstance(data.get("tokens"), dict):
            data["tokens"] = {}
        return data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Persist state atomically."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Remove all local TTLock state."""
        await self._store.async_remove()


class GuestyTTLockManager:
    """Synchronize the shared Guesty reservation PIN to TTLock locks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: GuestyDataUpdateCoordinator,
        pin_manager: GuestyLoxoneManager,
    ) -> None:
        """Initialize the TTLock provider."""
        self.hass = hass
        self.entry = entry
        self._coordinator = coordinator
        self._pin_manager = pin_manager
        self._storage = GuestyTTLockStorage(hass, entry.entry_id)
        self._data: dict[str, Any] = {"records": {}, "tokens": {}}
        self._client: TTLockApiClient | None = None
        self._fallback_clients: dict[str, TTLockApiClient] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self._unloaded = False
        self._cancel_timer: Callable[[], None] | None = None
        self._remove_pin_listener: Callable[[], None] | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._passcode_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._last_reconcile_at: str | None = None
        self._last_result = "never"
        self._last_error: str | None = None
        self._last_provisioned = 0
        self._last_changed = 0
        self._last_deleted = 0

    @property
    def _records(self) -> dict[str, dict[str, Any]]:
        """Return validated reservation records."""
        records = self._data.setdefault("records", {})
        return records if isinstance(records, dict) else {}

    @property
    def _account(self) -> dict[str, Any]:
        """Return the configured TTLock account."""
        value = self.entry.options.get(CONF_TTLOCK_ACCOUNT, {})
        return value if isinstance(value, dict) else {}

    @property
    def _mappings(self) -> dict[str, list[int]]:
        """Return valid per-listing lock mappings."""
        value = self.entry.options.get(CONF_TTLOCK_LISTING_MAPPINGS, {})
        return value if isinstance(value, dict) else {}

    @property
    def _configured_locks(self) -> dict[int, dict[str, Any]]:
        """Return configured compatible locks keyed by numeric TTLock ID."""
        value = self.entry.options.get(CONF_TTLOCK_LOCKS, [])
        if not isinstance(value, list):
            return {}
        result: dict[int, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                lock_id = int(item.get(CONF_TTLOCK_LOCK_ID))
            except (TypeError, ValueError):
                continue
            result[lock_id] = item
        return result

    async def async_setup(self) -> None:
        """Load state, subscribe to shared PIN changes, and reconcile."""
        self._data = await self._storage.async_load()
        if self.entry.options.get(CONF_TTLOCK_ENABLED, False):
            try:
                self._client = self._client_from_account(
                    self._account, use_stored_tokens=True
                )
            except (KeyError, TypeError, ValueError):
                self._last_result = "error"
                self._last_error = "invalid_configuration"
        self._remove_pin_listener = self._pin_manager.async_add_listener(
            self.async_schedule_reconcile
        )
        self.async_schedule_reconcile()

    async def async_unload(self) -> None:
        """Stop timers and background work."""
        self._unloaded = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._remove_pin_listener is not None:
            self._remove_pin_listener()
            self._remove_pin_listener = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._listeners.clear()
        self._fallback_clients.clear()
        self._client = None

    @callback
    def async_schedule_reconcile(self) -> None:
        """Debounce coordinator and shared-PIN updates into one pass."""
        if self._unloaded:
            return
        self._pending = True
        if self._task and not self._task.done():
            return
        self._task = self.hass.async_create_task(
            self._async_reconcile_loop(),
            "guesty_ttlock_reconcile",
        )

    async def _async_reconcile_loop(self) -> None:
        """Process updates arriving during an active pass."""
        try:
            while self._pending and not self._unloaded:
                self._pending = False
                await asyncio.sleep(0.75)
                try:
                    await self.async_reconcile()
                except Exception as err:  # Defensive task boundary.
                    self._last_result = "error"
                    self._last_error = type(err).__name__
                    _LOGGER.exception("Unexpected TTLock PIN synchronization failure")
                    self._notify_listeners()
        except asyncio.CancelledError:
            raise

    async def async_reconcile(self) -> None:
        """Reconcile TTLock from the existing Guesty reservation snapshot."""
        async with self._lock:
            now = dt_util.utcnow()
            self._last_provisioned = 0
            self._last_changed = 0
            self._last_deleted = 0
            self._passcode_cache.clear()
            errors: list[str] = []
            next_run: datetime | None = None
            data = self._coordinator.data
            enabled = bool(self.entry.options.get(CONF_TTLOCK_ENABLED, False))
            data_stale = data is None or bool(getattr(data, "data_stale", False))
            eligible: dict[str, GuestyReservation] = {}
            if enabled and data is not None:
                eligible = {
                    reservation.id: reservation
                    for reservation in data.reservations
                    if reservation.is_active_status()
                    and reservation.listing_id in self._mappings
                    and reservation.listing_id in data.listings
                }

            for reservation_id in list(self._records):
                if reservation_id in eligible:
                    continue
                record = self._records.get(reservation_id, {})
                stored_end = self._parse_time(record.get("access_end"))
                if (
                    data_stale
                    and enabled
                    and record.get("listing_id") in self._mappings
                    and stored_end is not None
                    and stored_end > now
                ):
                    next_run = self._earlier(next_run, stored_end)
                    continue
                retry_at = self._retry_at(record)
                if record.get("retired") and retry_at is not None and retry_at > now:
                    next_run = self._earlier(next_run, retry_at)
                    continue
                try:
                    await self._async_retire(reservation_id)
                except TTLockApiError as err:
                    self._record_retry_failure(record, now)
                    errors.append(self._error_reason(err))
                    retry_at = self._retry_at(record)
                    if retry_at is not None:
                        next_run = self._earlier(next_run, retry_at)

            if data is not None:
                for reservation in sorted(
                    eligible.values(),
                    key=lambda item: self._reservation_sync_order(
                        item,
                        data.listings[item.listing_id],
                        now,
                    ),
                ):
                    listing = data.listings[reservation.listing_id]
                    try:
                        start, end = self._pin_manager.reservation_access_window(
                            reservation, listing
                        )
                    except (TypeError, ValueError):
                        errors.append("invalid_reservation_time")
                        continue
                    record = self._records.setdefault(reservation.id, {})
                    record["listing_id"] = reservation.listing_id
                    record["access_start"] = start.isoformat()
                    record["access_end"] = end.isoformat()
                    record.setdefault("locks", {})
                    marker = self._passcode_name(reservation.id)

                    if end <= now:
                        try:
                            await self._async_retire(reservation.id)
                        except TTLockApiError as err:
                            self._record_retry_failure(record, now)
                            errors.append(self._error_reason(err))
                        continue
                    next_run = self._earlier(next_run, end)
                    if data_stale:
                        continue

                    lock_ids = self._mapping_lock_ids(reservation.listing_id)
                    if not lock_ids:
                        try:
                            await self._async_delete_all(record, marker)
                        except TTLockApiError as err:
                            self._record_retry_failure(record, now)
                            record["last_error"] = self._error_reason(err)
                            errors.append(record["last_error"])
                        else:
                            record["last_error"] = "invalid_mapping"
                            errors.append("invalid_mapping")
                        continue
                    try:
                        snapshot = record.get(_ACCOUNT_SNAPSHOT_KEY)
                        if (
                            isinstance(snapshot, dict)
                            and record.get("locks")
                            and self._account_key(snapshot)
                            != self._account_key(self._account)
                        ):
                            # Never reuse password IDs from a different TTLock
                            # account. Remove them with the retained old account
                            # snapshot before provisioning on the new account.
                            await self._async_delete_all(record, marker)
                        await self._async_remove_unmapped_locks(
                            record, set(lock_ids), marker
                        )
                    except TTLockApiError as err:
                        self._record_retry_failure(record, now)
                        record["last_error"] = self._error_reason(err)
                        errors.append(record["last_error"])
                        continue

                    lead = timedelta(
                        minutes=int(
                            self.entry.options.get(
                                CONF_TTLOCK_PROVISION_LEAD_MINUTES,
                                DEFAULT_TTLOCK_PROVISION_LEAD_MINUTES,
                            )
                        )
                    )
                    provision_at = start - lead
                    record["provision_at"] = provision_at.isoformat()
                    if now < provision_at:
                        if record.get("locks"):
                            try:
                                await self._async_delete_all(record, marker)
                            except TTLockApiError as err:
                                self._record_retry_failure(record, now)
                                record["last_error"] = self._error_reason(err)
                                errors.append(record["last_error"])
                                retry_at = self._retry_at(record)
                                if retry_at is not None:
                                    next_run = self._earlier(next_run, retry_at)
                                continue
                            else:
                                record.pop("last_error", None)
                                self._clear_retry(record)
                        next_run = self._earlier(next_run, provision_at)
                        continue

                    pin = self._pin_manager.reservation_pin_snapshot(reservation.id)
                    code = pin.get("code")
                    if not pin.get("field_synced") or not isinstance(code, str):
                        record["last_error"] = "guesty_pin_pending"
                        continue
                    if not _CODE_PATTERN.fullmatch(code):
                        record["last_error"] = "invalid_pin"
                        errors.append("invalid_pin")
                        continue
                    retry_at = self._retry_at(record)
                    if retry_at is not None and retry_at > now:
                        next_run = self._earlier(next_run, retry_at)
                        errors.append(str(record.get("last_error") or "ttlock_retry"))
                        continue
                    try:
                        await self._async_ensure_reservation(
                            reservation,
                            record,
                            lock_ids,
                            code,
                            start,
                            end,
                        )
                    except TTLockCodeConflictError:
                        try:
                            await self._async_delete_all(record, marker)
                            if not self._allow_conflict_rotation(record, now):
                                self._record_retry_failure(record, now)
                                record["last_error"] = "code_conflict"
                                errors.append("code_conflict")
                                retry_at = self._retry_at(record)
                                if retry_at is not None:
                                    next_run = self._earlier(next_run, retry_at)
                                continue
                            await self._storage.async_save(self._data)
                            rotated = (
                                await self._pin_manager.async_rotate_external_conflict(
                                    reservation.id, code
                                )
                            )
                        except (TTLockApiError, GuestyApiError, GuestyAuthError) as err:
                            self._record_retry_failure(record, now)
                            record["last_error"] = self._error_reason(err)
                            errors.append(record["last_error"])
                        else:
                            record["last_error"] = (
                                "code_conflict_rotated" if rotated else "code_conflict"
                            )
                            if not rotated:
                                self._record_retry_failure(record, now)
                                errors.append("code_conflict")
                            else:
                                self._clear_retry(record)
                                self._pending = True
                    except TTLockOperationPendingError as err:
                        record["retry_at"] = (now + _REMOTE_PENDING_RETRY).isoformat()
                        record["last_error"] = self._error_reason(err)
                        errors.append(record["last_error"])
                    except TTLockApiError as err:
                        self._record_retry_failure(record, now)
                        record["last_error"] = self._error_reason(err)
                        errors.append(record["last_error"])
                    else:
                        record.pop("last_error", None)
                        self._clear_retry(record)
                        record.pop("conflict_rotation_times", None)
                        verify_at = self._next_verification_at(record)
                        if verify_at is not None:
                            next_run = self._earlier(next_run, verify_at)
                    retry_at = self._retry_at(record)
                    if retry_at is not None:
                        next_run = self._earlier(next_run, retry_at)

            await self._persist_tokens()
            await self._storage.async_save(self._data)
            self._schedule_at(next_run)
            self._last_reconcile_at = now.isoformat()
            self._last_result = "ok" if not errors else "partial"
            self._last_error = errors[0] if errors else None
            self._notify_listeners()

    async def _async_ensure_reservation(
        self,
        reservation: GuestyReservation,
        record: dict[str, Any],
        lock_ids: list[int],
        code: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Create or update the passcode on every mapped lock."""
        client = self._current_client()
        marker = self._passcode_name(reservation.id)
        now = dt_util.utcnow()
        locks = record.setdefault("locks", {})
        if not isinstance(locks, dict):
            locks = record["locks"] = {}
        record[_ACCOUNT_SNAPSHOT_KEY] = self._account_snapshot(client)

        for lock_id in lock_ids:
            state = locks.setdefault(str(lock_id), {})
            if not isinstance(state, dict):
                state = locks[str(lock_id)] = {}
            fingerprint = self._fingerprint(lock_id, code, start, end)
            verified_at = self._parse_time(state.get("verified_at"))
            if (
                state.get("fingerprint") == fingerprint
                and isinstance(state.get("keyboard_pwd_id"), int)
                and verified_at is not None
                and verified_at <= now
                and now < (verified_at + _REMOTE_VERIFY_INTERVAL)
            ):
                continue

            passcodes = await self._async_list_passcodes(client, lock_id)
            password_id = state.get("keyboard_pwd_id")
            owned = (
                self._find_passcode(passcodes, password_id)
                if isinstance(password_id, int)
                else None
            )
            if isinstance(password_id, int) and (
                owned is None or owned.get("keyboardPwdName") != marker
            ):
                # A locally remembered ID is not proof of ownership. TTLock IDs
                # may disappear or the local store may be restored out of date.
                for key in ("keyboard_pwd_id", "fingerprint", "verified_at"):
                    state.pop(key, None)
                password_id = None
                owned = None

            if not isinstance(password_id, int):
                recovered = next(
                    (
                        item
                        for item in passcodes
                        if item.get("keyboardPwdName") == marker
                        and self._as_int(item.get("keyboardPwdId")) is not None
                    ),
                    None,
                )
                if recovered is not None:
                    password_id = self._as_int(recovered.get("keyboardPwdId"))
                    state["keyboard_pwd_id"] = password_id
                    owned = recovered

            if (
                isinstance(password_id, int)
                and owned is not None
                and self._passcode_matches(
                    owned,
                    password_id=password_id,
                    code=code,
                    name=marker,
                    start=start,
                    end=end,
                )
            ):
                state["fingerprint"] = fingerprint
                state["verified_at"] = now.isoformat()
                state["synced_at"] = now.isoformat()
                state.pop("create_started", None)
                await self._storage.async_save(self._data)
                continue

            if owned is not None:
                remote_status = self._remote_status(owned)
                if remote_status in _REMOTE_PENDING_STATUSES:
                    raise TTLockOperationPendingError(
                        "TTLock passcode operation is still pending"
                    )
                if remote_status in _REMOTE_INVALID_STATUSES:
                    await self._async_delete_lock_state(record, lock_id, state, marker)
                    for key in ("keyboard_pwd_id", "fingerprint", "verified_at"):
                        state.pop(key, None)
                    password_id = None
                    owned = None
                    passcodes = await self._async_list_passcodes(
                        client, lock_id, force=True
                    )

            for item in passcodes:
                existing_code = str(item.get("keyboardPwd", "")).strip()
                existing_id = self._as_int(item.get("keyboardPwdId"))
                if (
                    existing_code == code
                    and existing_id != password_id
                    and self._passcode_blocks_code(item)
                ):
                    raise TTLockCodeConflictError(
                        "TTLock passcode is already assigned to another entry"
                    )

            if isinstance(password_id, int):
                try:
                    await client.async_change_passcode(
                        lock_id=lock_id,
                        password_id=password_id,
                        code=code,
                        name=marker,
                        valid_from=start,
                        valid_until=end,
                    )
                    self._invalidate_passcode_cache(client, lock_id)
                except TTLockApiError:
                    recovered = await self._async_list_passcodes(
                        client, lock_id, force=True
                    )
                    if not any(
                        self._passcode_matches(
                            item,
                            password_id=password_id,
                            code=code,
                            name=marker,
                            start=start,
                            end=end,
                        )
                        for item in recovered
                    ):
                        raise
                self._last_changed += 1
            else:
                state["create_started"] = True
                await self._storage.async_save(self._data)
                try:
                    password_id = await client.async_add_passcode(
                        lock_id=lock_id,
                        code=code,
                        name=marker,
                        valid_from=start,
                        valid_until=end,
                    )
                    state["keyboard_pwd_id"] = password_id
                    self._invalidate_passcode_cache(client, lock_id)
                    await self._storage.async_save(self._data)
                except TTLockApiError:
                    recovered = await self._async_list_passcodes(
                        client, lock_id, force=True
                    )
                    match = next(
                        (
                            item
                            for item in recovered
                            if self._passcode_matches(
                                item,
                                password_id=None,
                                code=code,
                                name=marker,
                                start=start,
                                end=end,
                            )
                        ),
                        None,
                    )
                    recovered_id = (
                        self._as_int(match.get("keyboardPwdId"))
                        if isinstance(match, dict)
                        else None
                    )
                    if recovered_id is None:
                        raise
                    password_id = recovered_id
                state["keyboard_pwd_id"] = password_id
                self._last_provisioned += 1

            confirmed = await self._async_list_passcodes(client, lock_id, force=True)
            match = next(
                (
                    item
                    for item in confirmed
                    if self._passcode_matches(
                        item,
                        password_id=password_id,
                        code=code,
                        name=marker,
                        start=start,
                        end=end,
                    )
                ),
                None,
            )
            if match is None:
                pending = self._find_passcode(confirmed, password_id)
                if (
                    pending is not None
                    and self._remote_status(pending) in _REMOTE_PENDING_STATUSES
                ):
                    raise TTLockOperationPendingError(
                        "TTLock passcode operation is still pending"
                    )
                raise TTLockApiError("TTLock did not confirm the passcode operation")
            state["fingerprint"] = fingerprint
            state.pop("create_started", None)
            state["verified_at"] = now.isoformat()
            state["synced_at"] = now.isoformat()
            await self._storage.async_save(self._data)

    async def _async_remove_unmapped_locks(
        self, record: dict[str, Any], expected: set[int], marker: str
    ) -> None:
        """Delete passcodes from locks removed from a listing mapping."""
        locks = record.get("locks")
        if not isinstance(locks, dict):
            return
        for raw_lock_id in list(locks):
            lock_id = self._as_int(raw_lock_id)
            if lock_id is not None and lock_id in expected:
                continue
            state = locks.get(raw_lock_id)
            if isinstance(state, dict) and lock_id is not None:
                await self._async_delete_lock_state(record, lock_id, state, marker)
            locks.pop(raw_lock_id, None)

    async def _async_delete_all(self, record: dict[str, Any], marker: str) -> None:
        """Delete every managed passcode while retaining failures for retry."""
        locks = record.get("locks")
        if not isinstance(locks, dict):
            return
        for raw_lock_id in list(locks):
            lock_id = self._as_int(raw_lock_id)
            state = locks.get(raw_lock_id)
            if lock_id is None or not isinstance(state, dict):
                locks.pop(raw_lock_id, None)
                continue
            await self._async_delete_lock_state(record, lock_id, state, marker)
            locks.pop(raw_lock_id, None)

    async def _async_delete_lock_state(
        self,
        record: dict[str, Any],
        lock_id: int,
        state: dict[str, Any],
        marker: str,
    ) -> None:
        """Delete one passcode and verify ambiguous already-absent results."""
        password_id = state.get("keyboard_pwd_id")
        if not isinstance(password_id, int):
            return
        client = self._client_for_record(record)
        passcodes = await self._async_list_passcodes(client, lock_id, force=True)
        remote = self._find_passcode(passcodes, password_id)
        if remote is None:
            return
        if remote.get("keyboardPwdName") != marker:
            _LOGGER.warning(
                "Refusing to delete a TTLock passcode whose reservation marker changed"
            )
            return
        try:
            await client.async_delete_passcode(lock_id=lock_id, password_id=password_id)
            self._invalidate_passcode_cache(client, lock_id)
        except (TTLockAuthError, TTLockGatewayError, TTLockRateLimitError):
            raise
        except TTLockApiError:
            passcodes = await self._async_list_passcodes(client, lock_id, force=True)
            if any(
                self._as_int(item.get("keyboardPwdId")) == password_id
                and item.get("keyboardPwdName") == marker
                for item in passcodes
            ):
                raise
        self._last_deleted += 1

    async def _async_retire(self, reservation_id: str) -> None:
        """Delete TTLock passcodes, retaining a tombstone on failure."""
        record = self._records.get(reservation_id)
        if not isinstance(record, dict):
            return
        record["retired"] = True
        await self._storage.async_save(self._data)
        await self._async_delete_all(record, self._passcode_name(reservation_id))
        self._records.pop(reservation_id, None)
        await self._storage.async_save(self._data)

    def _mapping_lock_ids(self, listing_id: str) -> list[int]:
        """Return unique configured lock IDs for one listing."""
        mapping = self._mappings.get(listing_id)
        raw_ids = (
            mapping.get(CONF_TTLOCK_LOCK_IDS) if isinstance(mapping, dict) else mapping
        )
        if not isinstance(raw_ids, list):
            return []
        configured = self._configured_locks
        result: list[int] = []
        for value in raw_ids:
            lock_id = self._as_int(value)
            if lock_id is None or lock_id not in configured or lock_id in result:
                continue
            result.append(lock_id)
        return result[:TTLOCK_MAX_LOCKS_PER_LISTING]

    def _current_client(self) -> TTLockApiClient:
        """Return the configured account client."""
        if self._client is None:
            raise TTLockAuthError("TTLock is not configured")
        return self._client

    async def _async_list_passcodes(
        self,
        client: TTLockApiClient,
        lock_id: int,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """List passcodes once per lock and reconciliation unless invalidated."""
        key = (id(client), lock_id)
        if force or key not in self._passcode_cache:
            self._passcode_cache[key] = await client.async_list_passcodes(lock_id)
        return self._passcode_cache[key]

    def _invalidate_passcode_cache(self, client: TTLockApiClient, lock_id: int) -> None:
        """Discard a passcode list after a remote mutation."""
        self._passcode_cache.pop((id(client), lock_id), None)

    def _client_for_record(self, record: Mapping[str, Any]) -> TTLockApiClient:
        """Use the original account snapshot when configuration changed."""
        snapshot = record.get(_ACCOUNT_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict):
            return self._current_client()
        current_key = self._account_key(self._account)
        snapshot_key = self._account_key(snapshot)
        if current_key and current_key == snapshot_key:
            if self._client is not None:
                return self._current_client()
            client = self._fallback_clients.get(current_key)
            if client is None:
                # A disabled provider still needs the latest configured secret
                # and private tokens to revoke previously installed codes.
                try:
                    client = self._client_from_account(
                        self._account, use_stored_tokens=True
                    )
                except (KeyError, TypeError, ValueError):
                    client = None
                else:
                    self._fallback_clients[current_key] = client
            if client is not None:
                return client
        client = self._fallback_clients.get(snapshot_key)
        if client is None:
            client = self._client_from_account(snapshot, use_stored_tokens=False)
            self._fallback_clients[snapshot_key] = client
        return client

    def _client_from_account(
        self, account: Mapping[str, Any], *, use_stored_tokens: bool
    ) -> TTLockApiClient:
        """Build a client from validated private account data."""
        tokens = self._data.get("tokens", {}) if use_stored_tokens else {}
        if not isinstance(tokens, dict):
            tokens = {}
        if tokens.get(_TOKEN_ACCOUNT_KEY) != self._account_key(account):
            tokens = {}
        return TTLockApiClient.from_hass(
            self.hass,
            region=str(account[CONF_TTLOCK_REGION]),
            client_id=str(account[CONF_TTLOCK_CLIENT_ID]),
            client_secret=str(account[CONF_TTLOCK_CLIENT_SECRET]),
            username=str(account.get(CONF_TTLOCK_USERNAME, "")),
            access_token=str(
                tokens.get(CONF_TTLOCK_ACCESS_TOKEN)
                or account.get(CONF_TTLOCK_ACCESS_TOKEN, "")
            ),
            refresh_token=str(
                tokens.get(CONF_TTLOCK_REFRESH_TOKEN)
                or account.get(CONF_TTLOCK_REFRESH_TOKEN, "")
            ),
            token_expires_at=str(
                tokens.get(CONF_TTLOCK_TOKEN_EXPIRES_AT)
                or account.get(CONF_TTLOCK_TOKEN_EXPIRES_AT, "")
            ),
        )

    async def _persist_tokens(self) -> None:
        """Persist refreshed tokens privately without reloading Home Assistant."""
        if self._client is not None:
            tokens = self._client.token_snapshot()
            current_key = self._account_key(self._account)
            tokens[_TOKEN_ACCOUNT_KEY] = current_key
            self._data["tokens"] = tokens
            for record in self._records.values():
                snapshot = record.get(_ACCOUNT_SNAPSHOT_KEY)
                if (
                    isinstance(snapshot, dict)
                    and self._account_key(snapshot) == current_key
                ):
                    snapshot.update(tokens)
        for account_key, client in self._fallback_clients.items():
            tokens = client.token_snapshot()
            for record in self._records.values():
                snapshot = record.get(_ACCOUNT_SNAPSHOT_KEY)
                if (
                    isinstance(snapshot, dict)
                    and self._account_key(snapshot) == account_key
                ):
                    snapshot.update(tokens)

    @staticmethod
    def _account_snapshot(client: TTLockApiClient) -> dict[str, str]:
        """Persist only fields required for later cleanup."""
        return {
            CONF_TTLOCK_REGION: client.region,
            CONF_TTLOCK_CLIENT_ID: client.client_id,
            CONF_TTLOCK_CLIENT_SECRET: client.client_secret,
            CONF_TTLOCK_USERNAME: client.username,
            **client.token_snapshot(),
        }

    @staticmethod
    def _account_key(account: Mapping[str, Any]) -> str:
        """Return a stable, non-secret account identifier."""
        value = "\0".join(
            str(account.get(key, "")).strip().lower()
            for key in (
                CONF_TTLOCK_REGION,
                CONF_TTLOCK_CLIENT_ID,
                CONF_TTLOCK_USERNAME,
            )
        )
        return (
            hashlib.sha256(value.encode()).hexdigest()[:20] if value.strip("\0") else ""
        )

    @staticmethod
    def _passcode_name(reservation_id: str) -> str:
        """Return a stable privacy-safe recovery marker."""
        marker = hashlib.sha256(reservation_id.encode()).hexdigest()[:12].upper()
        return f"Guesty-{marker}"

    @classmethod
    def _find_passcode(
        cls, passcodes: list[dict[str, Any]], password_id: Any
    ) -> dict[str, Any] | None:
        """Find one passcode by normalized positive ID."""
        normalized = cls._as_int(password_id)
        if normalized is None:
            return None
        return next(
            (
                item
                for item in passcodes
                if cls._as_int(item.get("keyboardPwdId")) == normalized
            ),
            None,
        )

    @staticmethod
    def _remote_status(item: Mapping[str, Any]) -> int | None:
        """Normalize an optional TTLock operation status."""
        if "status" not in item:
            return None
        try:
            return int(item.get("status"))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _passcode_blocks_code(cls, item: Mapping[str, Any]) -> bool:
        """Return whether an entry can still reserve its numeric code."""
        status = cls._remote_status(item)
        return status is None or status not in {2, 5, 8}

    @staticmethod
    def _fingerprint(lock_id: int, code: str, start: datetime, end: datetime) -> str:
        """Fingerprint every remote property that requires an update."""
        value = "\0".join((str(lock_id), code, start.isoformat(), end.isoformat()))
        return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def _passcode_matches(
        cls,
        item: Mapping[str, Any],
        *,
        password_id: int | None,
        code: str,
        name: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Confirm an operation that may have succeeded before transport failed."""
        item_id = cls._as_int(item.get("keyboardPwdId"))
        if password_id is not None and item_id != password_id:
            return False
        if password_id is None and item_id is None:
            return False
        status = cls._remote_status(item)
        if status is not None and status != _REMOTE_NORMAL_STATUS:
            return False
        try:
            start_ms = int(item.get("startDate"))
            end_ms = int(item.get("endDate"))
        except (TypeError, ValueError):
            return False
        expected_start = int(start.astimezone(dt_util.UTC).timestamp() * 1000)
        expected_end = int(end.astimezone(dt_util.UTC).timestamp() * 1000)
        return (
            str(item.get("keyboardPwd", "")).strip() == code
            and item.get("keyboardPwdName") == name
            and start_ms == expected_start
            and end_ms == expected_end
        )

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Listen for completed TTLock reconciliation passes."""
        self._listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._listeners.discard(listener)

        return _remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Refresh TTLock status entities."""
        for listener in tuple(self._listeners):
            listener()

    def listing_status_snapshot(self, listing_id: str) -> dict[str, Any]:
        """Return a privacy-safe TTLock delivery status."""
        if (
            not self.entry.options.get(CONF_TTLOCK_ENABLED, False)
            or listing_id not in self._mappings
        ):
            return {"ttlock_status": "not_configured"}
        data = self._coordinator.data
        if data is None:
            return {"ttlock_status": "error", "data_stale": True}
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
                    start, end = self._pin_manager.reservation_access_window(
                        reservation, listing
                    )
                except (TypeError, ValueError):
                    continue
                if end > now:
                    candidates.append(
                        (0 if start <= now < end else 1, start, end, reservation)
                    )
        cleanup_pending = any(
            isinstance(record, dict)
            and record.get("listing_id") == listing_id
            and record.get("retired")
            for record in self._records.values()
        )
        if not candidates:
            return {
                "ttlock_status": "cleanup_pending"
                if cleanup_pending
                else "no_reservation",
                "data_stale": bool(getattr(data, "data_stale", False)),
            }
        _priority, start, end, reservation = min(
            candidates, key=lambda item: (item[0], item[1], item[3].id)
        )
        lead = timedelta(
            minutes=int(
                self.entry.options.get(
                    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
                    DEFAULT_TTLOCK_PROVISION_LEAD_MINUTES,
                )
            )
        )
        provision_at = start - lead
        record = self._records.get(reservation.id)
        lock_ids = self._mapping_lock_ids(listing_id)
        snapshot: dict[str, Any] = {
            "ttlock_status": "scheduled" if now < provision_at else "pending",
            "access_start": start,
            "access_end": end,
            "provision_at": provision_at,
            "reservation_status": reservation.status,
            "data_stale": bool(getattr(data, "data_stale", False)),
            "mapped_locks": len(lock_ids),
            "provisioned_locks": 0,
        }
        if not isinstance(record, dict):
            return snapshot
        locks = record.get("locks", {})
        ready = sum(
            1
            for lock_id in lock_ids
            if isinstance(locks, dict)
            and isinstance(locks.get(str(lock_id)), dict)
            and self._state_is_verified(locks[str(lock_id)], now)
        )
        snapshot["provisioned_locks"] = ready
        retry_at = self._retry_at(record)
        if retry_at is not None:
            snapshot["retry_at"] = retry_at
        last_error = record.get("last_error")
        if isinstance(last_error, str) and last_error != "guesty_pin_pending":
            snapshot["error_reason"] = last_error
        if cleanup_pending or record.get("retired"):
            snapshot["ttlock_status"] = "cleanup_pending"
        elif ready and ready < len(lock_ids):
            snapshot["ttlock_status"] = "partial"
        elif last_error == "guesty_pin_pending":
            snapshot["ttlock_status"] = "pending"
        elif last_error == "operation_pending":
            snapshot["ttlock_status"] = "pending"
        elif last_error == "gateway_unavailable":
            snapshot["ttlock_status"] = "gateway_offline"
        elif last_error in {"code_conflict", "code_conflict_rotated"}:
            snapshot["ttlock_status"] = "conflict"
        elif retry_at is not None or last_error in {"invalid_mapping", "invalid_pin"}:
            snapshot["ttlock_status"] = "error"
        elif lock_ids and ready == len(lock_ids):
            snapshot["ttlock_status"] = "provisioned"
        return snapshot

    def diagnostics(self) -> dict[str, Any]:
        """Return operational counts without credentials, codes, or names."""
        records = self._records
        return {
            "enabled": bool(self.entry.options.get(CONF_TTLOCK_ENABLED, False)),
            "configured_locks": len(self._configured_locks),
            "mapped_listings": len(self._mappings),
            "last_reconcile_at": self._last_reconcile_at,
            "last_reconcile_result": self._last_result,
            "has_last_error": self._last_error is not None,
            "last_error": self._last_error,
            "provisioned_during_last_reconcile": self._last_provisioned,
            "changed_during_last_reconcile": self._last_changed,
            "deleted_during_last_reconcile": self._last_deleted,
            "local_records": len(records),
            "remote_passcodes": sum(
                1
                for record in records.values()
                if isinstance(record, dict)
                for state in (
                    record.get("locks", {}).values()
                    if isinstance(record.get("locks"), dict)
                    else []
                )
                if isinstance(state, dict)
                and isinstance(state.get("keyboard_pwd_id"), int)
            ),
            "retrying_records": sum(
                1
                for record in records.values()
                if isinstance(record, dict) and self._retry_at(record) is not None
            ),
        }

    def account_for_reconfigure(self) -> dict[str, Any]:
        """Return current private credentials for the in-process options flow."""
        account = dict(self._account)
        tokens = self._data.get("tokens", {})
        if isinstance(tokens, dict) and tokens.get(
            _TOKEN_ACCOUNT_KEY
        ) == self._account_key(account):
            for key in (
                CONF_TTLOCK_ACCESS_TOKEN,
                CONF_TTLOCK_REFRESH_TOKEN,
                CONF_TTLOCK_TOKEN_EXPIRES_AT,
            ):
                value = tokens.get(key)
                if isinstance(value, str):
                    account[key] = value
        if self._client is not None:
            account.update(self._client.token_snapshot())
        return account

    def _schedule_at(self, moment: datetime | None) -> None:
        """Schedule the next provisioning, retry, or checkout transition."""
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

    def _next_verification_at(self, record: Mapping[str, Any]) -> datetime | None:
        """Return the earliest periodic remote ownership/status verification."""
        locks = record.get("locks")
        if not isinstance(locks, dict):
            return None
        result: datetime | None = None
        for state in locks.values():
            if not isinstance(state, dict):
                continue
            if not isinstance(state.get("keyboard_pwd_id"), int) or not isinstance(
                state.get("fingerprint"), str
            ):
                continue
            verified_at = self._parse_time(state.get("verified_at"))
            if verified_at is None:
                return dt_util.utcnow() + timedelta(seconds=1)
            candidate = verified_at + _REMOTE_VERIFY_INTERVAL
            result = candidate if result is None or candidate < result else result
        return result

    @classmethod
    def _state_is_verified(cls, state: Mapping[str, Any], now: datetime) -> bool:
        """Return whether local readiness is backed by a recent remote read."""
        if not isinstance(state.get("keyboard_pwd_id"), int) or not isinstance(
            state.get("fingerprint"), str
        ):
            return False
        verified_at = cls._parse_time(state.get("verified_at"))
        return bool(
            verified_at is not None
            and verified_at <= now < verified_at + _REMOTE_VERIFY_INTERVAL
        )

    @classmethod
    def _allow_conflict_rotation(cls, record: dict[str, Any], now: datetime) -> bool:
        """Limit authoritative Guesty code changes caused by one provider."""
        cutoff = now - _CONFLICT_ROTATION_WINDOW
        values = record.get("conflict_rotation_times", [])
        recent: list[datetime] = []
        if isinstance(values, list):
            for value in values:
                parsed = cls._parse_time(value)
                if parsed is not None and cutoff <= parsed <= now:
                    recent.append(parsed)
        if len(recent) >= _MAX_CONFLICT_ROTATIONS_PER_WINDOW:
            record["conflict_rotation_times"] = [value.isoformat() for value in recent]
            return False
        recent.append(now)
        record["conflict_rotation_times"] = [value.isoformat() for value in recent]
        return True

    @staticmethod
    def _record_retry_failure(record: dict[str, Any], now: datetime) -> None:
        """Persist bounded exponential backoff."""
        try:
            count = min(max(int(record.get("retry_count", 0)), 0) + 1, 20)
        except (TypeError, ValueError):
            count = 1
        delay = min(
            TTLOCK_RETRY_BASE_SECONDS * (2 ** (count - 1)),
            TTLOCK_RETRY_MAX_SECONDS,
        )
        record["retry_count"] = count
        record["retry_at"] = (now + timedelta(seconds=delay)).isoformat()

    @staticmethod
    def _clear_retry(record: dict[str, Any]) -> None:
        """Clear persistent retry state after success."""
        record.pop("retry_count", None)
        record.pop("retry_at", None)

    @staticmethod
    def _retry_at(record: Mapping[str, Any]) -> datetime | None:
        """Parse a persistent retry timestamp."""
        return GuestyTTLockManager._parse_time(record.get("retry_at"))

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        """Parse one stored timestamp."""
        if not isinstance(value, str):
            return None
        try:
            parsed = dt_util.parse_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed is None or parsed.tzinfo is None:
            return None
        return parsed

    @staticmethod
    def _earlier(current: datetime | None, candidate: datetime) -> datetime:
        """Return the earlier datetime."""
        return candidate if current is None or candidate < current else current

    def _reservation_sync_order(
        self,
        reservation: GuestyReservation,
        listing: Any,
        now: datetime,
    ) -> tuple[int, datetime, str]:
        """Sort malformed reservations last without aborting the entire pass."""
        try:
            start, _end = self._pin_manager.reservation_access_window(
                reservation,
                listing,
            )
        except (TypeError, ValueError):
            start = datetime.max.replace(tzinfo=dt_util.UTC)
        return (0 if start <= now else 1, start, reservation.id)

    @staticmethod
    def _as_int(value: Any) -> int | None:
        """Return a positive integer or None."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _error_reason(err: Exception) -> str:
        """Return a stable, non-secret status reason."""
        if isinstance(err, TTLockGatewayError):
            return "gateway_unavailable"
        if isinstance(err, TTLockRateLimitError):
            return "rate_limited"
        if isinstance(err, TTLockOperationPendingError):
            return "operation_pending"
        if isinstance(err, TTLockAuthError):
            return "authentication_failed"
        if isinstance(err, GuestyAuthError):
            return "guesty_authentication_failed"
        if isinstance(err, GuestyApiError):
            return "guesty_sync_failed"
        return "ttlock_api_error"


async def async_remove_stored_ttlock_passcodes(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Best-effort remove passcodes, then erase local account credentials."""
    storage = GuestyTTLockStorage(hass, entry.entry_id)
    data = await storage.async_load()
    records = data.get("records", {})
    cleanup_complete = True
    clients: dict[str, TTLockApiClient] = {}
    if not isinstance(records, dict):
        await storage.async_remove()
        return True
    for reservation_id, record in list(records.items()):
        if not isinstance(record, dict):
            records.pop(reservation_id, None)
            continue
        record["retired"] = True
        snapshot = record.get(_ACCOUNT_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict):
            cleanup_complete = False
            continue
        key = GuestyTTLockManager._account_key(snapshot)
        try:
            client = clients.get(key)
            if client is None:
                client = TTLockApiClient.from_hass(
                    hass,
                    region=str(snapshot[CONF_TTLOCK_REGION]),
                    client_id=str(snapshot[CONF_TTLOCK_CLIENT_ID]),
                    client_secret=str(snapshot[CONF_TTLOCK_CLIENT_SECRET]),
                    username=str(snapshot.get(CONF_TTLOCK_USERNAME, "")),
                    access_token=str(snapshot.get(CONF_TTLOCK_ACCESS_TOKEN, "")),
                    refresh_token=str(snapshot.get(CONF_TTLOCK_REFRESH_TOKEN, "")),
                    token_expires_at=str(
                        snapshot.get(CONF_TTLOCK_TOKEN_EXPIRES_AT, "")
                    ),
                )
                clients[key] = client
            locks = record.get("locks", {})
            if isinstance(locks, dict):
                for raw_lock_id, state in list(locks.items()):
                    lock_id = GuestyTTLockManager._as_int(raw_lock_id)
                    password_id = (
                        state.get("keyboard_pwd_id")
                        if isinstance(state, dict)
                        else None
                    )
                    if lock_id is not None and isinstance(password_id, int):
                        passcodes = await client.async_list_passcodes(lock_id)
                        remote = GuestyTTLockManager._find_passcode(
                            passcodes, password_id
                        )
                        marker = GuestyTTLockManager._passcode_name(reservation_id)
                        if (
                            remote is not None
                            and remote.get("keyboardPwdName") == marker
                        ):
                            await client.async_delete_passcode(
                                lock_id=lock_id, password_id=password_id
                            )
                        elif remote is not None:
                            _LOGGER.warning(
                                "Refusing to remove a TTLock passcode whose "
                                "reservation marker changed"
                            )
                    locks.pop(raw_lock_id, None)
        except (KeyError, TypeError, ValueError, TTLockApiError):
            cleanup_complete = False
            _LOGGER.warning(
                "Could not remove a managed TTLock passcode during integration removal"
            )
        else:
            records.pop(reservation_id, None)
        await storage.async_save(data)
    # TTLock passcodes retain their remote end time. With the config entry
    # deleted no durable retry owner remains, so do not orphan OAuth secrets in
    # a store that can never be reached again.
    await storage.async_remove()
    return cleanup_complete and not records
