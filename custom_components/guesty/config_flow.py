"""Config flow for Guesty."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_LISTING_SYNC_INTERVAL,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_STALE_THRESHOLD_HOURS,
    DEFAULT_LISTING_SYNC_INTERVAL,
    DEFAULT_RESERVATION_DAYS_FUTURE,
    DEFAULT_RESERVATION_DAYS_PAST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
        ),
        vol.Optional(
            CONF_LISTING_SYNC_INTERVAL, default=DEFAULT_LISTING_SYNC_INTERVAL
        ): vol.All(vol.Coerce(int), vol.Range(min=3600, max=604800)),
        vol.Optional(
            CONF_RESERVATION_DAYS_PAST, default=DEFAULT_RESERVATION_DAYS_PAST
        ): vol.All(vol.Coerce(int), vol.Range(min=7, max=180)),
        vol.Optional(
            CONF_RESERVATION_DAYS_FUTURE,
            default=DEFAULT_RESERVATION_DAYS_FUTURE,
        ): vol.All(vol.Coerce(int), vol.Range(min=30, max=730)),
        vol.Optional(
            CONF_STALE_THRESHOLD_HOURS, default=DEFAULT_STALE_THRESHOLD_HOURS
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=48)),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Validate the user input and return info for the config entry."""
    client = GuestyApiClient.from_hass(
        hass,
        data[CONF_CLIENT_ID],
        data[CONF_CLIENT_SECRET],
    )
    account_id = await client.async_validate_credentials()
    return {"title": "Guesty", "unique_id": account_id}


class GuestyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Guesty."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except GuestyAuthError:
                errors["base"] = "invalid_auth"
            except GuestyApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during Guesty setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["title"],
                    data={
                        CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                        CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET],
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    },
                    options={
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                        CONF_LISTING_SYNC_INTERVAL: DEFAULT_LISTING_SYNC_INTERVAL,
                        CONF_RESERVATION_DAYS_PAST: DEFAULT_RESERVATION_DAYS_PAST,
                        CONF_RESERVATION_DAYS_FUTURE: DEFAULT_RESERVATION_DAYS_FUTURE,
                        CONF_STALE_THRESHOLD_HOURS: DEFAULT_STALE_THRESHOLD_HOURS,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GuestyOptionsFlow:
        """Get the options flow for this handler."""
        return GuestyOptionsFlow(config_entry)


class GuestyOptionsFlow(OptionsFlow):
    """Handle Guesty options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Guesty options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    CONF_SCAN_INTERVAL: options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_LISTING_SYNC_INTERVAL: options.get(
                        CONF_LISTING_SYNC_INTERVAL, DEFAULT_LISTING_SYNC_INTERVAL
                    ),
                    CONF_RESERVATION_DAYS_PAST: options.get(
                        CONF_RESERVATION_DAYS_PAST, DEFAULT_RESERVATION_DAYS_PAST
                    ),
                    CONF_RESERVATION_DAYS_FUTURE: options.get(
                        CONF_RESERVATION_DAYS_FUTURE,
                        DEFAULT_RESERVATION_DAYS_FUTURE,
                    ),
                    CONF_STALE_THRESHOLD_HOURS: options.get(
                        CONF_STALE_THRESHOLD_HOURS, DEFAULT_STALE_THRESHOLD_HOURS
                    ),
                },
            ),
        )
