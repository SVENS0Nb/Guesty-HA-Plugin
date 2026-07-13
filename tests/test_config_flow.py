"""Tests for Guesty setup, options, and reauthentication flows."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import config_flow
from custom_components.guesty.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
)


VALIDATED = {
    "title": "Guesty",
    "unique_id": "account-hash",
    CONF_ACCESS_TOKEN: "validated-token",
    CONF_TOKEN_EXPIRES_AT: 123456.0,
}


@pytest.mark.asyncio
async def test_user_flow_stores_first_token_for_setup(hass, monkeypatch) -> None:
    """Setup reuses the validation token instead of spending another token."""
    monkeypatch.setattr(
        config_flow,
        "validate_input",
        AsyncMock(return_value=VALIDATED),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={
            CONF_CLIENT_ID: " client ",
            CONF_CLIENT_SECRET: " secret ",
            CONF_SCAN_INTERVAL: 300,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLIENT_ID] == "client"
    assert result["data"][CONF_CLIENT_SECRET] == "secret"
    assert result["data"][CONF_ACCESS_TOKEN] == "validated-token"
    assert result["options"][CONF_EXPOSE_GUEST_DETAILS] is False


@pytest.mark.asyncio
async def test_reauth_updates_credentials_and_token(hass, monkeypatch) -> None:
    """Expired credentials can be replaced without deleting the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="existing-account",
        data={CONF_CLIENT_ID: "old", CONF_CLIENT_SECRET: "old-secret"},
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        config_flow,
        "validate_input",
        AsyncMock(return_value=VALIDATED),
    )

    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        {
            CONF_CLIENT_ID: "new-client",
            CONF_CLIENT_SECRET: "new-secret",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_ID] == "new-client"
    assert entry.data[CONF_ACCESS_TOKEN] == "validated-token"


@pytest.mark.asyncio
async def test_options_flow_uses_modern_config_entry_property(hass) -> None:
    """The options flow is compatible with current Home Assistant releases."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 600,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == 600
    assert result["data"][CONF_EXPOSE_GUEST_DETAILS] is True
