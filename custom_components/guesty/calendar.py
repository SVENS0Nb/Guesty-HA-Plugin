"""Calendar platform for Guesty reservations."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GuestyDataUpdateCoordinator
from .models import GuestyListing, GuestyReservation, reservation_overlaps_range

_LOGGER = logging.getLogger(__name__)


def _add_listing_entities(
    coordinator: GuestyDataUpdateCoordinator,
    entry: ConfigEntry,
    hass: HomeAssistant,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create calendars for all listings not yet registered."""
    if not coordinator.data:
        return

    known_ids: set[str] = hass.data[DOMAIN][entry.entry_id].setdefault(
        "calendar_listing_ids", set()
    )
    new_ids = set(coordinator.data.listings) - known_ids
    if not new_ids:
        return

    entities = [
        GuestyReservationCalendar(coordinator, listing_id) for listing_id in new_ids
    ]
    async_add_entities(entities)
    known_ids.update(new_ids)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty calendars for every listing."""
    coordinator: GuestyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    _add_listing_entities(coordinator, entry, hass, async_add_entities)

    @callback
    def _handle_coordinator_update() -> None:
        """Add calendars when Guesty returns new listings."""
        _add_listing_entities(coordinator, entry, hass, async_add_entities)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


class GuestyReservationCalendar(
    CoordinatorEntity[GuestyDataUpdateCoordinator], CalendarEntity
):
    """Calendar for a Guesty listing."""

    _attr_has_entity_name = True
    _attr_translation_key = "reservations"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
    ) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_calendar_{listing_id}"
        )

    @property
    def listing(self) -> GuestyListing | None:
        """Return the listing for this calendar."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.listings.get(self._listing_id)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next calendar event."""
        listing = self.listing
        occupancy = (
            self.coordinator.data.occupancy.get(self._listing_id)
            if self.coordinator.data
            else None
        )
        if not listing or not occupancy:
            return None

        if occupancy.current_reservation:
            reservation = occupancy.current_reservation
            return self._reservation_to_event(reservation, listing)

        if occupancy.next_reservation:
            return self._reservation_to_event(occupancy.next_reservation, listing)

        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events in the requested range."""
        listing = self.listing
        if not listing:
            return []

        events: list[CalendarEvent] = []
        for reservation in self.coordinator.get_listing_reservations(self._listing_id):
            if reservation_overlaps_range(
                reservation,
                listing,
                start_date,
                end_date,
            ):
                try:
                    events.append(self._reservation_to_event(reservation, listing))
                except ValueError:
                    _LOGGER.debug(
                        "Skipping invalid reservation %s for calendar",
                        reservation.id,
                    )

        events.sort(key=lambda event: event.start)
        return events

    def _reservation_to_event(
        self,
        reservation: GuestyReservation,
        listing: GuestyListing,
    ) -> CalendarEvent:
        """Convert a reservation into a calendar event."""
        summary = reservation.guest_name or reservation.confirmation_code or "Gast"
        description_parts = []
        if reservation.confirmation_code:
            description_parts.append(
                f"Bestätigung: {reservation.confirmation_code}"
            )
        if reservation.guest_name:
            description_parts.append(f"Gast: {reservation.guest_name}")
        description_parts.append(f"Status: {reservation.status}")

        return CalendarEvent(
            start=reservation.check_in_datetime(listing),
            end=reservation.check_out_datetime(listing),
            summary=summary,
            description="\n".join(description_parts),
            uid=reservation.id,
        )

    @property
    def device_info(self) -> dict:
        """Return device information."""
        listing = self.listing
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
        return (
            super().available
            and self.coordinator.data is not None
            and self._listing_id in self.coordinator.data.listings
        )
