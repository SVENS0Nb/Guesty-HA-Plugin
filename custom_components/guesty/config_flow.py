"""Config flow for Guesty."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyAuthError,
    GuestyPermissionError,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LATE_MINUTES,
    CONF_ACCESS_LISTINGS,
    CONF_ACCESS_LOCK_1,
    CONF_ACCESS_LOCK_1_NAME,
    CONF_ACCESS_LOCK_2,
    CONF_ACCESS_LOCK_2_NAME,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_LISTING_SYNC_INTERVAL,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_STALE_THRESHOLD_HOURS,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_ACCESS_CUSTOM_FIELD,
    DEFAULT_ACCESS_EARLY_MINUTES,
    DEFAULT_ACCESS_ENABLED,
    DEFAULT_ACCESS_LATE_MINUTES,
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
        vol.Optional(
            CONF_EXPOSE_GUEST_DETAILS, default=DEFAULT_EXPOSE_GUEST_DETAILS
        ): bool,
        vol.Optional(CONF_ACCESS_ENABLED, default=DEFAULT_ACCESS_ENABLED): bool,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input and return info for the config entry."""
    client_id = data[CONF_CLIENT_ID].strip()
    client_secret = data[CONF_CLIENT_SECRET].strip()
    if not client_id or not client_secret:
        raise GuestyAuthError("Client ID and Client Secret are required")

    client = GuestyApiClient.from_hass(hass, client_id, client_secret)
    account_id = await client.async_validate_credentials()
    return {
        "title": "Guesty",
        "unique_id": account_id,
        CONF_ACCESS_TOKEN: client.access_token,
        CONF_TOKEN_EXPIRES_AT: client.token_expires_at,
    }


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
            except GuestyAuthError as err:
                _LOGGER.error("Guesty authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except GuestyPermissionError as err:
                _LOGGER.error("Guesty permission error: %s", err)
                errors["base"] = "no_permissions"
            except GuestyApiError as err:
                _LOGGER.error("Guesty API error during setup: %s", err)
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
                        CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                        CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET].strip(),
                        CONF_ACCESS_TOKEN: info[CONF_ACCESS_TOKEN],
                        CONF_TOKEN_EXPIRES_AT: info[CONF_TOKEN_EXPIRES_AT],
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
                        CONF_EXPOSE_GUEST_DETAILS: DEFAULT_EXPOSE_GUEST_DETAILS,
                        CONF_ACCESS_ENABLED: DEFAULT_ACCESS_ENABLED,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication after Guesty rejects the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Validate and store replacement Guesty credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except GuestyAuthError:
                errors["base"] = "invalid_auth"
            except GuestyPermissionError:
                errors["base"] = "no_permissions"
            except GuestyApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during Guesty reauthentication")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                        CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET].strip(),
                        CONF_ACCESS_TOKEN: info[CONF_ACCESS_TOKEN],
                        CONF_TOKEN_EXPIRES_AT: info[CONF_TOKEN_EXPIRES_AT],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GuestyOptionsFlow:
        """Get the options flow for this handler."""
        return GuestyOptionsFlow()


class GuestyOptionsFlow(OptionsFlow):
    """Handle Guesty options."""

    _pending_options: dict[str, Any]
    _pending_mappings: dict[str, list[dict[str, str]]]
    _listing_queue: list[str]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Guesty options."""
        if user_input is not None:
            self._pending_options = {**self.config_entry.options, **user_input}
            if not user_input.get(CONF_ACCESS_ENABLED, DEFAULT_ACCESS_ENABLED):
                return self.async_create_entry(title="", data=self._pending_options)
            return await self.async_step_access()

        options = self.config_entry.options
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
                    CONF_EXPOSE_GUEST_DETAILS: options.get(
                        CONF_EXPOSE_GUEST_DETAILS, DEFAULT_EXPOSE_GUEST_DETAILS
                    ),
                    CONF_ACCESS_ENABLED: options.get(
                        CONF_ACCESS_ENABLED, DEFAULT_ACCESS_ENABLED
                    ),
                },
            ),
        )

    async def async_step_access(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure global guest access settings and mapped listings."""
        coordinator = self.config_entry.runtime_data.coordinator
        listings = coordinator.data.listings if coordinator.data else {}
        choices = [
            selector.SelectOptionDict(value=listing_id, label=listing.display_name)
            for listing_id, listing in sorted(
                listings.items(), key=lambda item: item[1].display_name.lower()
            )
        ]
        if not choices:
            return self.async_abort(reason="no_listings")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_ACCESS_LISTINGS)
            if not isinstance(selected, list) or not selected:
                errors["base"] = "select_listing"
            else:
                self._pending_options.update(
                    {
                        CONF_ACCESS_CUSTOM_FIELD: str(
                            user_input[CONF_ACCESS_CUSTOM_FIELD]
                        ).strip(),
                        CONF_ACCESS_EARLY_MINUTES: int(
                            user_input[CONF_ACCESS_EARLY_MINUTES]
                        ),
                        CONF_ACCESS_LATE_MINUTES: int(
                            user_input[CONF_ACCESS_LATE_MINUTES]
                        ),
                    }
                )
                self._listing_queue = list(
                    dict.fromkeys(
                        listing_id for listing_id in selected if listing_id in listings
                    )
                )
                if not self._listing_queue:
                    errors["base"] = "select_listing"
                else:
                    self._pending_mappings = {}
                    return await self.async_step_listing()

        current_mappings = self.config_entry.options.get(CONF_ACCESS_LOCK_MAPPINGS, {})
        selected_listings = (
            [listing_id for listing_id in current_mappings if listing_id in listings]
            if isinstance(current_mappings, dict)
            else []
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_CUSTOM_FIELD): vol.All(
                    str, vol.Length(min=1, max=128)
                ),
                vol.Required(CONF_ACCESS_EARLY_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=180)
                ),
                vol.Required(CONF_ACCESS_LATE_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=180)
                ),
                vol.Required(CONF_ACCESS_LISTINGS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="access",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_ACCESS_CUSTOM_FIELD: self.config_entry.options.get(
                        CONF_ACCESS_CUSTOM_FIELD, DEFAULT_ACCESS_CUSTOM_FIELD
                    ),
                    CONF_ACCESS_EARLY_MINUTES: self.config_entry.options.get(
                        CONF_ACCESS_EARLY_MINUTES, DEFAULT_ACCESS_EARLY_MINUTES
                    ),
                    CONF_ACCESS_LATE_MINUTES: self.config_entry.options.get(
                        CONF_ACCESS_LATE_MINUTES, DEFAULT_ACCESS_LATE_MINUTES
                    ),
                    CONF_ACCESS_LISTINGS: selected_listings,
                },
            ),
            errors=errors,
        )

    async def async_step_listing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign one or two lock entities to one selected listing."""
        listing_id = self._listing_queue[0]
        coordinator = self.config_entry.runtime_data.coordinator
        listing = coordinator.data.listings[listing_id]
        errors: dict[str, str] = {}

        if user_input is not None:
            lock_1 = user_input[CONF_ACCESS_LOCK_1]
            lock_2 = user_input.get(CONF_ACCESS_LOCK_2)
            if lock_2 and lock_1 == lock_2:
                errors["base"] = "same_lock"
            else:
                doors = [
                    {
                        "entity_id": lock_1,
                        "name": str(
                            user_input.get(CONF_ACCESS_LOCK_1_NAME) or "Tür 1"
                        ).strip()[:80],
                    }
                ]
                if lock_2:
                    doors.append(
                        {
                            "entity_id": lock_2,
                            "name": str(
                                user_input.get(CONF_ACCESS_LOCK_2_NAME) or "Tür 2"
                            ).strip()[:80],
                        }
                    )
                self._pending_mappings[listing_id] = doors
                self._listing_queue.pop(0)
                if self._listing_queue:
                    return await self.async_step_listing()
                self._pending_options[CONF_ACCESS_LOCK_MAPPINGS] = (
                    self._pending_mappings
                )
                return self.async_create_entry(title="", data=self._pending_options)

        current = self.config_entry.options.get(CONF_ACCESS_LOCK_MAPPINGS, {})
        existing = current.get(listing_id, []) if isinstance(current, dict) else []
        first = existing[0] if len(existing) > 0 else {}
        second = existing[1] if len(existing) > 1 else {}
        lock_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="lock")
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_LOCK_1): lock_selector,
                vol.Required(CONF_ACCESS_LOCK_1_NAME): str,
                vol.Optional(CONF_ACCESS_LOCK_2): lock_selector,
                vol.Optional(CONF_ACCESS_LOCK_2_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="listing",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_ACCESS_LOCK_1: first.get("entity_id"),
                    CONF_ACCESS_LOCK_1_NAME: first.get("name", "Haustür"),
                    CONF_ACCESS_LOCK_2: second.get("entity_id"),
                    CONF_ACCESS_LOCK_2_NAME: second.get("name", "Wohnungstür"),
                },
            ),
            errors=errors,
            description_placeholders={"listing": listing.display_name},
        )
