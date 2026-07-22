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
from .ttlock_api import TTLockApiClient, TTLockApiError, TTLockAuthError
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
    CONF_GUESTY_CODE_SUFFIX,
    CONF_GUESTY_CODE_SUFFIXES,
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
    CONF_TTLOCK_ACCESS_TOKEN,
    CONF_TTLOCK_ACCOUNT,
    CONF_TTLOCK_CLIENT_ID,
    CONF_TTLOCK_CLIENT_SECRET,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    CONF_TTLOCK_LISTINGS,
    CONF_TTLOCK_LOCK_ID,
    CONF_TTLOCK_LOCK_IDS,
    CONF_TTLOCK_LOCK_NAME,
    CONF_TTLOCK_LOCKS,
    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
    CONF_TTLOCK_REFRESH_TOKEN,
    CONF_TTLOCK_REGION,
    CONF_TTLOCK_TOKEN_EXPIRES_AT,
    CONF_TTLOCK_USERNAME,
    CONF_RESERVATION_DAYS_FUTURE,
    CONF_RESERVATION_DAYS_PAST,
    CONF_STALE_THRESHOLD_HOURS,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_EXPOSE_GUEST_DETAILS,
    DEFAULT_GUESTY_CODE_SUFFIX,
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
    DEFAULT_TTLOCK_ENABLED,
    DEFAULT_TTLOCK_PROVISION_LEAD_MINUTES,
    DEFAULT_TTLOCK_REGION,
    DEFAULT_RESERVATION_DAYS_FUTURE,
    DEFAULT_RESERVATION_DAYS_PAST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_THRESHOLD_HOURS,
    DOMAIN,
    GUESTY_CODE_SUFFIX_MAX_LENGTH,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    TTLOCK_API_BASE_URLS,
    TTLOCK_MAX_LOCKS_PER_LISTING,
)

_LOGGER = logging.getLogger(__name__)

CONF_LOXONE_SERVER_COUNT = "loxone_server_count"
CONF_TTLOCK_PASSWORD = "ttlock_password"
TTLOCK_OPEN_PLATFORM_URL = "https://euopen.ttlock.com/"
TTLOCK_OAUTH_DOC_URL = "https://euopen.ttlock.com/doc/oauth2"
GUESTY_CODE_SUFFIX_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
)


def _guesty_code_suffix(value: Any) -> str:
    """Validate a short non-numeric Guesty PIN display suffix."""
    suffix = str(value or "").strip()
    if len(suffix) > GUESTY_CODE_SUFFIX_MAX_LENGTH:
        raise vol.Invalid("Guesty code suffix is too long")
    if any(character.isdigit() or not character.isprintable() for character in suffix):
        raise vol.Invalid(
            "Guesty code suffix cannot contain digits or invisible controls"
        )
    return suffix


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
        vol.Optional(CONF_TTLOCK_ENABLED, default=DEFAULT_TTLOCK_ENABLED): bool,
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
                        CONF_TTLOCK_ENABLED: DEFAULT_TTLOCK_ENABLED,
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
    _pending_code_suffixes: dict[str, str]
    _loxone_server_queue: list[int]
    _loxone_listing_queue: list[str]
    _pending_ttlock_account: dict[str, Any]
    _pending_ttlock_locks: list[dict[str, Any]]
    _pending_ttlock_mappings: dict[str, dict[str, list[int]]]
    _ttlock_listing_queue: list[str]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Guesty options."""
        if user_input is not None:
            self._pending_options = {**self.config_entry.options, **user_input}
            self._pending_code_suffixes = {}
            self._pending_options[CONF_GUESTY_CODE_SUFFIXES] = (
                self._pending_code_suffixes
            )
            if user_input.get(CONF_ACCESS_ENABLED, DEFAULT_ACCESS_ENABLED):
                return await self.async_step_access()
            if user_input.get(CONF_LOXONE_ENABLED, DEFAULT_LOXONE_ENABLED):
                return await self.async_step_loxone()
            if user_input.get(CONF_TTLOCK_ENABLED, DEFAULT_TTLOCK_ENABLED):
                return await self.async_step_ttlock()
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
                    CONF_TTLOCK_ENABLED: options.get(
                        CONF_TTLOCK_ENABLED, DEFAULT_TTLOCK_ENABLED
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
                if self._pending_options.get(
                    CONF_TTLOCK_ENABLED, DEFAULT_TTLOCK_ENABLED
                ):
                    return await self.async_step_ttlock()
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
            custom_field = str(user_input.get(CONF_LOXONE_CUSTOM_FIELD, "")).strip()
            if not isinstance(selected, list) or not selected:
                errors["base"] = "select_listing"
            elif (
                not prefix.isascii()
                or not prefix.isdigit()
                or not 1 <= len(prefix) <= 2
            ):
                errors["base"] = "invalid_code_prefix"
            elif not custom_field:
                errors["base"] = "custom_field_not_found"
            else:
                try:
                    await self.config_entry.runtime_data.client.async_resolve_custom_field(
                        custom_field
                    )
                except (GuestyApiError, GuestyAuthError):
                    errors["base"] = "custom_field_not_found"

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
                            CONF_LOXONE_CUSTOM_FIELD: custom_field,
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
                vol.Required(CONF_LOXONE_CUSTOM_FIELD): vol.All(
                    str, vol.Length(min=1, max=128)
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
                    CONF_LOXONE_CUSTOM_FIELD: (
                        self.config_entry.options.get(CONF_LOXONE_CUSTOM_FIELD)
                        or DEFAULT_LOXONE_CUSTOM_FIELD
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

        if user_input and CONF_LOXONE_GROUP_UUIDS in user_input:
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
                try:
                    suffix = _guesty_code_suffix(
                        user_input.get(CONF_GUESTY_CODE_SUFFIX)
                    )
                except vol.Invalid:
                    errors[CONF_GUESTY_CODE_SUFFIX] = "invalid_code_suffix"
                else:
                    server_id = next(iter(server_ids))
                    self._pending_code_suffixes[listing_id] = suffix
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
                    if self._pending_options.get(
                        CONF_TTLOCK_ENABLED, DEFAULT_TTLOCK_ENABLED
                    ):
                        return await self.async_step_ttlock()
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
                vol.Optional(CONF_LOXONE_GROUP_UUIDS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_GUESTY_CODE_SUFFIX): GUESTY_CODE_SUFFIX_SELECTOR,
            },
            # A slow Miniserver check can leave a stale credential submission in
            # flight while this next step is already active. Drop only those
            # now-unrelated fields instead of exposing Voluptuous internals in
            # the UI; the credential step has already validated and stored them
            # in the in-memory pending configuration.
            extra=vol.REMOVE_EXTRA,
        )
        current_suffixes = self.config_entry.options.get(CONF_GUESTY_CODE_SUFFIXES, {})
        existing_suffix = (
            current_suffixes.get(listing_id, DEFAULT_GUESTY_CODE_SUFFIX)
            if isinstance(current_suffixes, dict)
            else DEFAULT_GUESTY_CODE_SUFFIX
        )
        schema = self.add_suggested_values_to_schema(
            schema,
            {
                CONF_LOXONE_GROUP_UUIDS: selected_values,
                CONF_GUESTY_CODE_SUFFIX: self._pending_code_suffixes.get(
                    listing_id, existing_suffix
                ),
            },
        )
        # Home Assistant's suggested-value helper rebuilds the schema with the
        # default PREVENT_EXTRA policy, so restore the deliberate stale-submit
        # handling after applying suggestions.
        schema = vol.Schema(schema.schema, extra=vol.REMOVE_EXTRA)
        return self.async_show_form(
            step_id="loxone_listing",
            data_schema=schema,
            errors=errors,
            description_placeholders={"listing": listing.display_name},
        )

    async def async_step_ttlock(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure and validate one TTLock Open Platform account."""
        coordinator = self.config_entry.runtime_data.coordinator
        listings = coordinator.data.listings if coordinator.data else {}
        if not listings:
            return self.async_abort(reason="no_listings")
        listing_choices = [
            selector.SelectOptionDict(value=listing_id, label=listing.display_name)
            for listing_id, listing in sorted(
                listings.items(), key=lambda item: item[1].display_name.lower()
            )
        ]
        existing_account = self.config_entry.options.get(CONF_TTLOCK_ACCOUNT, {})
        if not isinstance(existing_account, dict):
            existing_account = {}
        ttlock_manager = getattr(self.config_entry.runtime_data, "ttlock_manager", None)
        if ttlock_manager is not None:
            existing_account = ttlock_manager.account_for_reconfigure()
        errors: dict[str, str] = {}
        loxone_configured = bool(
            self._pending_options.get(CONF_LOXONE_ENABLED, DEFAULT_LOXONE_ENABLED)
        )
        access_times_configured = loxone_configured or bool(
            self._pending_options.get(CONF_ACCESS_ENABLED, DEFAULT_ACCESS_ENABLED)
        )

        if user_input is not None:
            selected = user_input.get(CONF_TTLOCK_LISTINGS)
            prefix = str(
                user_input.get(CONF_LOXONE_CODE_PREFIX)
                or self._pending_options.get(CONF_LOXONE_CODE_PREFIX)
                or self.config_entry.options.get(CONF_LOXONE_CODE_PREFIX)
                or DEFAULT_LOXONE_CODE_PREFIX
            ).strip()
            custom_field = str(
                user_input.get(CONF_LOXONE_CUSTOM_FIELD)
                or self._pending_options.get(CONF_LOXONE_CUSTOM_FIELD)
                or self.config_entry.options.get(CONF_LOXONE_CUSTOM_FIELD)
                or DEFAULT_LOXONE_CUSTOM_FIELD
            ).strip()
            early_minutes = int(
                user_input.get(
                    CONF_ACCESS_EARLY_MINUTES,
                    self._pending_options.get(
                        CONF_ACCESS_EARLY_MINUTES,
                        self.config_entry.options.get(
                            CONF_ACCESS_EARLY_MINUTES, DEFAULT_ACCESS_EARLY_MINUTES
                        ),
                    ),
                )
            )
            late_minutes = int(
                user_input.get(
                    CONF_ACCESS_LATE_MINUTES,
                    self._pending_options.get(
                        CONF_ACCESS_LATE_MINUTES,
                        self.config_entry.options.get(
                            CONF_ACCESS_LATE_MINUTES, DEFAULT_ACCESS_LATE_MINUTES
                        ),
                    ),
                )
            )
            region = str(user_input.get(CONF_TTLOCK_REGION, "")).strip()
            client_id = str(user_input.get(CONF_TTLOCK_CLIENT_ID, "")).strip()
            username = str(user_input.get(CONF_TTLOCK_USERNAME, "")).strip()
            client_secret = str(user_input.get(CONF_TTLOCK_CLIENT_SECRET, "")).strip()
            password = str(user_input.get(CONF_TTLOCK_PASSWORD, ""))
            same_identity = (
                region == existing_account.get(CONF_TTLOCK_REGION)
                and client_id == existing_account.get(CONF_TTLOCK_CLIENT_ID)
                and username == existing_account.get(CONF_TTLOCK_USERNAME)
            )
            stored_client_secret = str(
                existing_account.get(CONF_TTLOCK_CLIENT_SECRET, "")
            )
            if not client_secret and same_identity:
                client_secret = stored_client_secret
            credentials_unchanged = (
                same_identity and client_secret == stored_client_secret
            )

            if not isinstance(selected, list) or not selected:
                errors["base"] = "select_listing"
            elif (
                not prefix.isascii()
                or not prefix.isdigit()
                or not 1 <= len(prefix) <= 2
            ):
                errors["base"] = "invalid_code_prefix"
            elif not custom_field:
                errors["base"] = "custom_field_not_found"
            elif region not in TTLOCK_API_BASE_URLS:
                errors["base"] = "ttlock_invalid_region"
            elif not client_id or not client_secret or not username:
                errors["base"] = "ttlock_invalid_auth"
            else:
                try:
                    await self.config_entry.runtime_data.client.async_resolve_custom_field(
                        custom_field
                    )
                    client = TTLockApiClient.from_hass(
                        self.hass,
                        region=region,
                        client_id=client_id,
                        client_secret=client_secret,
                        username=username,
                        access_token=(
                            str(existing_account.get(CONF_TTLOCK_ACCESS_TOKEN, ""))
                            if same_identity
                            else ""
                        ),
                        refresh_token=(
                            str(existing_account.get(CONF_TTLOCK_REFRESH_TOKEN, ""))
                            if same_identity
                            else ""
                        ),
                        token_expires_at=(
                            str(existing_account.get(CONF_TTLOCK_TOKEN_EXPIRES_AT, ""))
                            if same_identity
                            else ""
                        ),
                    )
                    if password:
                        await client.async_authenticate(username, password)
                    elif not same_identity:
                        raise TTLockAuthError("TTLock password is required")
                    elif not credentials_unchanged:
                        # Existing access tokens can make a mistyped replacement
                        # secret appear valid. Force a refresh with the new
                        # secret before it is saved.
                        await client.async_refresh_access_token()
                    locks = await client.async_list_locks()
                except (GuestyApiError, GuestyAuthError):
                    errors["base"] = "custom_field_not_found"
                except TTLockAuthError:
                    errors["base"] = "ttlock_invalid_auth"
                except (TTLockApiError, ValueError, KeyError):
                    errors["base"] = "ttlock_cannot_connect"
                else:
                    compatible: list[dict[str, Any]] = []
                    for item in locks:
                        try:
                            lock_id = int(item.get("lockId"))
                            password_version = int(item.get("keyboardPwdVersion", 0))
                            has_gateway = int(item.get("hasGateway", 0))
                        except (TypeError, ValueError):
                            continue
                        if password_version != 4 or has_gateway != 1:
                            continue
                        name = str(
                            item.get("lockAlias") or item.get("lockName") or lock_id
                        ).strip()[:120]
                        compatible.append(
                            {
                                CONF_TTLOCK_LOCK_ID: lock_id,
                                CONF_TTLOCK_LOCK_NAME: name,
                            }
                        )
                    if not compatible:
                        errors["base"] = "ttlock_no_compatible_locks"
                    else:
                        selected_ids = list(
                            dict.fromkeys(item for item in selected if item in listings)
                        )
                        if not selected_ids:
                            errors["base"] = "select_listing"
                        else:
                            self._pending_options.update(
                                {
                                    CONF_LOXONE_CUSTOM_FIELD: custom_field,
                                    CONF_LOXONE_CODE_PREFIX: prefix,
                                    CONF_ACCESS_EARLY_MINUTES: early_minutes,
                                    CONF_ACCESS_LATE_MINUTES: late_minutes,
                                    CONF_TTLOCK_PROVISION_LEAD_MINUTES: int(
                                        user_input[CONF_TTLOCK_PROVISION_LEAD_MINUTES]
                                    ),
                                }
                            )
                            self._pending_ttlock_account = {
                                CONF_TTLOCK_REGION: region,
                                CONF_TTLOCK_CLIENT_ID: client_id,
                                CONF_TTLOCK_CLIENT_SECRET: client_secret,
                                CONF_TTLOCK_USERNAME: username,
                                **client.token_snapshot(),
                            }
                            self._pending_ttlock_locks = compatible
                            self._pending_ttlock_mappings = {}
                            self._ttlock_listing_queue = selected_ids
                            return await self.async_step_ttlock_listing()

        current_mappings = self.config_entry.options.get(
            CONF_TTLOCK_LISTING_MAPPINGS, {}
        )
        selected_listings = (
            [listing_id for listing_id in current_mappings if listing_id in listings]
            if isinstance(current_mappings, dict)
            else []
        )
        region_options = [
            selector.SelectOptionDict(
                value="eu", label="EU / Europe (euapi.ttlock.com)"
            ),
            selector.SelectOptionDict(value="global", label="Global (api.ttlock.com)"),
            selector.SelectOptionDict(
                value="legacy", label="Legacy / Sciener (api.sciener.com)"
            ),
        ]
        password_selector = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_TTLOCK_REGION): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=region_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_TTLOCK_CLIENT_ID): vol.All(
                str, vol.Length(min=1, max=256)
            ),
            vol.Optional(CONF_TTLOCK_CLIENT_SECRET): password_selector,
            vol.Required(CONF_TTLOCK_USERNAME): vol.All(
                str, vol.Length(min=1, max=256)
            ),
            vol.Optional(CONF_TTLOCK_PASSWORD): password_selector,
            vol.Required(CONF_TTLOCK_LISTINGS): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=listing_choices,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_TTLOCK_PROVISION_LEAD_MINUTES): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=10080)
            ),
        }
        if not loxone_configured:
            schema_fields.update(
                {
                    vol.Required(CONF_LOXONE_CUSTOM_FIELD): vol.All(
                        str, vol.Length(min=1, max=128)
                    ),
                    vol.Required(CONF_LOXONE_CODE_PREFIX): vol.All(
                        str, vol.Length(min=1, max=2)
                    ),
                }
            )
        if not access_times_configured:
            schema_fields.update(
                {
                    vol.Required(CONF_ACCESS_EARLY_MINUTES): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=180)
                    ),
                    vol.Required(CONF_ACCESS_LATE_MINUTES): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=180)
                    ),
                }
            )
        schema = vol.Schema(schema_fields)
        return self.async_show_form(
            step_id="ttlock",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_LOXONE_CUSTOM_FIELD: (
                        self._pending_options.get(CONF_LOXONE_CUSTOM_FIELD)
                        or self.config_entry.options.get(CONF_LOXONE_CUSTOM_FIELD)
                        or DEFAULT_LOXONE_CUSTOM_FIELD
                    ),
                    CONF_LOXONE_CODE_PREFIX: self._pending_options.get(
                        CONF_LOXONE_CODE_PREFIX,
                        self.config_entry.options.get(
                            CONF_LOXONE_CODE_PREFIX, DEFAULT_LOXONE_CODE_PREFIX
                        ),
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
                    CONF_TTLOCK_PROVISION_LEAD_MINUTES: self.config_entry.options.get(
                        CONF_TTLOCK_PROVISION_LEAD_MINUTES,
                        DEFAULT_TTLOCK_PROVISION_LEAD_MINUTES,
                    ),
                    CONF_TTLOCK_REGION: existing_account.get(
                        CONF_TTLOCK_REGION, DEFAULT_TTLOCK_REGION
                    ),
                    CONF_TTLOCK_CLIENT_ID: existing_account.get(
                        CONF_TTLOCK_CLIENT_ID, ""
                    ),
                    CONF_TTLOCK_CLIENT_SECRET: "",
                    CONF_TTLOCK_USERNAME: existing_account.get(
                        CONF_TTLOCK_USERNAME, ""
                    ),
                    CONF_TTLOCK_PASSWORD: "",
                    CONF_TTLOCK_LISTINGS: selected_listings,
                },
            ),
            errors=errors,
            description_placeholders={
                "open_platform_url": TTLOCK_OPEN_PLATFORM_URL,
                "oauth_doc_url": TTLOCK_OAUTH_DOC_URL,
            },
        )

    async def async_step_ttlock_listing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign one to six compatible TTLock locks to one listing."""
        listing_id = self._ttlock_listing_queue[0]
        listing = self.config_entry.runtime_data.coordinator.data.listings[listing_id]
        choices = [
            selector.SelectOptionDict(
                value=str(item[CONF_TTLOCK_LOCK_ID]),
                label=str(item[CONF_TTLOCK_LOCK_NAME]),
            )
            for item in self._pending_ttlock_locks
        ]
        valid_ids = {
            int(item[CONF_TTLOCK_LOCK_ID]) for item in self._pending_ttlock_locks
        }
        errors: dict[str, str] = {}
        suffix_already_configured = listing_id in self._pending_code_suffixes
        if user_input and CONF_TTLOCK_LOCK_IDS in user_input:
            selected = user_input.get(CONF_TTLOCK_LOCK_IDS)
            parsed: list[int] = []
            if isinstance(selected, list):
                for value in selected:
                    try:
                        lock_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if lock_id in valid_ids and lock_id not in parsed:
                        parsed.append(lock_id)
            if not parsed:
                errors["base"] = "select_ttlock_lock"
            elif len(parsed) > TTLOCK_MAX_LOCKS_PER_LISTING:
                errors["base"] = "too_many_ttlock_locks"
            else:
                try:
                    suffix = (
                        _guesty_code_suffix(user_input.get(CONF_GUESTY_CODE_SUFFIX))
                        if not suffix_already_configured
                        else None
                    )
                except vol.Invalid:
                    errors[CONF_GUESTY_CODE_SUFFIX] = "invalid_code_suffix"
                else:
                    if suffix is not None:
                        self._pending_code_suffixes[listing_id] = suffix
                    self._pending_ttlock_mappings[listing_id] = {
                        CONF_TTLOCK_LOCK_IDS: parsed
                    }
                    self._ttlock_listing_queue.pop(0)
                    if self._ttlock_listing_queue:
                        return await self.async_step_ttlock_listing()
                    self._pending_options[CONF_TTLOCK_ACCOUNT] = (
                        self._pending_ttlock_account
                    )
                    self._pending_options[CONF_TTLOCK_LOCKS] = (
                        self._pending_ttlock_locks
                    )
                    self._pending_options[CONF_TTLOCK_LISTING_MAPPINGS] = (
                        self._pending_ttlock_mappings
                    )
                    return self.async_create_entry(title="", data=self._pending_options)

        current = self.config_entry.options.get(CONF_TTLOCK_LISTING_MAPPINGS, {})
        existing = current.get(listing_id, {}) if isinstance(current, dict) else {}
        raw_selected = (
            existing.get(CONF_TTLOCK_LOCK_IDS, [])
            if isinstance(existing, dict)
            else existing
        )
        if not isinstance(raw_selected, list):
            raw_selected = []
        valid_choice_values = {item["value"] for item in choices}
        selected_values = [
            str(value) for value in raw_selected if str(value) in valid_choice_values
        ]
        schema_fields: dict[Any, Any] = {
            vol.Optional(CONF_TTLOCK_LOCK_IDS): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=choices,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if not suffix_already_configured:
            schema_fields[vol.Optional(CONF_GUESTY_CODE_SUFFIX)] = (
                GUESTY_CODE_SUFFIX_SELECTOR
            )
        # Authentication and lock discovery can outlive a duplicate frontend
        # submission. Once this step is active, discard fields from that prior
        # account form and keep waiting for an explicit lock selection.
        schema = vol.Schema(schema_fields, extra=vol.REMOVE_EXTRA)
        current_suffixes = self.config_entry.options.get(CONF_GUESTY_CODE_SUFFIXES, {})
        existing_suffix = (
            current_suffixes.get(listing_id, DEFAULT_GUESTY_CODE_SUFFIX)
            if isinstance(current_suffixes, dict)
            else DEFAULT_GUESTY_CODE_SUFFIX
        )
        suggested_values: dict[str, Any] = {
            CONF_TTLOCK_LOCK_IDS: selected_values,
        }
        if not suffix_already_configured:
            suggested_values[CONF_GUESTY_CODE_SUFFIX] = existing_suffix
        schema = self.add_suggested_values_to_schema(schema, suggested_values)
        schema = vol.Schema(schema.schema, extra=vol.REMOVE_EXTRA)
        return self.async_show_form(
            step_id="ttlock_listing",
            data_schema=schema,
            errors=errors,
            description_placeholders={"listing": listing.display_name},
        )
