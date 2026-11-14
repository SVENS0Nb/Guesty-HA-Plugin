"""Regression tests for old and current HA device relationship contracts."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import devices


@dataclass
class ModernDeviceInfo:
    """Reject deprecated keywords like the current typed DeviceInfo contract."""

    identifiers: set[tuple[str, str]]
    name: str
    manufacturer: str
    model: str
    via_device_id: str | None = None


class LegacyDeviceInfo(dict):
    """Older HA accepts the identifier tuple rather than a registry ID."""

    __annotations__ = {"via_device": tuple[str, str]}


def test_modern_device_info_uses_exact_owned_hub_id(monkeypatch):
    monkeypatch.setattr(devices.dr, "DeviceInfo", ModernDeviceInfo)
    entry = SimpleNamespace(
        entry_id="test-entry", runtime_data=SimpleNamespace(hub_device_id="hub-id")
    )
    info = devices.listing_device_info(entry, "test-listing", "Test listing")
    assert info.identifiers == {("guesty", "test-listing")}
    assert info.name == "Test listing"
    assert info.via_device_id == "hub-id"
    assert not hasattr(info, "via_device")
    assert devices.hub_device_info(entry.entry_id).identifiers == {
        ("guesty", "test-entry")
    }


def test_legacy_device_info_preserves_supported_parent_tuple(monkeypatch):
    monkeypatch.setattr(devices.dr, "DeviceInfo", LegacyDeviceInfo)
    entry = SimpleNamespace(entry_id="test-entry")
    info = devices.listing_device_info(entry, "test-listing", "Test listing")
    assert info["via_device"] == ("guesty", "test-entry")
    assert "via_device_id" not in info


@pytest.mark.parametrize("device_id", [None, "", 123])
def test_modern_device_info_rejects_uninitialized_parent(monkeypatch, device_id):
    monkeypatch.setattr(devices.dr, "DeviceInfo", ModernDeviceInfo)
    entry = SimpleNamespace(
        entry_id="test-entry", runtime_data=SimpleNamespace(hub_device_id=device_id)
    )
    with pytest.raises(ValueError, match="must be initialized"):
        devices.listing_device_info(entry, "test-listing", "Test listing")


@pytest.mark.asyncio
async def test_hub_setup_preserves_existing_device_and_user_name(hass):
    entry = MockConfigEntry(domain="guesty", title="Test account")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    original = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("guesty", entry.entry_id)}
    )
    registry.async_update_device(original.id, name_by_user="My existing hub")
    entry.runtime_data = SimpleNamespace(hub_device_id=None)
    devices.async_setup_hub_device(hass, entry)
    first_id = entry.runtime_data.hub_device_id
    devices.async_setup_hub_device(hass, entry)
    assert first_id == entry.runtime_data.hub_device_id == original.id
    assert registry.async_get(original.id).name_by_user == "My existing hub"


@pytest.mark.asyncio
async def test_hub_setup_keeps_config_entries_separate(hass):
    ids = []
    for _ in range(2):
        entry = MockConfigEntry(domain="guesty", title="Same title")
        entry.add_to_hass(hass)
        entry.runtime_data = SimpleNamespace(hub_device_id=None)
        devices.async_setup_hub_device(hass, entry)
        ids.append(entry.runtime_data.hub_device_id)
    assert ids[0] != ids[1]
