"""Sensor platform for Guesty occupancy."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_STALE_THRESHOLD_HOURS,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    SENSOR_OCCUPANCY,
    SENSOR_SYNC_STATUS,
)
from .coordinator import GuestyDataUpdateCoordinator
from .models import ListingOccupancy


def _add_listing_entities(
    coordinator: GuestyDataUpdateCoordinator,
    entry: ConfigEntry,
    hass: HomeAssistant,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors for all listings not yet registered."""
    if not coordinator.data:
        return

    known_ids: set[str] = hass.data[DOMAIN][entry.entry_id].setdefault(
        "sensor_listing_ids", set()
    )
    new_ids = set(coordinator.data.listings) - known_ids
    if not new_ids:
        return

    entities = [GuestyOccupancySensor(coordinator, listing_id) for listing_id in new_ids]
    async_add_entities(entities)
    known_ids.update(new_ids)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty occupancy sensors for every listing."""
    coordinator: GuestyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities: list[SensorEntity] = [GuestySyncStatusSensor(coordinator)]
    async_add_entities(entities)

    _add_listing_entities(coordinator, entry, hass, async_add_entities)

    @callback
    def _handle_coordinator_update() -> None:
        """Add sensors when Guesty returns new listings."""
        _add_listing_entities(coordinator, entry, hass, async_add_entities)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


class GuestyOccupancySensor(CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity):
    """Representation of a Guesty listing occupancy sensor."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["vacant", "occupied"]
    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_OCCUPANCY
    _attr_entity_registry_enabled_default = True

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
            "listing_id": self._listing_id,
            "data_stale": data.data_stale if data else None,
            "cache_age_minutes": data.cache_age_minutes if data else None,
            "last_sync": data.last_sync if data else None,
        }

        if not occupancy:
            return attributes

        listing = occupancy.listing
        attributes.update(
            {
                "listing_title": listing.title,
                "listing_nickname": listing.nickname,
                "listing_active": listing.active,
            }
        )

        if occupancy.current_reservation:
            reservation = occupancy.current_reservation
            attributes.update(
                {
                    "current_guest": reservation.guest_name,
                    "current_confirmation_code": reservation.confirmation_code,
                    "current_check_in": reservation.check_in_datetime(
                        listing
                    ).isoformat(),
                    "current_check_out": reservation.check_out_datetime(
                        listing
                    ).isoformat(),
                }
            )

        if occupancy.next_reservation and occupancy.next_check_in:
            attributes.update(
                {
                    "next_guest": occupancy.next_reservation.guest_name,
                    "next_confirmation_code": occupancy.next_reservation.confirmation_code,
                    "next_check_in": occupancy.next_check_in.isoformat(),
                    "next_check_out": (
                        occupancy.next_check_out.isoformat()
                        if occupancy.next_check_out
                        else None
                    ),
                }
            )

        return attributes

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
            "last_incremental_sync": data.last_incremental_sync,
            "cache_age_minutes": data.cache_age_minutes,
            "data_stale": data.data_stale,
            "last_error": data.last_error,
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
