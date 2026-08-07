"""Tests for reservation-scoped guest door access."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import access
from custom_components.guesty.access import GuestyAccessManager, _preferred_language
from custom_components.guesty.api import (
    GuestyApiError,
    GuestyNotFoundError,
    GuestyRetryableError,
)
from custom_components.guesty.const import (
    CONF_ACCESS_CUSTOM_FIELD,
    CONF_ACCESS_ENABLED,
    CONF_ACCESS_FAVICON_URL,
    CONF_ACCESS_LOGO_URL,
    CONF_ACCESS_LOCK_MAPPINGS,
    CONF_SCAN_INTERVAL,
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
            cache_age_minutes=0,
        )
    )
    client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value="65fab102a5284d73c6206db0"),
        async_update_reservation_custom_field=AsyncMock(),
        async_get_reservation_custom_field=AsyncMock(),
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
    client.async_resolve_custom_field.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_remote_drift_is_repaired_without_token_rotation(
    hass, monkeypatch
) -> None:
    """A stale Guesty field is replaced with the still-current local URL."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    expected_url = client.async_update_reservation_custom_field.await_args.args[2]
    record.pop("remote_verified_at")
    client.async_get_reservation_custom_field.return_value = (
        "https://ha.test/api/guesty/access/old-entry/old-token"
    )
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    client.async_get_reservation_custom_field.assert_awaited_once_with(
        "reservation-1", "65fab102a5284d73c6206db0"
    )
    client.async_update_reservation_custom_field.assert_awaited_once_with(
        "reservation-1",
        "65fab102a5284d73c6206db0",
        expected_url,
    )
    assert record["version"] == old_version
    assert manager._token_for("reservation-1", record["version"]) == old_token
    assert record["field_synced"] is True
    assert record["write_verified"] is True
    assert manager.diagnostics()["remote_drift_during_last_reconcile"] == 1


@pytest.mark.asyncio
async def test_matching_remote_link_is_audited_without_an_extra_write(
    hass, monkeypatch
) -> None:
    """A current Guesty value refreshes proof without needless write traffic."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    expected_url = client.async_update_reservation_custom_field.await_args.args[2]
    record.pop("remote_verified_at")
    client.async_get_reservation_custom_field.return_value = expected_url
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    client.async_get_reservation_custom_field.assert_awaited_once()
    client.async_update_reservation_custom_field.assert_not_awaited()
    diagnostics = manager.diagnostics()
    assert diagnostics["remote_verified_during_last_reconcile"] == 1
    assert diagnostics["remote_drift_during_last_reconcile"] == 0
    assert diagnostics["remotely_checked_records"] == 1


@pytest.mark.asyncio
async def test_remote_link_read_failure_keeps_last_confirmed_url(
    hass, monkeypatch
) -> None:
    """A verification outage cannot revoke or rewrite a valid local link."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record.pop("remote_verified_at")
    client.async_get_reservation_custom_field.side_effect = GuestyRetryableError(
        "offline"
    )
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["field_synced"] is True
    assert record["write_verified"] is True
    assert record["verify_retry_count"] == 1
    assert manager.diagnostics()["last_remote_verification_error"] == (
        "GuestyRetryableError"
    )


@pytest.mark.asyncio
async def test_unknown_token_schedules_one_bounded_current_link_audit(
    hass, monkeypatch
) -> None:
    """A public stale token triggers repair without an unbounded GET loop."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    expected_url = client.async_update_reservation_custom_field.await_args.args[2]
    client.async_get_reservation_custom_field.return_value = "stale"
    client.async_update_reservation_custom_field.reset_mock()
    schedule = MagicMock()
    monkeypatch.setattr(manager, "async_schedule_reconcile", schedule)

    assert manager._validate_token("x" * 40) is None
    assert manager._validate_token("y" * 40) is None

    schedule.assert_called_once_with()
    assert manager._force_remote_verification is True
    await manager.async_reconcile()

    client.async_get_reservation_custom_field.assert_awaited_once()
    client.async_update_reservation_custom_field.assert_awaited_once_with(
        "reservation-1",
        "65fab102a5284d73c6206db0",
        expected_url,
    )
    assert record["version"] == old_version
    assert manager._force_remote_verification is False


@pytest.mark.asyncio
async def test_remote_link_audit_is_bounded_and_prioritized(hass, monkeypatch) -> None:
    """One pass checks no more than two current links before future work."""
    manager, client = await _manager(hass, monkeypatch)
    for number in range(2, 4):
        reservation = _reservation()
        reservation.id = f"reservation-{number}"
        manager._coordinator.data.reservations.append(reservation)
    await manager.async_reconcile()
    assert len(manager._records) == 3
    for record in manager._records.values():
        record.pop("remote_verified_at")

    async def _remote_value(reservation_id, _field_id):
        return manager._access_url_for_record(
            reservation_id,
            manager._records[reservation_id],
        )

    client.async_get_reservation_custom_field.reset_mock()
    client.async_get_reservation_custom_field.side_effect = _remote_value
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    assert client.async_get_reservation_custom_field.await_count == 2
    checked = {
        call.args[0]
        for call in client.async_get_reservation_custom_field.await_args_list
    }
    assert checked == {"reservation-1", "reservation-2"}
    client.async_update_reservation_custom_field.assert_not_awaited()
    assert manager.diagnostics()["remote_verified_during_last_reconcile"] == 2
    assert manager.diagnostics()["deferred_during_last_reconcile"] == 1

    old_proof = (dt_util.utcnow() - timedelta(minutes=6)).isoformat()
    for reservation_id in checked:
        manager._records[reservation_id]["remote_verified_at"] = old_proof
    client.async_get_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    next_checked = {
        call.args[0]
        for call in client.async_get_reservation_custom_field.await_args_list
    }
    assert "reservation-3" in next_checked


@pytest.mark.asyncio
async def test_remote_audit_rechecks_headroom_before_repair_write(
    hass, monkeypatch
) -> None:
    """The verification GET cannot spend the reserve needed by normal sync."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record.pop("remote_verified_at")
    client.last_rate_limit_remaining = 5

    async def _low_headroom(_reservation_id, _field_id):
        client.last_rate_limit_remaining = 4
        return "stale"

    client.async_get_reservation_custom_field.side_effect = _low_headroom
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    client.async_get_reservation_custom_field.assert_awaited_once()
    client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["field_synced"] is False
    assert record["write_verified"] is False
    assert manager.diagnostics()["deferred_during_last_reconcile"] == 1


@pytest.mark.asyncio
async def test_guest_name_change_does_not_rotate_access_link(hass, monkeypatch) -> None:
    """Presentation-only guest updates keep an already issued bearer URL valid."""
    manager, client = await _manager(hass, monkeypatch)
    reservation = manager._coordinator.data.reservations[0]
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    client.async_update_reservation_custom_field.reset_mock()

    reservation.guest_name = "Corrected display name"
    await manager.async_reconcile()

    assert record["version"] == old_version
    assert manager._token_for("reservation-1", record["version"]) == old_token
    assert (await manager.async_get_portal(old_token)).status == 200
    client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_door_label_change_does_not_rotate_access_link(hass, monkeypatch) -> None:
    """Changing a visible door name updates the page without replacing its URL."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    client.async_update_reservation_custom_field.reset_mock()
    mappings = {
        "listing-1": [
            {"entity_id": "lock.front_door", "name": "Main entrance"},
            {"entity_id": "lock.apartment", "name": "Apartment entrance"},
        ]
    }
    hass.config_entries.async_update_entry(
        manager.entry,
        options={
            **manager.entry.options,
            CONF_ACCESS_LOCK_MAPPINGS: mappings,
        },
    )

    await manager.async_reconcile()

    assert record["version"] == old_version
    assert manager._token_for("reservation-1", record["version"]) == old_token
    page = await manager.async_get_portal(old_token, "en")
    assert page.status == 200
    assert "Open Main entrance" in page.text
    client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_fingerprint_migrates_without_rotating_access_link(
    hass, monkeypatch
) -> None:
    """An unchanged pre-versioned record adopts the new shape in place."""
    manager, client = await _manager(hass, monkeypatch)
    reservation = manager._coordinator.data.reservations[0]
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    record["fingerprint"] = manager._legacy_reservation_fingerprint(reservation)
    record.pop("fingerprint_version")
    client.async_update_reservation_custom_field.reset_mock()

    # Validation remains available during the short setup-to-reconcile window.
    assert (await manager.async_get_portal(old_token)).status == 200
    await manager.async_reconcile()

    assert record["version"] == old_version
    assert record["fingerprint_version"] == access._ACCESS_FINGERPRINT_VERSION
    assert record["fingerprint"] == manager._reservation_fingerprint(reservation)
    assert (await manager.async_get_portal(old_token)).status == 200
    client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_legacy_authorization_state_rotates_access_link(
    hass, monkeypatch
) -> None:
    """Legacy migration cannot preserve a token after a permission input changed."""
    manager, client = await _manager(hass, monkeypatch)
    reservation = manager._coordinator.data.reservations[0]
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    record["fingerprint"] = manager._legacy_reservation_fingerprint(reservation)
    record.pop("fingerprint_version")
    reservation.check_out_utc = (dt_util.utcnow() + timedelta(hours=2)).isoformat()
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    assert record["version"] == old_version + 1
    assert manager._validate_token(old_token) is None
    client.async_update_reservation_custom_field.assert_awaited_once()


@pytest.mark.asyncio
async def test_booking_time_change_still_rotates_access_link(hass, monkeypatch) -> None:
    """Authorization-relevant timing updates still invalidate the old URL."""
    manager, client = await _manager(hass, monkeypatch)
    reservation = manager._coordinator.data.reservations[0]
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    reservation.check_out_utc = (dt_util.utcnow() + timedelta(hours=2)).isoformat()
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    assert record["version"] == old_version + 1
    assert manager._validate_token(old_token) is None
    client.async_update_reservation_custom_field.assert_awaited_once()


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
async def test_stale_persisted_field_id_is_replaced_after_reload(
    hass, monkeypatch
) -> None:
    """A deleted and recreated same-name field is re-resolved automatically."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_token = manager._token_for("reservation-1", old_version)
    old_field_id = record["field_id"]
    new_field_id = "75fab102a5284d73c6206db1"

    # Simulate the next integration reload with a persisted old ID. Runtime
    # validation must bypass that cache and find the recreated Guesty field.
    manager._validated_field_references.clear()
    client.async_resolve_custom_field.return_value = new_field_id
    client.async_update_reservation_custom_field.reset_mock()
    client.async_delete_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    assert record["field_id"] == new_field_id
    assert record["version"] == old_version + 1
    assert manager._validate_token(old_token) is None
    client.async_delete_reservation_custom_field.assert_awaited_once_with(
        "reservation-1", old_field_id
    )
    write_args = client.async_update_reservation_custom_field.await_args.args
    assert write_args[:2] == ("reservation-1", new_field_id)
    assert old_token not in write_args[2]
    assert manager.diagnostics()["recovered_during_last_reconcile"] == 1


@pytest.mark.asyncio
async def test_failed_write_rotates_link_and_retries_once(hass, monkeypatch) -> None:
    """A field write failure refreshes the ID and retries with a new bearer URL."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    old_url = client.async_update_reservation_custom_field.await_args.args[2]
    new_field_id = "75fab102a5284d73c6206db1"
    record["field_synced"] = False
    record["write_verified"] = False
    record["url_hash"] = None
    client.async_update_reservation_custom_field.reset_mock()
    client.async_update_reservation_custom_field.side_effect = [
        GuestyNotFoundError("stale field"),
        None,
    ]
    client.async_resolve_custom_field.return_value = new_field_id

    await manager.async_reconcile()

    assert client.async_update_reservation_custom_field.await_count == 2
    first_write, retry = (
        call.args
        for call in client.async_update_reservation_custom_field.await_args_list
    )
    assert first_write[1] != retry[1]
    assert first_write[2] == old_url
    assert retry[2] != old_url
    assert record["version"] == old_version + 1
    assert record["field_id"] == new_field_id
    assert record["write_verified"] is True
    assert "recovery_marker" not in record
    assert manager.diagnostics()["recovered_during_last_reconcile"] == 1


@pytest.mark.asyncio
async def test_temporary_write_failure_keeps_existing_bearer_link(
    hass, monkeypatch
) -> None:
    """Rate limits and outages never masquerade as a stale field definition."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    resolve_count = client.async_resolve_custom_field.await_count
    record.update({"field_synced": False, "write_verified": False, "url_hash": None})
    client.async_update_reservation_custom_field.reset_mock()
    client.async_update_reservation_custom_field.side_effect = GuestyRetryableError(
        "rate limited"
    )

    await manager.async_reconcile()

    assert record["version"] == old_version
    assert client.async_resolve_custom_field.await_count == resolve_count
    client.async_update_reservation_custom_field.assert_awaited_once()
    assert record["publish_retry_count"] == 1


@pytest.mark.asyncio
async def test_failed_field_refresh_is_contained_and_backed_off(
    hass, monkeypatch
) -> None:
    """A failed recovery lookup cannot abort cleanup and record persistence."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_version = record["version"]
    record.update({"field_synced": False, "write_verified": False, "url_hash": None})
    client.async_update_reservation_custom_field.side_effect = GuestyNotFoundError(
        "field missing"
    )
    client.async_resolve_custom_field.side_effect = GuestyRetryableError("offline")

    await manager.async_reconcile()

    assert record["version"] == old_version
    assert record["publish_retry_count"] == 1
    assert manager.diagnostics()["last_reconcile_result"] == "partial"


@pytest.mark.asyncio
async def test_bulk_link_publication_has_a_bounded_write_budget(
    hass, monkeypatch
) -> None:
    """A large future booking set cannot consume the whole Guesty allowance."""
    manager, client = await _manager(hass, monkeypatch)
    for number in range(2, 5):
        reservation = _reservation()
        reservation.id = f"reservation-{number}"
        manager._coordinator.data.reservations.append(reservation)
    client.async_update_reservation_custom_field.reset_mock()

    await manager.async_reconcile()

    assert client.async_update_reservation_custom_field.await_count == 2
    assert manager.diagnostics()["deferred_during_last_reconcile"] == 1
    assert manager._cancel_timer is not None


@pytest.mark.asyncio
async def test_link_publication_preserves_guesty_headroom_without_fast_loop(
    hass, monkeypatch
) -> None:
    """Exhausted long-window capacity queues links until the normal poll."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record.update({"field_synced": False, "write_verified": False, "url_hash": None})
    client.async_update_reservation_custom_field.reset_mock()
    client.last_rate_limit_remaining = 4
    hass.config_entries.async_update_entry(
        manager.entry,
        options={
            **manager.entry.options,
            CONF_SCAN_INTERVAL: 120,
        },
    )
    scheduled_at = MagicMock()
    manager._schedule_at = scheduled_at
    now = dt_util.utcnow()
    monkeypatch.setattr(access.dt_util, "utcnow", lambda: now)

    await manager.async_reconcile()

    client.async_update_reservation_custom_field.assert_not_awaited()
    assert scheduled_at.call_args_list[-1].args == (now + timedelta(seconds=120),)
    assert manager.diagnostics()["deferred_during_last_reconcile"] == 1


@pytest.mark.asyncio
async def test_failed_recovery_does_not_rotate_on_every_poll(hass, monkeypatch) -> None:
    """Repeated upstream failure retries the same link instead of token churn."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record["field_synced"] = False
    record["write_verified"] = False
    record["url_hash"] = None
    client.async_update_reservation_custom_field.side_effect = GuestyApiError("offline")

    await manager.async_reconcile()
    recovered_version = record["version"]
    resolve_count = client.async_resolve_custom_field.await_count

    await manager.async_reconcile()

    assert record["version"] == recovered_version
    assert client.async_resolve_custom_field.await_count == resolve_count
    assert manager.diagnostics()["recovered_during_last_reconcile"] == 0


@pytest.mark.asyncio
async def test_failed_publish_uses_persistent_exponential_backoff(
    hass, monkeypatch
) -> None:
    """Repeated coordinator refreshes do not hammer a failing Guesty write."""
    manager, client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    record["field_synced"] = False
    record["write_verified"] = False
    record["url_hash"] = None
    client.async_update_reservation_custom_field.reset_mock()
    client.async_update_reservation_custom_field.side_effect = GuestyApiError("offline")

    await manager.async_reconcile()
    first_attempts = client.async_update_reservation_custom_field.await_count

    assert first_attempts >= 1
    assert record["publish_retry_count"] == 1

    await manager.async_reconcile()

    assert client.async_update_reservation_custom_field.await_count == first_attempts
    assert manager.diagnostics()["deferred_during_last_reconcile"] == 1

    record["publish_retry_at"] = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
    await manager.async_reconcile()

    assert client.async_update_reservation_custom_field.await_count > first_attempts
    assert record["publish_retry_count"] == 2


@pytest.mark.asyncio
async def test_update_during_reconcile_is_not_lost(hass, monkeypatch) -> None:
    """A coordinator update arriving during a write triggers a second pass."""
    manager, _client = await _manager(hass, monkeypatch)
    manager._reconcile_task = None
    reconcile = AsyncMock()
    listener = MagicMock()
    manager.async_add_listener(listener)

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
    assert listener.call_count == 2


@pytest.mark.asyncio
async def test_listing_snapshot_recreates_current_verified_link(
    hass, monkeypatch
) -> None:
    """A listing diagnostic can display its link without storing the bearer URL."""
    manager, _client = await _manager(hass, monkeypatch)

    snapshot = manager.listing_access_snapshot("listing-1")

    assert snapshot["status"] == "synced"
    assert snapshot["access_active"] is True
    assert snapshot["field_synced"] is True
    assert snapshot["write_verified"] is True
    assert snapshot["reservation"].id == "reservation-1"
    assert snapshot["access_url"].startswith(
        f"https://ha.test/api/guesty/access/{manager.entry.entry_id}/"
    )
    assert "access_url" not in manager._records["reservation-1"]


@pytest.mark.asyncio
async def test_listing_snapshot_reports_unconfigured_mapping(hass, monkeypatch) -> None:
    """Listings without an enabled lock mapping do not expose access links."""
    manager, _client = await _manager(hass, monkeypatch)

    assert manager.listing_access_snapshot("listing-2") == {"status": "not_configured"}
    hass.config_entries.async_update_entry(
        manager.entry,
        options={**manager.entry.options, CONF_ACCESS_ENABLED: False},
    )
    assert manager.listing_access_snapshot("listing-1") == {"status": "not_configured"}


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
    assert "Haustür wurde geöffnet" in result.text
    assert "Haustür öffnen" in result.text
    assert "Bitte kontaktiere deinen Gastgeber." not in result.text
    service_call.assert_awaited_once_with(
        "lock",
        "unlock",
        target={"entity_id": "lock.front_door"},
        blocking=True,
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("es-MX,es;q=0.9,en;q=0.7", "es"),
        ("fr-CA;q=0.8,en;q=0.9", "en"),
        ("it-IT,it;q=0.9", "en"),
        (None, "en"),
    ],
)
def test_portal_language_uses_browser_preference(
    header: str | None, expected: str
) -> None:
    """The portal follows supported browser languages and falls back to English."""
    assert _preferred_language(header) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "title", "detail"),
    [
        (
            "de",
            "Zugang nicht verfügbar",
            "Diese Seite ist nur im Buchungszeitraum verfügbar.",
        ),
        (
            "en",
            "Access Unavailable",
            "This page is only available during the booking period.",
        ),
        (
            "es",
            "Acceso no disponible",
            "Esta página solo está disponible durante el período de la reserva.",
        ),
        (
            "fr",
            "Accès indisponible",
            "Cette page est disponible uniquement pendant la période de réservation.",
        ),
    ],
)
async def test_unavailable_page_explains_booking_period(
    hass, monkeypatch, language: str, title: str, detail: str
) -> None:
    """Unavailable links show a localized, privacy-safe timing explanation."""
    manager, _client = await _manager(hass, monkeypatch)

    page = await manager.async_get_portal("x" * 40, language)

    assert page.status == 404
    assert f'<html lang="{language}">' in page.text
    assert f"<h1>{title}</h1>" in page.text
    assert f"<p>{detail}</p>" in page.text
    assert "reservation-1" not in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "title", "button"),
    [
        ("de", "Türzugang", "Haustür öffnen"),
        ("en", "Door Access", "Open Front door"),
        ("es", "Acceso a la puerta", "Abrir Puerta principal"),
        ("fr", "Accès à la porte", "Ouvrir Porte d’entrée"),
    ],
)
async def test_portal_localizes_reusable_ajax_controls(
    hass, monkeypatch, language: str, title: str, button: str
) -> None:
    """All supported languages retain controls while messages auto-hide."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])

    page = await manager.async_get_portal(token, language)

    assert page.status == 200
    assert f'<html lang="{language}">' in page.text
    assert title in page.text
    assert button in page.text
    assert page.text.count('<form method="post">') == 2
    assert "event.preventDefault()" in page.text
    assert "fetch(window.location.href" in page.text
    assert "setTimeout(hideNotice, 5000)" in page.text
    assert "setInterval" not in page.text
    assert "Bitte kontaktiere deinen Gastgeber." not in page.text
    content_security_policy = page.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in content_security_policy
    assert "script-src 'nonce-" in content_security_policy
    assert "script-src 'unsafe-inline'" not in content_security_policy


@pytest.mark.asyncio
async def test_custom_translated_labels_do_not_rotate_access_link(
    hass, monkeypatch
) -> None:
    """Changing presentation labels keeps permissions and the bearer URL stable."""
    manager, _client = await _manager(hass, monkeypatch)
    reservation = manager._coordinator.data.reservations[0]
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    fingerprint = manager._reservation_fingerprint(reservation)
    mappings = {
        "listing-1": [
            {
                "entity_id": "lock.front_door",
                "name": "Haustür",
                "name_de": "Haustür",
                "name_en": "Main entrance",
                "name_es": "Entrada de huéspedes",
                "name_fr": "Entrée des invités",
            },
            {
                "entity_id": "lock.apartment",
                "name": "Wohnungstür",
            },
        ]
    }
    hass.config_entries.async_update_entry(
        manager.entry,
        options={
            **manager.entry.options,
            CONF_ACCESS_LOCK_MAPPINGS: mappings,
        },
    )

    assert manager._reservation_fingerprint(reservation) == fingerprint
    page = await manager.async_get_portal(token, "en")

    assert page.status == 200
    assert "Open Main entrance" in page.text
    assert "Haustür" not in page.text


@pytest.mark.asyncio
async def test_portal_renders_at_most_six_configured_locks(hass, monkeypatch) -> None:
    """Six selected locks render, while excess raw configuration is ignored."""
    manager, _client = await _manager(hass, monkeypatch)
    raw_doors = [
        {"entity_id": f"lock.door_{index}", "name": f"Door {index}"}
        for index in range(1, 8)
    ]
    hass.config_entries.async_update_entry(
        manager.entry,
        options={
            **manager.entry.options,
            CONF_ACCESS_LOCK_MAPPINGS: {"listing-1": raw_doors},
        },
    )

    doors = manager._mappings["listing-1"]
    page = manager._portal_page("opaque-token", doors, "en")

    assert len(doors) == 6
    assert page.text.count('<form method="post">') == 6
    assert "Open Door 6" in page.text
    assert "Open Door 7" not in page.text


@pytest.mark.asyncio
async def test_portal_renders_safely_scoped_logo_and_favicon(hass, monkeypatch) -> None:
    """Optional branding is escaped, centered, and limited by the CSP."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    hass.config_entries.async_update_entry(
        manager.entry,
        options={
            **manager.entry.options,
            CONF_ACCESS_LOGO_URL: (
                "https://assets.example.com/guest-logo.svg?v=1&theme=light"
            ),
            CONF_ACCESS_FAVICON_URL: "https://icons.example.net/favicon.png",
        },
    )

    page = await manager.async_get_portal(token, "en")
    unavailable = await manager.async_get_portal("x" * 40, "en")

    for response in (page, unavailable):
        assert (
            'src="https://assets.example.com/guest-logo.svg?v=1&amp;theme=light"'
            in response.text
        )
        assert (
            '<link rel="icon" href="https://icons.example.net/favicon.png"'
            in response.text
        )
        assert response.text.index('class="brand"') < response.text.index("<h1>")
        assert "max-height:6rem" in response.text
        assert 'referrerpolicy="no-referrer"' in response.text
        content_security_policy = response.headers["Content-Security-Policy"]
        assert (
            "img-src https://assets.example.com https://icons.example.net"
            in content_security_policy
        )
        assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("de", "Haustür wurde geöffnet"),
        ("en", "Front door was opened"),
        ("es", "Se abrió Puerta principal"),
        ("fr", "Porte d’entrée a été ouverte"),
    ],
)
async def test_ajax_unlock_returns_localized_message_and_fresh_nonces(
    hass, monkeypatch, language: str, message: str
) -> None:
    """Successful AJAX actions keep the page usable without a reload."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    service_call = AsyncMock()

    async def _async_call(_registry, *args, **kwargs) -> None:
        await service_call(*args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", _async_call)
    hass.states.async_set("lock.front_door", "locked")

    result = await manager.async_unlock(
        token,
        "0",
        manager._action_nonce(token, 0),
        language,
        as_json=True,
    )
    payload = json.loads(result.text)

    assert result.status == 200
    assert result.content_type == "application/json"
    assert payload == {
        "ok": True,
        "code": "unlocked",
        "message": message,
        "nonces": {
            "0": manager._action_nonce(token, 0),
            "1": manager._action_nonce(token, 1),
        },
    }
    service_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_ajax_expired_nonce_is_refreshed_without_unlocking(
    hass, monkeypatch
) -> None:
    """The client can retry once with fresh nonces after a page was left open."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])
    service_call = AsyncMock()

    async def _async_call(_registry, *args, **kwargs) -> None:
        await service_call(*args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", _async_call)
    hass.states.async_set("lock.front_door", "locked")

    result = await manager.async_unlock(
        token,
        "0",
        "expired",
        "fr",
        as_json=True,
    )
    payload = json.loads(result.text)

    assert result.status == 403
    assert payload["ok"] is False
    assert payload["code"] == "invalid_nonce"
    assert payload["message"] == "Session actualisée"
    assert payload["nonces"]["0"] == manager._action_nonce(token, 0)
    service_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_cache_or_changed_reservation_fails_closed(
    hass, monkeypatch
) -> None:
    """Aged data and changed dates invalidate an old link before reconciliation."""
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    token = manager._token_for("reservation-1", record["version"])

    manager._coordinator.data.data_stale = True
    manager._coordinator.data.cache_age_minutes = 1
    assert (await manager.async_get_portal(token)).status == 200

    manager._coordinator.data.cache_age_minutes = 361
    assert (await manager.async_get_portal(token)).status == 404

    manager._coordinator.data.data_stale = False
    manager._coordinator.data.cache_age_minutes = 0
    reservation = manager._coordinator.data.reservations[0]
    reservation.status = "checked_in"
    assert (await manager.async_get_portal(token)).status == 200

    reservation.check_out_utc = (dt_util.utcnow() + timedelta(hours=2)).isoformat()
    assert (await manager.async_get_portal(token)).status == 404
    diagnostics = manager.diagnostics()
    assert diagnostics["last_validation_failure"] == "authorization_changed"
    assert diagnostics["last_validation_failure_at"] is not None
    assert diagnostics["validation_failure_counts"] == {
        "authorization_changed": 1,
        "stale_reservation_snapshot": 1,
    }


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
async def test_failed_cleanup_backs_off_and_eventually_prunes_local_state(
    hass, monkeypatch
) -> None:
    """Failed tombstone cleanup is bounded and old in-memory rate data is pruned."""
    manager, client = await _manager(hass, monkeypatch)
    manager._last_action[("reservation-1", 0)] = 1.0
    manager._action_windows[("reservation-1", 0)].append(1.0)
    manager._coordinator.data.reservations[0].status = "cancelled"
    client.async_delete_reservation_custom_field.side_effect = GuestyApiError("offline")

    await manager.async_reconcile()

    client.async_delete_reservation_custom_field.assert_awaited_once()
    record = manager._records["reservation-1"]
    assert record["cleanup_retry_count"] == 1

    await manager.async_reconcile()

    client.async_delete_reservation_custom_field.assert_awaited_once()
    record["revoked_at"] = (dt_util.utcnow() - timedelta(days=8)).isoformat()
    await manager.async_reconcile()

    assert "reservation-1" not in manager._records
    assert ("reservation-1", 0) not in manager._last_action
    assert ("reservation-1", 0) not in manager._action_windows


@pytest.mark.asyncio
async def test_reactivated_deleted_reservation_gets_a_new_random_token(
    hass, monkeypatch
) -> None:
    """Deleting and recreating a record cannot resurrect an old bearer URL."""
    versions = iter((101, 202))
    monkeypatch.setattr(access.secrets, "randbits", lambda _bits: next(versions))
    manager, _client = await _manager(hass, monkeypatch)
    record = manager._records["reservation-1"]
    old_token = manager._token_for("reservation-1", record["version"])

    manager._coordinator.data.reservations[0].status = "cancelled"
    await manager.async_reconcile()
    assert "reservation-1" not in manager._records

    manager._coordinator.data.reservations[0].status = "confirmed"
    await manager.async_reconcile()
    new_record = manager._records["reservation-1"]
    new_token = manager._token_for("reservation-1", new_record["version"])

    assert new_record["version"] == 202
    assert new_token != old_token


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
