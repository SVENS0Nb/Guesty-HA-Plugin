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

from .access_branding import MAX_BRANDING_URL_LENGTH, normalize_branding_url
from .access_names import (
    ACCESS_DOOR_LANGUAGES,
    DEFAULT_FIRST_DOOR_NAMES,
    DEFAULT_SECOND_DOOR_NAMES,
    localized_door_names,
)
from .api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyAuthError,
    GuestyPermissionError,
)
from .loxone_api import (
    LoxoneApiClient,
    LoxoneApiError,
    LoxoneAuthError,
    loxone_server_id,
    normalize_loxone_url,
)
from .const import (
    ACCESS_MAX_LOCKS,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_FAVICON_URL,
    CONF_ACCESS_LATE_MINUTES,
    CONF_ACCESS_LISTINGS,
    CONF_ACCESS_LOGO_URL,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_LISTING_SYNC_INTERVAL,
    CONF_LOXONE_CODE_PREFIX,
    CONF_LOXONE_CUSTOM_FIELD,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_GROUP_UUIDS,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_LOXONE_LISTINGS,
    CONF_LOXONE_MINISERVERS,
    CONF_LOXONE_PROVISION_LEAD_MINUTES,
    CONF_LOXONE_SERVER_GROUPS,
    CONF_LOXONE_SERVER_ID,
    CONF_LOXONE_SERVER_NAME,
    CONF_LOXONE_SERVER_PASSWORD,
    CONF_LOXONE_SERVER_URL,
    CONF_LOXONE_SERVER_USERNAME,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_STALE_THRESHOLD_HOURS,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_ACCESS_CUSTOM_FIELD,
    DEFAULT_ACCESS_EARLY_MINUTES,
    DEFAULT_ACCESS_ENABLED,
    DEFAULT_ACCESS_FAVICON_URL,
    DEFAULT_ACCESS_LATE_MINUTES,
    DEFAULT_ACCESS_LOGO_URL,
    DEFAULT_LISTING_SYNC_INTERVAL,
    DEFAULT_LOXONE_CODE_PREFIX,
    DEFAULT_LOXONE_CUSTOM_FIELD,
    DEFAULT_LOXONE_ENABLED,
    DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
    DEFAULT_RESERVATION_DAYS_FUTURE,
    DEFAULT_RESERVATION_DAYS_PAST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_LOXONE_SERVER_COUNT = "loxone_server_count"


def _lock_entity_field(position: int) -> str:
    """Return the options field for one lock position."""
    return f"access_lock_{position}"


def _lock_name_fields(position: int) -> dict[str, str]:
    """Return localized label fields for one lock position."""
    base = f"access_lock_{position}_name"
    return {
        language: base if language == "de" else f"{base}_{language}"
        for language in ACCESS_DOOR_LANGUAGES
    }


def _lock_defaults(position: int) -> dict[str, str]:
    """Return front-door defaults for the first slot and apartment-door defaults later."""
    return DEFAULT_FIRST_DOOR_NAMES if position == 1 else DEFAULT_SECOND_DOOR_NAMES


def _door_mapping_from_input(
    entity_id: str,
    user_input: dict[str, Any],
    fields: dict[str, str],
    defaults: dict[str, str],
) -> dict[str, str]:
    """Build complete localized mapping data, including legacy compatibility."""
    raw = {
        f"name_{language}": user_input.get(field) for language, field in fields.items()
    }
    raw["name"] = user_input.get(fields["de"])
    names = localized_door_names(raw, defaults)
    return {
        "entity_id": entity_id,
        "name": names["de"],
        **{f"name_{language}": name for language, name in names.items()},
    }


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
        vol.Optional(CONF_LOXONE_ENABLED, default=DEFAULT_LOXONE_ENABLED): bool,
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
                        CONF_LOXONE_ENABLED: DEFAULT_LOXONE_ENABLED,
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
    _pending_loxone_servers: list[dict[str, Any]]
    _pending_loxone_mappings: dict[str, dict[str, Any]]
    _loxone_server_queue: list[int]
    _loxone_listing_queue: list[str]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Guesty options."""
        if user_input is not None:
            self._pending_options = {**self.config_entry.options, **user_input}
            if user_input.get(CONF_ACCESS_ENABLED, DEFAULT_ACCESS_ENABLED):
                return await self.async_step_access()
            if user_input.get(CONF_LOXONE_ENABLED, DEFAULT_LOXONE_ENABLED):
                return await self.async_step_loxone()
            return self.async_create_entry(title="", data=self._pending_options)

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
                    CONF_LOXONE_ENABLED: options.get(
                        CONF_LOXONE_ENABLED, DEFAULT_LOXONE_ENABLED
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
            logo_url = normalize_branding_url(user_input.get(CONF_ACCESS_LOGO_URL))
            favicon_url = normalize_branding_url(
                user_input.get(CONF_ACCESS_FAVICON_URL)
            )
            branding_invalid = any(
                str(user_input.get(key) or "").strip() and normalized is None
                for key, normalized in (
                    (CONF_ACCESS_LOGO_URL, logo_url),
                    (CONF_ACCESS_FAVICON_URL, favicon_url),
                )
            )
            if branding_invalid:
                errors["base"] = "invalid_branding_url"
            elif not isinstance(selected, list) or not selected:
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
                        CONF_ACCESS_LOGO_URL: logo_url or "",
                        CONF_ACCESS_FAVICON_URL: favicon_url or "",
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
                vol.Optional(CONF_ACCESS_LOGO_URL): vol.All(
                    str, vol.Length(max=MAX_BRANDING_URL_LENGTH)
                ),
                vol.Optional(CONF_ACCESS_FAVICON_URL): vol.All(
                    str, vol.Length(max=MAX_BRANDING_URL_LENGTH)
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
                    CONF_ACCESS_CUSTOM_FIELD: (
                        user_input.get(CONF_ACCESS_CUSTOM_FIELD)
                        if user_input is not None
                        else self.config_entry.options.get(
                            CONF_ACCESS_CUSTOM_FIELD, DEFAULT_ACCESS_CUSTOM_FIELD
                        )
                    ),
                    CONF_ACCESS_LOGO_URL: (
                        user_input.get(CONF_ACCESS_LOGO_URL, "")
                        if user_input is not None
                        else self.config_entry.options.get(
                            CONF_ACCESS_LOGO_URL, DEFAULT_ACCESS_LOGO_URL
                        )
                    ),
                    CONF_ACCESS_FAVICON_URL: (
                        user_input.get(CONF_ACCESS_FAVICON_URL, "")
                        if user_input is not None
                        else self.config_entry.options.get(
                            CONF_ACCESS_FAVICON_URL, DEFAULT_ACCESS_FAVICON_URL
                        )
                    ),
                    CONF_ACCESS_EARLY_MINUTES: (
                        user_input.get(CONF_ACCESS_EARLY_MINUTES)
                        if user_input is not None
                        else self.config_entry.options.get(
                            CONF_ACCESS_EARLY_MINUTES, DEFAULT_ACCESS_EARLY_MINUTES
                        )
                    ),
                    CONF_ACCESS_LATE_MINUTES: (
                        user_input.get(CONF_ACCESS_LATE_MINUTES)
                        if user_input is not None
                        else self.config_entry.options.get(
                            CONF_ACCESS_LATE_MINUTES, DEFAULT_ACCESS_LATE_MINUTES
                        )
                    ),
                    CONF_ACCESS_LISTINGS: (
                        user_input.get(CONF_ACCESS_LISTINGS, [])
                        if user_input is not None
                        else selected_listings
                    ),
                },
            ),
            errors=errors,
        )

    async def async_step_listing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign one to six lock entities to one selected listing."""
        listing_id = self._listing_queue[0]
        coordinator = self.config_entry.runtime_data.coordinator
        listing = coordinator.data.listings[listing_id]
        errors: dict[str, str] = {}

        if user_input is not None:
            doors: list[dict[str, str]] = []
            selected_entities: set[str] = set()
            for position in range(1, ACCESS_MAX_LOCKS + 1):
                entity_id = user_input.get(_lock_entity_field(position))
                if not entity_id:
                    continue
                if entity_id in selected_entities:
                    errors["base"] = "same_lock"
                    break
                selected_entities.add(entity_id)
                doors.append(
                    _door_mapping_from_input(
                        entity_id,
                        user_input,
                        _lock_name_fields(position),
                        _lock_defaults(position),
                    )
                )

            if not errors:
                self._pending_mappings[listing_id] = doors
                self._listing_queue.pop(0)
                if self._listing_queue:
                    return await self.async_step_listing()
                self._pending_options[CONF_ACCESS_LOCK_MAPPINGS] = (
                    self._pending_mappings
                )
                if self._pending_options.get(
                    CONF_LOXONE_ENABLED, DEFAULT_LOXONE_ENABLED
                ):
                    return await self.async_step_loxone()
                return self.async_create_entry(title="", data=self._pending_options)

        current = self.config_entry.options.get(CONF_ACCESS_LOCK_MAPPINGS, {})
        existing = current.get(listing_id, []) if isinstance(current, dict) else []
        lock_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="lock")
        )
        required_label = vol.All(str, vol.Length(min=1, max=80))
        optional_label = vol.All(str, vol.Length(max=80))
        schema_fields: dict[Any, Any] = {}
        suggested_values: dict[str, Any] = {}
        for position in range(1, ACCESS_MAX_LOCKS + 1):
            entity_field = _lock_entity_field(position)
            name_fields = _lock_name_fields(position)
            existing_door = (
                existing[position - 1]
                if isinstance(existing, list) and len(existing) >= position
                else {}
            )
            names = localized_door_names(existing_door, _lock_defaults(position))
            entity_marker = vol.Required if position == 1 else vol.Optional
            label_marker = vol.Required if position == 1 else vol.Optional
            label_validator = required_label if position == 1 else optional_label
            schema_fields[entity_marker(entity_field)] = lock_selector
            suggested_values[entity_field] = existing_door.get("entity_id")
            for language, name_field in name_fields.items():
                schema_fields[label_marker(name_field)] = label_validator
                suggested_values[name_field] = names[language]

        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="listing",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                suggested_values,
            ),
            errors=errors,
            description_placeholders={"listing": listing.display_name},
        )

    async def async_step_loxone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure shared Loxone PIN timing and selected Guesty listings."""
        coordinator = self.config_entry.runtime_data.coordinator
        listings = coordinator.data.listings if coordinator.data else {}
        if not listings:
            return self.async_abort(reason="no_listings")
        choices = [
            selector.SelectOptionDict(value=listing_id, label=listing.display_name)
            for listing_id, listing in sorted(
                listings.items(), key=lambda item: item[1].display_name.lower()
            )
        ]
        current_servers = self.config_entry.options.get(CONF_LOXONE_MINISERVERS, [])
        if not isinstance(current_servers, list):
            current_servers = []
        current_mappings = self.config_entry.options.get(
            CONF_LOXONE_LISTING_MAPPINGS, {}
        )
        if not isinstance(current_mappings, dict):
            current_mappings = {}
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_LOXONE_LISTINGS)
            prefix = str(user_input.get(CONF_LOXONE_CODE_PREFIX, "")).strip()
            legacy_custom_field = str(
                user_input.get(CONF_LOXONE_CUSTOM_FIELD, "")
            ).strip()
            if not isinstance(selected, list) or not selected:
                errors["base"] = "select_listing"
            elif not prefix.isdigit() or not 1 <= len(prefix) <= 2:
                errors["base"] = "invalid_code_prefix"
            else:
                selected_ids = (
                    list(dict.fromkeys(item for item in selected if item in listings))
                    if not errors
                    else []
                )
                if not errors and not selected_ids:
                    errors["base"] = "select_listing"
                elif not errors:
                    self._pending_options.update(
                        {
                            CONF_LOXONE_PROVISION_LEAD_MINUTES: int(
                                user_input[CONF_LOXONE_PROVISION_LEAD_MINUTES]
                            ),
                            CONF_LOXONE_CODE_PREFIX: prefix,
                            CONF_LOXONE_CUSTOM_FIELD: legacy_custom_field,
                            CONF_ACCESS_EARLY_MINUTES: int(
                                user_input[CONF_ACCESS_EARLY_MINUTES]
                            ),
                            CONF_ACCESS_LATE_MINUTES: int(
                                user_input[CONF_ACCESS_LATE_MINUTES]
                            ),
                        }
                    )
                    server_count = int(user_input[CONF_LOXONE_SERVER_COUNT])
                    self._pending_loxone_servers = []
                    self._pending_loxone_mappings = {}
                    self._loxone_server_queue = list(range(server_count))
                    self._loxone_listing_queue = selected_ids
                    return await self.async_step_loxone_server()

        selected_listings = [
            listing_id for listing_id in current_mappings if listing_id in listings
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_LOXONE_PROVISION_LEAD_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=10080)
                ),
                vol.Required(CONF_LOXONE_CODE_PREFIX): vol.All(
                    str, vol.Length(min=1, max=2)
                ),
                vol.Optional(CONF_LOXONE_CUSTOM_FIELD): vol.All(
                    str, vol.Length(max=128)
                ),
                vol.Required(CONF_ACCESS_EARLY_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=180)
                ),
                vol.Required(CONF_ACCESS_LATE_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=180)
                ),
                vol.Required(CONF_LOXONE_SERVER_COUNT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=10)
                ),
                vol.Required(CONF_LOXONE_LISTINGS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="loxone",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_LOXONE_PROVISION_LEAD_MINUTES: self.config_entry.options.get(
                        CONF_LOXONE_PROVISION_LEAD_MINUTES,
                        DEFAULT_LOXONE_PROVISION_LEAD_MINUTES,
                    ),
                    CONF_LOXONE_CODE_PREFIX: self.config_entry.options.get(
                        CONF_LOXONE_CODE_PREFIX, DEFAULT_LOXONE_CODE_PREFIX
                    ),
                    CONF_LOXONE_CUSTOM_FIELD: self.config_entry.options.get(
                        CONF_LOXONE_CUSTOM_FIELD, DEFAULT_LOXONE_CUSTOM_FIELD
                    ),
                    CONF_ACCESS_EARLY_MINUTES: self._pending_options.get(
                        CONF_ACCESS_EARLY_MINUTES,
                        self.config_entry.options.get(
                            CONF_ACCESS_EARLY_MINUTES, DEFAULT_ACCESS_EARLY_MINUTES
                        ),
                    ),
                    CONF_ACCESS_LATE_MINUTES: self._pending_options.get(
                        CONF_ACCESS_LATE_MINUTES,
                        self.config_entry.options.get(
                            CONF_ACCESS_LATE_MINUTES, DEFAULT_ACCESS_LATE_MINUTES
                        ),
                    ),
                    CONF_LOXONE_SERVER_COUNT: max(len(current_servers), 1),
                    CONF_LOXONE_LISTINGS: selected_listings,
                },
            ),
            errors=errors,
        )

    async def async_step_loxone_server(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure and actively test one Loxone Miniserver connection."""
        index = self._loxone_server_queue[0]
        existing_servers = self.config_entry.options.get(CONF_LOXONE_MINISERVERS, [])
        if not isinstance(existing_servers, list):
            existing_servers = []
        existing = existing_servers[index] if index < len(existing_servers) else {}
        if not isinstance(existing, dict):
            existing = {}
        errors: dict[str, str] = {}

        if user_input is not None:
            password = str(user_input.get(CONF_LOXONE_SERVER_PASSWORD, ""))
            try:
                url = normalize_loxone_url(str(user_input[CONF_LOXONE_SERVER_URL]))
                username = str(user_input[CONF_LOXONE_SERVER_USERNAME]).strip()
                name = str(user_input[CONF_LOXONE_SERVER_NAME]).strip()
                existing_url = None
                if not password and existing:
                    try:
                        existing_url = normalize_loxone_url(
                            str(existing.get(CONF_LOXONE_SERVER_URL, ""))
                        )
                    except ValueError:
                        pass
                existing_username = str(
                    existing.get(CONF_LOXONE_SERVER_USERNAME, "")
                ).strip()
                if (
                    not password
                    and existing
                    and url == existing_url
                    and username == existing_username
                ):
                    password = str(existing.get(CONF_LOXONE_SERVER_PASSWORD, ""))
                client = LoxoneApiClient.from_hass(self.hass, url, username, password)
                groups = await client.async_get_groups()
                if not groups:
                    raise LoxoneApiError("No configurable Loxone user groups found")
            except LoxoneAuthError:
                errors["base"] = "loxone_invalid_auth"
            except (LoxoneApiError, ValueError, KeyError):
                errors["base"] = "loxone_cannot_connect"
            else:
                server_id = loxone_server_id(url, username)
                if any(
                    server.get(CONF_LOXONE_SERVER_ID) == server_id
                    for server in self._pending_loxone_servers
                ):
                    errors["base"] = "duplicate_loxone_server"
                else:
                    self._pending_loxone_servers.append(
                        {
                            CONF_LOXONE_SERVER_ID: server_id,
                            CONF_LOXONE_SERVER_NAME: name,
                            CONF_LOXONE_SERVER_URL: url,
                            CONF_LOXONE_SERVER_USERNAME: username,
                            CONF_LOXONE_SERVER_PASSWORD: password,
                            CONF_LOXONE_SERVER_GROUPS: groups,
                        }
                    )
                    self._loxone_server_queue.pop(0)
                    if self._loxone_server_queue:
                        return await self.async_step_loxone_server()
                    return await self.async_step_loxone_listing()

        password_selector = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_LOXONE_SERVER_NAME): vol.All(
                    str, vol.Length(min=1, max=80)
                ),
                vol.Required(CONF_LOXONE_SERVER_URL): vol.All(
                    str, vol.Length(min=8, max=512)
                ),
                vol.Required(CONF_LOXONE_SERVER_USERNAME): vol.All(
                    str, vol.Length(min=1, max=128)
                ),
                vol.Optional(CONF_LOXONE_SERVER_PASSWORD): password_selector,
            }
        )
        return self.async_show_form(
            step_id="loxone_server",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_LOXONE_SERVER_NAME: existing.get(
                        CONF_LOXONE_SERVER_NAME, f"Miniserver {index + 1}"
                    ),
                    CONF_LOXONE_SERVER_URL: existing.get(CONF_LOXONE_SERVER_URL, ""),
                    CONF_LOXONE_SERVER_USERNAME: existing.get(
                        CONF_LOXONE_SERVER_USERNAME, ""
                    ),
                    CONF_LOXONE_SERVER_PASSWORD: "",
                },
            ),
            errors=errors,
            description_placeholders={
                "number": str(index + 1),
                "total": str(index + 1 + len(self._loxone_server_queue) - 1),
            },
        )

    async def async_step_loxone_listing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Map one Guesty listing to groups on exactly one Miniserver."""
        listing_id = self._loxone_listing_queue[0]
        listing = self.config_entry.runtime_data.coordinator.data.listings[listing_id]
        choices: list[selector.SelectOptionDict] = []
        valid_values: dict[str, tuple[str, str]] = {}
        for server in self._pending_loxone_servers:
            server_id = server[CONF_LOXONE_SERVER_ID]
            server_name = server[CONF_LOXONE_SERVER_NAME]
            for group in server.get(CONF_LOXONE_SERVER_GROUPS, []):
                value = f"{server_id}|{group['uuid']}"
                valid_values[value] = (server_id, group["uuid"])
                choices.append(
                    selector.SelectOptionDict(
                        value=value,
                        label=f"{server_name} — {group['name']}",
                    )
                )
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_LOXONE_GROUP_UUIDS)
            parsed = (
                [valid_values[item] for item in selected if item in valid_values]
                if isinstance(selected, list)
                else []
            )
            server_ids = {item[0] for item in parsed}
            if not parsed:
                errors["base"] = "select_loxone_group"
            elif len(server_ids) != 1:
                errors["base"] = "groups_from_one_server"
            else:
                server_id = next(iter(server_ids))
                self._pending_loxone_mappings[listing_id] = {
                    CONF_LOXONE_SERVER_ID: server_id,
                    CONF_LOXONE_GROUP_UUIDS: list(
                        dict.fromkeys(item[1] for item in parsed)
                    ),
                }
                self._loxone_listing_queue.pop(0)
                if self._loxone_listing_queue:
                    return await self.async_step_loxone_listing()
                self._pending_options[CONF_LOXONE_MINISERVERS] = (
                    self._pending_loxone_servers
                )
                self._pending_options[CONF_LOXONE_LISTING_MAPPINGS] = (
                    self._pending_loxone_mappings
                )
                return self.async_create_entry(title="", data=self._pending_options)

        current = self.config_entry.options.get(CONF_LOXONE_LISTING_MAPPINGS, {})
        existing = current.get(listing_id, {}) if isinstance(current, dict) else {}
        selected_values = []
        if isinstance(existing, dict):
            selected_values = [
                f"{existing.get(CONF_LOXONE_SERVER_ID)}|{group_uuid}"
                for group_uuid in existing.get(CONF_LOXONE_GROUP_UUIDS, [])
                if f"{existing.get(CONF_LOXONE_SERVER_ID)}|{group_uuid}" in valid_values
            ]
        schema = vol.Schema(
            {
                vol.Required(CONF_LOXONE_GROUP_UUIDS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="loxone_listing",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_LOXONE_GROUP_UUIDS: selected_values}
            ),
            errors=errors,
            description_placeholders={"listing": listing.display_name},
        )
