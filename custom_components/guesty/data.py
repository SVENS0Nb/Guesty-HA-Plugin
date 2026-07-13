"""Runtime data types for the Guesty integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry

from .api import GuestyApiClient
from .coordinator import GuestyDataUpdateCoordinator
from .scheduler import GuestyTransitionScheduler


@dataclass(slots=True)
class GuestyRuntimeData:
    """Objects owned by a loaded Guesty config entry."""

    coordinator: GuestyDataUpdateCoordinator
    client: GuestyApiClient
    scheduler: GuestyTransitionScheduler
    sensor_listing_ids: set[str] = field(default_factory=set)
    calendar_listing_ids: set[str] = field(default_factory=set)


type GuestyConfigEntry = ConfigEntry[GuestyRuntimeData]
