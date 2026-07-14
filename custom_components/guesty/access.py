"""Reservation-scoped, time-limited guest access for Home Assistant locks."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    ACCESS_ACTION_NONCE_SECONDS,
    ACCESS_MAX_REQUEST_BYTES,
    ACCESS_RATE_LIMIT_MAX_ACTIONS,
    ACCESS_RATE_LIMIT_WINDOW_SECONDS,
    ACCESS_TOKEN_BYTES,
    ACCESS_UNLOCK_COOLDOWN_SECONDS,
    ACCESS_URL_PATH,
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LATE_MINUTES,
    CONF_ACCESS_LOCK_MAPPINGS,
    DEFAULT_ACCESS_CUSTOM_FIELD,
    DEFAULT_ACCESS_EARLY_MINUTES,
    DEFAULT_ACCESS_LATE_MINUTES,
    DOMAIN,
    EVENT_DOOR_ACCESS,
)
from .coordinator import GuestyDataUpdateCoordinator
from .models import GuestyReservation

_LOGGER = logging.getLogger(__name__)

ACCESS_STORAGE_VERSION = 1
ACCESS_STORAGE_KEY = "guesty_access"
ACCESS_MANAGERS = "access_managers"
ACCESS_VIEW_REGISTERED = "access_view_registered"

ACCESS_PORTAL_TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": {
        "title": "Türzugang",
        "open": "{door} öffnen",
        "opening": "Wird geöffnet …",
        "unavailable": "Zugang nicht verfügbar",
        "invalid_request": "Ungültige Anfrage",
        "invalid_nonce": "Sitzung wurde aktualisiert",
        "cooldown": "Bitte kurz warten und erneut versuchen",
        "lock_unavailable": "Das Schloss ist momentan nicht erreichbar",
        "unlock_failed": "Die Tür konnte nicht geöffnet werden",
        "unlocked": "{door} wurde geöffnet",
        "length_required": "Länge der Anfrage fehlt",
        "request_too_large": "Anfrage zu groß",
        "network_error": "Verbindung fehlgeschlagen. Bitte erneut versuchen",
    },
    "en": {
        "title": "Door access",
        "open": "Open {door}",
        "opening": "Opening …",
        "unavailable": "Access unavailable",
        "invalid_request": "Invalid request",
        "invalid_nonce": "Session refreshed",
        "cooldown": "Please wait a moment and try again",
        "lock_unavailable": "The lock is currently unavailable",
        "unlock_failed": "The door could not be opened",
        "unlocked": "{door} was opened",
        "length_required": "Request length missing",
        "request_too_large": "Request too large",
        "network_error": "Connection failed. Please try again",
    },
    "es": {
        "title": "Acceso a la puerta",
        "open": "Abrir {door}",
        "opening": "Abriendo …",
        "unavailable": "Acceso no disponible",
        "invalid_request": "Solicitud no válida",
        "invalid_nonce": "Sesión actualizada",
        "cooldown": "Espera un momento e inténtalo de nuevo",
        "lock_unavailable": "La cerradura no está disponible en este momento",
        "unlock_failed": "No se pudo abrir la puerta",
        "unlocked": "Se abrió {door}",
        "length_required": "Falta la longitud de la solicitud",
        "request_too_large": "La solicitud es demasiado grande",
        "network_error": "Error de conexión. Inténtalo de nuevo",
    },
    "fr": {
        "title": "Accès à la porte",
        "open": "Ouvrir {door}",
        "opening": "Ouverture …",
        "unavailable": "Accès indisponible",
        "invalid_request": "Requête non valide",
        "invalid_nonce": "Session actualisée",
        "cooldown": "Veuillez patienter un instant et réessayer",
        "lock_unavailable": "La serrure est actuellement indisponible",
        "unlock_failed": "La porte n’a pas pu être ouverte",
        "unlocked": "{door} a été ouverte",
        "length_required": "Longueur de la requête manquante",
        "request_too_large": "Requête trop volumineuse",
        "network_error": "Échec de la connexion. Veuillez réessayer",
    },
}


def _preferred_language(accept_language: str | None) -> str:
    """Select a supported portal language from an HTTP Accept-Language value."""
    if not accept_language:
        return "en"
    candidates: list[tuple[float, int, str]] = []
    for order, raw_item in enumerate(accept_language.split(",")):
        parts = [part.strip() for part in raw_item.split(";")]
        language = parts[0].lower().split("-", 1)[0]
        if language not in ACCESS_PORTAL_TRANSLATIONS:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if not parameter.lower().startswith("q="):
                continue
            try:
                quality = float(parameter[2:])
            except ValueError:
                quality = 0.0
        if quality > 0:
            candidates.append((-min(quality, 1.0), order, language))
    return min(candidates)[2] if candidates else "en"


def _portal_text(language: str, key: str, **values: str) -> str:
    """Return one translated portal string with an English fallback."""
    translations = ACCESS_PORTAL_TRANSLATIONS.get(
        language, ACCESS_PORTAL_TRANSLATIONS["en"]
    )
    return translations.get(key, ACCESS_PORTAL_TRANSLATIONS["en"][key]).format(**values)


class GuestyAccessStorage:
    """Persist access secrets and token metadata separately from API cache."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the access store."""
        self._store = Store(
            hass,
            ACCESS_STORAGE_VERSION,
            f"{ACCESS_STORAGE_KEY}_{entry_id}",
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> dict[str, Any]:
        """Load and validate the top-level structure."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {"secret": None, "records": {}, "resolved_field": {}}
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        if not isinstance(data.get("resolved_field"), dict):
            data["resolved_field"] = {}
        return data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Save access data."""
        await self._store.async_save(data)

    async def async_remove(self) -> None:
        """Remove access data."""
        await self._store.async_remove()


class GuestyAccessManager:
    """Create, validate, revoke, and audit reservation access links."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GuestyApiClient,
        coordinator: GuestyDataUpdateCoordinator,
    ) -> None:
        """Initialize the access manager."""
        self.hass = hass
        self.entry = entry
        self._client = client
        self._coordinator = coordinator
        self._storage = GuestyAccessStorage(hass, entry.entry_id)
        self._data: dict[str, Any] = {}
        self._secret = b""
        self._token_index: dict[str, str] = {}
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_pending = False
        self._unloaded = False
        self._last_reconcile_at: str | None = None
        self._last_reconcile_result = "never"
        self._last_reconcile_error: str | None = None
        self._last_eligible_count = 0
        self._last_published_count = 0
        self._last_recovered_count = 0
        self._validated_field_references: set[str] = set()
        self._listeners: set[Callable[[], None]] = set()
        self._last_action: dict[tuple[str, int], float] = {}
        self._action_windows: defaultdict[tuple[str, int], deque[float]] = defaultdict(
            deque
        )

    async def async_setup(self) -> None:
        """Load persistent state and reconcile it with current reservations."""
        self._data = await self._storage.async_load()
        encoded_secret = self._data.get("secret")
        try:
            secret = base64.urlsafe_b64decode(encoded_secret.encode())
        except (AttributeError, ValueError):
            secret = b""
        if len(secret) != ACCESS_TOKEN_BYTES:
            secret = secrets.token_bytes(ACCESS_TOKEN_BYTES)
            self._data["secret"] = base64.urlsafe_b64encode(secret).decode()
            await self._storage.async_save(self._data)
        self._secret = secret
        self._rebuild_token_index()
        self.async_schedule_reconcile()

    async def async_unload(self) -> None:
        """Stop scheduled work and reject future requests."""
        self._unloaded = True
        if self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
        self._token_index.clear()

    def async_schedule_reconcile(self) -> None:
        """Debounce coordinator updates into one access reconciliation."""
        if self._unloaded:
            return
        self._reconcile_pending = True
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._reconcile_task = self.hass.async_create_task(
            self._async_delayed_reconcile(),
            "guesty_access_reconcile",
        )

    async def _async_delayed_reconcile(self) -> None:
        """Debounce bursts without losing updates arriving during a write."""
        try:
            while self._reconcile_pending and not self._unloaded:
                self._reconcile_pending = False
                await asyncio.sleep(0.5)
                try:
                    await self.async_reconcile()
                except (GuestyApiError, GuestyAuthError) as err:
                    self._last_reconcile_result = "error"
                    self._last_reconcile_error = str(err)[:500]
                    _LOGGER.warning("Guest access synchronization deferred: %s", err)
                except Exception as err:
                    self._last_reconcile_result = "error"
                    self._last_reconcile_error = type(err).__name__
                    _LOGGER.exception("Unexpected guest access synchronization failure")
                finally:
                    self._notify_listeners()
        finally:
            # Close the small race where a listener sets pending just as the
            # loop finishes but still sees this task as running.
            self._reconcile_task = None
            if self._reconcile_pending and not self._unloaded:
                self.async_schedule_reconcile()

    async def async_reconcile(self) -> None:
        """Synchronize active reservations and revoke obsolete access."""
        async with self._reconcile_lock:
            self._last_reconcile_at = dt_util.utcnow().isoformat()
            self._last_reconcile_result = "running"
            self._last_reconcile_error = None
            self._last_published_count = 0
            self._last_recovered_count = 0
            records = self._records
            mappings = self._mappings
            enabled = bool(self.entry.options.get(CONF_ACCESS_ENABLED, False))
            coordinator_data = self._coordinator.data
            now = dt_util.utcnow()
            eligible: dict[str, tuple[GuestyReservation, str]] = {}

            if enabled and coordinator_data:
                for reservation in coordinator_data.reservations:
                    if (
                        reservation.listing_id not in mappings
                        or not reservation.is_active_status()
                    ):
                        continue
                    listing = coordinator_data.listings.get(reservation.listing_id)
                    if listing is None:
                        continue
                    try:
                        _start, end = self._access_window(reservation)
                    except ValueError:
                        continue
                    if end <= now:
                        continue
                    fingerprint = self._reservation_fingerprint(reservation)
                    eligible[reservation.id] = (reservation, fingerprint)
            self._last_eligible_count = len(eligible)

            for reservation_id, record in list(records.items()):
                if reservation_id not in eligible:
                    record["revoked"] = True

            for reservation_id, (reservation, fingerprint) in eligible.items():
                existing = records.get(reservation_id)
                if not isinstance(existing, dict):
                    existing = {}
                if existing.get("fingerprint") != fingerprint:
                    version = int(existing.get("version", 0)) + 1
                    token = self._token_for(reservation_id, version)
                    existing.update(
                        {
                            "version": version,
                            "token_hash": self._token_hash(token),
                            "fingerprint": fingerprint,
                            "listing_id": reservation.listing_id,
                            "url_hash": None,
                            "field_synced": False,
                            "write_verified": False,
                        }
                    )
                    existing.pop("recovery_marker", None)
                existing["revoked"] = False
                records[reservation_id] = existing

            # Revocation becomes effective locally before any remote API request.
            self._rebuild_token_index()
            await self._storage.async_save(self._data)

            if not enabled:
                await self._async_cleanup_revoked_records()
                self._last_reconcile_result = "disabled"
                return

            field_reference = str(
                self.entry.options.get(
                    CONF_ACCESS_CUSTOM_FIELD, DEFAULT_ACCESS_CUSTOM_FIELD
                )
            ).strip()
            if not field_reference or not mappings:
                await self._async_cleanup_revoked_records()
                self._last_reconcile_result = "not_configured"
                return

            field_id = await self._async_resolve_field(field_reference)
            try:
                base_url = get_url(
                    self.hass,
                    prefer_external=True,
                    allow_internal=False,
                ).rstrip("/")
            except NoURLAvailableError:
                _LOGGER.warning(
                    "Guest access is enabled but Home Assistant has no external URL"
                )
                self._last_reconcile_result = "error"
                self._last_reconcile_error = "external_url_missing"
                return
            if urlsplit(base_url).scheme != "https":
                _LOGGER.error(
                    "Guest access requires an external Home Assistant HTTPS URL"
                )
                self._last_reconcile_result = "error"
                self._last_reconcile_error = "external_url_not_https"
                return

            for reservation_id in sorted(eligible):
                record = records[reservation_id]
                field_id, publish_error = await self._async_publish_access_link(
                    reservation_id,
                    record,
                    field_reference,
                    field_id,
                    base_url,
                )
                if publish_error is not None:
                    _LOGGER.warning(
                        "Could not publish a Guesty access link for reservation %s: %s",
                        reservation_id,
                        publish_error,
                    )
                    self._last_reconcile_result = "partial"
                    self._last_reconcile_error = str(publish_error)[:500]

            await self._async_cleanup_revoked_records()
            await self._storage.async_save(self._data)
            if self._last_reconcile_result == "running":
                self._last_reconcile_result = "ok"

    async def _async_publish_access_link(
        self,
        reservation_id: str,
        record: dict[str, Any],
        field_reference: str,
        field_id: str,
        base_url: str,
    ) -> tuple[str, GuestyApiError | GuestyAuthError | None]:
        """Publish one link and perform one bounded self-healing retry."""
        old_field_id = record.get("field_id")
        if isinstance(old_field_id, str) and old_field_id != field_id:
            self._rotate_record_token(reservation_id, record)
            record["field_id"] = field_id
            self._last_recovered_count += 1
            # Invalidate the old token before touching the remote field.
            await self._storage.async_save(self._data)
            await self._async_clear_obsolete_field(reservation_id, old_field_id)
        else:
            record["field_id"] = field_id

        access_url, url_hash = self._access_url_and_hash(
            base_url, reservation_id, record
        )
        if (
            record.get("url_hash") == url_hash
            and record.get("field_synced")
            and record.get("write_verified")
        ):
            return field_id, None

        try:
            await self._client.async_update_reservation_custom_field(
                reservation_id,
                field_id,
                access_url,
            )
        except GuestyAuthError as err:
            return field_id, err
        except GuestyApiError as first_error:
            recovery = await self._async_prepare_link_recovery(
                reservation_id,
                record,
                field_reference,
                field_id,
                base_url,
            )
            if recovery is None:
                return field_id, first_error
            field_id, access_url, url_hash = recovery
            try:
                await self._client.async_update_reservation_custom_field(
                    reservation_id,
                    field_id,
                    access_url,
                )
            except (GuestyApiError, GuestyAuthError) as recovery_error:
                return field_id, recovery_error
            self._last_recovered_count += 1

        record["url_hash"] = url_hash
        record["field_synced"] = True
        record["write_verified"] = True
        record.pop("recovery_marker", None)
        self._last_published_count += 1
        return field_id, None

    async def _async_prepare_link_recovery(
        self,
        reservation_id: str,
        record: dict[str, Any],
        field_reference: str,
        current_field_id: str,
        base_url: str,
    ) -> tuple[str, str, str] | None:
        """Refresh a stale field ID and rotate a failed bearer URL once."""
        fingerprint = str(record.get("fingerprint", ""))
        current_marker = f"{current_field_id}:{fingerprint}"
        if record.get("recovery_marker") == current_marker:
            return None

        fresh_field_id = await self._async_resolve_field(
            field_reference,
            force_refresh=True,
        )
        recovery_marker = f"{fresh_field_id}:{fingerprint}"
        if record.get("recovery_marker") == recovery_marker:
            return None

        old_field_id = record.get("field_id")
        self._rotate_record_token(reservation_id, record)
        record["field_id"] = fresh_field_id
        record["recovery_marker"] = recovery_marker
        # Invalidate the old token locally before retrying the remote write.
        await self._storage.async_save(self._data)
        if isinstance(old_field_id, str) and old_field_id != fresh_field_id:
            await self._async_clear_obsolete_field(reservation_id, old_field_id)
        access_url, url_hash = self._access_url_and_hash(
            base_url, reservation_id, record
        )
        return fresh_field_id, access_url, url_hash

    async def _async_clear_obsolete_field(
        self,
        reservation_id: str,
        field_id: str,
    ) -> None:
        """Best-effort cleanup of an access value attached to an old field ID."""
        try:
            await self._client.async_delete_reservation_custom_field(
                reservation_id,
                field_id,
            )
        except (GuestyApiError, GuestyAuthError) as err:
            _LOGGER.warning(
                "Could not clear an obsolete Guesty access field for reservation %s: %s",
                reservation_id,
                err,
            )

    def _rotate_record_token(
        self,
        reservation_id: str,
        record: dict[str, Any],
    ) -> None:
        """Rotate one bearer token and immediately invalidate its predecessor."""
        try:
            version = int(record.get("version", 0)) + 1
        except (TypeError, ValueError):
            version = 1
        token = self._token_for(reservation_id, version)
        record.update(
            {
                "version": version,
                "token_hash": self._token_hash(token),
                "url_hash": None,
                "field_synced": False,
                "write_verified": False,
            }
        )
        self._rebuild_token_index()

    def _access_url_and_hash(
        self,
        base_url: str,
        reservation_id: str,
        record: Mapping[str, Any],
    ) -> tuple[str, str]:
        """Build an access URL and its non-secret persistence fingerprint."""
        token = self._token_for(reservation_id, int(record["version"]))
        access_url = f"{base_url}{ACCESS_URL_PATH}/{self.entry.entry_id}/{token}"
        return access_url, hashlib.sha256(access_url.encode()).hexdigest()

    def diagnostics(self) -> dict[str, Any]:
        """Return a privacy-preserving access synchronization summary."""
        records = self._records
        return {
            "last_reconcile_at": self._last_reconcile_at,
            "last_reconcile_result": self._last_reconcile_result,
            "has_last_reconcile_error": self._last_reconcile_error is not None,
            "last_reconcile_error": self._last_reconcile_error,
            "eligible_reservations": self._last_eligible_count,
            "published_during_last_reconcile": self._last_published_count,
            "recovered_during_last_reconcile": self._last_recovered_count,
            "local_records": len(records),
            "synced_records": sum(
                1 for record in records.values() if record.get("field_synced")
            ),
            "verified_records": sum(
                1 for record in records.values() if record.get("write_verified")
            ),
        }

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Listen for completed access reconciliation passes."""
        self._listeners.add(listener)

        @callback
        def _remove_listener() -> None:
            self._listeners.discard(listener)

        return _remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Update access entities after local or remote state changes."""
        for listener in tuple(self._listeners):
            listener()

    def listing_access_snapshot(self, listing_id: str) -> dict[str, Any]:
        """Return the current or next reservation link for one listing."""
        if (
            not self.entry.options.get(CONF_ACCESS_ENABLED, False)
            or listing_id not in self._mappings
        ):
            return {"status": "not_configured"}
        data = self._coordinator.data
        if data is None:
            return {"status": "error"}

        now = dt_util.utcnow()
        candidates: list[tuple[int, datetime, datetime, GuestyReservation]] = []
        for reservation in data.reservations:
            if (
                reservation.listing_id != listing_id
                or not reservation.is_active_status()
            ):
                continue
            try:
                start, end = self._access_window(reservation)
            except ValueError:
                continue
            if end <= now:
                continue
            priority = 0 if start <= now < end else 1
            candidates.append((priority, start, end, reservation))

        if not candidates:
            return {"status": "no_reservation"}
        _priority, start, end, reservation = min(
            candidates,
            key=lambda item: (item[0], item[1], item[3].id),
        )
        record = self._records.get(reservation.id)
        if not isinstance(record, dict) or record.get("revoked"):
            return {
                "status": "pending",
                "reservation": reservation,
                "access_start": start,
                "access_end": end,
                "access_active": start <= now < end,
            }

        access_url = self._access_url_for_record(reservation.id, record)
        field_synced = bool(record.get("field_synced"))
        verified = bool(record.get("write_verified"))
        synchronized = field_synced and verified
        status = "synced" if synchronized else "pending"
        if access_url is None or (
            not synchronized and self._last_reconcile_result in {"error", "partial"}
        ):
            status = "error"
        return {
            "status": status,
            "access_url": access_url,
            "reservation": reservation,
            "access_start": start,
            "access_end": end,
            "access_active": start <= now < end,
            "field_synced": field_synced,
            "write_verified": verified,
        }

    def _access_url_for_record(
        self,
        reservation_id: str,
        record: Mapping[str, Any],
    ) -> str | None:
        """Recreate a bearer URL without persisting it in Home Assistant storage."""
        version = record.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_internal=False,
            ).rstrip("/")
        except NoURLAvailableError:
            return None
        if urlsplit(base_url).scheme != "https":
            return None
        token = self._token_for(reservation_id, version)
        return f"{base_url}{ACCESS_URL_PATH}/{self.entry.entry_id}/{token}"

    async def _async_resolve_field(
        self,
        reference: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Resolve and cache a configured field, revalidating it after reload."""
        cached = self._data.get("resolved_field")
        if (
            not force_refresh
            and reference in self._validated_field_references
            and isinstance(cached, dict)
            and cached.get("reference") == reference
            and isinstance(cached.get("id"), str)
        ):
            return cached["id"]
        field_id = await self._client.async_resolve_custom_field(reference)
        self._data["resolved_field"] = {"reference": reference, "id": field_id}
        self._validated_field_references.add(reference)
        await self._storage.async_save(self._data)
        return field_id

    async def _async_cleanup_revoked_records(self) -> None:
        """Clear Guesty fields for revoked records and retain failed tombstones."""
        for reservation_id, record in list(self._records.items()):
            if not record.get("revoked"):
                continue
            if record.get("field_synced") and isinstance(record.get("field_id"), str):
                try:
                    await self._client.async_delete_reservation_custom_field(
                        reservation_id,
                        record["field_id"],
                    )
                except (GuestyApiError, GuestyAuthError) as err:
                    _LOGGER.warning(
                        "Access is revoked locally, but its Guesty field could not "
                        "yet be cleared for reservation %s: %s",
                        reservation_id,
                        err,
                    )
                    continue
            self._records.pop(reservation_id, None)
        self._rebuild_token_index()

    async def async_get_portal(
        self,
        token: str,
        language: str = "de",
        *,
        notice: str | None = None,
        notice_kind: str = "success",
        status: int = 200,
    ) -> web.Response:
        """Return a non-operative page or the active door controls."""
        validated = self._validate_token(token)
        if validated is None:
            return self._page(
                _portal_text(language, "unavailable"),
                language=language,
                status=404,
            )
        _reservation_id, _reservation, doors = validated
        return self._portal_page(
            token,
            doors,
            language,
            notice=notice,
            notice_kind=notice_kind,
            status=status,
        )

    async def async_unlock(
        self,
        token: str,
        door_value: str,
        nonce: str,
        language: str = "de",
        *,
        as_json: bool = False,
    ) -> web.Response:
        """Validate an action and unlock only a server-selected lock entity."""
        validated = self._validate_token(token)
        if validated is None:
            return self._action_response(
                token,
                None,
                language,
                "unavailable",
                "unavailable",
                status=404,
                as_json=as_json,
            )
        reservation_id, reservation, doors = validated
        try:
            door_index = int(door_value)
            if door_index < 0:
                raise IndexError
            door = doors[door_index]
        except (ValueError, IndexError):
            return self._action_response(
                token,
                doors,
                language,
                "invalid_request",
                "invalid_request",
                status=400,
                as_json=as_json,
            )
        if not self._valid_action_nonce(token, door_index, nonce):
            self._fire_audit(reservation, door.get("entity_id"), "invalid_request")
            return self._action_response(
                token,
                doors,
                language,
                "invalid_nonce",
                "invalid_nonce",
                status=403,
                as_json=as_json,
            )

        rate_result = self._check_rate_limit(reservation_id, door_index)
        if rate_result is not None:
            self._fire_audit(reservation, door.get("entity_id"), rate_result)
            return self._action_response(
                token,
                doors,
                language,
                "cooldown",
                rate_result,
                status=429,
                as_json=as_json,
            )

        entity_id = door["entity_id"]
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN}
            or not valid_entity_id(entity_id)
            or not entity_id.startswith("lock.")
        ):
            self._fire_audit(reservation, entity_id, "lock_unavailable")
            return self._action_response(
                token,
                doors,
                language,
                "lock_unavailable",
                "lock_unavailable",
                status=503,
                as_json=as_json,
            )

        try:
            async with asyncio.timeout(15):
                await self.hass.services.async_call(
                    "lock",
                    "unlock",
                    target={"entity_id": entity_id},
                    blocking=True,
                )
        except Exception as err:
            # Never expose integration or lock details to the public response.
            _LOGGER.warning("Guest access could not unlock %s: %s", entity_id, err)
            self._fire_audit(reservation, entity_id, "unlock_failed")
            return self._action_response(
                token,
                doors,
                language,
                "unlock_failed",
                "unlock_failed",
                status=503,
                as_json=as_json,
            )

        self._fire_audit(reservation, entity_id, "unlocked")
        return self._action_response(
            token,
            doors,
            language,
            "unlocked",
            "unlocked",
            status=200,
            as_json=as_json,
            door=door["name"],
        )

    def _validate_token(
        self, token: str
    ) -> tuple[str, GuestyReservation, list[dict[str, str]]] | None:
        """Validate token, current reservation state, time, and mapping."""
        if self._unloaded or not self.entry.options.get(CONF_ACCESS_ENABLED, False):
            return None
        if len(token) < 32 or len(token) > 128:
            return None
        token_hash = self._token_hash(token)
        reservation_id = self._token_index.get(token_hash)
        if reservation_id is None:
            return None
        record = self._records.get(reservation_id)
        if (
            not isinstance(record, dict)
            or record.get("revoked")
            or not hmac.compare_digest(str(record.get("token_hash", "")), token_hash)
        ):
            return None

        data = self._coordinator.data
        # Physical access fails closed when Guesty data is explicitly stale.
        if data is None or data.data_stale:
            return None
        reservation = next(
            (item for item in data.reservations if item.id == reservation_id), None
        )
        if reservation is None or not reservation.is_active_status():
            return None
        doors = self._mappings.get(reservation.listing_id)
        if not doors:
            return None
        try:
            fingerprint = self._reservation_fingerprint(reservation)
            start, end = self._access_window(reservation)
        except ValueError:
            return None
        if record.get("fingerprint") != fingerprint:
            return None
        now = dt_util.utcnow()
        if not start <= now < end:
            return None
        return reservation_id, reservation, doors

    def _access_window(
        self, reservation: GuestyReservation
    ) -> tuple[datetime, datetime]:
        """Return the configured access window for a reservation."""
        data = self._coordinator.data
        if data is None or reservation.listing_id not in data.listings:
            raise ValueError("Listing not available")
        start, end = reservation.stay_datetimes(data.listings[reservation.listing_id])
        early = int(
            self.entry.options.get(
                CONF_ACCESS_EARLY_MINUTES, DEFAULT_ACCESS_EARLY_MINUTES
            )
        )
        late = int(
            self.entry.options.get(
                CONF_ACCESS_LATE_MINUTES, DEFAULT_ACCESS_LATE_MINUTES
            )
        )
        return start - timedelta(minutes=early), end + timedelta(minutes=late)

    def _reservation_fingerprint(self, reservation: GuestyReservation) -> str:
        """Hash every server-side permission input to invalidate stale tokens."""
        start, end = self._access_window(reservation)
        payload = {
            "listing_id": reservation.listing_id,
            "active": reservation.is_active_status(),
            "guest_name": reservation.guest_name or "",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "doors": self._mappings.get(reservation.listing_id, []),
            "custom_field": str(
                self.entry.options.get(
                    CONF_ACCESS_CUSTOM_FIELD, DEFAULT_ACCESS_CUSTOM_FIELD
                )
            ).strip(),
        }
        return hmac.new(
            self._secret,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()

    @property
    def _records(self) -> dict[str, dict[str, Any]]:
        """Return the validated record mapping."""
        records = self._data.setdefault("records", {})
        if not isinstance(records, dict):
            records = {}
            self._data["records"] = records
        return records

    @property
    def _mappings(self) -> dict[str, list[dict[str, str]]]:
        """Return only valid one-or-two-lock listing mappings."""
        raw = self.entry.options.get(CONF_ACCESS_LOCK_MAPPINGS, {})
        if not isinstance(raw, Mapping):
            return {}
        mappings: dict[str, list[dict[str, str]]] = {}
        for listing_id, value in raw.items():
            if not isinstance(listing_id, str) or not isinstance(value, list):
                continue
            doors: list[dict[str, str]] = []
            for item in value[:2]:
                if not isinstance(item, Mapping):
                    continue
                entity_id = item.get("entity_id")
                name = item.get("name")
                if (
                    isinstance(entity_id, str)
                    and entity_id.startswith("lock.")
                    and valid_entity_id(entity_id)
                ):
                    doors.append(
                        {
                            "entity_id": entity_id,
                            "name": (str(name or "Tür").strip() or "Tür")[:80],
                        }
                    )
            if doors:
                mappings[listing_id] = doors
        return mappings

    def _token_for(self, reservation_id: str, version: int) -> str:
        """Derive an opaque token without storing the bearer value."""
        digest = hmac.new(
            self._secret,
            f"{self.entry.entry_id}:{reservation_id}:{version}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def _token_hash(token: str) -> str:
        """Hash a public bearer token for lookup and storage."""
        return hashlib.sha256(token.encode()).hexdigest()

    def _rebuild_token_index(self) -> None:
        """Rebuild the in-memory token lookup without revoked records."""
        self._token_index = {
            record["token_hash"]: reservation_id
            for reservation_id, record in self._records.items()
            if isinstance(record, dict)
            and not record.get("revoked")
            and isinstance(record.get("token_hash"), str)
        }

    def _action_nonce(
        self, token: str, door_index: int, bucket: int | None = None
    ) -> str:
        """Create a short-lived, action-specific CSRF token."""
        if bucket is None:
            bucket = int(time.time() // ACCESS_ACTION_NONCE_SECONDS)
        return hmac.new(
            self._secret,
            f"{token}:{door_index}:{bucket}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _valid_action_nonce(self, token: str, door_index: int, nonce: str) -> bool:
        """Accept the current or immediately previous nonce bucket."""
        bucket = int(time.time() // ACCESS_ACTION_NONCE_SECONDS)
        return any(
            hmac.compare_digest(self._action_nonce(token, door_index, candidate), nonce)
            for candidate in (bucket, bucket - 1)
        )

    def _check_rate_limit(self, reservation_id: str, door_index: int) -> str | None:
        """Apply a cooldown and a rolling per-reservation action limit."""
        key = (reservation_id, door_index)
        now = time.monotonic()
        last = self._last_action.get(key)
        if last is not None and now - last < ACCESS_UNLOCK_COOLDOWN_SECONDS:
            return "cooldown"
        window = self._action_windows[key]
        while window and now - window[0] >= ACCESS_RATE_LIMIT_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= ACCESS_RATE_LIMIT_MAX_ACTIONS:
            return "rate_limited"
        window.append(now)
        self._last_action[key] = now
        return None

    def _fire_audit(
        self,
        reservation: GuestyReservation,
        entity_id: str | None,
        result: str,
    ) -> None:
        """Fire a local audit event without guest details or bearer tokens."""
        self.hass.bus.async_fire(
            EVENT_DOOR_ACCESS,
            {
                "reservation_id": reservation.id,
                "listing_id": reservation.listing_id,
                "entity_id": entity_id,
                "result": result,
            },
        )

    def _action_nonces(
        self,
        token: str,
        doors: list[dict[str, str]],
    ) -> dict[str, str]:
        """Return fresh per-door nonces for a validated bearer URL."""
        return {
            str(index): self._action_nonce(token, index) for index in range(len(doors))
        }

    def _action_response(
        self,
        token: str,
        doors: list[dict[str, str]] | None,
        language: str,
        message_key: str,
        code: str,
        *,
        status: int,
        as_json: bool,
        door: str = "",
    ) -> web.Response:
        """Return an AJAX result or the same reusable controls as a fallback."""
        message = _portal_text(language, message_key, door=door)
        if as_json:
            response = web.json_response(
                {
                    "ok": status < 400,
                    "code": code,
                    "message": message,
                    "nonces": self._action_nonces(token, doors) if doors else {},
                },
                status=status,
            )
            response.headers.update(self._security_headers())
            return response
        if doors is None:
            return self._page(message, language=language, status=status)
        return self._portal_page(
            token,
            doors,
            language,
            notice=message,
            notice_kind="success" if status < 400 else "error",
            status=status,
        )

    def _portal_page(
        self,
        token: str,
        doors: list[dict[str, str]],
        language: str,
        *,
        notice: str | None = None,
        notice_kind: str = "success",
        status: int = 200,
    ) -> web.Response:
        """Render persistent door controls enhanced with same-origin AJAX."""
        language = language if language in ACCESS_PORTAL_TRANSLATIONS else "en"
        nonces = self._action_nonces(token, doors)
        opening_label = html.escape(_portal_text(language, "opening"), quote=True)
        buttons = []
        for index, door in enumerate(doors):
            label = html.escape(_portal_text(language, "open", door=door["name"]))
            buttons.append(
                '<form method="post">'
                f'<input type="hidden" name="door" value="{index}">'
                f'<input type="hidden" name="nonce" value="{nonces[str(index)]}">'
                f'<button type="submit" data-busy-label="{opening_label}">'
                f"{label}</button></form>"
            )

        script_nonce = secrets.token_urlsafe(18)
        escaped_notice = html.escape(notice or "")
        notice_class = "success" if notice_kind == "success" else "error"
        notice_hidden = "" if notice else " hidden"
        network_error = html.escape(_portal_text(language, "network_error"), quote=True)
        title = html.escape(_portal_text(language, "title"))
        document = f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:1.25rem;
background:#f5f7fa;color:#14213d}}main{{background:white;padding:2rem;border-radius:1rem;
box-shadow:0 .25rem 1.5rem #0002}}h1{{margin-top:0}}form{{margin:1rem 0}}
button{{width:100%;padding:1rem;border:0;border-radius:.75rem;background:#0b57d0;
color:white;font-size:1.05rem;font-weight:600;cursor:pointer}}
button:disabled{{cursor:wait;opacity:.65}}.notice{{margin:0 0 1.25rem;padding:.9rem 1rem;
border-radius:.75rem;font-weight:600}}.notice.success{{background:#e8f5e9;color:#176b2c}}
.notice.error{{background:#ffebee;color:#9b1c1c}}.notice[hidden]{{display:none}}
</style></head><body><main data-network-error="{network_error}">
<h1>{title}</h1><div id="notice" class="notice {notice_class}" role="status"
aria-live="polite"{notice_hidden}>{escaped_notice}</div>{"".join(buttons)}</main>
<script nonce="{script_nonce}">
(() => {{
  const main = document.querySelector("main");
  const notice = document.getElementById("notice");
  let noticeTimer;
  const hideNotice = () => {{ notice.hidden = true; }};
  const showNotice = (message, success) => {{
    clearTimeout(noticeTimer);
    notice.textContent = message;
    notice.className = `notice ${{success ? "success" : "error"}}`;
    notice.hidden = false;
    noticeTimer = setTimeout(hideNotice, 5000);
  }};
  if (!notice.hidden) noticeTimer = setTimeout(hideNotice, 5000);
  const updateNonces = (nonces) => {{
    if (!nonces) return;
    document.querySelectorAll("form").forEach((form) => {{
      const door = form.elements.door.value;
      if (nonces[door]) form.elements.nonce.value = nonces[door];
    }});
  }};
  document.querySelectorAll("form").forEach((form) => {{
    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const button = form.querySelector("button");
      const originalLabel = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = button.dataset.busyLabel;
      try {{
        for (let attempt = 0; attempt < 2; attempt += 1) {{
          const response = await fetch(window.location.href, {{
            method: "POST",
            headers: {{
              "Accept": "application/json",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
            }},
            body: new URLSearchParams(new FormData(form)),
            credentials: "same-origin",
            cache: "no-store",
            redirect: "error"
          }});
          const payload = await response.json();
          updateNonces(payload.nonces);
          if (response.status === 403 && payload.code === "invalid_nonce" && attempt === 0) {{
            continue;
          }}
          showNotice(payload.message || main.dataset.networkError, response.ok);
          break;
        }}
      }} catch (_error) {{
        showNotice(main.dataset.networkError, false);
      }} finally {{
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = originalLabel;
      }}
    }});
  }});
}})();
</script></body></html>"""
        return web.Response(
            text=document,
            content_type="text/html",
            status=status,
            headers=self._security_headers(script_nonce),
        )

    @staticmethod
    def _security_headers(script_nonce: str | None = None) -> dict[str, str]:
        """Return restrictive headers shared by portal HTML and AJAX responses."""
        content_security_policy = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if script_nonce:
            content_security_policy += f"; script-src 'nonce-{script_nonce}'"
        return {
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": content_security_policy,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }

    @classmethod
    def _page(
        cls,
        title: str,
        *,
        language: str = "en",
        body: str = "",
        status: int,
    ) -> web.Response:
        """Return a self-contained non-interactive page without fallback text."""
        language = language if language in ACCESS_PORTAL_TRANSLATIONS else "en"
        escaped_title = html.escape(title)
        document = f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escaped_title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;
padding:1.25rem;background:#f5f7fa;color:#14213d}}main{{background:white;padding:2rem;
border-radius:1rem;box-shadow:0 0.25rem 1.5rem #0002}}form{{margin:1rem 0}}
button{{width:100%;padding:1rem;border:0;border-radius:.75rem;background:#0b57d0;
color:white;font-size:1.05rem;font-weight:600}}p{{line-height:1.5}}</style></head>
<body><main><h1>{escaped_title}</h1>{body}</main></body></html>"""
        return web.Response(
            text=document,
            content_type="text/html",
            status=status,
            headers=cls._security_headers(),
        )


class GuestyAccessView(HomeAssistantView):
    """Public portal whose actions are authenticated by the access manager."""

    url = f"{ACCESS_URL_PATH}/{{entry_id}}/{{token}}"
    name = "api:guesty:access"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the shared view."""
        self._hass = hass

    def _manager(self, entry_id: str) -> GuestyAccessManager | None:
        """Return a loaded access manager."""
        domain_data = self._hass.data.get(DOMAIN, {})
        managers = domain_data.get(ACCESS_MANAGERS, {})
        manager = managers.get(entry_id) if isinstance(managers, dict) else None
        return manager if isinstance(manager, GuestyAccessManager) else None

    @staticmethod
    def _language(request: web.Request) -> str:
        """Select the page language from the browser request."""
        return _preferred_language(request.headers.get("Accept-Language"))

    @staticmethod
    def _wants_json(request: web.Request) -> bool:
        """Return whether the enhanced page requested an AJAX result."""
        return "application/json" in request.headers.get("Accept", "").lower()

    async def get(
        self, request: web.Request, entry_id: str, token: str
    ) -> web.Response:
        """Render controls without performing a lock action."""
        language = self._language(request)
        manager = self._manager(entry_id)
        if manager is None:
            return GuestyAccessManager._page(
                _portal_text(language, "unavailable"),
                language=language,
                status=404,
            )
        return await manager.async_get_portal(token, language)

    async def post(
        self, request: web.Request, entry_id: str, token: str
    ) -> web.Response:
        """Process a small, CSRF-protected unlock form."""
        language = self._language(request)
        as_json = self._wants_json(request)
        manager = self._manager(entry_id)
        if manager is None:
            return GuestyAccessManager._page(
                _portal_text(language, "unavailable"),
                language=language,
                status=404,
            )
        if request.content_length is None:
            return manager._action_response(
                token,
                None,
                language,
                "length_required",
                "length_required",
                status=411,
                as_json=as_json,
            )
        if request.content_length > ACCESS_MAX_REQUEST_BYTES:
            return manager._action_response(
                token,
                None,
                language,
                "request_too_large",
                "request_too_large",
                status=413,
                as_json=as_json,
            )
        try:
            form = await request.post()
        except (ValueError, web.HTTPException):
            return manager._action_response(
                token,
                None,
                language,
                "invalid_request",
                "invalid_request",
                status=400,
                as_json=as_json,
            )
        return await manager.async_unlock(
            token,
            str(form.get("door", "")),
            str(form.get("nonce", "")),
            language,
            as_json=as_json,
        )


def async_register_access_manager(
    hass: HomeAssistant, manager: GuestyAccessManager
) -> None:
    """Register the shared route once and expose one loaded manager."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    managers = domain_data.setdefault(ACCESS_MANAGERS, {})
    managers[manager.entry.entry_id] = manager
    if hass.http is not None and not domain_data.get(ACCESS_VIEW_REGISTERED):
        hass.http.register_view(GuestyAccessView(hass))
        domain_data[ACCESS_VIEW_REGISTERED] = True


def async_unregister_access_manager(hass: HomeAssistant, entry_id: str) -> None:
    """Remove an unloaded manager while leaving the harmless shared route."""
    managers = hass.data.get(DOMAIN, {}).get(ACCESS_MANAGERS, {})
    if isinstance(managers, dict):
        managers.pop(entry_id, None)
