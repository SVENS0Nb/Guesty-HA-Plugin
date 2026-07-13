"""Calendar platform for Guesty reservations."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EXPOSE_GUEST_DETAILS,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DOMAIN,
)
from .coordinator import GuestyDataUpdateCoordinator
from .data import GuestyConfigEntry
from .models import GuestyListing, GuestyReservation, reservation_overlaps_range

_LOGGER = logging.getLogger(__name__)


def _add_listing_entities(
    coordinator: GuestyDataUpdateCoordinator,
    entry: GuestyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create calendars for all listings not yet registered."""
    if not coordinator.data:
        return

    known_ids = entry.runtime_data.calendar_listing_ids
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
    entry: GuestyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty calendars for every listing."""
    coordinator = entry.runtime_data.coordinator

    _add_listing_entities(coordinator, entry, async_add_entities)

    @callback
    def _handle_coordinator_update() -> None:
        """Add calendars when Guesty returns new listings."""
        _add_listing_entities(coordinator, entry, async_add_entities)

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
                except (TypeError, ValueError):
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
        language = getattr(getattr(self, "_hass", None), "config", None)
        is_german = getattr(language, "language", "en") == "de"
        summary = "Reserviert" if is_german else "Reserved"
        confirmation_label = "Bestätigung" if is_german else "Confirmation"
        guest_label = "Gast" if is_german else "Guest"
        status_label = "Status"
        description_parts: list[str] = []
        if self._expose_guest_details:
            summary = reservation.guest_name or reservation.confirmation_code or summary
            if reservation.confirmation_code:
                description_parts.append(
                    f"{confirmation_label}: {reservation.confirmation_code}"
                )
            if reservation.guest_name:
                description_parts.append(f"{guest_label}: {reservation.guest_name}")
        description_parts.append(f"{status_label}: {reservation.status}")
        check_in, check_out = reservation.stay_datetimes(listing)

        return CalendarEvent(
            start=check_in,
            end=check_out,
            summary=summary,
            description="\n".join(description_parts),
            uid=reservation.id,
        )

    @property
    def _expose_guest_details(self) -> bool:
        """Return whether calendar events may include guest details."""
        return self.coordinator.config_entry.options.get(
            CONF_EXPOSE_GUEST_DETAILS,
            DEFAULT_EXPOSE_GUEST_DETAILS,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Notify calendar listeners and write the updated entity state."""
        if update_event_listeners := getattr(
            self, "async_update_event_listeners", None
        ):
            update_event_listeners()
        super()._handle_coordinator_update()

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
