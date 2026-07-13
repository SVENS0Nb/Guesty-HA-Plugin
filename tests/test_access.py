"""Tests for reservation-scoped guest door access."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import access
from custom_components.guesty.access import GuestyAccessManager
from custom_components.guesty.api import GuestyApiError
from custom_components.guesty.const import (
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_LOCK_MAPPINGS,
    DOMAIN,
)
from custom_components.guesty.models import GuestyListing, GuestyReservation
from homeassistant.util import dt as dt_util


def _listing() -> GuestyListing:
    return GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )


def _reservation() -> GuestyReservation:
    now = dt_util.utcnow()
    reservation = GuestyReservation.from_api(
        {
            "_id": "reservation-1",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": (now - timedelta(hours=1)).isoformat(),
            "checkOut": (now + timedelta(hours=1)).isoformat(),
        }
    )
    assert reservation is not None
    return reservation


async def _manager(hass, monkeypatch) -> tuple[GuestyAccessManager, object]:
    reservation = _reservation()
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
            data_stale=False,
        )
    )
    client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value="65fab102a5284d73c6206db0"),
        async_update_reservation_custom_field=AsyncMock(),
        async_delete_reservation_custom_field=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_ACCESS_ENABLED: True,
            CONF_ACCESS_CUSTOM_FIELD: "Door access link",
            CONF_ACCESS_LOCK_MAPPINGS: {
                "listing-1": [
                    {"entity_id": "lock.front_door", "name": "Haustür"},
                    {"entity_id": "lock.apartment", "name": "Wohnungstür"},
                ]
            },
        },
    )
    entry.add_to_hass(hass)
    manager = GuestyAccessManager(hass, entry, client, coordinator)
    manager._data = {"records": {}, "resolved_field": {}}
    manager._secret = b"s" * 32
    manager._storage.async_save = AsyncMock()
    monkeypatch.setattr(access, "get_url", lambda *args, **kwargs: "https://ha.test")
    await manager.async_reconcile()
    return manager, client


@pytest.mark.asyncio
async def test_reconcile_writes_each_unchanged_link_only_once(
    hass, monkeypatch
) -> None:
    """Repeated coordinator notifications do not create Guesty API traffic."""
    manager, client = await _manager(hass, monkeypatch)

    await manager.async_reconcile()

    client.async_update_reservation_custom_field.assert_awaited_once()
    args = client.async_update_reservation_custom_field.await_args.args
    assert args[:2] == ("reservation-1", "65fab102a5284d73c6206db0")
    assert args[2].startswith(
        f"https://ha.test/api/guesty/access/{manager.entry.entry_id}/"
    )


@pytest.mark.asyncio
async def test_unverified_v130_record_is_republished(hass, monkeypatch) -> None:
    """Records created before response verification receive one safe retry."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record.pop("write_verified")
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    client.async_update_reservation_custom_field.assert_awaited_once()
    assert record["write_verified"] is True
    assert manager.diagnostics()["verified_records"] == 1


@pytest.mark.asyncio
async def test_update_during_reconcile_is_not_lost(hass, monkeypatch) -> None:
    """A coordinator update arriving during a write triggers a second pass."""
    manager, _client = await _manager(hass, monkeypatch)
    manager._reconcile_task = None
    reconcile = AsyncMock()

    async def _reconcile() -> None:
        await reconcile()
        if reconcile.await_count == 1:
            manager.async_schedule_reconcile()

    monkeypatch.setattr(manager, "async_reconcile", _reconcile)
    monkeypatch.setattr(access.asyncio, "sleep", AsyncMock())

    manager.async_schedule_reconcile()
    task = manager._reconcile_task
    assert task is not None
    await task

    assert reconcile.await_count == 2


@pytest.mark.asyncio
async def test_get_never_unlocks_and_valid_post_uses_server_mapping(
    hass, monkeypatch
) -> None:
    """GET is inert; POST cannot supply an arbitrary entity id."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    service_call = AsyncMock()

    async def _async_call(_registry, *args, **kwargs) -> None:
        await service_call(*args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", _async_call)
    hass.states.async_set("lock.front_door", "locked")

    page = await manager.async_get_portal(token)

    assert page.status == 200
    assert "Haustür öffnen" in page.text
    service_call.assert_not_awaited()

    invalid = await manager.async_unlock(token, "0", "invalid")
    assert invalid.status == 403
    service_call.assert_not_awaited()

    nonce = manager._action_nonce(token, 0)
    result = await manager.async_unlock(token, "0", nonce)

    assert result.status == 200
    service_call.assert_awaited_once_with(
        "lock",
        "unlock",
        target={"entity_id": "lock.front_door"},
        blocking=True,
    )


@pytest.mark.asyncio
async def test_stale_or_changed_reservation_fails_closed(hass, monkeypatch) -> None:
    """Stale data and changed dates invalidate an old link before reconciliation."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])

    manager._coordinator.data.data_stale = True
    assert (await manager.async_get_portal(token)).status == 404

    manager._coordinator.data.data_stale = False
    reservation = manager._coordinator.data.reservations[0]
    reservation.status = "checked_in"
    assert (await manager.async_get_portal(token)).status == 200

    reservation.check_out_utc = (dt_util.utcnow() + timedelta(hours=2)).isoformat()
    assert (await manager.async_get_portal(token)).status == 404


@pytest.mark.asyncio
async def test_cancellation_revokes_before_remote_field_cleanup(
    hass, monkeypatch
) -> None:
    """A failed Guesty cleanup cannot keep local physical access active."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    manager._coordinator.data.reservations[0].status = "cancelled"
    client.async_delete_reservation_custom_field.side_effect = GuestyApiError("offline")

    await manager.async_reconcile()

    assert (await manager.async_get_portal(token)).status == 404


@pytest.mark.asyncio
async def test_access_links_are_never_published_over_http(hass, monkeypatch) -> None:
    """A reverse proxy must provide an external HTTPS URL for bearer links."""
    manager, client = await _manager(hass, monkeypatch)
    client.async_update_reservation_custom_field.reset_mock()
    manager._records["reservation-1"]["url_hash"] = None
    manager._records["reservation-1"]["field_synced"] = False
    monkeypatch.setattr(access, "get_url", lambda *args, **kwargs: "http://ha.test")

    await manager.async_reconcile()

    client.async_update_reservation_custom_field.assert_not_awaited()
