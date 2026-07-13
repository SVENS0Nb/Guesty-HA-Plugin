"""Unit tests for Guesty webhook lifecycle handling."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUESTY_PATH = ROOT / "custom_components" / "guesty"


def _load_module(name: str, path: Path, package: str) -> ModuleType:
    """Load a module from the integration without importing its package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    module.__package__ = package
    spec.loader.exec_module(module)
    return module


def _load_webhook() -> tuple[ModuleType, ModuleType]:
    """Load webhook.py with an in-memory Home Assistant webhook registry."""
    webhook_registry = ModuleType("homeassistant.components.webhook")
    webhook_registry.handlers = {}
    webhook_registry.async_generate_id = lambda: "generated-id"
    webhook_registry.async_unregister = lambda hass, webhook_id: (
        webhook_registry.handlers.pop(webhook_id, None)
    )

    def async_register(
        hass,
        domain,
        name,
        webhook_id,
        handler,
        *,
        allowed_methods=None,
    ) -> None:
        webhook_registry.handlers[webhook_id] = handler

    webhook_registry.async_register = async_register

    components = ModuleType("homeassistant.components")
    components.webhook = webhook_registry
    sys.modules["homeassistant"] = ModuleType("homeassistant")
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.webhook"] = webhook_registry

    package = ModuleType("custom_components.guesty")
    package.__path__ = [str(GUESTY_PATH)]
    sys.modules["custom_components.guesty"] = package
    _load_module(
        "custom_components.guesty.const",
        GUESTY_PATH / "const.py",
        "custom_components.guesty",
    )
    loaded = _load_module(
        "custom_components.guesty.webhook",
        GUESTY_PATH / "webhook.py",
        "custom_components.guesty",
    )
    return loaded, webhook_registry


webhook_module, webhook_registry = _load_webhook()


class ConfigEntries:
    """Minimal config entry manager."""

    @staticmethod
    def async_update_entry(entry, *, data) -> None:
        entry.data = data


@pytest.mark.asyncio
async def test_reload_rebinds_handler_to_current_coordinator() -> None:
    """A stable webhook URL invokes the newest coordinator after reload."""
    webhook_registry.handlers.clear()
    hass = SimpleNamespace(config_entries=ConfigEntries())
    entry = SimpleNamespace(data={}, title="Guesty")
    old_coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    new_coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())

    webhook_id = await webhook_module.async_setup_webhook(hass, entry, old_coordinator)
    await webhook_module.async_setup_webhook(hass, entry, new_coordinator)

    request = SimpleNamespace(json=AsyncMock(return_value={"event": "test"}))
    response = await webhook_registry.handlers[webhook_id](hass, webhook_id, request)

    assert webhook_id == "generated-id"
    assert response.status == 200
    old_coordinator.async_handle_webhook.assert_not_awaited()
    new_coordinator.async_handle_webhook.assert_awaited_once_with({"event": "test"})


@pytest.mark.asyncio
async def test_non_object_payload_is_rejected() -> None:
    """Webhook JSON must be an object before event handling."""
    webhook_registry.handlers.clear()
    hass = SimpleNamespace(config_entries=ConfigEntries())
    entry = SimpleNamespace(data={}, title="Guesty")
    coordinator = SimpleNamespace(async_handle_webhook=AsyncMock())
    webhook_id = await webhook_module.async_setup_webhook(hass, entry, coordinator)
    request = SimpleNamespace(json=AsyncMock(return_value=[]))

    response = await webhook_registry.handlers[webhook_id](hass, webhook_id, request)

    assert response.status == 400
    coordinator.async_handle_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_url_is_not_registered_with_guesty() -> None:
    """Guesty registration requires a publicly reachable HA URL."""

    class NoURLAvailableError(Exception):
        pass

    network = ModuleType("homeassistant.helpers.network")
    network.NoURLAvailableError = NoURLAvailableError
    network.get_url = lambda *args, **kwargs: (_ for _ in ()).throw(
        NoURLAvailableError()
    )
    sys.modules["homeassistant.helpers"] = ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers.network"] = network

    client = SimpleNamespace(async_register_webhook=AsyncMock())
    hass = SimpleNamespace(config_entries=ConfigEntries())
    entry = SimpleNamespace(data={})

    result = await webhook_module.async_register_guesty_webhook(
        hass, entry, client, "webhook-id"
    )

    assert result is None
    client.async_register_webhook.assert_not_awaited()
