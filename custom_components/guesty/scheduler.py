"""Schedule occupancy transitions without polling."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .models import GuestyListing, GuestyReservation, get_next_transition

if TYPE_CHECKING:
    from .coordinator import GuestyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Avoid scheduling transitions too far in the future.
MAX_SCHEDULE_HORIZON = timedelta(days=30)


class GuestyTransitionScheduler:
    """Schedule local occupancy recalculations at check-in/out times."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: GuestyDataUpdateCoordinator,
        on_transition: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Initialize the scheduler."""
        self.hass = hass
        self._coordinator = coordinator
        self._on_transition = on_transition
        self._unsub: CALLBACK_TYPE | None = None

    def async_schedule(self) -> None:
        """Schedule the next occupancy transition across all listings."""
        self.async_unschedule()
        if not self._coordinator.data:
            return

        now = dt_util.now()
        next_transition: datetime | None = None
        listings = self._coordinator.data.listings
        reservations = self._coordinator.data.reservations

        for listing in listings.values():
            transition = get_next_transition(listing, reservations, now)
            if transition is None:
                continue
            if transition - now > MAX_SCHEDULE_HORIZON:
                continue
            if next_transition is None or transition < next_transition:
                next_transition = transition

        if next_transition is None:
            _LOGGER.debug("No upcoming occupancy transitions to schedule")
            return

        _LOGGER.debug("Next occupancy transition scheduled at %s", next_transition)

        @callback
        def _handle_transition(now: datetime) -> None:
            self._unsub = None
            self.hass.async_create_task(self._on_transition())

        self._unsub = async_track_point_in_time(
            self.hass,
            _handle_transition,
            next_transition,
        )

    def async_unschedule(self) -> None:
        """Cancel a scheduled transition."""
        if self._unsub:
            self._unsub()
            self._unsub = None
