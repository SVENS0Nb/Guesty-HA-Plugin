"""Sensor platform for Guesty occupancy."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_STALE_THRESHOLD_HOURS,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    SENSOR_ACCESS_LINK,
    SENSOR_CURRENT_GUEST,
    SENSOR_GUESTY_KEYCODE_STATUS,
    SENSOR_LOXONE_PIN_STATUS,
    SENSOR_OCCUPANCY,
    SENSOR_SYNC_STATUS,
)
from .coordinator import GuestyDataUpdateCoordinator
from .data import GuestyConfigEntry
from .models import ListingOccupancy

if TYPE_CHECKING:
    from .access import GuestyAccessManager
    from .loxone import GuestyLoxoneManager


def _add_listing_entities(
    coordinator: GuestyDataUpdateCoordinator,
    entry: GuestyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors for all listings not yet registered."""
    if not coordinator.data:
        return

    known_ids = entry.runtime_data.sensor_listing_ids
    new_ids = set(coordinator.data.listings) - known_ids
    if not new_ids:
        return

    entities = [
        entity
        for listing_id in new_ids
        for entity in (
            GuestyOccupancySensor(coordinator, listing_id),
            GuestyCurrentGuestSensor(coordinator, listing_id),
            GuestyAccessLinkSensor(
                coordinator,
                entry.runtime_data.access_manager,
                listing_id,
            ),
            GuestyKeycodeStatusSensor(
                coordinator,
                entry.runtime_data.loxone_manager,
                listing_id,
            ),
            GuestyLoxonePinStatusSensor(
                coordinator,
                entry.runtime_data.loxone_manager,
                listing_id,
            ),
        )
    ]
    async_add_entities(entities)
    known_ids.update(new_ids)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty occupancy sensors for every listing."""
    coordinator = entry.runtime_data.coordinator

    entities: list[SensorEntity] = [GuestySyncStatusSensor(coordinator)]
    async_add_entities(entities)

    _add_listing_entities(coordinator, entry, async_add_entities)

    @callback
    def _handle_coordinator_update() -> None:
        """Add sensors when Guesty returns new listings."""
        _add_listing_entities(coordinator, entry, async_add_entities)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


class GuestyOccupancySensor(
    CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Representation of a Guesty listing occupancy sensor."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["vacant", "occupied"]
    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_OCCUPANCY
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = frozenset(
        {
            "current_guest",
            "current_confirmation_code",
            "next_guest",
            "next_confirmation_code",
        }
    )

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
    ) -> None:
        """Initialize the occupancy sensor."""
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{listing_id}"

    @property
    def occupancy(self) -> ListingOccupancy | None:
        """Return occupancy data for this listing."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.occupancy.get(self._listing_id)

    @property
    def native_value(self) -> str | None:
        """Return the occupancy state."""
        occupancy = self.occupancy
        return occupancy.status if occupancy else None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        data = self.coordinator.data
        occupancy = self.occupancy
        attributes: dict = {
            "data_stale": data.data_stale if data else None,
            "cache_age_minutes": data.cache_age_minutes if data else None,
            "last_sync": data.last_sync if data else None,
        }

        if not occupancy:
            return attributes

        listing = occupancy.listing
        attributes.update(
            {
                "listing_active": listing.active,
            }
        )

        if occupancy.current_reservation:
            reservation = occupancy.current_reservation
            attributes.update(
                {
                    "current_check_in": reservation.check_in_datetime(
                        listing
                    ).isoformat(),
                    "current_check_out": reservation.check_out_datetime(
                        listing
                    ).isoformat(),
                }
            )
            if self._expose_guest_details:
                attributes.update(
                    {
                        "current_guest": reservation.guest_name,
                        "current_confirmation_code": reservation.confirmation_code,
                    }
                )

        if occupancy.next_reservation and occupancy.next_check_in:
            attributes.update(
                {
                    "next_check_in": occupancy.next_check_in.isoformat(),
                    "next_check_out": (
                        occupancy.next_check_out.isoformat()
                        if occupancy.next_check_out
                        else None
                    ),
                }
            )
            if self._expose_guest_details:
                attributes.update(
                    {
                        "next_guest": occupancy.next_reservation.guest_name,
                        "next_confirmation_code": (
                            occupancy.next_reservation.confirmation_code
                        ),
                    }
                )

        return attributes

    @property
    def _expose_guest_details(self) -> bool:
        """Return whether guest details may be exposed in entity state."""
        return self.coordinator.config_entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        )

    @property
    def device_info(self) -> dict:
        """Return device information."""
        listing = (
            self.coordinator.data.listings.get(self._listing_id)
            if self.coordinator.data
            else None
        )
        listing_name = listing.display_name if listing else self._listing_id
        return {
            "identifiers": {(DOMAIN, self._listing_id)},
            "name": listing_name,
            "manufacturer": "Guesty",
            "model": "Listing",
            "via_device": (DOMAIN, self.coordinator.config_entry.entry_id),
        }

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        if not (
            super().available
            and self.coordinator.data is not None
            and self._listing_id in self.coordinator.data.listings
        ):
            return False
        if self.coordinator.data.data_stale:
            stale_threshold = self.coordinator.config_entry.options.get(
                CONF_STALE_THRESHOLD_HOURS, DEFAULT_STALE_THRESHOLD_HOURS
            )
            cache_age = self.coordinator.data.cache_age_minutes
            if cache_age is not None and cache_age > stale_threshold * 60 * 2:
                return False
        return True


class GuestyCurrentGuestSensor(
    CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Sensor exposing the current guest name after an explicit privacy opt-in."""

    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:account"
    _attr_translation_key = SENSOR_CURRENT_GUEST

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
    ) -> None:
        """Initialize the current guest sensor."""
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{listing_id}_current_guest"
        )

    @property
    def native_value(self) -> str | None:
        """Return the current guest name when private details are enabled."""
        if not self._expose_guest_details or not self.coordinator.data:
            return None
        occupancy = self.coordinator.data.occupancy.get(self._listing_id)
        if not occupancy or not occupancy.current_reservation:
            return None
        return occupancy.current_reservation.guest_name

    @property
    def _expose_guest_details(self) -> bool:
        """Return whether guest details may be exposed in entity state."""
        return self.coordinator.config_entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        )

    @property
    def device_info(self) -> dict:
        """Return device information."""
        listing = (
            self.coordinator.data.listings.get(self._listing_id)
            if self.coordinator.data
            else None
        )
        listing_name = listing.display_name if listing else self._listing_id
        return {
            "identifiers": {(DOMAIN, self._listing_id)},
            "name": listing_name,
            "manufacturer": "Guesty",
            "model": "Listing",
            "via_device": (DOMAIN, self.coordinator.config_entry.entry_id),
        }

    @property
    def available(self) -> bool:
        """Return whether explicitly enabled guest data is current enough to show."""
        if not (
            self._expose_guest_details
            and super().available
            and self.coordinator.data is not None
            and self._listing_id in self.coordinator.data.listings
        ):
            return False
        if self.coordinator.data.data_stale:
            stale_threshold = self.coordinator.config_entry.options.get(
                CONF_STALE_THRESHOLD_HOURS, DEFAULT_STALE_THRESHOLD_HOURS
            )
            cache_age = self.coordinator.data.cache_age_minutes
            if cache_age is not None and cache_age > stale_threshold * 60 * 2:
                return False
        return True


class GuestyAccessLinkSensor(
    CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Privacy-conscious view of the current or next guest access link."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "not_configured",
        "no_reservation",
        "pending",
        "synced",
        "error",
    ]
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:link-lock"
    _attr_translation_key = SENSOR_ACCESS_LINK
    _unrecorded_attributes = frozenset({"access_url", "guest_name"})

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        access_manager: GuestyAccessManager,
        listing_id: str,
    ) -> None:
        """Initialize a disabled-by-default access link sensor."""
        super().__init__(coordinator)
        self._access_manager = access_manager
        self._listing_id = listing_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{listing_id}_access_link"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to both Guesty data and completed access writes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._access_manager.async_add_listener(self._handle_access_update)
        )

    @callback
    def _handle_access_update(self) -> None:
        """Refresh after the access manager finishes a reconciliation pass."""
        self.async_write_ha_state()

    @property
    def snapshot(self) -> dict[str, Any]:
        """Return the current access snapshot."""
        return self._access_manager.listing_access_snapshot(self._listing_id)

    @property
    def native_value(self) -> str:
        """Return whether a generated URL is confirmed in Guesty."""
        return str(self.snapshot.get("status", "error"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the bearer URL live without allowing recorder history."""
        snapshot = self.snapshot
        attributes: dict[str, Any] = {
            "access_active": snapshot.get("access_active", False),
            "field_synced": snapshot.get("field_synced", False),
            "write_verified": snapshot.get("write_verified", False),
        }
        access_url = snapshot.get("access_url")
        if isinstance(access_url, str):
            attributes["access_url"] = access_url
        for key in ("access_start", "access_end"):
            value = snapshot.get(key)
            if value is not None:
                attributes[key] = value.isoformat()
        reservation = snapshot.get("reservation")
        if reservation is not None:
            attributes["reservation_status"] = reservation.status
            if self._expose_guest_details and reservation.guest_name:
                attributes["guest_name"] = reservation.guest_name
        return attributes

    @property
    def _expose_guest_details(self) -> bool:
        """Return whether the existing privacy opt-in allows the guest name."""
        return self.coordinator.config_entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        )

    @property
    def device_info(self) -> dict:
        """Attach the sensor to its Guesty listing device."""
        listing = (
            self.coordinator.data.listings.get(self._listing_id)
            if self.coordinator.data
            else None
        )
        listing_name = listing.display_name if listing else self._listing_id
        return {
            "identifiers": {(DOMAIN, self._listing_id)},
            "name": listing_name,
            "manufacturer": "Guesty",
            "model": "Listing",
            "via_device": (DOMAIN, self.coordinator.config_entry.entry_id),
        }

    @property
    def available(self) -> bool:
        """Return whether current Guesty listing data is available."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._listing_id in self.coordinator.data.listings
        )


class _GuestyLoxoneStatusSensor(
    CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Base for privacy-safe per-listing code-delivery status sensors."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_registry_enabled_default = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _status_key: str
    _unique_suffix: str

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        loxone_manager: GuestyLoxoneManager,
        listing_id: str,
    ) -> None:
        """Initialize one delivery status sensor."""
        super().__init__(coordinator)
        self._loxone_manager = loxone_manager
        self._listing_id = listing_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{listing_id}_{self._unique_suffix}"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to completed PIN synchronization passes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._loxone_manager.async_add_listener(self._handle_loxone_update)
        )

    @callback
    def _handle_loxone_update(self) -> None:
        """Refresh after the PIN manager changes local or remote state."""
        self.async_write_ha_state()

    @property
    def snapshot(self) -> dict[str, Any]:
        """Return the shared status snapshot without a PIN or guest details."""
        return self._loxone_manager.listing_status_snapshot(self._listing_id)

    @property
    def native_value(self) -> str:
        """Return the relevant delivery state."""
        return str(self.snapshot.get(self._status_key, "error"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose operational timing and booleans, never the code itself."""
        snapshot = self.snapshot
        attributes: dict[str, Any] = {
            "data_stale": snapshot.get("data_stale", False),
            "field_synced": snapshot.get("field_synced", False),
            "loxone_user_created": snapshot.get("loxone_user_created", False),
        }
        reservation_status = snapshot.get("reservation_status")
        if isinstance(reservation_status, str):
            attributes["reservation_status"] = reservation_status
        for key in ("access_start", "access_end", "provision_at"):
            value = snapshot.get(key)
            if isinstance(value, datetime):
                attributes[key] = value.isoformat()
        return attributes

    @property
    def device_info(self) -> dict:
        """Attach the sensor to its Guesty listing device."""
        listing = (
            self.coordinator.data.listings.get(self._listing_id)
            if self.coordinator.data
            else None
        )
        listing_name = listing.display_name if listing else self._listing_id
        return {
            "identifiers": {(DOMAIN, self._listing_id)},
            "name": listing_name,
            "manufacturer": "Guesty",
            "model": "Listing",
            "via_device": (DOMAIN, self.coordinator.config_entry.entry_id),
        }

    @property
    def available(self) -> bool:
        """Return whether the Guesty listing still exists."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._listing_id in self.coordinator.data.listings
        )


class GuestyKeycodeStatusSensor(_GuestyLoxoneStatusSensor):
    """Report whether the authoritative Guesty code is synchronized."""

    _attr_options = [
        "not_configured",
        "no_reservation",
        "pending",
        "synced",
        "conflict",
        "error",
    ]
    _attr_icon = "mdi:key-variant"
    _attr_translation_key = SENSOR_GUESTY_KEYCODE_STATUS
    _status_key = "guesty_status"
    _unique_suffix = "guesty_keycode_status"


class GuestyLoxonePinStatusSensor(_GuestyLoxoneStatusSensor):
    """Report whether the code is scheduled or provisioned in Loxone."""

    _attr_options = [
        "not_configured",
        "no_reservation",
        "scheduled",
        "pending",
        "provisioned",
        "cleanup_pending",
        "conflict",
        "error",
    ]
    _attr_icon = "mdi:account-key"
    _attr_translation_key = SENSOR_LOXONE_PIN_STATUS
    _status_key = "loxone_status"
    _unique_suffix = "loxone_pin_status"


class GuestySyncStatusSensor(
    CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Sensor reporting Guesty sync health."""

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_SYNC_STATUS
    _attr_icon = "mdi:cloud-sync"

    def __init__(self, coordinator: GuestyDataUpdateCoordinator) -> None:
        """Initialize sync status sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_sync_status"

    @property
    def native_value(self) -> str | None:
        """Return sync status."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.sync_status

    @property
    def extra_state_attributes(self) -> dict:
        """Return sync diagnostics."""
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "last_sync": data.last_sync,
            "last_listing_sync": data.last_listing_sync,
            "last_reservation_sync": data.last_reservation_sync,
            "last_full_reservation_sync": data.last_full_reservation_sync,
            "last_incremental_sync": data.last_incremental_sync,
            "cache_age_minutes": data.cache_age_minutes,
            "data_stale": data.data_stale,
            "has_last_error": data.last_error is not None,
            "webhook_active": data.webhook_active,
            "listings_count": len(data.listings),
            "reservations_count": len(data.reservations),
        }

    @property
    def device_info(self) -> dict:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.config_entry.entry_id)},
            "name": "Guesty",
            "manufacturer": "Guesty",
            "model": "Open API",
        }
