"""Version-compatible, entry-owned Guesty device relationships."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

if TYPE_CHECKING:
    from .data import GuestyConfigEntry


def hub_device_info(entry_id: str) -> dr.DeviceInfo:
    """Describe the existing integration device without changing its identity."""
    return dr.DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Guesty",
        manufacturer="Guesty",
        model="Open API",
    )


@callback
def async_setup_hub_device(hass: HomeAssistant, entry: GuestyConfigEntry) -> None:
    """Resolve the owned hub before either entity platform starts."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Guesty",
        manufacturer="Guesty",
        model="Open API",
    )
    entry.runtime_data.hub_device_id = device.id


def listing_device_info(
    entry: GuestyConfigEntry, listing_id: str, listing_name: str
) -> dr.DeviceInfo:
    """Keep listing identifiers stable and use the supported parent field."""
    fields = {
        "identifiers": {(DOMAIN, listing_id)},
        "name": listing_name,
        "manufacturer": "Guesty",
        "model": "Listing",
    }
    # The annotation capability check supports both older TypedDict and newer
    # dataclass DeviceInfo implementations, without relying on deprecated keys.
    if "via_device_id" in dr.DeviceInfo.__annotations__:
        device_id = entry.runtime_data.hub_device_id
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("Guesty hub device must be initialized first")
        return dr.DeviceInfo(**fields, via_device_id=device_id)
    return dr.DeviceInfo(**fields, via_device=(DOMAIN, entry.entry_id))
