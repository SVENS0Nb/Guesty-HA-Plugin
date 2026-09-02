"""Tests for reservation-driven Loxone PIN provisioning."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import loxone
from custom_components.guesty.api import (
    GuestyApiClient,
    GuestyApiError,
    GuestyKeyCodeWriteResult,
    GuestyKeyCodeUnavailableError,
    GuestyNotFoundError,
    GuestyPermissionError,
    GuestyRetryableError,
    KEYCODE_WRITE_ROUTE_V2,
    KEYCODE_WRITE_ROUTE_V3,
)
from custom_components.guesty.const import (
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_LATE_MINUTES,
    CONF_CLIENT_ID,
    CONF_EXPOSE_GUEST_DETAILS,
    CONF_GUESTY_CODE_SUFFIXES,
    CONF_LOXONE_CODE_PREFIX,
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
    CONF_PIN_CUSTOM_FIELD,
    CONF_PIN_CUSTOM_ENABLED,
    CONF_PIN_NATIVE_ENABLED,
    CONF_PIN_OFFLINE_PROVISIONING,
    CONF_SCAN_INTERVAL,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    CONF_TTLOCK_LOCK_IDS,
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
PIN_FIELD_ID = "65fab102a5284d73c6206db0"


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
    status: str = "confirmed",
) -> GuestyReservation:
    result = GuestyReservation.from_api(
        {
            "_id": reservation_id,
            "listingId": "listing-1",
            "status": status,
            "checkIn": check_in.isoformat(),
            "checkOut": check_out.isoformat(),
            "guest": {"fullName": "Max Mustermann"},
            "lastUpdatedAt": "2026-07-14T11:59:00+00:00",
            "notes": {"keyCode": key_code} if key_code else {},
            "customFields": (
                [{"fieldId": PIN_FIELD_ID, "value": key_code}] if key_code else []
            ),
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
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client-id"},
        options=options or _options(),
    )
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
        ),
        reservation_webhook_received_at=MagicMock(return_value=None),
    )
    guesty_client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value=PIN_FIELD_ID),
        async_get_reservation_custom_field=AsyncMock(
            side_effect=lambda reservation_id, field_id: next(
                (
                    item.custom_fields.get(field_id)
                    for item in coordinator.data.reservations
                    if item.id == reservation_id
                ),
                None,
            )
        ),
        async_update_reservation_custom_field=AsyncMock(),
        async_update_reservation_key_code=AsyncMock(),
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
async def test_webhook_pin_is_persisted_immediately_but_first_written_after_one_minute(
    hass, monkeypatch
) -> None:
    """A fresh webhook separates durable PIN allocation from Guesty writes."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    coordinator.reservation_webhook_received_at.return_value = NOW

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    code = record["code"]
    assert len(code) == 6 and code.isdigit()
    assert record["field_synced"] is False
    assert (
        record["webhook_pin_first_write_at"] == (NOW + timedelta(minutes=1)).isoformat()
    )
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()
    manager._schedule_at.assert_called_with(NOW + timedelta(minutes=1))

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=59),
    )
    await manager.async_reconcile()
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=1),
    )
    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        code,
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["field_synced"] is True
    assert (
        record["webhook_pin_first_write_at"] == (NOW + timedelta(minutes=1)).isoformat()
    )

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=2),
    )
    await manager.async_reconcile()

    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        code,
    )
    assert record["field_synced"] is True
    assert "webhook_pin_received_at" not in record
    assert manager._last_generated == 0


@pytest.mark.asyncio
async def test_webhook_omitted_custom_projection_confirms_empty_before_staging_pin(
    hass, monkeypatch
) -> None:
    """A sparse Booking.com custom projection uses one exact empty-field read."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    reservation.custom_fields = {}
    reservation.custom_fields_observed = False
    reservation.custom_fields_read_failed = True
    reservation.custom_fields_projection_omitted = True
    manager, coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    coordinator.reservation_webhook_received_at.return_value = NOW

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    guesty_client.async_get_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
    )
    assert len(record["code"]) == 6 and record["code"].isdigit()
    assert record["custom_field_id"] == PIN_FIELD_ID
    assert record["custom_last_error"] == "guesty_sync_queued"
    assert (
        record["webhook_pin_first_write_at"] == (NOW + timedelta(minutes=1)).isoformat()
    )
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()
    manager._schedule_at.assert_called_with(NOW + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_webhook_omitted_custom_projection_exact_read_failure_retries_safely(
    hass, monkeypatch
) -> None:
    """A failed exact confirmation keeps generation blocked and schedules retry."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    reservation.custom_fields = {}
    reservation.custom_fields_observed = False
    reservation.custom_fields_read_failed = True
    reservation.custom_fields_projection_omitted = True
    manager, coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    coordinator.reservation_webhook_received_at.return_value = NOW
    guesty_client.async_get_reservation_custom_field.side_effect = GuestyRetryableError(
        "temporarily unavailable"
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert "code" not in record
    assert record["field_synced"] is False
    assert record["custom_last_error"] == "guesty_temporarily_unavailable"
    assert record["guesty_custom_retry_at"] == (NOW + timedelta(minutes=5)).isoformat()
    manager._schedule_at.assert_called_with(NOW + timedelta(minutes=5))
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_recovers_existing_booking_with_omitted_empty_custom_projection(
    hass, monkeypatch
) -> None:
    """A booking missed by the webhook is repaired by the normal shared poll."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    reservation.custom_fields = {}
    reservation.custom_fields_observed = False
    reservation.custom_fields_read_failed = True
    reservation.custom_fields_projection_omitted = True
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    code = record["code"]
    guesty_client.async_get_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
    )
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        code,
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["native_synced"] is True
    assert record["custom_synced"] is False

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        code,
    )
    assert record["field_synced"] is True
    assert record["native_synced"] is True
    assert record["custom_synced"] is True
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_pin_fast_retry_runs_each_minute_then_uses_normal_backoff(
    hass, monkeypatch
) -> None:
    """Minutes one through five retry before ordinary failure timing resumes."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_PIN_CUSTOM_ENABLED] = False
    manager, coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )
    coordinator.reservation_webhook_received_at.return_value = NOW
    guesty_client.async_update_reservation_key_code.side_effect = GuestyPermissionError(
        "temporary test failure"
    )

    await manager.async_reconcile()
    assert guesty_client.async_update_reservation_key_code.await_count == 0

    for minute in range(1, 6):
        current = NOW + timedelta(minutes=minute)
        monkeypatch.setattr(loxone.dt_util, "utcnow", lambda current=current: current)
        await manager.async_reconcile()
        assert guesty_client.async_update_reservation_key_code.await_count == minute
        record = manager._records[reservation.id]
        expected_retry = current + timedelta(minutes=1 if minute < 5 else 5)
        assert record["guesty_native_retry_at"] == expected_retry.isoformat()

    record = manager._records[reservation.id]
    assert record["webhook_pin_fast_failures"] == 4
    assert record["guesty_native_retry_count"] == 1


@pytest.mark.asyncio
async def test_webhook_pin_first_write_delay_survives_manager_restart(
    hass, monkeypatch
) -> None:
    """Private staged state prevents an early PUT after a Home Assistant reload."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    coordinator.reservation_webhook_received_at.return_value = NOW
    await manager.async_reconcile()
    staged_data = manager._data

    restarted, restarted_coordinator, restarted_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    restarted._data = staged_data
    restarted_coordinator.reservation_webhook_received_at.return_value = None
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=30),
    )

    await restarted.async_reconcile()

    restarted_client.async_update_reservation_key_code.assert_not_awaited()
    restarted_client.async_update_reservation_custom_field.assert_not_awaited()
    restarted._schedule_at.assert_called_with(NOW + timedelta(minutes=1))
    guesty_client.async_update_reservation_key_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_clears_pending_reconcile_task(hass, monkeypatch) -> None:
    """Reload cannot leave a Loxone worker or pending follow-up pass behind."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._pending = True
    manager._task = hass.async_create_task(
        asyncio.Event().wait(),
        "test_guesty_loxone_reconcile",
    )
    await asyncio.sleep(0)

    await manager.async_unload()

    assert manager._task is None
    assert manager._pending is False
    assert manager._unloaded is True


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
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, code
    )
    remote.async_add_or_update_user.assert_not_awaited()
    assert manager._records[reservation.id]["field_synced"] is True


@pytest.mark.asyncio
async def test_empty_channel_reservation_generates_pin_on_confirmed_v2_route(
    hass, monkeypatch
) -> None:
    """A sparse V3 channel row cannot strand the first V2 Keycode publication."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code = None
    reservation.key_code_observed = True
    reservation.key_code_v2_observed = True
    reservation.key_code_route = KEYCODE_WRITE_ROUTE_V2
    reservation.key_code_read_failed = False
    reservation.custom_fields = {}
    reservation.custom_fields_observed = True
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    code = record["code"]
    assert len(code) == 6 and code.startswith("7") and code.isdigit()
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        code,
        preferred_route=KEYCODE_WRITE_ROUTE_V2,
        allow_v2_fallback=False,
    )
    guesty_client.async_get_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()
    assert record["native_synced"] is True
    assert record["custom_synced"] is False


@pytest.mark.asyncio
async def test_guesty_confirmation_suffix_is_never_sent_to_loxone(
    hass, monkeypatch
) -> None:
    """Guesty displays the keypad key while providers retain the numeric PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=1),
        check_out=NOW + timedelta(days=1),
    )
    options = _options()
    options[CONF_GUESTY_CODE_SUFFIXES] = {"listing-1": "☑️"}
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    code = manager._records[reservation.id]["code"]
    assert len(code) == 6 and code.isdigit()
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, f"{code}☑️"
    )
    remote.async_set_access_code.assert_awaited_once_with("user-uuid", code)


@pytest.mark.asyncio
async def test_existing_suffixed_guesty_code_is_adopted_without_rotation(
    hass, monkeypatch
) -> None:
    """A valid Guesty display suffix does not become part of the provider PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=1),
        check_out=NOW + timedelta(days=1),
        key_code="712345#",
    )
    options = _options()
    options[CONF_GUESTY_CODE_SUFFIXES] = {"listing-1": "#"}
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    remote.async_set_access_code.assert_awaited_once_with("user-uuid", "712345")


@pytest.mark.asyncio
async def test_changed_confirmation_suffix_rewrites_display_without_rotating_pin(
    hass, monkeypatch
) -> None:
    """Changing the listing suffix preserves and only reformats the Guesty PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345#",
    )
    options = _options()
    options[CONF_GUESTY_CODE_SUFFIXES] = {"listing-1": "☑️"}
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, "712345☑️"
    )
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_suffix_rewrite_failure_retries_same_pin_from_private_state(
    hass, monkeypatch
) -> None:
    """A failed display-only rewrite cannot rotate or strand the provider PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345#",
    )
    options = _options()
    options[CONF_GUESTY_CODE_SUFFIXES] = {"listing-1": "☑️"}
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )
    guesty_client.async_update_reservation_key_code.side_effect = [
        GuestyApiError("offline"),
        None,
    ]

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["field_synced"] is False

    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()

    assert record["code"] == "712345"
    assert record["field_synced"] is True
    assert guesty_client.async_update_reservation_key_code.await_args.args == (
        reservation.id,
        "712345☑️",
    )
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.parametrize("value", ["٧٢٣٤٥٦#", "１２３４５６☑️"])
def test_guesty_code_parser_rejects_non_ascii_digits(value) -> None:
    """Localized numeral glyphs can never become a provider PIN."""
    assert GuestyLoxoneManager._parse_guesty_code(value) is None


@pytest.mark.asyncio
async def test_ttlock_only_listing_reuses_guesty_pin_without_loxone_side_effects(
    hass, monkeypatch
) -> None:
    """The shared PIN owner also serves TTLock-only listings."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=2),
        check_out=NOW + timedelta(days=4),
    )
    options = _options()
    options[CONF_LOXONE_ENABLED] = False
    options[CONF_TTLOCK_ENABLED] = True
    options[CONF_TTLOCK_LISTING_MAPPINGS] = {"listing-1": {CONF_TTLOCK_LOCK_IDS: [101]}}
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_awaited_once()
    remote.async_add_or_update_user.assert_not_awaited()
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["guesty_status"] == "synced"
    assert snapshot["loxone_status"] == "not_configured"


@pytest.mark.asyncio
async def test_bulk_pin_migration_is_prioritized_and_bounded(hass, monkeypatch) -> None:
    """Every booking gets one mirror before lower-priority redundancy work."""
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
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == ["reservation-1", "reservation-2"]
    assert manager._records["reservation-3"]["last_error"] == "guesty_sync_queued"
    assert manager._records["reservation-4"]["last_error"] == "guesty_sync_queued"
    assert manager.diagnostics()["native_keycodes_synced"] == 2
    assert manager.diagnostics()["native_keycodes_queued"] == 2
    assert manager.diagnostics()["native_keycode_failures"] == 0

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 4
    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == [
        "reservation-1",
        "reservation-2",
        "reservation-3",
        "reservation-4",
    ]
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=62),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 4
    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_custom_field.await_args_list
    ] == [
        "reservation-1",
        "reservation-2",
    ]

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=93),
    )
    await manager.async_reconcile()

    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_custom_field.await_args_list
    ] == [
        "reservation-1",
        "reservation-2",
        "reservation-3",
        "reservation-4",
    ]
    assert all(
        manager._records[reservation.id]["native_synced"]
        and manager._records[reservation.id]["custom_synced"]
        for reservation in reservations
    )
    assert manager.diagnostics()["native_keycodes_pending"] == 0
    assert manager.diagnostics()["native_keycodes_queued"] == 0


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
    guesty_client.async_update_reservation_key_code.side_effect = GuestyPermissionError(
        "private Guesty response"
    )

    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 1
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["guesty_status"] == "error"
    assert snapshot["error_reason"] == "guesty_permission_denied"
    assert "private Guesty response" not in str(manager.diagnostics())
    assert manager.diagnostics()["native_keycode_failures"] == 1
    assert manager.diagnostics()["native_keycodes_queued"] == 1


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
async def test_setup_recovers_persisted_native_404_backoff_once(
    hass, monkeypatch, caplog
) -> None:
    """The native-Keycode rollout retries old 404 records without PIN rotation."""
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
                    "code": "712345",
                    "field_synced": False,
                    "last_error": "guesty_reservation_not_found",
                    "guesty_retry_at": retry_at.isoformat(),
                    "guesty_retry_count": 4,
                }
            },
            # v2.2.8 persisted version 2 before the native 404 compatibility
            # route existed. v2.2.9 failed to advance this migration marker.
            "guesty_retry_state_version": 2,
        }
    )
    manager.async_schedule_reconcile = MagicMock()
    caplog.set_level("WARNING", logger="custom_components.guesty.loxone")

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert "guesty_retry_at" not in record
    assert "guesty_retry_count" not in record
    assert record["last_error"] == "guesty_sync_queued"
    assert manager._data["guesty_retry_state_version"] == 4
    assert manager._data["guesty_client_fingerprint"] == (
        manager._guesty_client_fingerprint()
    )
    assert "reason=state_migration" in caplog.text
    assert reservation.id not in caplog.text
    assert "712345" not in caplog.text
    manager._storage.async_save.assert_awaited_once()
    manager.async_schedule_reconcile.assert_called_once_with()


@pytest.mark.asyncio
async def test_recovered_404_backlog_resumes_through_global_write_budget(
    hass, monkeypatch
) -> None:
    """A production-sized stale retry state resumes without a write burst."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-{days}",
        )
        for days in range(1, 5)
    ]
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    coordinator.data.reservations = reservations
    original_codes = {
        reservation.id: f"71234{index}"
        for index, reservation in enumerate(reservations)
    }
    manager._storage.async_load = AsyncMock(
        return_value={
            "records": {
                reservation.id: {
                    "code": original_codes[reservation.id],
                    "field_synced": False,
                    "last_error": (
                        "guesty_reservation_not_found"
                        if index % 2 == 0
                        else "guesty_keycode_rejected"
                    ),
                    "guesty_retry_at": (NOW + timedelta(hours=1)).isoformat(),
                    "guesty_retry_count": 4,
                }
                for index, reservation in enumerate(reservations)
            },
            "guesty_retry_state_version": 2,
        }
    )
    manager.async_schedule_reconcile = MagicMock()

    await manager.async_setup()
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == [reservations[0].id, reservations[1].id]
    assert {
        reservation_id: record["code"]
        for reservation_id, record in manager._records.items()
    } == original_codes
    assert all(
        manager._records[reservation.id]["field_synced"] is True
        for reservation in reservations[:2]
    )
    assert all(
        manager._records[reservation.id]["last_error"] == "guesty_sync_queued"
        for reservation in reservations[2:]
    )
    assert manager.diagnostics()["native_keycodes_queued"] == 2


@pytest.mark.asyncio
async def test_v3_retry_state_migrates_endpoint_failure_to_v2_route(
    hass, monkeypatch
) -> None:
    """The new V2 contract immediately resumes old route-specific 404s."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._storage.async_load = AsyncMock(
        return_value={
            "records": {
                reservation.id: {
                    "code": "712345",
                    "field_synced": False,
                    "native_synced": False,
                    "native_last_error": "guesty_keycode_endpoint_unavailable",
                    "guesty_native_retry_at": (NOW + timedelta(hours=1)).isoformat(),
                    "guesty_native_retry_count": 3,
                }
            },
            "pin_state_schema_version": 2,
            "guesty_retry_state_version": 3,
            "guesty_client_fingerprint": manager._guesty_client_fingerprint(),
        }
    )
    manager.async_schedule_reconcile = MagicMock()

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["native_write_route"] == KEYCODE_WRITE_ROUTE_V2
    assert record["native_last_error"] == "guesty_sync_queued"
    assert "guesty_native_retry_at" not in record
    assert "guesty_native_retry_count" not in record
    assert manager._data["guesty_retry_state_version"] == 4


@pytest.mark.asyncio
async def test_v3_retry_state_does_not_misclassify_generic_rejection_as_v2(
    hass, monkeypatch
) -> None:
    """Only a proven route-specific 404 can seed the V2 write route."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._storage.async_load = AsyncMock(
        return_value={
            "records": {
                reservation.id: {
                    "code": "712345",
                    "native_synced": False,
                    "native_last_error": "guesty_keycode_rejected",
                    "guesty_native_retry_at": (NOW + timedelta(hours=1)).isoformat(),
                    "guesty_native_retry_count": 3,
                }
            },
            "pin_state_schema_version": 2,
            "guesty_retry_state_version": 3,
            "guesty_client_fingerprint": manager._guesty_client_fingerprint(),
        }
    )
    manager.async_schedule_reconcile = MagicMock()

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert "native_write_route" not in record
    assert record["native_last_error"] == "guesty_sync_queued"


@pytest.mark.asyncio
async def test_failed_native_read_allows_confirmed_custom_mirror_to_continue(
    hass, monkeypatch
) -> None:
    """A V3 read outage cannot trigger a blind native write or block custom PINs."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    reservation.key_code = None
    reservation.key_code_observed = False
    reservation.key_code_read_failed = True
    reservation.custom_fields = {PIN_FIELD_ID: "712345"}
    reservation.custom_fields_observed = True
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["code"] == "712345"
    assert record["custom_synced"] is True
    assert record["field_synced"] is True
    assert record["native_last_error"] == ("guesty_native_keycode_read_unavailable")


@pytest.mark.asyncio
async def test_failed_custom_read_allows_confirmed_native_mirror_to_continue(
    hass, monkeypatch
) -> None:
    """A PIN projection outage cannot trigger an exact-read fan-out or blind write."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    reservation.custom_fields = {}
    reservation.custom_fields_observed = False
    reservation.custom_fields_read_failed = True
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    guesty_client.async_get_reservation_custom_field.assert_not_awaited()
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert record["code"] == "712345"
    assert record["native_synced"] is True
    assert record["field_synced"] is True
    assert record["custom_last_error"] == "guesty_custom_field_read_unavailable"


@pytest.mark.asyncio
async def test_failed_pin_reads_never_generate_or_overwrite_a_code(
    hass, monkeypatch
) -> None:
    """No PIN is generated while enabled Guesty sources are unreadable."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    reservation.key_code = None
    reservation.key_code_observed = False
    reservation.key_code_read_failed = True
    reservation.custom_fields = {}
    reservation.custom_fields_observed = False
    reservation.custom_fields_read_failed = True
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    guesty_client.async_get_reservation_custom_field.assert_not_awaited()
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert "code" not in record
    assert record["field_synced"] is False
    assert record["native_last_error"] == "guesty_native_keycode_read_unavailable"
    assert record["custom_last_error"] == "guesty_custom_field_read_unavailable"


@pytest.mark.asyncio
async def test_setup_preserves_current_persisted_native_backoff(
    hass, monkeypatch, caplog
) -> None:
    """A current retry remains bounded and becomes visible after restart."""
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
                    "code": "712345",
                    "field_synced": False,
                    "last_error": "guesty_keycode_endpoint_unavailable",
                    "guesty_retry_at": retry_at.isoformat(),
                    "guesty_retry_count": 4,
                }
            },
            "guesty_retry_state_version": 4,
            "guesty_client_fingerprint": manager._guesty_client_fingerprint(),
        }
    )
    manager.async_schedule_reconcile = MagicMock()
    caplog.set_level("WARNING", logger="custom_components.guesty.loxone")

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["guesty_retry_at"] == retry_at.isoformat()
    assert record["guesty_retry_count"] == 4
    assert record["last_error"] == "guesty_keycode_endpoint_unavailable"
    assert "waiting for persisted retries" in caplog.text
    assert "guesty_keycode_endpoint_unavailable:1" in caplog.text
    assert reservation.id not in caplog.text
    assert "712345" not in caplog.text
    manager._storage.async_save.assert_awaited_once()
    manager.async_schedule_reconcile.assert_called_once_with()


@pytest.mark.asyncio
async def test_setup_recovers_guesty_backoff_after_client_change(
    hass, monkeypatch
) -> None:
    """New Guesty credentials retry pending writes while preserving each PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._storage.async_load = AsyncMock(
        return_value={
            "records": {
                reservation.id: {
                    "code": "712345",
                    "field_synced": False,
                    "last_error": "guesty_permission_denied",
                    "guesty_retry_at": (NOW + timedelta(hours=1)).isoformat(),
                    "guesty_retry_count": 4,
                }
            },
            "guesty_retry_state_version": 2,
            "guesty_client_fingerprint": "previous-client",
            "guesty_keycode_write_route": "legacy",
        }
    )
    manager.async_schedule_reconcile = MagicMock()

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["last_error"] == "guesty_sync_queued"
    assert "guesty_retry_at" not in record
    assert "guesty_retry_count" not in record
    assert manager._data["guesty_client_fingerprint"] == (
        manager._guesty_client_fingerprint()
    )
    assert "guesty_keycode_write_route" not in manager._data
    manager._storage.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_awaiting_payment_reservation_receives_native_keycode(
    hass, monkeypatch
) -> None:
    """Payment state cannot silently exclude an otherwise active booking."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        status="awaiting_payment",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        record["code"],
    )
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_private_code_migrates_to_native_keycode_without_rotation(
    hass, monkeypatch
) -> None:
    """A sparse response cannot block migration of an existing private PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code_observed = False
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
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, "712345"
    )


@pytest.mark.asyncio
async def test_sparse_private_code_migration_respects_guesty_write_budget(
    hass, monkeypatch
) -> None:
    """Sparse legacy records cannot bypass the bounded Guesty write queue."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-{days}",
        )
        for days in range(1, 5)
    ]
    for reservation in reservations:
        reservation.key_code_observed = False
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    coordinator.data.reservations = reservations
    manager._data = {
        "records": {
            reservation.id: {
                "listing_id": reservation.listing_id,
                "code": f"7{index:05d}",
                "field_synced": True,
            }
            for index, reservation in enumerate(reservations, start=1)
        }
    }

    await manager.async_reconcile()

    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == ["reservation-1", "reservation-2"]
    assert manager._records["reservation-3"]["last_error"] == "guesty_sync_queued"
    assert manager._records["reservation-4"]["last_error"] == "guesty_sync_queued"


def test_guesty_write_budget_preserves_reported_api_headroom(hass, monkeypatch) -> None:
    """No Keycode PUT is attempted inside the four-request API reserve."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_SCAN_INTERVAL] = 120
    manager, _coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )

    guesty_client.last_rate_limit_remaining = 4
    assert manager._guesty_write_budget(NOW) == 0
    assert manager._next_guesty_write_at(NOW) == NOW + timedelta(seconds=120)

    guesty_client.last_rate_limit_remaining = 7
    assert manager._guesty_write_budget(NOW) == 0
    assert manager._next_guesty_write_at(NOW) == NOW + timedelta(seconds=120)

    guesty_client.last_rate_limit_remaining = 8
    assert manager._guesty_write_budget(NOW) == 1
    assert manager._next_guesty_write_at(NOW) == NOW

    GuestyApiClient._capture_rate_limit_headers(
        guesty_client,
        {
            "X-RateLimit-Remaining-Second": "0",
            "X-RateLimit-Remaining-Minute": "20",
            "X-RateLimit-Remaining-Hour": "100",
        },
    )
    assert manager._guesty_write_budget(NOW) == 2
    assert manager._next_guesty_write_at(NOW) == NOW


@pytest.mark.asyncio
async def test_write_rechecks_rate_headroom_immediately_before_put(
    hass, monkeypatch
) -> None:
    """Reads in the same pass can remove a previously calculated write slot."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_SCAN_INTERVAL] = 120
    manager, _coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )
    manager._guesty_writes_remaining = 2
    guesty_client.last_rate_limit_remaining = 7
    record: dict = {}

    with pytest.raises(loxone._GuestyWriteDeferred) as raised:
        await manager._async_write_custom_mirror(
            reservation,
            record,
            PIN_FIELD_ID,
            "712345",
            NOW,
        )

    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert raised.value.retry_at == NOW + timedelta(seconds=120)
    assert record["custom_last_error"] == "guesty_sync_queued"


@pytest.mark.asyncio
async def test_exhausted_guesty_headroom_queues_without_a_put(
    hass, monkeypatch
) -> None:
    """A low response-header allowance never causes another Keycode request."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_SCAN_INTERVAL] = 120
    manager, _coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        reservation,
        options=options,
    )
    guesty_client.last_rate_limit_remaining = 4

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    assert manager._records[reservation.id]["last_error"] == "guesty_sync_queued"
    manager._schedule_at.assert_called_once_with(NOW + timedelta(seconds=120))


@pytest.mark.asyncio
async def test_native_keycode_not_found_uses_custom_field_fallback(
    hass, monkeypatch, caplog
) -> None:
    """Two ambiguous native attempts are charged before custom-field retry."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    guesty_client.async_update_reservation_key_code.side_effect = GuestyNotFoundError(
        "private upstream detail",
        404,
        request_id="request-404",
        endpoint="reservations-v3",
    )
    caplog.set_level("INFO", logger="custom_components.guesty.loxone")

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["field_synced"] is False
    assert record["native_synced"] is False
    assert record["custom_synced"] is False
    assert record["last_error"] == "guesty_reservation_not_found"
    guesty_client.async_update_reservation_key_code.assert_awaited_once()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert "operation=native_keycode_write" in caplog.text
    assert "reason=guesty_reservation_not_found" in caplog.text
    assert "endpoint=reservations-v3" in caplog.text
    assert "http_status=404" in caplog.text
    assert "request_id=request-404" in caplog.text
    assert "retry_count=1" in caplog.text
    assert "retry_in_seconds=300" in caplog.text
    assert "private upstream detail" not in caplog.text
    assert reservation.id not in caplog.text


@pytest.mark.asyncio
async def test_native_keycode_not_found_retries_same_pin_after_sparse_response(
    hass, monkeypatch, caplog
) -> None:
    """A failed initial native write remains retryable when notes are omitted."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code_observed = False
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    guesty_client.async_update_reservation_key_code.side_effect = [
        GuestyNotFoundError("reservation missing"),
        None,
    ]
    caplog.set_level("INFO", logger="custom_components.guesty.loxone")

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    generated = record["code"]
    assert record["field_synced"] is False
    assert record["native_synced"] is False
    assert record["custom_synced"] is False
    assert record["last_error"] == "guesty_reservation_not_found"

    # A coordinator update before the bounded retry time must not create a
    # rapid write loop.
    await manager.async_reconcile()
    assert guesty_client.async_update_reservation_key_code.await_count == 1

    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()

    assert record["code"] == generated
    assert record["field_synced"] is True
    assert "last_error" not in record
    assert record["native_synced"] is True
    assert record["custom_synced"] is False
    assert "guesty_native_retry_at" not in record
    assert "guesty_native_retry_count" not in record
    assert guesty_client.async_update_reservation_key_code.await_count == 2
    guesty_client.async_update_reservation_key_code.assert_awaited_with(
        reservation.id,
        generated,
    )
    assert "Guesty reservation PIN mirror synchronized" in caplog.text
    assert "retry_count=1" in caplog.text
    assert generated not in caplog.text
    assert reservation.id not in caplog.text

    # Redundancy backfill deliberately waits for the next traffic window so a
    # newly confirmed PIN cannot starve another booking.
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=7),
    )
    await manager.async_reconcile()
    assert record["custom_synced"] is True
    assert "guesty_retry_at" not in record
    assert "guesty_retry_count" not in record


@pytest.mark.asyncio
async def test_keycode_endpoint_failure_prioritizes_custom_fallback(
    hass, monkeypatch
) -> None:
    """A bounded v3 route probe caches v2 for the next write window."""
    missing = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        reservation_id="reservation-missing",
    )
    manager, _coordinator, guesty_client, _remote = _manager(hass, monkeypatch, missing)
    manager._data["guesty_write_attempts"] = [NOW.isoformat()]
    guesty_client.async_update_reservation_key_code.side_effect = [
        GuestyKeyCodeUnavailableError("notes endpoint unavailable"),
        GuestyKeyCodeWriteResult(1, KEYCODE_WRITE_ROUTE_V2),
    ]

    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 1
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert manager._records[missing.id]["field_synced"] is False
    assert manager._records[missing.id]["last_error"] == "guesty_sync_queued"
    assert manager._records[missing.id]["native_write_route"] == (
        KEYCODE_WRITE_ROUTE_V2
    )
    assert manager.diagnostics()["native_keycode_failures"] == 0
    assert manager.diagnostics()["native_keycodes_queued"] == 1

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert guesty_client.async_update_reservation_key_code.await_args.kwargs == {
        "preferred_route": KEYCODE_WRITE_ROUTE_V2,
        "allow_v2_fallback": False,
    }
    assert manager._records[missing.id]["field_synced"] is True
    assert manager._records[missing.id]["native_synced"] is True
    assert manager._records[missing.id]["custom_synced"] is False

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=7),
    )
    await manager.async_reconcile()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once()
    assert manager._records[missing.id]["custom_synced"] is True


@pytest.mark.asyncio
async def test_two_keycodes_are_written_per_30_second_window(hass, monkeypatch) -> None:
    """Each documented v3 PUT consumes one of the two global write slots."""
    first = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        reservation_id="reservation-first",
    )
    second = _reservation(
        check_in=NOW + timedelta(days=3),
        check_out=NOW + timedelta(days=4),
        reservation_id="reservation-second",
    )
    manager, coordinator, guesty_client, _remote = _manager(
        hass,
        monkeypatch,
        first,
    )
    coordinator.data.reservations.append(second)
    manager._data["guesty_keycode_write_route"] = "legacy"

    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert manager._records[first.id]["field_synced"] is True
    assert manager._records[second.id]["field_synced"] is True
    assert manager.diagnostics()["guesty_writes_during_last_reconcile"] == 2
    assert manager.diagnostics()["guesty_keycode_write_route"] == KEYCODE_WRITE_ROUTE_V3
    assert not guesty_client.async_update_reservation_key_code.await_args_list[0].kwargs
    assert guesty_client.async_update_reservation_key_code.await_args_list[
        1
    ].kwargs == {
        "preferred_route": KEYCODE_WRITE_ROUTE_V3,
        "allow_v2_fallback": False,
    }


@pytest.mark.asyncio
async def test_confirmed_v2_route_is_cached_while_custom_mirror_stays_active(
    hass, monkeypatch
) -> None:
    """A channel route is reused and manual custom edits still reach Keycode."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=3),
        check_out=NOW + timedelta(days=4),
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    guesty_client.async_update_reservation_key_code.side_effect = [
        GuestyKeyCodeWriteResult(2, KEYCODE_WRITE_ROUTE_V2),
        GuestyKeyCodeWriteResult(1, KEYCODE_WRITE_ROUTE_V2),
    ]

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    original_code = record["code"]
    assert record["native_write_route"] == KEYCODE_WRITE_ROUTE_V2
    assert record["native_synced"] is True
    assert record["custom_synced"] is False
    assert manager.diagnostics()["guesty_writes_during_last_reconcile"] == 2
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert record["custom_synced"] is True
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        original_code,
    )

    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    reservation.last_updated_at = "2026-07-14T12:02:00+00:00"
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=62),
    )
    await manager.async_reconcile()

    assert record["code"] == "734567"
    assert record["native_write_route"] == KEYCODE_WRITE_ROUTE_V2
    assert guesty_client.async_update_reservation_key_code.await_args_list[-1] == call(
        reservation.id,
        "734567",
        preferred_route=KEYCODE_WRITE_ROUTE_V2,
        allow_v2_fallback=False,
    )


@pytest.mark.asyncio
async def test_guesty_write_limit_is_global_across_overlapping_reconciles(
    hass, monkeypatch
) -> None:
    """A retry pass and a new-reservation pass share one persistent write limit."""
    missing = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-missing-{days}",
        )
        for days in (1, 2)
    ]
    queued = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-queued-{days}",
        )
        for days in (3, 4)
    ]
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, missing[0]
    )
    coordinator.data.reservations = missing

    await manager.async_reconcile()
    assert guesty_client.async_update_reservation_key_code.await_count == 2

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=4),
    )
    coordinator.data.reservations.extend(queued)
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert all(
        manager._records[reservation.id]["last_error"] == "guesty_sync_queued"
        for reservation in queued
    )

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 4
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=62),
    )
    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 4
    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == [
        missing[0].id,
        missing[1].id,
        queued[0].id,
        queued[1].id,
    ]
    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_custom_field.await_args_list
    ] == [missing[0].id, missing[1].id]


@pytest.mark.asyncio
async def test_global_write_deferral_preserves_reservation_failure_backoff(
    hass, monkeypatch
) -> None:
    """Global throttling cannot erase a reservation's failure reason or count."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-{days}",
        )
        for days in (1, 2, 3)
    ]
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    coordinator.data.reservations = reservations[:2]

    await manager.async_reconcile()
    assert guesty_client.async_update_reservation_key_code.await_count == 2

    failed_record = {
        "listing_id": "listing-1",
        "code": "712345",
        "field_synced": False,
        "field_id": "notes.keyCode",
        "last_error": "guesty_reservation_not_found",
        "guesty_retry_count": 3,
        "guesty_retry_at": (NOW + timedelta(seconds=4)).isoformat(),
    }
    manager._records[reservations[2].id] = failed_record
    coordinator.data.reservations.append(reservations[2])
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=4),
    )

    await manager.async_reconcile()

    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert failed_record["last_error"] == "guesty_reservation_not_found"
    assert failed_record["guesty_retry_count"] == 3
    assert datetime.fromisoformat(failed_record["guesty_retry_at"]) >= (
        NOW + timedelta(seconds=30)
    )


@pytest.mark.asyncio
async def test_persisted_guesty_write_limit_survives_manager_restart(
    hass, monkeypatch
) -> None:
    """Restarting the manager cannot create a fresh Guesty write allowance."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            reservation_id=f"reservation-{days}",
        )
        for days in (1, 2, 3)
    ]
    first_manager, first_coordinator, first_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    first_coordinator.data.reservations = reservations[:2]
    await first_manager.async_reconcile()
    assert first_client.async_update_reservation_key_code.await_count == 2

    second_manager, second_coordinator, second_client, _remote = _manager(
        hass, monkeypatch, reservations[2]
    )
    second_manager._data = first_manager._data
    second_coordinator.data.reservations = reservations
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=4),
    )

    await second_manager.async_reconcile()

    second_client.async_update_reservation_key_code.assert_not_awaited()
    assert (
        second_manager._records[reservations[2].id]["last_error"]
        == "guesty_sync_queued"
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
    assert manager._records == {}

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["guesty_confirmed_code"] == "712345"
    assert record["field_synced"] is True
    assert manager.reservation_pin_snapshot(reservation.id) == {
        "code": "712345",
        "field_synced": True,
        "access_start": (NOW + timedelta(days=10)).isoformat(),
        "access_end": (NOW + timedelta(days=12)).isoformat(),
    }
    guesty_client.async_update_reservation_key_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_custom_field_pin_is_adopted_and_fills_native(
    hass, monkeypatch
) -> None:
    """A populated custom field is adopted without rotation and fills Keycode."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.custom_fields[PIN_FIELD_ID] = "712345#"
    options = _options()
    options[CONF_GUESTY_CODE_SUFFIXES] = {"listing-1": "#"}
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["native_synced"] is True
    assert record["custom_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, "712345#"
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_native_pin_fills_custom_field_without_rotation(
    hass, monkeypatch
) -> None:
    """An existing Keycode fills the empty configurable mirror exactly once."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    reservation.custom_fields.clear()
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["field_synced"] is True
    assert record["native_synced"] is True
    assert record["custom_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "712345",
    )

    # Guesty emits reservation.updated for the integration's own confirmed PUT.
    # A targeted read-back with the same values must be an idempotent no-op.
    echoed = _reservation(
        reservation_id=reservation.id,
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    echoed.last_updated_at = "2026-07-14T12:01:00+00:00"
    _coordinator.data.reservations = [echoed]

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "712345",
    )


@pytest.mark.asyncio
async def test_native_write_webhook_echo_does_not_create_write_loop(
    hass, monkeypatch
) -> None:
    """The reservation.updated echo of our native PUT is a no-op."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="734567",
    )
    reservation.key_code = None
    reservation.key_code_observed = True
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        "734567",
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()

    echoed = _reservation(
        reservation_id=reservation.id,
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="734567",
    )
    echoed.last_updated_at = "2026-07-14T12:01:00+00:00"
    coordinator.data.reservations = [echoed]

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        "734567",
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_dual_sources_need_no_guesty_write(hass, monkeypatch) -> None:
    """Two matching populated sources are adopted without API write traffic."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert manager._records[reservation.id]["field_synced"] is True


@pytest.mark.asyncio
async def test_custom_only_mode_ignores_native_keycode_completely(
    hass, monkeypatch
) -> None:
    """A disabled Keycode cannot cause reads, writes, or mirror conflicts."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    options = _options()
    options[CONF_PIN_NATIVE_ENABLED] = False
    options[CONF_PIN_CUSTOM_ENABLED] = True
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "734567"
    assert record["field_synced"] is True
    assert record["custom_synced"] is True
    assert "conflict" not in record
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["native_keycode_enabled"] is False
    assert snapshot["native_keycode_synced"] is False
    assert snapshot["custom_field_enabled"] is True
    assert snapshot["custom_field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_set_access_code.assert_awaited_once_with("user-uuid", "734567")


@pytest.mark.asyncio
async def test_native_only_mode_ignores_custom_field_completely(
    hass, monkeypatch
) -> None:
    """A disabled custom field is neither resolved, read, written, nor compared."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    options = _options()
    options[CONF_PIN_NATIVE_ENABLED] = True
    options[CONF_PIN_CUSTOM_ENABLED] = False
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["field_synced"] is True
    assert record["native_synced"] is True
    assert "conflict" not in record
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["native_keycode_enabled"] is True
    assert snapshot["native_keycode_synced"] is True
    assert snapshot["custom_field_enabled"] is False
    assert snapshot["custom_field_synced"] is False
    guesty_client.async_resolve_custom_field.assert_not_awaited()
    guesty_client.async_get_reservation_custom_field.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_set_access_code.assert_awaited_once_with("user-uuid", "712345")


@pytest.mark.asyncio
async def test_no_pin_source_fails_closed_at_runtime(hass, monkeypatch) -> None:
    """Malformed legacy options cannot provision when every source is disabled."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    options = _options()
    options[CONF_PIN_NATIVE_ENABLED] = False
    options[CONF_PIN_CUSTOM_ENABLED] = False
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["field_synced"] is False
    assert record["last_error"] == "pin_source_not_configured"
    guesty_client.async_resolve_custom_field.assert_not_awaited()
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


def test_disabling_native_source_removes_its_readiness_but_keeps_baseline(
    hass, monkeypatch
) -> None:
    """Offline delivery cannot rely on an excluded source during switchover."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    options = _options()
    options[CONF_PIN_NATIVE_ENABLED] = False
    options[CONF_PIN_CUSTOM_ENABLED] = True
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )
    manager._records[reservation.id] = {
        "code": "712345",
        "field_synced": True,
        "native_synced": True,
        "native_baseline_value": "712345",
        "native_last_error": "guesty_keycode_endpoint_unavailable",
        "guesty_native_retry_at": (NOW + timedelta(hours=1)).isoformat(),
        "guesty_native_retry_count": 2,
    }

    assert manager._clear_disabled_pin_source_state() is True

    record = manager._records[reservation.id]
    assert record["field_synced"] is False
    assert record["native_synced"] is True
    assert record["native_baseline_value"] == "712345"
    assert "native_last_error" not in record
    assert "guesty_native_retry_at" not in record
    assert "guesty_native_retry_count" not in record


@pytest.mark.asyncio
async def test_manual_custom_field_edit_updates_native_and_loxone(
    hass, monkeypatch
) -> None:
    """A user edit in the configurable mirror becomes authoritative everywhere."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "734567"
    assert record["native_baseline_value"] == "734567"
    assert record["custom_baseline_value"] == "734567"
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, "734567"
    )
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    assert remote.async_set_access_code.await_args_list == [
        call("user-uuid", "712345"),
        call("user-uuid", "734567"),
    ]


@pytest.mark.asyncio
async def test_newer_manual_custom_edit_bypasses_stale_failure_backoff(
    hass, monkeypatch
) -> None:
    """A newer user edit is reconciled now while global write limits remain."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    record = manager._records[reservation.id]
    record["guesty_native_retry_at"] = (NOW + timedelta(hours=1)).isoformat()
    record["guesty_native_retry_count"] = 3
    record["native_last_error"] = "guesty_temporarily_unavailable"

    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        "734567",
    )
    assert record["code"] == "734567"
    assert "guesty_native_retry_count" not in record
    assert "native_last_error" not in record


@pytest.mark.asyncio
async def test_guesty_phase_finishes_for_all_bookings_before_provider_io(
    hass, monkeypatch
) -> None:
    """One slow provider cannot delay another booking's first Guesty mirror."""
    first = _reservation(
        reservation_id="reservation-1",
        check_in=NOW + timedelta(hours=1),
        check_out=NOW + timedelta(days=1),
    )
    second = _reservation(
        reservation_id="reservation-2",
        check_in=NOW + timedelta(hours=2),
        check_out=NOW + timedelta(days=1, hours=1),
    )
    manager, coordinator, guesty_client, remote = _manager(hass, monkeypatch, first)
    coordinator.data.reservations = [first, second]
    events: list[tuple[str, str]] = []

    async def _write_keycode(reservation_id: str, _value: str, **_kwargs):
        events.append(("guesty", reservation_id))

    async def _create_user(**kwargs):
        events.append(("provider", kwargs["user_id"]))
        return f"user-{len(events)}"

    guesty_client.async_update_reservation_key_code.side_effect = _write_keycode
    remote.async_add_or_update_user.side_effect = _create_user

    await manager.async_reconcile()

    assert events[:2] == [
        ("guesty", "reservation-1"),
        ("guesty", "reservation-2"),
    ]
    assert any(kind == "provider" for kind, _value in events[2:])


@pytest.mark.asyncio
async def test_manual_native_edit_updates_custom_field_and_loxone(
    hass, monkeypatch
) -> None:
    """A user edit in Keycode is propagated to the configurable mirror."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.key_code = "734567"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "734567"
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "734567",
    )
    assert remote.async_set_access_code.await_args_list == [
        call("user-uuid", "712345"),
        call("user-uuid", "734567"),
    ]


@pytest.mark.asyncio
async def test_failed_secondary_propagation_cannot_revert_canonical_pin(
    hass, monkeypatch
) -> None:
    """A stale mirror remains stale on retry instead of becoming authoritative."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    guesty_client.async_update_reservation_custom_field.side_effect = GuestyApiError(
        "offline"
    )

    reservation.key_code = "734567"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "734567"
    assert record["native_synced"] is True
    assert record["custom_synced"] is False

    guesty_client.async_update_reservation_custom_field.side_effect = None
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=6),
    )
    await manager.async_reconcile()

    assert record["code"] == "734567"
    assert record["native_baseline_value"] == "734567"
    assert record["custom_baseline_value"] == "734567"
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_with(
        reservation.id,
        PIN_FIELD_ID,
        "734567",
    )


@pytest.mark.asyncio
async def test_simultaneous_different_manual_edits_prefer_native_keycode(
    hass, monkeypatch
) -> None:
    """Native Keycode deterministically wins simultaneous different edits."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.key_code = "723456"
    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "723456"
    assert record.get("conflict") is None
    assert record["field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "723456",
    )
    remote.async_delete_user.assert_not_awaited()
    assert remote.async_set_access_code.await_args_list == [
        call("user-uuid", "712345"),
        call("user-uuid", "723456"),
    ]


@pytest.mark.asyncio
async def test_unexplained_initial_dual_source_mismatch_prefers_native_keycode(
    hass, monkeypatch
) -> None:
    """Native Keycode seeds an unexplained initial two-source mismatch."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    reservation.custom_fields[PIN_FIELD_ID] = "734567"
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record.get("conflict") is None
    assert record["field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "712345",
    )
    remote.async_set_access_code.assert_awaited_once_with("user-uuid", "712345")


@pytest.mark.asyncio
async def test_changed_custom_field_reference_is_resolved_and_seeded(
    hass, monkeypatch
) -> None:
    """Changing the configured field does not rotate the established PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="712345",
    )
    options = _options()
    options[CONF_PIN_CUSTOM_FIELD] = "{{replacement_pin}}"
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )
    manager._data["resolved_pin_field"] = {
        "reference": "{{door_code}}",
        "id": "65fab102a5284d73c6206db1",
    }
    replacement_field_id = "65fab102a5284d73c6206db2"
    guesty_client.async_resolve_custom_field.return_value = replacement_field_id

    await manager.async_reconcile()

    guesty_client.async_resolve_custom_field.assert_awaited_once_with(
        "{{replacement_pin}}"
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        replacement_field_id,
        "712345",
    )
    assert manager._records[reservation.id]["code"] == "712345"


@pytest.mark.asyncio
async def test_missing_custom_field_resolution_uses_persistent_backoff(
    hass, monkeypatch
) -> None:
    """A missing field cannot create a repeated account-definition read loop."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    guesty_client.async_resolve_custom_field.side_effect = GuestyNotFoundError(
        "missing"
    )

    await manager.async_reconcile()
    await manager.async_reconcile()

    guesty_client.async_resolve_custom_field.assert_awaited_once()
    guesty_client.async_update_reservation_key_code.assert_awaited_once()
    record = manager._records[reservation.id]
    assert record["native_synced"] is True
    assert record["custom_synced"] is False
    assert record["field_synced"] is True
    assert manager._data["pin_field_resolve_retry_count"] == 1


@pytest.mark.asyncio
async def test_confirmed_snapshot_provisions_exact_window_during_guesty_outage(
    hass, monkeypatch
) -> None:
    """Offline mode uses only the last confirmed PIN and validity window."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=10),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    remote.async_add_or_update_user.assert_not_awaited()
    confirmed_end = manager._records[reservation.id]["access_end"]

    reservation.check_out_utc = (NOW + timedelta(days=30)).isoformat()
    coordinator.data.data_stale = True
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(hours=5),
    )
    await manager.async_reconcile()

    remote.async_add_or_update_user.assert_awaited_once()
    assert remote.async_add_or_update_user.await_args.kwargs["valid_until"] == (
        datetime.fromisoformat(confirmed_end)
    )
    assert manager._records[reservation.id]["offline_snapshot_in_use"] is True


@pytest.mark.asyncio
async def test_offline_provisioning_can_be_disabled(hass, monkeypatch) -> None:
    """Administrators can retain strict fail-closed behavior during outages."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=10),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    options = _options()
    options[CONF_PIN_OFFLINE_PROVISIONING] = False
    manager, coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation, options=options
    )
    await manager.async_reconcile()

    coordinator.data.data_stale = True
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(hours=5),
    )
    await manager.async_reconcile()

    remote.async_add_or_update_user.assert_not_awaited()


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
async def test_moving_provisioned_booking_outside_lead_removes_loxone_user(
    hass, monkeypatch
) -> None:
    """A postponed stay cannot retain its former Loxone access window."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    original_code = manager._records[reservation.id]["code"]

    reservation.check_in_utc = (NOW + timedelta(days=10)).isoformat()
    reservation.check_out_utc = (NOW + timedelta(days=12)).isoformat()
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    remote.async_delete_user.assert_awaited_once_with("user-uuid")
    remote.async_add_or_update_user.assert_awaited_once()
    assert manager._records[reservation.id]["code"] == original_code
    assert manager._records[reservation.id]["field_synced"] is True
    assert "user_uuid" not in manager._records[reservation.id]
    assert "fingerprint" not in manager._records[reservation.id]
    assert "code_set" not in manager._records[reservation.id]


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

    reservation.key_code = "799999"
    await manager.async_reconcile()
    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "799999"
    assert manager._records[reservation.id]["guesty_confirmed_code"] == "799999"
    assert original_code != manager._records[reservation.id]["code"]
    guesty_client.async_update_reservation_key_code.assert_awaited_once()
    assert remote.async_set_access_code.await_args_list == [
        call("user-uuid", original_code),
        call("user-uuid", "799999"),
    ]
    snapshot = manager.listing_status_snapshot("listing-1")
    assert snapshot["guesty_status"] == "synced"
    assert snapshot["loxone_status"] == "provisioned"


@pytest.mark.asyncio
async def test_legacy_pending_rotation_is_cancelled_after_guesty_confirmation(
    hass, monkeypatch
) -> None:
    """An upgrade cannot finish an old automatic replacement over Guesty."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
        key_code="711111",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "code": "722222",
        "field_synced": False,
        "field_id": "notes.keyCode",
        "guesty_confirmed_code": "711111",
        "replacement_pending": True,
        "replacement_rejected_code": "711111",
    }

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "711111"
    assert record["guesty_confirmed_code"] == "711111"
    assert record["field_synced"] is True
    assert "replacement_pending" not in record
    assert "replacement_rejected_code" not in record
    guesty_client.async_update_reservation_key_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_sparse_snapshot_cancels_legacy_rotation_without_false_deletion(
    hass, monkeypatch
) -> None:
    """Omitted notes cannot turn cancellation of an old rotation into conflict."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code_observed = False
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "code": "722222",
        "field_synced": False,
        "field_id": "notes.keyCode",
        "guesty_confirmed_code": "711111",
        "replacement_pending": True,
        "replacement_rejected_code": "711111",
    }

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "711111"
    assert record["field_synced"] is True
    assert record.get("conflict") is None
    assert "replacement_pending" not in record
    guesty_client.async_update_reservation_key_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_duplicate_without_stored_pin_gets_unique_replacement(
    hass, monkeypatch
) -> None:
    """A new duplicate gets a generated value when no saved PIN can restore it."""
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
    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert manager._records[first.id]["code"] == "799999"
    assert manager._records[first.id].get("conflict") is None
    replacement = manager._records[second.id]["code"]
    assert replacement != "799999"
    assert len(replacement) == 6 and replacement.isdigit()
    assert manager._records[second.id].get("conflict") is None
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        second.id,
        replacement,
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        second.id,
        PIN_FIELD_ID,
        replacement,
    )
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_duplicate_restores_editor_previous_saved_code(
    hass, monkeypatch
) -> None:
    """A manual duplicate is overwritten with that reservation's saved PIN."""
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
            established.id: {
                "code": "711111",
                "field_synced": True,
                "field_id": "notes.keyCode",
                "guesty_confirmed_code": "711111",
            },
            edited.id: {
                "code": "722222",
                "field_synced": True,
                "field_id": "notes.keyCode",
                "guesty_confirmed_code": "722222",
            },
        }
    }

    await manager.async_reconcile()
    await manager.async_reconcile()

    assert manager._records[established.id]["code"] == "711111"
    assert manager._records[established.id].get("conflict") is None
    assert manager._records[edited.id]["code"] == "722222"
    assert manager._records[edited.id]["guesty_confirmed_code"] == "722222"
    assert manager._records[edited.id].get("conflict") is None
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        edited.id,
        "722222",
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        edited.id,
        PIN_FIELD_ID,
        "722222",
    )


@pytest.mark.asyncio
async def test_persisted_legacy_duplicate_conflict_restores_source_baseline(
    hass, monkeypatch
) -> None:
    """A v2.4.3 duplicate record recovers its safe per-source baseline."""
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
        reservation_id="reservation-2",
    )
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, established
    )
    coordinator.data.reservations.append(edited)
    manager._records[established.id] = {
        "code": "711111",
        "guesty_confirmed_code": "711111",
        "native_baseline_value": "711111",
        "custom_baseline_value": "711111",
        "native_synced": True,
        "custom_synced": True,
        "field_synced": True,
    }
    manager._records[edited.id] = {
        "code": "711111",
        "guesty_confirmed_code": "711111",
        "guesty_display_value": "711111",
        "native_baseline_value": "722222",
        "custom_baseline_value": "722222",
        "native_synced": False,
        "custom_synced": False,
        "field_synced": False,
        "conflict": True,
        "last_error": "guesty_duplicate_keycode",
    }

    await manager.async_reconcile()

    record = manager._records[edited.id]
    assert record["code"] == "722222"
    assert record["guesty_confirmed_code"] == "722222"
    assert record.get("conflict") is None
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        edited.id,
        "722222",
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        edited.id,
        PIN_FIELD_ID,
        "722222",
    )


def test_only_guesty_source_conflicts_block_other_pin_providers(
    hass, monkeypatch
) -> None:
    """A Loxone-local collision must not unnecessarily revoke TTLock access."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    record = {
        "code": "712345",
        "field_synced": True,
        "conflict": True,
        "last_error": "code_conflict",
    }
    manager._records[reservation.id] = record

    assert manager.reservation_pin_snapshot(reservation.id)["field_synced"] is True

    record["last_error"] = "guesty_duplicate_keycode"
    assert manager.reservation_pin_snapshot(reservation.id)["field_synced"] is False


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
    manager._records[reservation.id]["external_rejected_codes"] = ["700001"]
    manager._records[reservation.id].update(
        {
            "guesty_confirmed_code": "712345",
            "guesty_display_value": "712345#",
            "native_baseline_value": "712345#",
            "custom_baseline_value": "712345#",
            "replacement_rejected_code": "700002",
        }
    )
    coordinator.data.reservations = []
    remote.async_delete_user.side_effect = LoxoneApiError("offline")

    await manager.async_reconcile()

    assert "code" not in manager._records[reservation.id]
    assert "external_rejected_codes" not in manager._records[reservation.id]
    for key in (
        "guesty_confirmed_code",
        "guesty_display_value",
        "native_baseline_value",
        "custom_baseline_value",
        "replacement_rejected_code",
    ):
        assert key not in manager._records[reservation.id]
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

    guesty_client.async_update_reservation_key_code.assert_not_awaited()
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

    assert reservation.key_code == "712345"
    assert reservation.id not in manager._records
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_ended_confirmed_reservation_cannot_block_future_custom_mirror(
    hass, monkeypatch
) -> None:
    """A checked-out booking is cleanup-only and cannot starve PIN backfill."""
    ended = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW - timedelta(minutes=1),
        key_code="711111",
        reservation_id="reservation-ended",
    )
    future = _reservation(
        check_in=NOW + timedelta(days=2),
        check_out=NOW + timedelta(days=3),
        key_code="722222",
        reservation_id="reservation-future",
    )
    future.custom_fields = {}
    future.custom_fields_observed = True
    manager, coordinator, guesty_client, _remote = _manager(hass, monkeypatch, future)
    coordinator.data.reservations = [ended, future]

    await manager.async_reconcile()

    assert ended.id not in manager._records
    assert manager._records[future.id]["native_synced"] is True
    assert manager._records[future.id]["custom_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        future.id,
        PIN_FIELD_ID,
        "722222",
    )


@pytest.mark.asyncio
async def test_custom_backfill_is_prioritized_by_nearest_check_in(
    hass, monkeypatch
) -> None:
    """Nearest stays consume bounded mirror slots before distant bookings."""
    reservations = [
        _reservation(
            check_in=NOW + timedelta(days=days),
            check_out=NOW + timedelta(days=days + 1),
            key_code=code,
            reservation_id=f"reservation-{days}",
        )
        for days, code in ((1, "711111"), (2, "722222"), (30, "733333"))
    ]
    for reservation in reservations:
        reservation.custom_fields = {}
        reservation.custom_fields_observed = True
    manager, coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservations[0]
    )
    coordinator.data.reservations = reservations

    await manager.async_reconcile()

    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_custom_field.await_args_list
    ] == ["reservation-1", "reservation-2"]
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    assert manager._records[reservations[0].id]["custom_synced"] is True
    assert manager._records[reservations[1].id]["custom_synced"] is True
    assert manager._records[reservations[2].id]["custom_synced"] is False


@pytest.mark.asyncio
async def test_loxone_collision_keeps_confirmed_guesty_code_unchanged(
    hass, monkeypatch
) -> None:
    """A Miniserver collision is fail-closed without a Guesty rewrite."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_set_access_code.side_effect = LoxoneCodeConflictError("duplicate")

    await manager.async_reconcile()

    remote.async_delete_user.assert_awaited_once_with("user-uuid")
    record = manager._records[reservation.id]
    confirmed = guesty_client.async_update_reservation_key_code.await_args.args[1]
    assert remote.async_add_or_update_user.await_count == 1
    assert remote.async_set_access_code.await_count == 1
    assert guesty_client.async_update_reservation_key_code.await_count == 1
    assert reservation.key_code == confirmed
    assert record["code"] == confirmed
    assert record["guesty_confirmed_code"] == confirmed
    assert record["conflict"] is True
    assert record["last_error"] == "code_conflict"
    assert record.get("loxone_retry_at") is not None


@pytest.mark.asyncio
async def test_repeated_loxone_collisions_never_rotate_confirmed_code(
    hass, monkeypatch
) -> None:
    """Backoff retries a stable PIN without ever rewriting Guesty."""
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
    confirmed = record["code"]
    assert remote.async_set_access_code.await_count == 1
    assert remote.async_delete_user.await_count == 1
    assert guesty_client.async_update_reservation_key_code.await_count == 1
    assert record["field_synced"] is True
    assert record["conflict"] is True
    assert record["last_error"] == "code_conflict"
    assert record.get("loxone_retry_at") is not None

    await manager.async_reconcile()
    assert guesty_client.async_update_reservation_key_code.await_count == 1
    assert remote.async_set_access_code.await_count == 1
    assert record["code"] == confirmed
    assert guesty_client.async_update_reservation_key_code.await_args.args == (
        reservation.id,
        confirmed,
    )


@pytest.mark.asyncio
async def test_one_conflicting_booking_cannot_capture_other_keycode_writes(
    hass, monkeypatch
) -> None:
    """A colliding current stay cannot keep later booking writes on its URL."""
    conflicting = _reservation(
        check_in=NOW + timedelta(hours=1),
        check_out=NOW + timedelta(days=1),
        reservation_id="reservation-conflicting",
    )
    future = _reservation(
        check_in=NOW + timedelta(days=2),
        check_out=NOW + timedelta(days=3),
        reservation_id="reservation-future",
    )
    manager, coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, conflicting
    )
    coordinator.data.reservations.append(future)
    remote.async_set_access_code.side_effect = LoxoneCodeConflictError("duplicate")

    await manager.async_reconcile()

    first_pass = guesty_client.async_update_reservation_key_code.await_args_list.copy()
    assert [item.args[0] for item in first_pass] == [
        conflicting.id,
        future.id,
    ]
    assert manager._records[conflicting.id]["last_error"] == "code_conflict"
    assert manager._records[future.id]["field_synced"] is True

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert [
        item.args[0]
        for item in guesty_client.async_update_reservation_key_code.await_args_list
    ] == [
        conflicting.id,
        future.id,
    ]
    assert guesty_client.async_update_reservation_key_code.await_args.args == (
        future.id,
        manager._records[future.id]["code"],
    )
    assert manager._records[future.id]["field_synced"] is True


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

    assert remote.async_set_access_code.await_count == 1
    assert remote.async_delete_user.await_count == 2
    assert not manager._records[reservation.id].get("code_set")
    assert manager._records[reservation.id].get("collision_cleanup_pending") is None
    assert manager._records[reservation.id]["last_error"] == "code_conflict"


@pytest.mark.asyncio
async def test_loxone_collision_never_attempts_a_guesty_replacement_write(
    hass, monkeypatch
) -> None:
    """Provider rejection cannot consume another Guesty Keycode write."""
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
    confirmed = record["code"]
    assert record["field_synced"] is True
    assert record["guesty_confirmed_code"] == confirmed
    assert record["last_error"] == "code_conflict"
    assert guesty_client.async_update_reservation_key_code.await_count == 1

    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW + timedelta(minutes=6))
    await manager.async_reconcile()

    assert record["code"] == confirmed
    assert record["field_synced"] is True
    assert guesty_client.async_update_reservation_key_code.await_count == 1
    assert guesty_client.async_update_reservation_key_code.await_args.args == (
        reservation.id,
        confirmed,
    )


@pytest.mark.asyncio
async def test_empty_guesty_mirror_is_restored_from_other_source(
    hass, monkeypatch
) -> None:
    """Clearing one mirror restores it without revoking the confirmed PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_code = manager._records[reservation.id]["code"]

    reservation.key_code = ""
    reservation.key_code_observed = True
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == old_code
    assert record["guesty_confirmed_code"] == old_code
    remote.async_delete_user.assert_not_awaited()
    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert record["field_synced"] is True
    assert record.get("conflict") is None


@pytest.mark.asyncio
async def test_both_empty_guesty_mirrors_restore_saved_code(hass, monkeypatch) -> None:
    """Clearing both mirrors restores the saved PIN without revoking access."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.key_code = ""
    reservation.custom_fields[PIN_FIELD_ID] = ""
    reservation.key_code_observed = True
    reservation.custom_fields_observed = True
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["guesty_confirmed_code"] == "712345"
    assert record["field_synced"] is True
    assert record.get("conflict") is None
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        "712345",
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "712345",
    )
    remote.async_delete_user.assert_not_awaited()
    assert remote.async_set_access_code.await_count == 1


@pytest.mark.asyncio
async def test_queued_saved_pin_repair_keeps_existing_provider_access(
    hass, monkeypatch
) -> None:
    """The shared Guesty write limit cannot revoke an already confirmed PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
        key_code="712345",
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    manager._data["guesty_write_attempts"] = [NOW.isoformat(), NOW.isoformat()]

    reservation.key_code = ""
    reservation.custom_fields[PIN_FIELD_ID] = ""
    reservation.key_code_observed = True
    reservation.custom_fields_observed = True
    reservation.last_updated_at = "2026-07-14T12:01:00+00:00"
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == "712345"
    assert record["field_synced"] is True
    assert record["repair_confirmation_pending"] is True
    assert manager.reservation_pin_snapshot(reservation.id)["field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    guesty_client.async_update_reservation_custom_field.assert_not_awaited()
    remote.async_delete_user.assert_not_awaited()
    assert remote.async_set_access_code.await_count == 1

    monkeypatch.setattr(
        loxone.dt_util,
        "utcnow",
        lambda: NOW + timedelta(seconds=31),
    )
    await manager.async_reconcile()

    assert record["field_synced"] is True
    assert "repair_confirmation_pending" not in record
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        "712345",
    )
    guesty_client.async_update_reservation_custom_field.assert_awaited_once_with(
        reservation.id,
        PIN_FIELD_ID,
        "712345",
    )


@pytest.mark.asyncio
async def test_invalid_guesty_edit_restores_saved_code(hass, monkeypatch) -> None:
    """An invalid manual value is overwritten with the last saved PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=5),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_code = manager._records[reservation.id]["code"]

    reservation.key_code = "not-six-digits"
    reservation.key_code_observed = True
    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["code"] == old_code
    assert record["guesty_confirmed_code"] == old_code
    remote.async_delete_user.assert_not_awaited()
    assert guesty_client.async_update_reservation_key_code.await_count == 2
    assert guesty_client.async_update_reservation_key_code.await_args.args == (
        reservation.id,
        old_code,
    )
    assert record["field_synced"] is True
    assert record.get("conflict") is None


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
        "field_id": "notes.keyCode",
        "source_last_updated_at": reservation.last_updated_at,
    }

    await manager.async_reconcile()

    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    remote.async_add_or_update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_reservation_without_notes_projection_still_gets_initial_keycode(
    hass, monkeypatch
) -> None:
    """An undocumented omitted notes object does not block initial provisioning."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=10),
        check_out=NOW + timedelta(days=12),
    )
    reservation.key_code_observed = False
    manager, _coordinator, guesty_client, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    generated = manager._records[reservation.id]["code"]
    assert generated.isdigit() and len(generated) == 6
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id,
        generated,
    )
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
        return_value={
            "records": {
                "valid": {
                    "code": "712345",
                    "last_error": "guesty_custom_field_rejected",
                    "guesty_retry_at": "2026-07-15T12:00:00+00:00",
                    "guesty_retry_count": 3,
                },
                "invalid": [],
            },
            "resolved_field": {"reference": "{{door_code}}", "id": "old-field"},
        }
    )

    data = await storage.async_load()

    assert data == {
        "records": {"valid": {"code": "712345"}},
    }


@pytest.mark.parametrize("invalid_prefix", ["123", "٧"])
def test_invalid_prefixes_are_rejected(hass, monkeypatch, invalid_prefix) -> None:
    """Prefixes must reserve enough codes and contain only ASCII digits."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    options = _options()
    options[CONF_LOXONE_CODE_PREFIX] = invalid_prefix
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation, options=options
    )

    with pytest.raises(ValueError, match="prefix"):
        manager._generate_code()


def test_external_conflict_codes_are_not_generated_again(hass, monkeypatch) -> None:
    """Previously rejected provider codes remain excluded for the reservation."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
    )
    manager, _coordinator, _guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "code": "799999",
        "external_rejected_codes": ["712345"],
    }
    monkeypatch.setattr(loxone.secrets, "randbelow", lambda _capacity: 12345)

    assert manager._generate_code() == "712346"


@pytest.mark.asyncio
async def test_stale_external_conflict_cannot_overwrite_newer_guesty_keycode(
    hass, monkeypatch
) -> None:
    """A delayed provider collision is ignored after Guesty changed the PIN."""
    reservation = _reservation(
        check_in=NOW + timedelta(days=1),
        check_out=NOW + timedelta(days=2),
        key_code="799999",
    )
    manager, _coordinator, guesty_client, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "code": "712345",
        "field_synced": True,
        "field_id": "notes.keyCode",
    }
    manager.async_schedule_reconcile = MagicMock()

    rotated = await manager.async_rotate_external_conflict(
        reservation.id,
        "712345",
    )

    assert rotated is False
    assert manager._records[reservation.id]["code"] == "712345"
    guesty_client.async_update_reservation_key_code.assert_not_awaited()
    manager.async_schedule_reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_integration_removal_erases_credentials_after_delete_failure(
    hass, monkeypatch
) -> None:
    """Failed final cleanup never orphans credentials after entry deletion."""
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
    storage.async_remove.assert_awaited_once_with()
    storage.async_save.assert_awaited_once_with(data)
