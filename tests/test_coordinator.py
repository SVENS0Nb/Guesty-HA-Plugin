"""Tests for Guesty coordinator decisions and webhook deduplication."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.exceptions import ConfigEntryAuthFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty.api import GuestyAuthError
from custom_components.guesty.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.guesty.coordinator import (
    GuestyDataUpdateCoordinator,
    _is_full_reservation_sync_due,
)
from custom_components.guesty.models import GuestyListing, GuestyReservation

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={},
    )


def _empty_cache() -> dict:
    return {
        "listings": {},
        "reservations": [],
        "access_token": None,
        "token_expires_at": None,
        "last_sync": None,
        "last_listing_sync": None,
        "last_reservation_sync": None,
        "last_full_reservation_sync": None,
        "last_incremental_sync": None,
        "last_error": None,
    }


def _coordinator(hass, client=None, storage=None) -> GuestyDataUpdateCoordinator:
    entry = _entry()
    entry.add_to_hass(hass)
    return GuestyDataUpdateCoordinator(
        hass,
        entry,
        client or SimpleNamespace(),
        storage or SimpleNamespace(),
    )


async def _wait_webhook_worker(instance: GuestyDataUpdateCoordinator) -> None:
    """Wait for the single owned webhook worker to drain its queue."""
    task = instance._webhook_batch_task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_coordinator_uses_stdlib_timedelta(hass) -> None:
    """Coordinator setup uses a real timedelta accepted by Home Assistant."""
    instance = _coordinator(hass)
    assert instance.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@pytest.mark.parametrize(
    ("last_full_sync", "expected"),
    [
        (None, True),
        ("invalid", True),
        ((NOW - timedelta(hours=23)).isoformat(), False),
        ((NOW - timedelta(hours=24)).isoformat(), True),
    ],
)
def test_full_sync_uses_dedicated_timestamp(
    monkeypatch, last_full_sync, expected
) -> None:
    """Daily full-sync decisions do not depend on incremental cursors."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    assert _is_full_reservation_sync_due(last_full_sync) is expected


@pytest.mark.asyncio
async def test_auth_failure_starts_reauthentication(hass) -> None:
    """Rejected credentials are surfaced as ConfigEntryAuthFailed."""
    client = SimpleNamespace(
        async_get_listings=AsyncMock(side_effect=GuestyAuthError("invalid")),
        async_get_reservations=AsyncMock(return_value=[]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    with pytest.raises(ConfigEntryAuthFailed):
        await instance._async_fetch_data(full_reservation_sync=True)


@pytest.mark.asyncio
async def test_sparse_listing_webhook_uses_listing_only_api_fallback(
    hass, monkeypatch
) -> None:
    """An incomplete listing event never triggers a full reservation scan."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_listings=AsyncMock(return_value=[]),
        async_get_reservations=AsyncMock(),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance.async_handle_webhook({"event": "listing.updated"})
    await _wait_webhook_worker(instance)

    client.async_get_listings.assert_awaited_once_with()
    client.async_get_reservations.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_reservation_webhooks_are_coalesced(hass, monkeypatch) -> None:
    """Guesty's duplicate notifications cannot trigger duplicate API calls."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    instance = _coordinator(hass)
    instance._async_apply_reservation_webhook = AsyncMock()
    payload = {
        "event": "reservation.updated",
        "reservation": {"_id": "65f19af19824d7e6ff848f11"},
    }
    await asyncio.gather(
        instance.async_handle_webhook(payload),
        instance.async_handle_webhook(payload),
    )
    await _wait_webhook_worker(instance)

    instance._async_apply_reservation_webhook.assert_awaited_once_with(
        "65f19af19824d7e6ff848f11"
    )


@pytest.mark.asyncio
async def test_webhook_arriving_mid_fetch_is_replayed(hass, monkeypatch) -> None:
    """A later change to the same reservation cannot be lost during an API call."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    instance = _coordinator(hass)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def apply_reservation(reservation_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()

    instance._async_apply_reservation_webhook = apply_reservation
    payload = {
        "event": "reservation.updated",
        "reservation": {"_id": "65f19af19824d7e6ff848f11"},
    }
    first = asyncio.create_task(instance.async_handle_webhook(payload))
    await started.wait()
    second = asyncio.create_task(instance.async_handle_webhook(payload))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    await _wait_webhook_worker(instance)

    assert calls == 2


@pytest.mark.asyncio
async def test_reservation_burst_uses_one_incremental_refresh(
    hass, monkeypatch
) -> None:
    """Bulk edits use one filtered sync instead of one API call per reservation."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    instance = _coordinator(hass)
    instance.async_refresh = AsyncMock()
    instance._async_apply_reservation_webhook = AsyncMock()

    await asyncio.gather(
        instance.async_handle_webhook(
            {"event": "reservation.updated", "reservation": {"_id": "res-1"}}
        ),
        instance.async_handle_webhook(
            {"event": "reservation.updated", "reservation": {"_id": "res-2"}}
        ),
    )
    await _wait_webhook_worker(instance)

    instance.async_refresh.assert_awaited_once_with()
    instance._async_apply_reservation_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_or_unsafe_webhook_is_ignored(hass) -> None:
    """Untrusted webhook payloads cannot trigger arbitrary API refreshes."""
    instance = _coordinator(hass)
    instance.async_request_refresh = AsyncMock()
    instance._async_apply_reservation_webhook = AsyncMock()

    await instance.async_handle_webhook({"event": "unknown.event"})
    await instance.async_handle_webhook(
        {
            "event": "reservation.updated",
            "reservation": {"_id": "../unsafe"},
        }
    )

    instance.async_request_refresh.assert_not_awaited()
    instance._async_apply_reservation_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_the_owned_webhook_worker(hass, monkeypatch) -> None:
    """Reloading the config entry cannot leave a debounce or API task behind."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 3600
    )
    instance = _coordinator(hass)

    await instance.async_handle_webhook(
        {"event": "reservation.updated.v2", "data": {"reservationId": "res-1"}}
    )
    task = instance._webhook_batch_task
    assert task is not None and not task.done()

    await instance.async_shutdown()

    assert task.cancelled()
    assert instance._webhook_batch_task is None
    assert not instance._pending_reservation_ids


def test_v2_webhook_id_is_extracted() -> None:
    """The newer nested Guesty payload shape is supported."""
    assert (
        GuestyDataUpdateCoordinator._reservation_id_from_webhook(
            {
                "event": "reservation.updated.v2",
                "data": {"reservation": {"id": "65f19af19824d7e6ff848f11"}},
            }
        )
        == "65f19af19824d7e6ff848f11"
    )


def _listing(listing_id: str = "listing-1") -> GuestyListing:
    return GuestyListing(
        id=listing_id,
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )


def _reservation(
    reservation_id: str = "reservation-1",
    *,
    key_code: str | None = None,
) -> GuestyReservation:
    return GuestyReservation(
        id=reservation_id,
        listing_id="listing-1",
        status="confirmed",
        confirmation_code=None,
        check_in_date="2026-07-14",
        check_out_date="2026-07-16",
        check_in_utc=None,
        check_out_utc=None,
        planned_arrival=None,
        planned_departure=None,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name=None,
        last_updated_at=None,
        key_code=key_code,
        key_code_observed=key_code is not None,
    )


@pytest.mark.asyncio
async def test_new_listing_webhook_uses_payload_and_targeted_reservations(hass) -> None:
    """A new listing appears immediately without a complete account scan."""
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_listings=AsyncMock(),
        async_get_reservations=AsyncMock(return_value=[]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_apply_listing_webhooks(
        [
            {
                "event": "listing.new",
                "listing": {
                    "_id": "listing-1",
                    "title": "New Apartment",
                    "timezone": "Europe/Berlin",
                },
            }
        ]
    )

    assert set(instance.data.listings) == {"listing-1"}
    assert instance.data.listings["listing-1"].title == "New Apartment"
    client.async_get_listings.assert_not_awaited()
    client.async_get_reservations.assert_awaited_once_with(
        30,
        365,
        listing_ids={"listing-1"},
    )


@pytest.mark.asyncio
async def test_removed_listing_webhook_prunes_listing_and_reservations(hass) -> None:
    """Removed listings become unavailable immediately with no API request."""
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    cache["reservations"] = [_reservation().to_dict()]
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_listings=AsyncMock(),
        async_get_reservations=AsyncMock(),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_apply_listing_webhooks(
        [{"event": "listing.removed", "listing": {"_id": "listing-1"}}]
    )

    assert instance.data.listings == {}
    assert instance.data.reservations == []
    client.async_get_listings.assert_not_awaited()
    client.async_get_reservations.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_reservation_does_not_advance_global_cursor(hass) -> None:
    """A single webhook fetch cannot hide other changes from the next poll."""
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    cache["last_incremental_sync"] = "2026-07-13T11:55:00+00:00"
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(return_value=_reservation())
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_apply_reservation_webhook("reservation-1")

    saved_cache = storage.async_save.await_args.args[0]
    assert saved_cache["last_incremental_sync"] == "2026-07-13T11:55:00+00:00"


@pytest.mark.asyncio
async def test_targeted_webhook_exposes_keycode_only_in_memory(hass) -> None:
    """A manual Guesty PIN edit reaches listeners without entering disk cache."""
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    reservation = _reservation(key_code="799999")
    client = SimpleNamespace(async_get_reservation=AsyncMock(return_value=reservation))
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_apply_reservation_webhook("reservation-1")

    assert instance.data.reservations[0].key_code == "799999"
    assert instance.data.reservations[0].key_code_observed is True
    saved_cache = storage.async_save.await_args.args[0]
    assert "key_code" not in saved_cache["reservations"][0]


@pytest.mark.asyncio
async def test_missing_webhook_uses_faster_listing_poll_fallback(
    hass, monkeypatch
) -> None:
    """A lost webhook subscription cannot hide listing changes for a day."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_sync": (NOW - timedelta(minutes=5)).isoformat(),
            "last_listing_sync": (NOW - timedelta(minutes=16)).isoformat(),
            "last_reservation_sync": (NOW - timedelta(minutes=5)).isoformat(),
            "last_full_reservation_sync": (NOW - timedelta(hours=1)).isoformat(),
            "last_incremental_sync": (NOW - timedelta(minutes=5)).isoformat(),
        }
    )
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_listings=AsyncMock(return_value=[_listing()]),
        async_get_reservations=AsyncMock(return_value=[]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_fetch_data(full_reservation_sync=False)

    client.async_get_listings.assert_awaited_once_with()
