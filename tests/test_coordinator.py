"""Unit tests for Guesty coordinator sync decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUESTY_PATH = ROOT / "custom_components" / "guesty"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _load_module(name: str, path: Path, package: str) -> ModuleType:
    """Load a module from the integration without importing its package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    module.__package__ = package
    spec.loader.exec_module(module)
    return module


def _load_coordinator() -> ModuleType:
    """Load coordinator.py with minimal Home Assistant dependencies."""
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object

    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            self.update_interval = kwargs["update_interval"]
            self.data = None

        @classmethod
        def __class_getitem__(cls, item):
            return cls

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = RuntimeError

    dt_util = ModuleType("homeassistant.util.dt")
    dt_util.utcnow = lambda: NOW
    dt_util.parse_datetime = lambda value: (
        None if value == "invalid" else datetime.fromisoformat(value)
    )

    sys.modules["homeassistant"] = ModuleType("homeassistant")
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["homeassistant.util"] = ModuleType("homeassistant.util")
    sys.modules["homeassistant.util.dt"] = dt_util

    package = ModuleType("custom_components.guesty")
    package.__path__ = [str(GUESTY_PATH)]
    sys.modules["custom_components.guesty"] = package
    _load_module(
        "custom_components.guesty.const",
        GUESTY_PATH / "const.py",
        "custom_components.guesty",
    )

    api = ModuleType("custom_components.guesty.api")
    api.GuestyApiClient = object
    api.GuestyApiError = type("GuestyApiError", (Exception,), {})
    api.GuestyAuthError = type("GuestyAuthError", (Exception,), {})
    sys.modules["custom_components.guesty.api"] = api

    models = ModuleType("custom_components.guesty.models")
    models.GuestyListing = object
    models.GuestyReservation = object
    models.ListingOccupancy = object
    models.calculate_listing_occupancy = lambda *args: None
    models.merge_reservations = lambda existing, updates, **kwargs: updates
    sys.modules["custom_components.guesty.models"] = models

    storage = ModuleType("custom_components.guesty.storage")
    storage.GuestyStorage = object
    sys.modules["custom_components.guesty.storage"] = storage

    return _load_module(
        "custom_components.guesty.coordinator",
        GUESTY_PATH / "coordinator.py",
        "custom_components.guesty",
    )


coordinator = _load_coordinator()


def test_coordinator_uses_stdlib_timedelta() -> None:
    """Coordinator setup does not rely on a Home Assistant timedelta helper."""
    entry = SimpleNamespace(options={}, data={})

    instance = coordinator.GuestyDataUpdateCoordinator(
        object(), entry, object(), object()
    )

    assert instance.update_interval == timedelta(
        seconds=coordinator.DEFAULT_SCAN_INTERVAL
    )


@pytest.mark.parametrize(
    ("last_full_sync", "expected"),
    [
        (None, True),
        ("invalid", True),
        ((NOW - timedelta(hours=23)).isoformat(), False),
        ((NOW - timedelta(hours=24)).isoformat(), True),
    ],
)
def test_full_sync_uses_dedicated_timestamp(last_full_sync, expected) -> None:
    """Daily full sync decisions do not depend on incremental cursors."""
    assert coordinator._is_full_reservation_sync_due(last_full_sync) is expected


@pytest.mark.asyncio
async def test_listing_webhook_forces_full_sync(monkeypatch) -> None:
    """Listing events bypass the normal listing polling interval."""
    instance = object.__new__(coordinator.GuestyDataUpdateCoordinator)
    force_full_sync = AsyncMock()
    monkeypatch.setattr(instance, "async_force_full_sync", force_full_sync)

    await instance.async_handle_webhook({"event": "listing.updated"})

    force_full_sync.assert_awaited_once_with()
