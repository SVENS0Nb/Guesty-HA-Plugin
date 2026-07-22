"""Tests for Guesty setup, options, and reauthentication flows."""

from __future__ import annotations

from unittest.mock import AsyncMock
from types import SimpleNamespace

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from voluptuous_serialize import convert

from custom_components.guesty import config_flow
from custom_components.guesty.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_FAVICON_URL,
    CONF_ACCESS_LATE_MINUTES,
    CONF_ACCESS_LISTINGS,
    CONF_ACCESS_LOGO_URL,
    CONF_ACCESS_LOCK_1,
    CONF_ACCESS_LOCK_1_NAME,
    CONF_ACCESS_LOCK_1_NAME_EN,
    CONF_ACCESS_LOCK_1_NAME_ES,
    CONF_ACCESS_LOCK_1_NAME_FR,
    CONF_ACCESS_LOCK_2,
    CONF_ACCESS_LOCK_3,
    CONF_ACCESS_LOCK_4,
    CONF_ACCESS_LOCK_5,
    CONF_ACCESS_LOCK_6,
    CONF_ACCESS_LOCK_6_NAME,
    CONF_ACCESS_LOCK_6_NAME_EN,
    CONF_ACCESS_LOCK_6_NAME_ES,
    CONF_ACCESS_LOCK_6_NAME_FR,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_GUESTY_CODE_SUFFIX,
    CONF_GUESTY_CODE_SUFFIXES,
    CONF_LOXONE_CODE_PREFIX,
    CONF_LOXONE_CUSTOM_FIELD,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_GROUP_UUIDS,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_LOXONE_LISTINGS,
    CONF_LOXONE_MINISERVERS,
    CONF_LOXONE_PROVISION_LEAD_MINUTES,
    CONF_LOXONE_SERVER_ID,
    CONF_LOXONE_SERVER_NAME,
    CONF_LOXONE_SERVER_PASSWORD,
    CONF_LOXONE_SERVER_URL,
    CONF_LOXONE_SERVER_USERNAME,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TTLOCK_ACCOUNT,
    CONF_TTLOCK_ACCESS_TOKEN,
    CONF_TTLOCK_CLIENT_ID,
    CONF_TTLOCK_CLIENT_SECRET,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    CONF_TTLOCK_LISTINGS,
    CONF_TTLOCK_LOCK_ID,
    CONF_TTLOCK_LOCK_IDS,
    CONF_TTLOCK_LOCKS,
    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
    CONF_TTLOCK_REFRESH_TOKEN,
    CONF_TTLOCK_REGION,
    CONF_TTLOCK_TOKEN_EXPIRES_AT,
    CONF_TTLOCK_USERNAME,
    DOMAIN,
)
from custom_components.guesty.models import GuestyListing
from custom_components.guesty.loxone_api import loxone_server_id


VALIDATED = {
    "title": "Guesty",
    "unique_id": "account-hash",
    CONF_ACCESS_TOKEN: "validated-token",
    CONF_TOKEN_EXPIRES_AT: 123456.0,
}


@pytest.mark.parametrize("suffix", ["#", "*", "☑️", "☑️ / #", ""])
def test_guesty_code_suffix_accepts_visible_non_numeric_text(suffix) -> None:
    """Administrators may enter short visible keypad instructions."""
    assert config_flow._guesty_code_suffix(suffix) == suffix


@pytest.mark.parametrize(
    "suffix",
    ["1", "#2", "123456", "\x00", "\u202e", "\u200b", "123456789"],
)
def test_guesty_code_suffix_rejects_digits_controls_and_long_values(suffix) -> None:
    """Unsafe or ambiguous display suffixes never reach Guesty."""
    with pytest.raises(config_flow.vol.Invalid):
        config_flow._guesty_code_suffix(suffix)


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


@pytest.mark.asyncio
async def test_options_flow_maps_up_to_six_locks_per_listing(hass) -> None:
    """Secure access configuration stores only server-selected lock entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        )
    )

    form = await hass.config_entries.options.async_init(entry.entry_id)
    access_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: True,
        },
    )
    assert access_form["type"] is FlowResultType.FORM
    assert access_form["step_id"] == "access"

    listing_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_ACCESS_CUSTOM_FIELD: "Door access link",
            CONF_ACCESS_LOGO_URL: "https://assets.example.com/guest-logo.png",
            CONF_ACCESS_FAVICON_URL: "https://assets.example.com/favicon.ico",
            CONF_ACCESS_EARLY_MINUTES: 0,
            CONF_ACCESS_LATE_MINUTES: 0,
            CONF_ACCESS_LISTINGS: ["listing-1"],
        },
    )
    assert listing_form["type"] is FlowResultType.FORM
    assert listing_form["step_id"] == "listing"
    suggestions = {
        marker.schema: marker.description.get("suggested_value")
        for marker in listing_form["data_schema"].schema
    }
    assert suggestions[CONF_ACCESS_LOCK_6_NAME] == "Wohnungstür"
    assert suggestions[CONF_ACCESS_LOCK_6_NAME_EN] == "Apartment door"
    assert suggestions[CONF_ACCESS_LOCK_6_NAME_ES] == "Puerta del apartamento"
    assert suggestions[CONF_ACCESS_LOCK_6_NAME_FR] == "Porte de l’appartement"

    duplicate = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_ACCESS_LOCK_1: "lock.front_door",
            CONF_ACCESS_LOCK_1_NAME: "Haustür",
            CONF_ACCESS_LOCK_1_NAME_EN: "Front door",
            CONF_ACCESS_LOCK_1_NAME_ES: "Puerta principal",
            CONF_ACCESS_LOCK_1_NAME_FR: "Porte d’entrée",
            CONF_ACCESS_LOCK_6: "lock.front_door",
        },
    )
    assert duplicate["type"] is FlowResultType.FORM
    assert duplicate["errors"] == {"base": "same_lock"}

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_ACCESS_LOCK_1: "lock.front_door",
            CONF_ACCESS_LOCK_1_NAME: "Haustür",
            CONF_ACCESS_LOCK_1_NAME_EN: "Front door",
            CONF_ACCESS_LOCK_1_NAME_ES: "Puerta principal",
            CONF_ACCESS_LOCK_1_NAME_FR: "Porte d’entrée",
            CONF_ACCESS_LOCK_2: "lock.apartment_1",
            CONF_ACCESS_LOCK_3: "lock.apartment_2",
            CONF_ACCESS_LOCK_4: "lock.apartment_3",
            CONF_ACCESS_LOCK_5: "lock.apartment_4",
            CONF_ACCESS_LOCK_6: "lock.apartment_5",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        result["data"][CONF_ACCESS_LOGO_URL]
        == "https://assets.example.com/guest-logo.png"
    )
    assert (
        result["data"][CONF_ACCESS_FAVICON_URL]
        == "https://assets.example.com/favicon.ico"
    )
    doors = result["data"][CONF_ACCESS_LOCK_MAPPINGS]["listing-1"]
    assert [door["entity_id"] for door in doors] == [
        "lock.front_door",
        "lock.apartment_1",
        "lock.apartment_2",
        "lock.apartment_3",
        "lock.apartment_4",
        "lock.apartment_5",
    ]
    assert doors[0] == {
        "entity_id": "lock.front_door",
        "name": "Haustür",
        "name_de": "Haustür",
        "name_en": "Front door",
        "name_es": "Puerta principal",
        "name_fr": "Porte d’entrée",
    }
    for door in doors[1:]:
        assert door == {
            "entity_id": door["entity_id"],
            "name": "Wohnungstür",
            "name_de": "Wohnungstür",
            "name_en": "Apartment door",
            "name_es": "Puerta del apartamento",
            "name_fr": "Porte de l’appartement",
        }


@pytest.mark.asyncio
async def test_options_flow_rejects_insecure_branding_url(hass) -> None:
    """Public portal branding cannot weaken HTTPS or CSP protections."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        )
    )
    form = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: True,
        },
    )

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_ACCESS_CUSTOM_FIELD: "Door access link",
            CONF_ACCESS_LOGO_URL: "http://assets.example.com/logo.png",
            CONF_ACCESS_FAVICON_URL: "https://assets.example.com/favicon.ico",
            CONF_ACCESS_EARLY_MINUTES: 0,
            CONF_ACCESS_LATE_MINUTES: 0,
            CONF_ACCESS_LISTINGS: ["listing-1"],
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "access"
    assert result["errors"] == {"base": "invalid_branding_url"}


@pytest.mark.asyncio
async def test_options_flow_tests_loxone_and_maps_groups(hass, monkeypatch) -> None:
    """The UI stores tested Miniserver credentials and one-server group mappings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        ),
        client=SimpleNamespace(
            async_resolve_custom_field=AsyncMock(
                return_value="65fab102a5284d73c6206db0"
            )
        ),
    )
    loxone_client = SimpleNamespace(
        async_get_groups=AsyncMock(
            return_value=[
                {"uuid": "group-front", "name": "Haustür"},
                {"uuid": "group-flat", "name": "Wohnung"},
            ]
        )
    )
    monkeypatch.setattr(
        config_flow.LoxoneApiClient,
        "from_hass",
        lambda *args: loxone_client,
    )

    form = await hass.config_entries.options.async_init(entry.entry_id)
    loxone_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: False,
            CONF_LOXONE_ENABLED: True,
        },
    )
    assert loxone_form["step_id"] == "loxone"

    server_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_PROVISION_LEAD_MINUTES: 360,
            CONF_LOXONE_CODE_PREFIX: "7",
            CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
            CONF_ACCESS_EARLY_MINUTES: 15,
            CONF_ACCESS_LATE_MINUTES: 30,
            config_flow.CONF_LOXONE_SERVER_COUNT: 1,
            CONF_LOXONE_LISTINGS: ["listing-1"],
        },
    )
    assert server_form["step_id"] == "loxone_server"

    loxone_server_input = {
        CONF_LOXONE_SERVER_NAME: "Haus",
        CONF_LOXONE_SERVER_URL: "https://loxone.example.test/proxy/",
        CONF_LOXONE_SERVER_USERNAME: "service",
        CONF_LOXONE_SERVER_PASSWORD: "secret",
    }
    listing_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        loxone_server_input,
    )
    assert listing_form["step_id"] == "loxone_listing"
    convert(listing_form["data_schema"], custom_serializer=cv.custom_serializer)

    repeated_form = await hass.config_entries.options.async_configure(
        form["flow_id"], loxone_server_input
    )
    assert repeated_form["step_id"] == "loxone_listing"
    assert repeated_form["errors"] == {}

    server_id = loxone_server_id("https://loxone.example.test/proxy", "service")
    invalid_suffix_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_GROUP_UUIDS: [f"{server_id}|group-front"],
            CONF_GUESTY_CODE_SUFFIX: "7#",
        },
    )
    assert invalid_suffix_form["step_id"] == "loxone_listing"
    assert invalid_suffix_form["errors"] == {
        CONF_GUESTY_CODE_SUFFIX: "invalid_code_suffix"
    }

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_GROUP_UUIDS: [
                f"{server_id}|group-front",
                f"{server_id}|group-flat",
            ],
            CONF_GUESTY_CODE_SUFFIX: "#",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOXONE_LISTING_MAPPINGS] == {
        "listing-1": {
            CONF_LOXONE_SERVER_ID: server_id,
            CONF_LOXONE_GROUP_UUIDS: ["group-front", "group-flat"],
        }
    }
    assert result["data"][CONF_LOXONE_CUSTOM_FIELD] == "{{door_code}}"
    assert result["data"][CONF_GUESTY_CODE_SUFFIXES] == {"listing-1": "#"}
    assert (
        result["data"][CONF_LOXONE_MINISERVERS][0][CONF_LOXONE_SERVER_PASSWORD]
        == "secret"
    )


@pytest.mark.asyncio
async def test_options_flow_tests_ttlock_and_maps_compatible_locks(
    hass, monkeypatch
) -> None:
    """TTLock has an independent validated config section and listing mapping."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        ),
        client=SimpleNamespace(
            async_resolve_custom_field=AsyncMock(
                return_value="65fab102a5284d73c6206db0"
            )
        ),
    )
    ttlock_client = SimpleNamespace(
        async_authenticate=AsyncMock(),
        async_refresh_access_token=AsyncMock(),
        async_list_locks=AsyncMock(
            return_value=[
                {
                    "lockId": 101,
                    "lockAlias": "Haustür",
                    "keyboardPwdVersion": 4,
                    "hasGateway": 1,
                },
                {
                    "lockId": 202,
                    "lockAlias": "Ohne Gateway",
                    "keyboardPwdVersion": 4,
                    "hasGateway": 0,
                },
                {
                    "lockId": 303,
                    "lockAlias": "Altes Schloss",
                    "keyboardPwdVersion": 3,
                    "hasGateway": 1,
                },
            ]
        ),
        token_snapshot=lambda: {
            "access_token": "tt-access",
            "refresh_token": "tt-refresh",
            "token_expires_at": "2026-10-20T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        config_flow.TTLockApiClient,
        "from_hass",
        lambda *args, **kwargs: ttlock_client,
    )

    form = await hass.config_entries.options.async_init(entry.entry_id)
    ttlock_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: False,
            CONF_LOXONE_ENABLED: False,
            CONF_TTLOCK_ENABLED: True,
        },
    )
    assert ttlock_form["step_id"] == "ttlock"

    ttlock_account_input = {
        CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
        CONF_LOXONE_CODE_PREFIX: "7",
        CONF_ACCESS_EARLY_MINUTES: 15,
        CONF_ACCESS_LATE_MINUTES: 30,
        CONF_TTLOCK_PROVISION_LEAD_MINUTES: 360,
        CONF_TTLOCK_REGION: "eu",
        CONF_TTLOCK_CLIENT_ID: "tt-client",
        CONF_TTLOCK_CLIENT_SECRET: "tt-secret",
        CONF_TTLOCK_USERNAME: "owner@example.com",
        config_flow.CONF_TTLOCK_PASSWORD: "app-password",
        CONF_TTLOCK_LISTINGS: ["listing-1"],
    }
    listing_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        ttlock_account_input,
    )
    assert listing_form["step_id"] == "ttlock_listing"
    convert(listing_form["data_schema"], custom_serializer=cv.custom_serializer)
    ttlock_client.async_authenticate.assert_awaited_once_with(
        "owner@example.com", "app-password"
    )

    repeated_form = await hass.config_entries.options.async_configure(
        form["flow_id"], ttlock_account_input
    )
    assert repeated_form["step_id"] == "ttlock_listing"
    assert repeated_form["errors"] == {}
    ttlock_client.async_authenticate.assert_awaited_once()

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_TTLOCK_LOCK_IDS: ["101"],
            CONF_GUESTY_CODE_SUFFIX: "☑️",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TTLOCK_LISTING_MAPPINGS] == {
        "listing-1": {CONF_TTLOCK_LOCK_IDS: [101]}
    }
    assert result["data"][CONF_GUESTY_CODE_SUFFIXES] == {"listing-1": "☑️"}
    assert result["data"][CONF_TTLOCK_LOCKS] == [
        {CONF_TTLOCK_LOCK_ID: 101, "name": "Haustür"}
    ]
    assert result["data"][CONF_TTLOCK_ACCOUNT][CONF_TTLOCK_CLIENT_ID] == ("tt-client")


@pytest.mark.asyncio
async def test_shared_suffix_is_configured_once_when_both_providers_use_listing(
    hass, monkeypatch
) -> None:
    """TTLock cannot overwrite the suffix already chosen for the same Loxone listing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        ),
        client=SimpleNamespace(
            async_resolve_custom_field=AsyncMock(return_value="field-id")
        ),
    )
    loxone_client = SimpleNamespace(
        async_get_groups=AsyncMock(
            return_value=[{"uuid": "group-front", "name": "Front door"}]
        )
    )
    ttlock_client = SimpleNamespace(
        async_authenticate=AsyncMock(),
        async_refresh_access_token=AsyncMock(),
        async_list_locks=AsyncMock(
            return_value=[
                {
                    "lockId": 101,
                    "lockAlias": "Apartment",
                    "keyboardPwdVersion": 4,
                    "hasGateway": 1,
                }
            ]
        ),
        token_snapshot=lambda: {
            CONF_TTLOCK_ACCESS_TOKEN: "access",
            CONF_TTLOCK_REFRESH_TOKEN: "refresh",
            CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-20T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        config_flow.LoxoneApiClient,
        "from_hass",
        lambda *args, **kwargs: loxone_client,
    )
    monkeypatch.setattr(
        config_flow.TTLockApiClient,
        "from_hass",
        lambda *args, **kwargs: ttlock_client,
    )

    form = await hass.config_entries.options.async_init(entry.entry_id)
    loxone_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: False,
            CONF_LOXONE_ENABLED: True,
            CONF_TTLOCK_ENABLED: True,
        },
    )
    server_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_PROVISION_LEAD_MINUTES: 360,
            CONF_LOXONE_CODE_PREFIX: "7",
            CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
            CONF_ACCESS_EARLY_MINUTES: 0,
            CONF_ACCESS_LATE_MINUTES: 0,
            config_flow.CONF_LOXONE_SERVER_COUNT: 1,
            CONF_LOXONE_LISTINGS: ["listing-1"],
        },
    )
    assert loxone_form["step_id"] == "loxone"
    assert server_form["step_id"] == "loxone_server"
    listing_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_SERVER_NAME: "Haus",
            CONF_LOXONE_SERVER_URL: "https://loxone.example.test",
            CONF_LOXONE_SERVER_USERNAME: "service",
            CONF_LOXONE_SERVER_PASSWORD: "secret",
        },
    )
    server_id = loxone_server_id("https://loxone.example.test", "service")
    ttlock_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_GROUP_UUIDS: [f"{server_id}|group-front"],
            CONF_GUESTY_CODE_SUFFIX: "☑️ / #",
        },
    )
    assert listing_form["step_id"] == "loxone_listing"
    assert ttlock_form["step_id"] == "ttlock"
    ttlock_listing_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_TTLOCK_PROVISION_LEAD_MINUTES: 360,
            CONF_TTLOCK_REGION: "eu",
            CONF_TTLOCK_CLIENT_ID: "tt-client",
            CONF_TTLOCK_CLIENT_SECRET: "tt-secret",
            CONF_TTLOCK_USERNAME: "owner@example.com",
            config_flow.CONF_TTLOCK_PASSWORD: "password",
            CONF_TTLOCK_LISTINGS: ["listing-1"],
        },
    )
    assert ttlock_listing_form["step_id"] == "ttlock_listing"
    assert CONF_GUESTY_CODE_SUFFIX not in {
        key.schema for key in ttlock_listing_form["data_schema"].schema
    }

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {CONF_TTLOCK_LOCK_IDS: ["101"]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GUESTY_CODE_SUFFIXES] == {"listing-1": "☑️ / #"}


@pytest.mark.asyncio
async def test_ttlock_reconfigure_reuses_latest_private_refresh_token(
    hass, monkeypatch
) -> None:
    """Opening options later does not fall back to an obsolete refresh token."""
    listing = GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )
    stale_account = {
        CONF_TTLOCK_REGION: "eu",
        CONF_TTLOCK_CLIENT_ID: "tt-client",
        CONF_TTLOCK_CLIENT_SECRET: "tt-secret",
        CONF_TTLOCK_USERNAME: "owner@example.com",
        CONF_TTLOCK_ACCESS_TOKEN: "old-access",
        CONF_TTLOCK_REFRESH_TOKEN: "old-refresh",
        CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-01-01T00:00:00+00:00",
    }
    current_account = {
        **stale_account,
        CONF_TTLOCK_ACCESS_TOKEN: "current-access",
        CONF_TTLOCK_REFRESH_TOKEN: "current-refresh",
        CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-01T00:00:00+00:00",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={
            CONF_SCAN_INTERVAL: 300,
            CONF_TTLOCK_ENABLED: True,
            CONF_TTLOCK_ACCOUNT: stale_account,
            CONF_TTLOCK_LISTING_MAPPINGS: {"listing-1": {CONF_TTLOCK_LOCK_IDS: [101]}},
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(listings={listing.id: listing})
        ),
        client=SimpleNamespace(
            async_resolve_custom_field=AsyncMock(
                return_value="65fab102a5284d73c6206db0"
            )
        ),
        ttlock_manager=SimpleNamespace(account_for_reconfigure=lambda: current_account),
    )
    created_with: dict = {}
    ttlock_client = SimpleNamespace(
        async_authenticate=AsyncMock(),
        async_refresh_access_token=AsyncMock(),
        async_list_locks=AsyncMock(
            return_value=[
                {
                    "lockId": 101,
                    "lockAlias": "Front door",
                    "keyboardPwdVersion": 4,
                    "hasGateway": 1,
                }
            ]
        ),
        token_snapshot=lambda: {
            CONF_TTLOCK_ACCESS_TOKEN: "current-access",
            CONF_TTLOCK_REFRESH_TOKEN: "current-refresh",
            CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-01T00:00:00+00:00",
        },
    )

    def _from_hass(*args, **kwargs):
        created_with.update(kwargs)
        return ttlock_client

    monkeypatch.setattr(config_flow.TTLockApiClient, "from_hass", _from_hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    ttlock_form = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: False,
            CONF_LOXONE_ENABLED: False,
            CONF_TTLOCK_ENABLED: True,
        },
    )

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
            CONF_LOXONE_CODE_PREFIX: "7",
            CONF_ACCESS_EARLY_MINUTES: 0,
            CONF_ACCESS_LATE_MINUTES: 0,
            CONF_TTLOCK_PROVISION_LEAD_MINUTES: 360,
            CONF_TTLOCK_REGION: "eu",
            CONF_TTLOCK_CLIENT_ID: "tt-client",
            CONF_TTLOCK_CLIENT_SECRET: "rotated-secret",
            CONF_TTLOCK_USERNAME: "owner@example.com",
            config_flow.CONF_TTLOCK_PASSWORD: "",
            CONF_TTLOCK_LISTINGS: ["listing-1"],
        },
    )

    assert ttlock_form["step_id"] == "ttlock"
    assert result["step_id"] == "ttlock_listing"
    assert created_with["refresh_token"] == "current-refresh"
    assert created_with["client_secret"] == "rotated-secret"
    ttlock_client.async_authenticate.assert_not_awaited()
    ttlock_client.async_refresh_access_token.assert_awaited_once_with()
    await hass.config_entries.options.async_configure(
        form["flow_id"], {CONF_TTLOCK_LOCK_IDS: ["101"]}
    )

    ttlock_client.async_refresh_access_token.reset_mock()
    ttlock_client.async_refresh_access_token.side_effect = config_flow.TTLockAuthError(
        "bad secret"
    )
    rejected_flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        rejected_flow["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            "listing_sync_interval": 86400,
            "reservation_days_past": 30,
            "reservation_days_future": 365,
            "stale_threshold_hours": 6,
            CONF_EXPOSE_GUEST_DETAILS: False,
            CONF_ACCESS_ENABLED: False,
            CONF_LOXONE_ENABLED: False,
            CONF_TTLOCK_ENABLED: True,
        },
    )
    rejected = await hass.config_entries.options.async_configure(
        rejected_flow["flow_id"],
        {
            CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
            CONF_LOXONE_CODE_PREFIX: "7",
            CONF_ACCESS_EARLY_MINUTES: 0,
            CONF_ACCESS_LATE_MINUTES: 0,
            CONF_TTLOCK_PROVISION_LEAD_MINUTES: 360,
            CONF_TTLOCK_REGION: "eu",
            CONF_TTLOCK_CLIENT_ID: "tt-client",
            CONF_TTLOCK_CLIENT_SECRET: "bad-secret",
            CONF_TTLOCK_USERNAME: "owner@example.com",
            config_flow.CONF_TTLOCK_PASSWORD: "",
            CONF_TTLOCK_LISTINGS: ["listing-1"],
        },
    )

    assert rejected["step_id"] == "ttlock"
    assert rejected["errors"] == {"base": "ttlock_invalid_auth"}
