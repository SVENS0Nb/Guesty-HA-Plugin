"""Unit tests for Guesty API error handling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import aiohttp
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


def _load_api() -> ModuleType:
    """Load the API module with small Home Assistant stubs."""
    homeassistant = ModuleType("homeassistant")
    helpers = ModuleType("homeassistant.helpers")
    aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.session
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client

    package = ModuleType("custom_components.guesty")
    package.__path__ = [str(GUESTY_PATH)]
    sys.modules["custom_components.guesty"] = package
    _load_module(
        "custom_components.guesty.const",
        GUESTY_PATH / "const.py",
        "custom_components.guesty",
    )

    models = ModuleType("custom_components.guesty.models")
    models.GuestyListing = object
    models.GuestyReservation = object
    models.build_reservation_filters = lambda *args, **kwargs: []
    sys.modules["custom_components.guesty.models"] = models

    return _load_module(
        "custom_components.guesty.api",
        GUESTY_PATH / "api.py",
        "custom_components.guesty",
    )


api = _load_api()


def _client() -> api.GuestyApiClient:
    """Return a client whose network method can be mocked."""
    return api.GuestyApiClient(object(), "client", "secret", token="token")


@pytest.mark.asyncio
async def test_network_error_is_retried(monkeypatch) -> None:
    """Transient aiohttp failures use the API retry loop."""
    client = _client()
    request_once = AsyncMock(
        side_effect=[aiohttp.ClientConnectionError("offline"), {"ok": True}]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    result = await client._async_request("GET", "/listings")

    assert result == {"ok": True}
    assert request_once.await_count == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_permanent_api_error_is_not_retried(monkeypatch) -> None:
    """Non-retryable API failures return immediately."""
    client = _client()
    request_once = AsyncMock(side_effect=api.GuestyApiError("bad request"))
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(api.GuestyApiError, match="bad request"):
        await client._async_request("GET", "/listings")

    assert request_once.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_header_controls_delay(monkeypatch) -> None:
    """A server-provided retry delay is used by the retry loop."""
    client = _client()
    request_once = AsyncMock(
        side_effect=[api.GuestyRetryableError("rate limited", 7.0), []]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(client, "_async_request_once", request_once)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    await client._async_request("GET", "/listings")

    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_permission_error_reaches_config_flow(monkeypatch) -> None:
    """Credential validation rejects tokens without listing access."""
    client = _client()
    monkeypatch.setattr(client, "_async_ensure_token", AsyncMock())
    monkeypatch.setattr(
        client,
        "_async_paginate",
        AsyncMock(side_effect=api.GuestyPermissionError("forbidden")),
    )

    with pytest.raises(api.GuestyPermissionError, match="forbidden"):
        await client.async_validate_credentials()


def test_invalid_json_is_reported_as_api_error() -> None:
    """Malformed successful responses use the integration error type."""
    with pytest.raises(api.GuestyApiError, match="Invalid JSON"):
        api.GuestyApiClient._parse_response_body("not json")
