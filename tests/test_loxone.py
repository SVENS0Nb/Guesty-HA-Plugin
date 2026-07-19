"""Tests for reservation-driven Loxone PIN provisioning."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import loxone
from custom_components.guesty.api import GuestyApiError, GuestyPermissionError
from custom_components.guesty.const import (
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_LATE_MINUTES,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_LOXONE_CODE_PREFIX,
    CONF_LOXONE_CUSTOM_FIELD,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_GROUP_UUIDS,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_LOXONE_MINISERVERS,
    CONF_LOXONE_PROVISION_LEAD_MINUTES,
    CONF_LOXONE_SERVER_ID,
    CONF_LOXONE_SERVER_GROUPS,
    CONF_LOXONE_SERVER_NAME,
    CONF_LOXONE_SERVER_PASSWORD,
    CONF_LOXONE_SERVER_URL,
    CONF_LOXONE_SERVER_USERNAME,
    DEFAULT_LOXONE_CUSTOM_FIELD,
    DOMAIN,
)
from custom_components.guesty.loxone import (
    GuestyLoxoneManager,
    GuestyLoxoneStorage,
    async_remove_stored_loxone_users,
)
from custom_components.guesty.loxone_api import (
    LoxoneApiError,
    LoxoneCodeConflictError,
)
from custom_components.guesty.models import GuestyListing, GuestyReservation

NOW = datetime.fromisoformat("2026-07-14T12:00:00+00:00")
FIELD_ID = "65fab102a5284d73c6206db0"


def _listing() -> GuestyListing:
    return GuestyListing(
        id="listing-1",
        title="Apartment",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="UTC",
        active=True,
    )


def _reservation(
    *,
    check_in: datetime,
    check_out: datetime,
    key_code: str | None = None,
    reservation_id: str = "reservation-1",
) -> GuestyReservation:
    result = GuestyReservation.from_api(
        {
            "_id": reservation_id,
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": check_in.isoformat(),
            "checkOut": check_out.isoformat(),
            "guest": {"fullName": "Max Mustermann"},
            "lastUpdatedAt": "2026-07-14T11:59:00+00:00",
            "customFields": (
                [{"fieldId": FIELD_ID, "value": key_code}] if key_code else []
            ),
            "notes": {"keyCode": key_code} if key_code else {},
        }
    )
    assert result is not None
    return result


def _options(*, expose_details: bool = False) -> dict:
    return {
        CONF_EXPOSE_GUEST_DETAILS: expose_details,
        CONF_LOXONE_ENABLED: True,
        CONF_LOXONE_PROVISION_LEAD_MINUTES: 360,
        CONF_LOXONE_CODE_PREFIX: "7",
        CONF_LOXONE_CUSTOM_FIELD: "{{door_code}}",
        CONF_ACCESS_EARLY_MINUTES: 0,
        CONF_ACCESS_LATE_MINUTES: 0,
        CONF_LOXONE_MINISERVERS: [
            {
                CONF_LOXONE_SERVER_ID: "server-1",
                CONF_LOXONE_SERVER_NAME: "Haus",
                CONF_LOXONE_SERVER_URL: "https://loxone.test",
                CONF_LOXONE_SERVER_USERNAME: "service",
                CONF_LOXONE_SERVER_PASSWORD: "secret",
                CONF_LOXONE_SERVER_GROUPS: [{"uuid": "group-1", "name": "Guests"}],
            }
        ],
        CONF_LOXONE_LISTING_MAPPINGS: {
            "listing-1": {
                CONF_LOXONE_SERVER_ID: "server-1",
                CONF_LOXONE_GROUP_UUIDS: ["group-1"],
            }
        },
    }


def _manager(
    hass,
    monkeypatch,
    reservation: GuestyReservation,
    *,
    options: dict | None = None,
):
    entry = MockConfigEntry(domain=DOMAIN, options=options or _options())
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
        )
    )
    guesty_client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value=FIELD_ID),
        async_get_reservation_custom_field=AsyncMock(return_value=None),
        async_update_reservation_custom_field=AsyncMock(),
    )
    manager = GuestyLoxoneManager(hass, entry, guesty_client, coordinator)
    manager._data = {"records": {}}
    manager._storage.async_save = AsyncMock()
    manager._schedule_at = MagicMock()
    remote = SimpleNamespace(
        async_find_user_by_userid=AsyncMock(return_value=None),
        async_add_or_update_user=AsyncMock(return_value="user-uuid"),
        async_set_access_code=AsyncMock(),
        async_delete_user=AsyncMock(),
    )
    monkeypatch.setattr(
        manager,
        "_loxone_client",
        lambda _server_id, _server_fallback=None: remote,
    )
    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW)
    return manager, coordinator, guesty_client, remote


@pytest.mark.asyncio
async def test_future_booking_gets_stable_guesty_code_without_early_loxone_user(
    hass, monkeypatch
) -> None:
    """The shared Guesty pass publishes a PIN now but defers the Loxone user."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()
    await manager.async_reconcile()

    code = manager._records[reservation.id]["code"]
    assert len(code) == 6 and code.startswith("7") and code.isdigit()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id, FIELD_ID, code
    )
    remote.async_add_or_update_user.assert_not_awaited()
    assert manager._records[reservation.id]["field_synced"] is True


@pytest.mark.asyncio
async def test_bulk_custom_field_migration_is_prioritized_and_bounded(
    hass, monkeypatch
) -> None:
    """Nearest stays migrate first without exhausting Guesty's API allowance."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-{days}",
        )
        for days in (4, 1, 3, 2)
    ]
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    coordinator.data.reservations = reservations

    await manager.async_reconcile()

    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_custom_field.await_args_list
    ] == ["reservation-1", "reservation-2"]
    assert manager._records["reservation-3"]["last_error"] == "guesty_sync_queued"
    assert manager._records["reservation-4"]["last_error"] == "guesty_sync_queued"
    assert manager.diagnostics()["custom_field_codes_synced"] == 2
    assert manager.diagnostics()["custom_field_codes_queued"] == 2
    assert manager.diagnostics()["custom_field_code_failures"] == 0

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_custom_field.await_count == 4
    assert all(
        manager._records[reservation.id]["field_synced"] for reservation in reservations
    )
    assert manager.diagnostics()["custom_field_codes_pending"] == 0
    assert manager.diagnostics()["custom_field_codes_queued"] == 0


@pytest.mark.asyncio
async def test_guesty_write_failure_is_visible_and_stops_the_batch(
    hass, monkeypatch
) -> None:
    """One rejected write is reported safely and later writes are queued."""
    first = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        reservation_id="reservation-1",
    )
    second = _reservation(
        check_in=NOW + timedelta(days=3),
        check_out=NOW + timedelta(days=4),
        reservation_id="reservation-2",
    )
    manager, coordinator, guesty_client, _remote = _manager(hass, monkeypatch, first)
    coordinator.data.reservations.append(second)
    guesty_client.async_update_reservation_custom_field.side_effect = (
        GuestyPermissionError("private Guesty response")
    )

    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_custom_field.await_count == 1
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["guesty_status"] == "error"
    assert snapshot["error_reason"] == "guesty_permission_denied"
    assert "private Guesty response" not in str(manager.diagnostics())
    assert manager.diagnostics()["custom_field_code_failures"] == 1
    assert manager.diagnostics()["custom_field_codes_queued"] == 1


@pytest.mark.asyncio
async def test_setup_recovers_reasonless_v180_guesty_backoff(hass, monkeypatch) -> None:
    """The affected v1.8.0 retry state is moved into the fast bounded queue."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    retry_at = NOW + timedelta(hours=1)
    manager._storage.async_load = AsyncMock(
        return_value={
            "records": {
                reservation.id: {
                    "field_synced": False,
                    "guesty_retry_at": retry_at.isoformat(),
                    "guesty_retry_count": 4,
                }
            },
            "resolved_field": {},
        }
    )
    manager.async_schedule_reconcile = MagicMock()

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert "guesty_retry_at" not in record
    assert "guesty_retry_count" not in record
    assert record["last_error"] == "guesty_sync_queued"
    manager._storage.async_save.assert_awaited_once()
    manager.async_schedule_reconcile.assert_called_once_with()


@pytest.mark.asyncio
async def test_existing_private_code_migrates_to_empty_custom_field_without_rotation(
    hass, monkeypatch
) -> None:
    """Upgrading from notes.Keycode keeps the already-issued guest code stable."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "listing_id": "listing-1",
        "code": "712345",
        "field_synced": True,
    }

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        FIELD_ID,
        "712345",
    )


@pytest.mark.asyncio
async def test_native_keycode_migrates_to_empty_custom_field_without_rotation(
    hass, monkeypatch
) -> None:
    """A code issued by v1.9.x is preserved when returning to a custom field."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.legacy_key_code = "712346"
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712346"
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        FIELD_ID,
        "712346",
    )


@pytest.mark.asyncio
async def test_empty_migration_option_uses_default_custom_field(
    hass, monkeypatch
) -> None:
    """A blank option saved by v1.9.x automatically returns to door_code."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    options = _options()
    options[CONF_LOXONE_CUSTOM_FIELD] = ""
    manager, _coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )

    await manager.async_reconcile()

    guesty_client.async_resolve_custom_field.assert_awaited_once_with(
        DEFAULT_LOXONE_CUSTOM_FIELD
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once()


@pytest.mark.asyncio
async def test_configured_custom_field_variable_is_resolved(hass, monkeypatch) -> None:
    """The Guesty variable can be changed without changing application code."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    options = _options()
    options[CONF_LOXONE_CUSTOM_FIELD] = "{{alternate_door_code}}"
    manager, _coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )

    await manager.async_reconcile()

    guesty_client.async_resolve_custom_field.assert_awaited_once_with(
        "{{alternate_door_code}}"
    )


@pytest.mark.asyncio
async def test_existing_guesty_keycode_is_adopted_without_rewrite(
    hass, monkeypatch
) -> None:
    """A pre-existing six-digit Guesty Keycode is adopted without a rewrite."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_is_provisioned_once_with_reservation_window(
    hass, monkeypatch
) -> None:
    """Inside the lead time, one user and one access-code assignment are enough."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()
    await manager.async_reconcile()

    remote.async_add_or_update_user.assert_awaited_once()
    kwargs = remote.async_add_or_update_user.await_args.kwargs
    assert kwargs["name"] == "Guesty Buchung reservation-1"
    assert kwargs["valid_from"] == reservation.check_in_datetime(_listing())
    assert kwargs["valid_until"] == reservation.check_out_datetime(_listing())
    assert kwargs["group_uuids"] == ["group-1"]
    remote.async_set_access_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_guesty_arrival_change_updates_existing_loxone_user(
    hass, monkeypatch
) -> None:
    """A same-day plannedArrival edit updates the existing user's validity."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    reservation.check_in_date = NOW.date().isoformat()
    manager, _coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.planned_arrival = "14:00"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    assert remote.async_add_or_update_user.await_count == 2
    first_update, second_update = remote.async_add_or_update_user.await_args_list
    assert first_update.kwargs["user_uuid"] is None
    assert second_update.kwargs["user_uuid"] == "user-uuid"
    assert second_update.kwargs["valid_from"] == NOW + timedelta(hours=2)
    assert second_update.kwargs["valid_until"] == reservation.check_out_datetime(
        _listing()
    )
    assert remote.async_set_access_code.await_count == 2
    assert manager._records[reservation.id]["code_set"] is True


@pytest.mark.asyncio
async def test_guest_name_is_used_in_loxone_only_after_privacy_opt_in(
    hass, monkeypatch
) -> None:
    """Disabling guest details also keeps the name out of Loxone."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=_options(expose_details=True),
    )

    await manager.async_reconcile()

    name = remote.async_add_or_update_user.await_args.kwargs["name"]
    assert "Max Mustermann" in name
    assert "reservation-1" not in name


@pytest.mark.asyncio
async def test_manual_guesty_keycode_change_updates_existing_loxone_user(
    hass, monkeypatch
) -> None:
    """A valid Guesty edit is authoritative even after Loxone provisioning."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    original_code = manager._records[reservation.id]["code"]

    reservation.custom_fields[FIELD_ID] = "799999"
    reservation.key_code = "799999"
    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "799999"
    assert original_code != manager._records[reservation.id]["code"]
    guesty_client.async_update_reservation_custom_field.assert_awaited_once()
    assert remote.async_set_access_code.await_args_list == [
        call("user-uuid", original_code),
        call("user-uuid", "799999"),
    ]
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["guesty_status"] == "synced"
    assert snapshot["loxone_status"] == "provisioned"


@pytest.mark.asyncio
async def test_duplicate_guesty_codes_are_replaced_before_loxone_provisioning(
    hass, monkeypatch
) -> None:
    """Two known Guesty bookings can never retain the same access code."""
    first = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="799999",
        reservation_id="reservation-1",
    )
    second = _reservation(
        check_in=NOW + timedelta(days=13),
        check_out=NOW + timedelta(days=15),
        key_code="799999",
        reservation_id="reservation-2",
    )
    manager, coordinator, guesty_client, remote = _manager(hass, monkeypatch, first)
    coordinator.data.reservations.append(second)

    await manager.async_reconcile()

    first_code = manager._records[first.id]["code"]
    second_code = manager._records[second.id]["code"]
    assert first_code != second_code
    assert first_code == "799999"
    assert second_code != "799999"
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        second.id, FIELD_ID, second_code
    )
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_duplicate_rotates_new_editor_not_established_owner(
    hass, monkeypatch
) -> None:
    """The booking that copied a code changes even when it sorts first."""
    established = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="711111",
        reservation_id="reservation-1",
    )
    edited = _reservation(
        check_in=NOW + timedelta(days=13),
        check_out=NOW + timedelta(days=15),
        key_code="711111",
        reservation_id="reservation-0",
    )
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, established
    )
    coordinator.data.reservations.append(edited)
    manager._data = {
        "records": {
            established.id: {"code": "711111", "field_synced": True},
            edited.id: {"code": "722222", "field_synced": True},
        }
    }

    await manager.async_reconcile()

    assert manager._records[established.id]["code"] == "711111"
    replacement = manager._records[edited.id]["code"]
    assert replacement not in {"711111", "722222"}
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        edited.id, FIELD_ID, replacement
    )


def test_unmapped_listing_reports_code_automation_not_configured(
    hass, monkeypatch
) -> None:
    """Listings without an explicit mapping never participate in code sync."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_LOXONE_LISTING_MAPPINGS] = {}
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    assert manager.listing_status_snapshot("listing-1") == {
        "guesty_status": "not_configured",
        "loxone_status": "not_configured",
    }


@pytest.mark.asyncio
async def test_cancel_removes_plaintext_before_retrying_remote_cleanup(
    hass, monkeypatch
) -> None:
    """A failed Loxone delete retains only a code-free cleanup tombstone."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    coordinator.data.reservations = []
    remote.async_delete_user.side_effect = LoxoneApiError("offline")

    await manager.async_reconcile()

    assert "code" not in manager._records[reservation.id]
    assert manager._records[reservation.id]["retired"] is True

    remote.async_delete_user.side_effect = None
    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()
    assert reservation.id not in manager._records


@pytest.mark.asyncio
async def test_stale_data_never_provisions_but_still_enforces_stored_end(
    hass, monkeypatch
) -> None:
    """An outage fails closed without leaving a user past its known validity."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    coordinator.data.data_stale = True

    await manager.async_reconcile()

    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()

    manager._records[reservation.id] = {
        "listing_id": "listing-1",
        "server_id": "server-1",
        "user_uuid": "user-uuid",
        "code": "712345",
        "access_end": (NOW - timedelta(minutes=1)).isoformat(),
    }
    coordinator.data.reservations = []
    await manager.async_reconcile()

    remote.async_delete_user.assert_awaited_once_with("user-uuid")
    assert reservation.id not in manager._records


@pytest.mark.asyncio
async def test_already_ended_active_reservation_does_not_create_local_state(
    hass, monkeypatch
) -> None:
    """A lagging Guesty status after checkout cannot recreate a PIN record."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW - timedelta(minutes=1),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    assert reservation.key_code is None
    assert reservation.id not in manager._records
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_loxone_collision_rotates_guesty_and_retries_immediately(
    hass, monkeypatch
) -> None:
    """A Miniserver collision gets a new Guesty code before one bounded retry."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_set_access_code.side_effect = [
        LoxoneCodeConflictError("duplicate"),
        None,
    ]

    await manager.async_reconcile()

    remote.async_delete_user.assert_awaited_once_with("user-uuid")
    record = manager._records[reservation.id]
    assert remote.async_add_or_update_user.await_count == 2
    assert remote.async_set_access_code.await_count == 2
    first_code = guesty_client.async_update_reservation_custom_field.await_args_list[
        0
    ].args[2]
    replacement = guesty_client.async_update_reservation_custom_field.await_args_list[
        1
    ].args[2]
    assert replacement != first_code
    assert reservation.key_code == replacement
    assert record["code"] == replacement
    assert record["code_set"] is True
    assert record.get("conflict") is None


@pytest.mark.asyncio
async def test_repeated_loxone_collisions_are_bounded_and_backed_off(
    hass, monkeypatch
) -> None:
    """A broken or saturated code namespace cannot create a request loop."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_set_access_code.side_effect = LoxoneCodeConflictError("duplicate")

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert remote.async_set_access_code.await_count == 3
    assert remote.async_delete_user.await_count == 3
    assert guesty_client.async_update_reservation_custom_field.await_count == 4
    assert record["conflict"] is True
    assert record["last_error"] == "code_conflict"
    assert record.get("loxone_retry_at") is not None


@pytest.mark.asyncio
async def test_failed_collision_delete_is_retried_before_code_assignment(
    hass, monkeypatch
) -> None:
    """A possibly duplicated user is cleaned up before any provisioning retry."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_set_access_code.side_effect = [
        LoxoneCodeConflictError("duplicate"),
        None,
    ]
    remote.async_delete_user.side_effect = [LoxoneApiError("offline"), None]

    await manager.async_reconcile()
    assert manager._records[reservation.id]["collision_cleanup_pending"] is True

    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()

    assert remote.async_set_access_code.await_count == 2
    assert remote.async_delete_user.await_count == 2
    assert manager._records[reservation.id]["code_set"] is True
    assert manager._records[reservation.id].get("collision_cleanup_pending") is None


@pytest.mark.asyncio
async def test_failed_guesty_rotation_write_retries_same_replacement(
    hass, monkeypatch
) -> None:
    """A Guesty outage cannot restore a code Loxone already rejected."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    guesty_client.async_update_reservation_custom_field.side_effect = [
        None,
        GuestyApiError("offline"),
        None,
    ]
    remote.async_set_access_code.side_effect = [
        LoxoneCodeConflictError("duplicate"),
        None,
    ]

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    replacement = record["code"]
    assert record["replacement_pending"] is True
    assert record["field_synced"] is False

    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()

    assert record["code"] == replacement
    assert record.get("replacement_pending") is None
    assert record["field_synced"] is True
    assert record["code_set"] is True
    assert guesty_client.async_update_reservation_custom_field.await_args.args == (
        reservation.id,
        FIELD_ID,
        replacement,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("edited_value", ["", "not-six-digits"])
async def test_empty_or_invalid_guesty_code_revokes_old_user_before_replacement(
    hass, monkeypatch, edited_value
) -> None:
    """An explicit Guesty edit cannot leave a hidden old Loxone PIN active."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_code = manager._records[reservation.id]["code"]

    reservation.custom_fields[FIELD_ID] = edited_value
    reservation.key_code = edited_value
    reservation.key_code_observed = True
    await manager.async_reconcile()

    replacement = manager._records[reservation.id]["code"]
    assert replacement != old_code
    assert replacement.isdigit() and len(replacement) == 6
    remote.async_delete_user.assert_awaited_once_with("user-uuid")
    guesty_client.async_update_reservation_custom_field.assert_awaited_with(
        reservation.id, FIELD_ID, replacement
    )
    assert manager._records[reservation.id]["code_set"] is True


@pytest.mark.asyncio
async def test_unobserved_cached_keycode_never_overwrites_guesty(
    hass, monkeypatch
) -> None:
    """A privacy-stripped cache value is not mistaken for a manual deletion."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code_observed = False
    reservation.custom_fields_observed = False
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "listing_id": "listing-1",
        "code": "712345",
        "field_synced": True,
        "field_id": FIELD_ID,
        "source_last_updated_at": reservation.last_updated_at,
    }

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_removed_server_uses_private_snapshot_for_cleanup(
    hass, monkeypatch
) -> None:
    """Changing a server URL cannot strand the old managed Loxone user."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_LOXONE_MINISERVERS] = []
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(listings={"listing-1": _listing()}, reservations=[])
    )
    manager = GuestyLoxoneManager(hass, entry, SimpleNamespace(), coordinator)
    manager._data = {
        "records": {
            reservation.id: {
                "server_id": "removed-server",
                "user_uuid": "user-uuid",
                "server_snapshot": {
                    CONF_LOXONE_SERVER_URL: "https://old-loxone.test",
                    CONF_LOXONE_SERVER_USERNAME: "service",
                    CONF_LOXONE_SERVER_PASSWORD: "old-secret",
                },
            }
        }
    }
    remote = SimpleNamespace(async_delete_user=AsyncMock())
    from_hass = MagicMock(return_value=remote)
    monkeypatch.setattr(loxone.LoxoneApiClient, "from_hass", from_hass)

    await manager._async_delete_remote_user(manager._records[reservation.id])

    from_hass.assert_called_once_with(
        hass,
        "https://old-loxone.test",
        "service",
        "old-secret",
    )
    remote.async_delete_user.assert_awaited_once_with("user-uuid")


def test_later_booking_error_has_priority_in_listing_status(hass, monkeypatch) -> None:
    """One healthy next booking cannot hide an error on a later booking."""
    first = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        reservation_id="reservation-1",
    )
    second = _reservation(
        check_in=NOW + timedelta(days=3),
        check_out=NOW + timedelta(days=4),
        reservation_id="reservation-2",
    )
    manager, coordinator, _guesty_client, _remote = _manager(hass, monkeypatch, first)
    coordinator.data.reservations.append(second)
    manager._data = {
        "records": {
            first.id: {"field_synced": True},
            second.id: {
                "field_synced": True,
                "last_error": "invalid_mapping",
            },
        }
    }

    snapshot = manager.listing_status_snapshot("listing-1")

    assert snapshot["access_start"] == second.check_in_datetime(_listing())
    assert snapshot["loxone_status"] == "error"


@pytest.mark.asyncio
async def test_private_storage_drops_invalid_record_values(hass) -> None:
    """One malformed private record cannot break every reconciliation pass."""
    storage = GuestyLoxoneStorage(hass, "entry-id")
    storage._store.async_load = AsyncMock(
        return_value={"records": {"valid": {"code": "712345"}, "invalid": []}}
    )

    data = await storage.async_load()

    assert data == {
        "records": {"valid": {"code": "712345"}},
        "resolved_field": {},
    }


def test_prefix_reserves_at_least_ten_thousand_codes(hass, monkeypatch) -> None:
    """Prefixes that reduce the namespace below 10,000 codes are rejected."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_LOXONE_CODE_PREFIX] = "123"
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    with pytest.raises(ValueError, match="prefix"):
        manager._generate_code()


@pytest.mark.asyncio
async def test_integration_removal_retains_code_free_tombstone_on_delete_failure(
    hass, monkeypatch
) -> None:
    """Failed final cleanup preserves retry data but never preserves the PIN."""
    entry = MockConfigEntry(domain=DOMAIN, options=_options())
    entry.add_to_hass(hass)
    data = {
        "records": {
            "reservation-1": {
                "code": "712345",
                "server_id": "server-1",
                "user_uuid": "user-uuid",
            }
        }
    }
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=data),
        async_save=AsyncMock(),
        async_remove=AsyncMock(),
    )
    remote = SimpleNamespace(
        async_delete_user=AsyncMock(side_effect=LoxoneApiError("offline"))
    )
    monkeypatch.setattr(loxone, "GuestyLoxoneStorage", lambda _hass, _id: storage)
    monkeypatch.setattr(
        loxone.LoxoneApiClient,
        "from_hass",
        MagicMock(return_value=remote),
    )

    result = await async_remove_stored_loxone_users(hass, entry)

    assert result is False
    assert data["records"]["reservation-1"]["retired"] is True
    assert "code" not in data["records"]["reservation-1"]
    storage.async_remove.assert_not_awaited()
    assert storage.async_save.await_count >= 2
