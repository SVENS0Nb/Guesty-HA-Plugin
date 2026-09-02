"""Tests for reservation-driven TTLock PIN provisioning."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import loxone, ttlock
from custom_components.guesty.const import (
    CONF_ACCESS_EARLY_MINUTES,
    CONF_ACCESS_LATE_MINUTES,
    CONF_GUESTY_CODE_SUFFIXES,
    CONF_LOXONE_CODE_PREFIX,
    CONF_LOXONE_ENABLED,
    CONF_TTLOCK_ACCESS_TOKEN,
    CONF_TTLOCK_ACCOUNT,
    CONF_TTLOCK_CLIENT_ID,
    CONF_TTLOCK_CLIENT_SECRET,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    CONF_TTLOCK_LOCK_ID,
    CONF_TTLOCK_LOCK_IDS,
    CONF_TTLOCK_LOCK_NAME,
    CONF_TTLOCK_LOCKS,
    CONF_TTLOCK_PROVISION_LEAD_MINUTES,
    CONF_TTLOCK_REFRESH_TOKEN,
    CONF_TTLOCK_REGION,
    CONF_TTLOCK_TOKEN_EXPIRES_AT,
    CONF_TTLOCK_USERNAME,
    DOMAIN,
)
from custom_components.guesty.loxone import GuestyLoxoneManager
from custom_components.guesty.models import GuestyListing, GuestyReservation
from custom_components.guesty.ttlock import (
    GuestyTTLockManager,
    GuestyTTLockStorage,
    async_remove_stored_ttlock_passcodes,
)
from custom_components.guesty.ttlock_api import TTLockApiError, TTLockGatewayError

NOW = datetime.fromisoformat("2026-07-20T12:00:00+00:00")
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
    check_in: datetime = NOW + timedelta(hours=1),
    check_out: datetime = NOW + timedelta(days=2),
) -> GuestyReservation:
    reservation = GuestyReservation.from_api(
        {
            "_id": "reservation-1",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": check_in.isoformat(),
            "checkOut": check_out.isoformat(),
            "lastUpdatedAt": NOW.isoformat(),
            "customFields": [],
        }
    )
    assert reservation is not None
    return reservation


def _options(lock_ids: list[int] | None = None) -> dict:
    lock_ids = lock_ids or [101, 102]
    return {
        CONF_TTLOCK_ENABLED: True,
        CONF_TTLOCK_PROVISION_LEAD_MINUTES: 360,
        CONF_TTLOCK_ACCOUNT: {
            CONF_TTLOCK_REGION: "eu",
            CONF_TTLOCK_CLIENT_ID: "client",
            CONF_TTLOCK_CLIENT_SECRET: "secret",
            CONF_TTLOCK_USERNAME: "owner@example.com",
            CONF_TTLOCK_ACCESS_TOKEN: "access",
            CONF_TTLOCK_REFRESH_TOKEN: "refresh",
        },
        CONF_TTLOCK_LOCKS: [
            {CONF_TTLOCK_LOCK_ID: lock_id, CONF_TTLOCK_LOCK_NAME: f"Lock {lock_id}"}
            for lock_id in lock_ids
        ],
        CONF_TTLOCK_LISTING_MAPPINGS: {"listing-1": {CONF_TTLOCK_LOCK_IDS: lock_ids}},
    }


def _manager(
    hass,
    monkeypatch,
    reservation: GuestyReservation,
    *,
    entry=None,
    coordinator=None,
    pin_manager=None,
):
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, options=_options())
        entry.add_to_hass(hass)
    if coordinator is None:
        coordinator = SimpleNamespace(
            data=SimpleNamespace(
                listings={"listing-1": _listing()},
                reservations=[reservation],
                data_stale=False,
            )
        )
    if pin_manager is None:
        pin_manager = SimpleNamespace(
            reservation_access_window=lambda item, _listing: (
                item.check_in_datetime(_listing),
                item.check_out_datetime(_listing),
            ),
            reservation_pin_snapshot=lambda _reservation_id: {
                "code": "712345",
                "field_synced": True,
            },
            async_rotate_external_conflict=AsyncMock(return_value=True),
        )
    manager = GuestyTTLockManager(hass, entry, coordinator, pin_manager)
    manager._data = {"records": {}, "tokens": {}}
    manager._storage.async_save = AsyncMock()
    manager._schedule_at = MagicMock()
    next_id = iter([1001, 1002, 1003, 1004])

    remote_entries: dict[int, list[dict]] = {}

    async def _list_passcodes(lock_id: int) -> list[dict]:
        return [dict(item) for item in remote_entries.get(lock_id, [])]

    async def _add_passcode(**kwargs) -> int:
        password_id = next(next_id)
        remote_entries.setdefault(kwargs["lock_id"], []).append(
            {
                "keyboardPwdId": password_id,
                "keyboardPwd": kwargs["code"],
                "keyboardPwdName": kwargs["name"],
                "startDate": int(kwargs["valid_from"].timestamp() * 1000),
                "endDate": int(kwargs["valid_until"].timestamp() * 1000),
                "status": 1,
            }
        )
        return password_id

    async def _change_passcode(**kwargs) -> None:
        for item in remote_entries.get(kwargs["lock_id"], []):
            if item["keyboardPwdId"] == kwargs["password_id"]:
                item.update(
                    {
                        "keyboardPwd": kwargs["code"],
                        "keyboardPwdName": kwargs["name"],
                        "startDate": int(kwargs["valid_from"].timestamp() * 1000),
                        "endDate": int(kwargs["valid_until"].timestamp() * 1000),
                        "status": 1,
                    }
                )
                return
        raise TTLockApiError("missing passcode")

    async def _delete_passcode(**kwargs) -> None:
        entries = remote_entries.get(kwargs["lock_id"], [])
        remote_entries[kwargs["lock_id"]] = [
            item for item in entries if item["keyboardPwdId"] != kwargs["password_id"]
        ]

    remote = SimpleNamespace(
        region="eu",
        client_id="client",
        client_secret="secret",
        username="owner@example.com",
        token_snapshot=lambda: {
            CONF_TTLOCK_ACCESS_TOKEN: "access",
            CONF_TTLOCK_REFRESH_TOKEN: "refresh",
            "token_expires_at": "",
        },
        entries=remote_entries,
        async_list_passcodes=AsyncMock(side_effect=_list_passcodes),
        async_add_passcode=AsyncMock(side_effect=_add_passcode),
        async_change_passcode=AsyncMock(side_effect=_change_passcode),
        async_delete_passcode=AsyncMock(side_effect=_delete_passcode),
    )
    manager._client = remote
    monkeypatch.setattr(ttlock.dt_util, "utcnow", lambda: NOW)
    return manager, coordinator, pin_manager, remote


@pytest.mark.asyncio
async def test_unload_clears_pending_reconcile_task(hass, monkeypatch) -> None:
    """Reload cannot leave a TTLock worker or pending follow-up pass behind."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, _remote = _manager(
        hass, monkeypatch, reservation
    )
    manager._pending = True
    manager._task = hass.async_create_task(
        asyncio.Event().wait(),
        "test_guesty_ttlock_reconcile",
    )
    await asyncio.sleep(0)

    await manager.async_unload()

    assert manager._task is None
    assert manager._pending is False
    assert manager._unloaded is True


@pytest.mark.asyncio
async def test_setup_requeues_old_retroactive_start_failure_once(
    hass, monkeypatch
) -> None:
    """The fixed path is not stranded behind an old active-stay retry."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW + timedelta(days=1),
    )
    manager, _coordinator, pin_manager, _remote = _manager(
        hass, monkeypatch, reservation
    )
    old_data = {
        "records": {
            reservation.id: {
                "listing_id": reservation.listing_id,
                "access_start": reservation.check_in_utc,
                "access_end": reservation.check_out_utc,
                "locks": {"101": {"keyboard_pwd_id": 1001}},
                "last_error": "ttlock_api_error",
                "retry_count": 5,
                "retry_at": (NOW + timedelta(hours=1)).isoformat(),
            }
        },
        "tokens": {},
    }
    manager._storage.async_load = AsyncMock(return_value=deepcopy(old_data))
    manager._storage.async_save = AsyncMock()
    manager.async_schedule_reconcile = MagicMock()
    pin_manager.async_add_listener = MagicMock(return_value=MagicMock())

    await manager.async_setup()

    record = manager._records[reservation.id]
    assert "retry_at" not in record
    assert "retry_count" not in record
    assert "last_error" not in record
    assert manager._data["retroactive_start_state_version"] == 1
    manager._storage.async_save.assert_awaited_once()

    saved = deepcopy(manager._data)
    manager._data = old_data
    manager._data["retroactive_start_state_version"] = 1
    recovered, changed = manager._migrate_retroactive_start_state(NOW)

    assert (recovered, changed) == (0, False)
    assert manager._records[reservation.id]["retry_at"]
    manager._data = saved


@pytest.mark.asyncio
async def test_future_reservation_defers_ttlock_without_extra_guesty_poll(
    hass, monkeypatch
) -> None:
    """Future Guesty codes exist immediately but do not fill TTLock early."""
    reservation = _reservation(check_in=NOW + timedelta(days=10))
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    remote.async_list_passcodes.assert_not_awaited()
    remote.async_add_passcode.assert_not_awaited()
    assert manager._records[reservation.id]["provision_at"]


@pytest.mark.asyncio
async def test_confirmed_ttlock_schedule_survives_guesty_outage_without_extension(
    hass, monkeypatch
) -> None:
    """TTLock uses the private confirmed window, never changed stale API data."""
    reservation = _reservation(
        check_in=NOW + timedelta(hours=10),
        check_out=NOW + timedelta(days=2),
    )
    confirmed_start = reservation.check_in_datetime(_listing())
    confirmed_end = reservation.check_out_datetime(_listing())
    pin_manager = SimpleNamespace(
        reservation_access_window=lambda _item, _listing: (
            confirmed_start,
            confirmed_end,
        ),
        reservation_pin_snapshot=lambda _reservation_id: {
            "code": "712345",
            "field_synced": True,
            "access_start": confirmed_start.isoformat(),
            "access_end": confirmed_end.isoformat(),
        },
        offline_reservation_snapshots=lambda: [(reservation, _listing())],
        async_rotate_external_conflict=AsyncMock(return_value=False),
    )
    manager, coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        pin_manager=pin_manager,
    )
    await manager.async_reconcile()
    remote.async_add_passcode.assert_not_awaited()

    reservation.check_out_utc = (NOW + timedelta(days=30)).isoformat()
    coordinator.data.data_stale = True
    monkeypatch.setattr(
        ttlock.dt_util,
        "utcnow",
        lambda: NOW + timedelta(hours=5),
    )
    await manager.async_reconcile()

    remote.async_add_passcode.assert_awaited()
    assert all(
        item.kwargs["valid_from"] == confirmed_start
        and item.kwargs["valid_until"] == confirmed_end
        for item in remote.async_add_passcode.await_args_list
    )


@pytest.mark.asyncio
async def test_malformed_reservation_does_not_block_valid_ttlock_delivery(
    hass, monkeypatch
) -> None:
    """One invalid Guesty interval is isolated from every valid reservation."""
    valid = _reservation()
    invalid = _reservation()
    invalid.id = "reservation-invalid"
    invalid.check_in_utc = "not-a-date"
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[invalid, valid],
            data_stale=False,
        )
    )
    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        valid,
        coordinator=coordinator,
    )

    await manager.async_reconcile()

    assert remote.async_add_passcode.await_count == 2
    assert manager.diagnostics()["last_reconcile_result"] == "partial"
    assert "reservation-invalid" not in manager._records


@pytest.mark.asyncio
async def test_same_guesty_code_is_installed_on_every_mapped_lock(
    hass, monkeypatch
) -> None:
    """TTLock receives one shared code and exact booking window per lock."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )

    await manager.async_reconcile()

    assert remote.async_add_passcode.await_count == 2
    assert {
        call.kwargs["lock_id"] for call in remote.async_add_passcode.await_args_list
    } == {101, 102}
    assert all(
        call.kwargs["code"] == "712345"
        for call in remote.async_add_passcode.await_args_list
    )
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == (
        "provisioned"
    )


@pytest.mark.asyncio
async def test_guesty_confirmation_suffix_is_never_sent_to_ttlock(
    hass, monkeypatch
) -> None:
    """The real shared PIN manager strips Guesty's display-only keypad key."""
    reservation = _reservation()
    options = {
        **_options([101]),
        CONF_LOXONE_ENABLED: False,
        CONF_LOXONE_CODE_PREFIX: "7",
        CONF_ACCESS_EARLY_MINUTES: 0,
        CONF_ACCESS_LATE_MINUTES: 0,
        CONF_GUESTY_CODE_SUFFIXES: {"listing-1": "#"},
    }
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
            data_stale=False,
        )
    )
    guesty_client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value=PIN_FIELD_ID),
        async_get_reservation_custom_field=AsyncMock(return_value=None),
        async_update_reservation_custom_field=AsyncMock(),
        async_update_reservation_key_code=AsyncMock(),
    )
    pin_manager = GuestyLoxoneManager(hass, entry, guesty_client, coordinator)
    pin_manager._data = {"records": {}}
    pin_manager._storage.async_save = AsyncMock()
    pin_manager._schedule_at = MagicMock()
    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW)

    await pin_manager.async_reconcile()

    code = pin_manager.reservation_pin_snapshot(reservation.id)["code"]
    guesty_client.async_update_reservation_key_code.assert_awaited_once_with(
        reservation.id, f"{code}#"
    )

    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        entry=entry,
        coordinator=coordinator,
        pin_manager=pin_manager,
    )
    await manager.async_reconcile()

    remote.async_add_passcode.assert_awaited_once()
    assert remote.async_add_passcode.await_args.kwargs["code"] == code
    assert "#" not in remote.async_add_passcode.await_args.kwargs["code"]


@pytest.mark.asyncio
async def test_existing_guesty_keycode_without_private_record_reaches_ttlock(
    hass, monkeypatch
) -> None:
    """TTLock receives Guesty's existing PIN without a local record or rewrite."""
    reservation = _reservation()
    reservation.key_code = "734567"
    reservation.key_code_observed = True
    options = {
        **_options([101]),
        CONF_LOXONE_ENABLED: False,
        CONF_LOXONE_CODE_PREFIX: "7",
        CONF_ACCESS_EARLY_MINUTES: 0,
        CONF_ACCESS_LATE_MINUTES: 0,
    }
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
            data_stale=False,
        )
    )
    guesty_client = SimpleNamespace(
        async_resolve_custom_field=AsyncMock(return_value=PIN_FIELD_ID),
        async_get_reservation_custom_field=AsyncMock(return_value=None),
        async_update_reservation_custom_field=AsyncMock(),
        async_update_reservation_key_code=AsyncMock(),
    )
    pin_manager = GuestyLoxoneManager(hass, entry, guesty_client, coordinator)
    pin_manager._data = {"records": {}}
    pin_manager._storage.async_save = AsyncMock()
    pin_manager._schedule_at = MagicMock()
    monkeypatch.setattr(loxone.dt_util, "utcnow", lambda: NOW)

    await pin_manager.async_reconcile()

    assert pin_manager.reservation_pin_snapshot(reservation.id)["code"] == "734567"
    assert pin_manager.reservation_pin_snapshot(reservation.id)["field_synced"] is True
    guesty_client.async_update_reservation_key_code.assert_not_awaited()

    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        entry=entry,
        coordinator=coordinator,
        pin_manager=pin_manager,
    )
    await manager.async_reconcile()

    remote.async_add_passcode.assert_awaited_once()
    assert remote.async_add_passcode.await_args.kwargs["code"] == "734567"


@pytest.mark.asyncio
async def test_current_stay_uses_one_persisted_retroactive_ttlock_start(
    hass, monkeypatch
) -> None:
    """A late setup starts now once and stays stable across manager reloads."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW + timedelta(days=1),
    )
    manager, coordinator, pin_manager, remote = _manager(hass, monkeypatch, reservation)

    await manager.async_reconcile()

    assert remote.async_add_passcode.await_count == 2
    assert all(
        call.kwargs["valid_from"] == NOW
        and call.kwargs["valid_until"] == NOW + timedelta(days=1)
        for call in remote.async_add_passcode.await_args_list
    )
    assert {
        state["remote_valid_from"]
        for state in manager._records[reservation.id]["locks"].values()
    } == {NOW.isoformat()}
    assert all(
        state["remote_valid_from_clamped"] is True
        for state in manager._records[reservation.id]["locks"].values()
    )

    reloaded = GuestyTTLockManager(
        hass,
        manager.entry,
        coordinator,
        pin_manager,
    )
    reloaded._data = deepcopy(manager._data)
    reloaded._client = remote
    reloaded._storage.async_save = AsyncMock()
    reloaded._schedule_at = MagicMock()
    monkeypatch.setattr(
        ttlock.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=15),
    )

    await reloaded.async_reconcile()

    assert remote.async_add_passcode.await_count == 2
    remote.async_change_passcode.assert_not_awaited()
    assert {
        state["remote_valid_from"]
        for state in reloaded._records[reservation.id]["locks"].values()
    } == {NOW.isoformat()}


@pytest.mark.asyncio
async def test_partial_current_stay_retry_reuses_first_retroactive_start(
    hass, monkeypatch
) -> None:
    """An offline second lock cannot make the late start drift on retry."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW + timedelta(days=1),
    )
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    original_add = remote.async_add_passcode.side_effect
    failed_once = False

    async def _fail_second_lock_once(**kwargs):
        nonlocal failed_once
        if kwargs["lock_id"] == 102 and not failed_once:
            failed_once = True
            raise TTLockGatewayError("offline")
        return await original_add(**kwargs)

    remote.async_add_passcode.side_effect = _fail_second_lock_once

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["last_error"] == "gateway_unavailable"
    assert record["locks"]["101"]["remote_valid_from"] == NOW.isoformat()
    assert record["locks"]["102"]["remote_valid_from"] == NOW.isoformat()

    monkeypatch.setattr(
        ttlock.dt_util,
        "utcnow",
        lambda: NOW + timedelta(minutes=5),
    )
    await manager.async_reconcile()

    lock_two_calls = [
        call
        for call in remote.async_add_passcode.await_args_list
        if call.kwargs["lock_id"] == 102
    ]
    assert len(lock_two_calls) == 2
    assert all(call.kwargs["valid_from"] == NOW for call in lock_two_calls)
    assert (
        manager._records[reservation.id]["locks"]["102"]["remote_valid_from"]
        == NOW.isoformat()
    )


@pytest.mark.asyncio
async def test_upgrade_preserves_confirmed_current_stay_start(
    hass, monkeypatch
) -> None:
    """A healthy passcode from an older release is never rewritten as late."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW + timedelta(days=1),
    )
    entry = MockConfigEntry(domain=DOMAIN, options=_options([101]))
    entry.add_to_hass(hass)
    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        entry=entry,
    )
    start = reservation.check_in_datetime(_listing())
    end = reservation.check_out_datetime(_listing())
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "access_start": start.isoformat(),
        "access_end": end.isoformat(),
        "locks": {
            "101": {
                "keyboard_pwd_id": 1001,
                "fingerprint": manager._fingerprint(101, "712345", start, end),
                "verified_at": NOW.isoformat(),
            }
        },
    }

    await manager.async_reconcile()

    remote.async_list_passcodes.assert_not_awaited()
    remote.async_add_passcode.assert_not_awaited()
    remote.async_change_passcode.assert_not_awaited()
    state = manager._records[reservation.id]["locks"]["101"]
    assert state["remote_valid_from"] == start.isoformat()
    assert state["remote_valid_from_clamped"] is False


@pytest.mark.asyncio
async def test_missing_confirmed_code_is_recreated_with_current_start(
    hass, monkeypatch
) -> None:
    """A disappeared legacy passcode is recovered without a stale start."""
    reservation = _reservation(
        check_in=NOW - timedelta(days=2),
        check_out=NOW + timedelta(days=1),
    )
    entry = MockConfigEntry(domain=DOMAIN, options=_options([101]))
    entry.add_to_hass(hass)
    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        entry=entry,
    )
    start = reservation.check_in_datetime(_listing())
    end = reservation.check_out_datetime(_listing())
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "access_start": start.isoformat(),
        "access_end": end.isoformat(),
        "locks": {
            "101": {
                "keyboard_pwd_id": 999,
                "fingerprint": manager._fingerprint(101, "712345", start, end),
                "verified_at": (NOW - timedelta(minutes=31)).isoformat(),
            }
        },
    }

    await manager.async_reconcile()

    remote.async_add_passcode.assert_awaited_once()
    assert remote.async_add_passcode.await_args.kwargs["valid_from"] == NOW
    state = manager._records[reservation.id]["locks"]["101"]
    assert state["remote_valid_from"] == NOW.isoformat()
    assert state["remote_valid_from_clamped"] is True


@pytest.mark.asyncio
async def test_booking_time_change_updates_existing_passcodes(
    hass, monkeypatch
) -> None:
    """Changed Guesty check-in/out times retain IDs and update TTLock periods."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    reservation.check_out_utc = (NOW + timedelta(days=3)).isoformat()

    await manager.async_reconcile()

    assert remote.async_change_passcode.await_count == 2
    assert remote.async_add_passcode.await_count == 2
    assert all(
        call.kwargs["valid_until"] == NOW + timedelta(days=3)
        for call in remote.async_change_passcode.await_args_list
    )


@pytest.mark.asyncio
async def test_planned_time_change_is_pending_until_ttlock_confirms_new_window(
    hass, monkeypatch
) -> None:
    """Manual Guesty planned times invalidate a recently verified TTLock window."""
    reservation = GuestyReservation.from_api(
        {
            "_id": "reservation-1",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": "2026-07-20T13:00:00+00:00",
            "checkOut": "2026-07-22T12:00:00+00:00",
            "checkInDateLocalized": "2026-07-20",
            "checkOutDateLocalized": "2026-07-22",
            "plannedArrival": "13:00",
            "plannedDeparture": "12:00",
            "lastUpdatedAt": NOW.isoformat(),
            "customFields": [],
        }
    )
    assert reservation is not None
    manager, coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    updated = GuestyReservation.from_api(
        {
            "_id": "reservation-1",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": "2026-07-20T13:00:00+00:00",
            "checkOut": "2026-07-22T12:00:00+00:00",
            "checkInDateLocalized": "2026-07-20",
            "checkOutDateLocalized": "2026-07-22",
            "plannedArrival": "14:30",
            "plannedDeparture": "09:15",
            "lastUpdatedAt": (NOW + timedelta(minutes=1)).isoformat(),
            "customFields": [],
        }
    )
    assert updated is not None
    coordinator.data.reservations = [updated]

    pending = manager.listing_status_snapshot("listing-1")
    assert pending["ttlock_status"] == "pending"
    assert pending["provisioned_locks"] == 0
    assert manager.diagnostics()["pending_window_updates"] == 1

    await manager.async_reconcile()

    expected_start = datetime.fromisoformat("2026-07-20T14:30:00+00:00")
    expected_end = datetime.fromisoformat("2026-07-22T09:15:00+00:00")
    assert remote.async_change_passcode.await_count == 2
    assert all(
        call.kwargs["valid_from"] == expected_start
        and call.kwargs["valid_until"] == expected_end
        for call in remote.async_change_passcode.await_args_list
    )
    record = manager._records[updated.id]
    assert all(
        state["confirmed_window_fingerprint"] == record["desired_window_fingerprint"]
        for state in record["locks"].values()
    )
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == (
        "provisioned"
    )
    assert manager.diagnostics()["pending_window_updates"] == 0


@pytest.mark.asyncio
async def test_current_stay_planned_time_change_preserves_clamp_and_updates_checkout(
    hass, monkeypatch
) -> None:
    """A current stay keeps its safe start while a new Guesty end is delivered."""
    reservation = GuestyReservation.from_api(
        {
            "_id": "reservation-1",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": "2026-07-18T10:00:00+00:00",
            "checkOut": "2026-07-21T12:00:00+00:00",
            "checkInDateLocalized": "2026-07-18",
            "checkOutDateLocalized": "2026-07-21",
            "plannedArrival": "10:00",
            "plannedDeparture": "12:00",
            "lastUpdatedAt": NOW.isoformat(),
            "customFields": [],
        }
    )
    assert reservation is not None
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()

    reservation.planned_arrival = "11:30"
    reservation.planned_departure = "15:30"
    await manager.async_reconcile()

    expected_end = datetime.fromisoformat("2026-07-21T15:30:00+00:00")
    assert remote.async_change_passcode.await_count == 2
    assert all(
        call.kwargs["valid_from"] == NOW and call.kwargs["valid_until"] == expected_end
        for call in remote.async_change_passcode.await_args_list
    )
    assert {
        state["remote_valid_from"]
        for state in manager._records[reservation.id]["locks"].values()
    } == {NOW.isoformat()}


@pytest.mark.asyncio
async def test_booking_moved_beyond_lead_removes_early_remote_passcodes(
    hass, monkeypatch
) -> None:
    """A postponed stay cannot leave the previous TTLock access window active."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    reservation.check_in_utc = (NOW + timedelta(days=10)).isoformat()
    reservation.check_out_utc = (NOW + timedelta(days=12)).isoformat()

    await manager.async_reconcile()

    assert remote.async_delete_passcode.await_count == 2
    assert remote.async_change_passcode.await_count == 0
    assert manager._records[reservation.id]["locks"] == {}
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == (
        "scheduled"
    )


@pytest.mark.asyncio
async def test_remote_duplicate_never_rotates_authoritative_guesty_code(
    hass, monkeypatch
) -> None:
    """A TTLock collision is fail-closed without changing Guesty's PIN."""
    reservation = _reservation()
    manager, _coordinator, pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_list_passcodes.side_effect = None
    remote.async_list_passcodes.return_value = [
        {"keyboardPwdId": 999, "keyboardPwd": "712345", "keyboardPwdName": "Other"}
    ]

    await manager.async_reconcile()

    pin_manager.async_rotate_external_conflict.assert_not_awaited()
    remote.async_add_passcode.assert_not_awaited()
    assert manager._records[reservation.id]["last_error"] == "code_conflict"
    assert manager._records[reservation.id]["retry_at"]


@pytest.mark.asyncio
async def test_repeated_remote_conflicts_keep_guesty_code_immutable(
    hass, monkeypatch
) -> None:
    """Repeated TTLock conflicts never request a Guesty code rotation."""
    reservation = _reservation()
    manager, _coordinator, pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_list_passcodes.side_effect = None
    remote.async_list_passcodes.return_value = [
        {"keyboardPwdId": 999, "keyboardPwd": "712345", "keyboardPwdName": "Other"}
    ]

    for _attempt in range(4):
        await manager.async_reconcile()

    pin_manager.async_rotate_external_conflict.assert_not_awaited()
    record = manager._records[reservation.id]
    assert "conflict_rotation_times" not in record
    assert record["last_error"] == "code_conflict"
    assert record["retry_at"]


@pytest.mark.asyncio
async def test_guesty_pin_conflict_revokes_existing_ttlock_passcodes(
    hass, monkeypatch
) -> None:
    """A blocked Guesty PIN cannot leave previously delivered TTLock access."""
    reservation = _reservation()
    manager, _coordinator, pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    assert len(manager._records[reservation.id]["locks"]) == 2

    pin_manager.reservation_pin_snapshot = MagicMock(
        return_value={"code": "712345", "field_synced": False}
    )
    await manager.async_reconcile()

    assert remote.async_delete_passcode.await_count == 2
    assert manager._records[reservation.id]["locks"] == {}
    assert manager._records[reservation.id]["last_error"] == "guesty_pin_pending"
    pin_manager.async_rotate_external_conflict.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_gateway_failure_keeps_successful_lock_for_targeted_retry(
    hass, monkeypatch
) -> None:
    """One offline lock does not repeat or discard successful lock writes."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    original_add = remote.async_add_passcode.side_effect

    async def _add_with_partial_failure(**kwargs):
        if kwargs["lock_id"] == 102:
            raise TTLockGatewayError("offline")
        return await original_add(**kwargs)

    remote.async_add_passcode.side_effect = _add_with_partial_failure

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["locks"]["101"]["keyboard_pwd_id"] == 1001
    assert "keyboard_pwd_id" not in record["locks"]["102"]
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == "partial"


@pytest.mark.asyncio
async def test_first_offline_lock_does_not_block_later_mapped_locks(
    hass, monkeypatch
) -> None:
    """Lock delivery errors are isolated within one reservation mapping."""
    reservation = _reservation()
    entry = MockConfigEntry(domain=DOMAIN, options=_options([101, 102, 103]))
    entry.add_to_hass(hass)
    manager, _coordinator, _pin_manager, remote = _manager(
        hass,
        monkeypatch,
        reservation,
        entry=entry,
    )
    original_add = remote.async_add_passcode.side_effect

    async def _fail_first_lock(**kwargs):
        if kwargs["lock_id"] == 101:
            raise TTLockGatewayError("offline")
        return await original_add(**kwargs)

    remote.async_add_passcode.side_effect = _fail_first_lock

    await manager.async_reconcile()

    locks = manager._records[reservation.id]["locks"]
    assert "keyboard_pwd_id" not in locks["101"]
    assert locks["101"]["last_error"] == "gateway_unavailable"
    assert isinstance(locks["102"]["keyboard_pwd_id"], int)
    assert isinstance(locks["103"]["keyboard_pwd_id"], int)


@pytest.mark.asyncio
async def test_ambiguous_add_response_recovers_by_private_reservation_marker(
    hass, monkeypatch
) -> None:
    """A lost success response is adopted instead of creating or rotating again."""
    reservation = _reservation()
    manager, _coordinator, pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    marker = manager._passcode_name(reservation.id)
    start = reservation.check_in_datetime(_listing())
    end = reservation.check_out_datetime(_listing())
    recovered = {
        "keyboardPwdId": 7001,
        "keyboardPwd": "712345",
        "keyboardPwdName": marker,
        "startDate": int(start.timestamp() * 1000),
        "endDate": int(end.timestamp() * 1000),
    }
    remote.async_list_passcodes.side_effect = [
        [],
        [recovered],
        [recovered],
        [],
        [recovered],
        [recovered],
    ]
    remote.async_add_passcode.side_effect = [
        TTLockApiError("response lost"),
        TTLockApiError("response lost"),
    ]

    await manager.async_reconcile()

    assert manager._records[reservation.id]["locks"]["101"]["keyboard_pwd_id"] == 7001
    pin_manager.async_rotate_external_conflict.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_deletion_is_detected_and_recreated(hass, monkeypatch) -> None:
    """Periodic verification repairs a passcode removed in the TTLock app."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_id = manager._records[reservation.id]["locks"]["101"]["keyboard_pwd_id"]
    remote.entries[101] = []
    monkeypatch.setattr(ttlock.dt_util, "utcnow", lambda: NOW + timedelta(minutes=31))

    await manager.async_reconcile()

    state = manager._records[reservation.id]["locks"]["101"]
    assert state["keyboard_pwd_id"] != old_id
    assert remote.async_add_passcode.await_count == 3
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == (
        "provisioned"
    )


@pytest.mark.asyncio
async def test_failed_remote_status_is_replaced_not_reported_ready(
    hass, monkeypatch
) -> None:
    """A TTLock add-failed record is deleted and safely recreated."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_id = manager._records[reservation.id]["locks"]["101"]["keyboard_pwd_id"]
    remote.entries[101][0]["status"] = 5
    monkeypatch.setattr(ttlock.dt_util, "utcnow", lambda: NOW + timedelta(minutes=31))

    await manager.async_reconcile()

    state = manager._records[reservation.id]["locks"]["101"]
    assert state["keyboard_pwd_id"] != old_id
    assert all(item["status"] == 1 for item in remote.entries[101])


@pytest.mark.asyncio
async def test_pending_remote_status_is_not_reported_provisioned(
    hass, monkeypatch
) -> None:
    """A gateway operation still in progress remains visibly pending."""
    reservation = _reservation()
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    remote.entries[101][0]["status"] = 4
    monkeypatch.setattr(ttlock.dt_util, "utcnow", lambda: NOW + timedelta(minutes=31))

    await manager.async_reconcile()

    record = manager._records[reservation.id]
    assert record["last_error"] == "operation_pending"
    assert record["retry_at"] == (NOW + timedelta(minutes=31, seconds=30)).isoformat()
    assert manager.listing_status_snapshot("listing-1")["ttlock_status"] == "pending"


@pytest.mark.asyncio
async def test_changed_marker_is_never_deleted(hass, monkeypatch) -> None:
    """A stale local ID cannot authorize deletion of a foreign passcode."""
    reservation = _reservation()
    manager, coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    old_id = manager._records[reservation.id]["locks"]["101"]["keyboard_pwd_id"]
    remote.entries[101][0]["keyboardPwdName"] = "Manually managed"
    coordinator.data.reservations = []

    await manager.async_reconcile()

    assert any(item["keyboardPwdId"] == old_id for item in remote.entries[101])
    assert all(
        call.kwargs["password_id"] != old_id
        for call in remote.async_delete_passcode.await_args_list
    )


@pytest.mark.asyncio
async def test_disabling_ttlock_uses_snapshot_client_for_cleanup(
    hass, monkeypatch
) -> None:
    """Disabling the provider immediately removes its still-active codes."""
    reservation = _reservation()
    manager, coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    manager._records[reservation.id]["retry_at"] = (
        NOW + timedelta(hours=1)
    ).isoformat()
    hass.config_entries.async_update_entry(
        manager.entry,
        options={**manager.entry.options, CONF_TTLOCK_ENABLED: False},
    )
    manager._client = None
    manager._client_from_account = MagicMock(return_value=remote)
    coordinator.data.reservations = []

    await manager.async_reconcile()

    assert remote.async_delete_passcode.await_count == 2
    assert reservation.id not in manager._records
    manager._client_from_account.assert_called()


@pytest.mark.asyncio
async def test_cancellation_deletes_only_managed_ttlock_passcodes(
    hass, monkeypatch
) -> None:
    """Fresh Guesty cancellation removes stored TTLock IDs idempotently."""
    reservation = _reservation()
    manager, coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    await manager.async_reconcile()
    coordinator.data.reservations = []

    await manager.async_reconcile()

    assert remote.async_delete_passcode.await_count == 2
    assert reservation.id not in manager._records


def test_private_tokens_are_used_only_for_the_matching_ttlock_account(
    hass, monkeypatch
) -> None:
    """An account switch cannot combine old tokens with new credentials."""
    manager, _coordinator, _pin_manager, _remote = _manager(
        hass, monkeypatch, _reservation()
    )
    manager._data["tokens"] = {
        "account_key": "different-account",
        CONF_TTLOCK_ACCESS_TOKEN: "old-account-access",
        CONF_TTLOCK_REFRESH_TOKEN: "old-account-refresh",
    }
    captured: dict = {}

    def _from_hass(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(ttlock.TTLockApiClient, "from_hass", _from_hass)

    manager._client_from_account(manager._account, use_stored_tokens=True)

    assert captured["access_token"] == "access"
    assert captured["refresh_token"] == "refresh"

    current_key = manager._account_key(manager._account)
    manager._data["tokens"] = {
        "account_key": current_key,
        CONF_TTLOCK_ACCESS_TOKEN: "current-private-access",
        CONF_TTLOCK_REFRESH_TOKEN: "current-private-refresh",
    }
    manager._client = None

    account = manager.account_for_reconfigure()

    assert account[CONF_TTLOCK_ACCESS_TOKEN] == "current-private-access"
    assert account[CONF_TTLOCK_REFRESH_TOKEN] == "current-private-refresh"


@pytest.mark.asyncio
async def test_live_token_callback_persists_before_reconcile_completion(
    hass, monkeypatch
) -> None:
    """Rotated live tokens are durable independently of provider outcome."""
    manager, _coordinator, _pin_manager, _remote = _manager(
        hass, monkeypatch, _reservation()
    )
    key = manager._account_key(manager._account)

    await manager._async_persist_token_update(
        key,
        {
            CONF_TTLOCK_ACCESS_TOKEN: "new-access",
            CONF_TTLOCK_REFRESH_TOKEN: "new-refresh",
            CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-20T12:00:00+00:00",
        },
    )

    assert manager._data["tokens"] == {
        "account_key": key,
        CONF_TTLOCK_ACCESS_TOKEN: "new-access",
        CONF_TTLOCK_REFRESH_TOKEN: "new-refresh",
        CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-20T12:00:00+00:00",
    }
    manager._storage.async_save.assert_awaited_once_with(manager._data)


@pytest.mark.asyncio
async def test_reconfigure_validation_persists_rotated_live_tokens(
    hass, monkeypatch
) -> None:
    """Opening options cannot strand the worker on a consumed refresh token."""
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, _reservation()
    )
    remote.access_token = "old-access"
    remote.refresh_token = "old-refresh"
    remote.token_expires_at = "2026-07-20T12:01:00+00:00"

    async def _list_locks():
        remote.access_token = "new-access"
        remote.refresh_token = "new-refresh"
        remote.token_expires_at = "2026-10-20T12:00:00+00:00"
        return [{"lockId": 101}]

    remote.async_list_locks = AsyncMock(side_effect=_list_locks)
    remote.token_snapshot = lambda: {
        CONF_TTLOCK_ACCESS_TOKEN: remote.access_token,
        CONF_TTLOCK_REFRESH_TOKEN: remote.refresh_token,
        CONF_TTLOCK_TOKEN_EXPIRES_AT: remote.token_expires_at,
    }

    locks, account = await manager.async_validate_reconfigure_account()

    assert locks == [{"lockId": 101}]
    assert account[CONF_TTLOCK_ACCESS_TOKEN] == "new-access"
    assert account[CONF_TTLOCK_REFRESH_TOKEN] == "new-refresh"
    assert manager._data["tokens"] == {
        "account_key": manager._account_key(manager._account),
        CONF_TTLOCK_ACCESS_TOKEN: "new-access",
        CONF_TTLOCK_REFRESH_TOKEN: "new-refresh",
        CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-20T12:00:00+00:00",
    }
    manager._storage.async_save.assert_awaited_once_with(manager._data)


@pytest.mark.asyncio
async def test_reconfigure_validation_keeps_rotated_token_after_list_failure(
    hass, monkeypatch
) -> None:
    """A post-refresh discovery failure cannot discard TTLock's new token."""
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, _reservation()
    )
    remote.access_token = "old-access"
    remote.refresh_token = "old-refresh"
    remote.token_expires_at = "2026-07-20T12:01:00+00:00"

    async def _failed_list():
        remote.access_token = "new-access"
        remote.refresh_token = "new-refresh"
        remote.token_expires_at = "2026-10-20T12:00:00+00:00"
        raise TTLockApiError("lock discovery failed")

    remote.async_list_locks = AsyncMock(side_effect=_failed_list)
    remote.token_snapshot = lambda: {
        CONF_TTLOCK_ACCESS_TOKEN: remote.access_token,
        CONF_TTLOCK_REFRESH_TOKEN: remote.refresh_token,
        CONF_TTLOCK_TOKEN_EXPIRES_AT: remote.token_expires_at,
    }

    with pytest.raises(TTLockApiError, match="lock discovery failed"):
        await manager.async_validate_reconfigure_account()

    assert manager._data["tokens"][CONF_TTLOCK_ACCESS_TOKEN] == "new-access"
    assert manager._data["tokens"][CONF_TTLOCK_REFRESH_TOKEN] == "new-refresh"
    manager._storage.async_save.assert_awaited_once_with(manager._data)


@pytest.mark.asyncio
async def test_successful_reauthentication_requeues_auth_failures(
    hass, monkeypatch
) -> None:
    """A repaired TTLock login immediately releases persisted auth backoff."""
    reservation = _reservation(
        check_in=NOW - timedelta(hours=1),
        check_out=NOW + timedelta(days=1),
    )
    manager, _coordinator, _pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    manager.async_schedule_reconcile = MagicMock()
    manager._records[reservation.id] = {
        "listing_id": reservation.listing_id,
        "access_start": reservation.check_in_utc,
        "access_end": reservation.check_out_utc,
        "locks": {},
        "last_error": "authentication_failed",
        "retry_count": 6,
        "retry_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    repaired = {
        **manager._account,
        CONF_TTLOCK_ACCESS_TOKEN: "repaired-access",
        CONF_TTLOCK_REFRESH_TOKEN: "repaired-refresh",
        CONF_TTLOCK_TOKEN_EXPIRES_AT: "2026-10-20T12:00:00+00:00",
    }

    adopted = await manager.async_adopt_reconfigure_account(repaired)

    assert adopted is True
    assert remote.access_token == "repaired-access"
    assert remote.refresh_token == "repaired-refresh"
    record = manager._records[reservation.id]
    assert "last_error" not in record
    assert "retry_count" not in record
    assert "retry_at" not in record
    assert manager._data["tokens"][CONF_TTLOCK_REFRESH_TOKEN] == "repaired-refresh"
    manager._storage.async_save.assert_awaited_once_with(manager._data)
    manager.async_schedule_reconcile.assert_called_once_with()

    await manager.async_reconcile()

    assert remote.async_add_passcode.await_count == 2
    assert all(
        call.kwargs["code"] == "712345"
        and call.kwargs["valid_from"] == NOW
        and call.kwargs["valid_until"] == NOW + timedelta(days=1)
        for call in remote.async_add_passcode.await_args_list
    )
    assert "last_error" not in manager._records[reservation.id]


@pytest.mark.asyncio
async def test_entry_removal_refuses_to_delete_foreign_passcode(
    hass, monkeypatch
) -> None:
    """Integration removal also requires the private reservation marker."""
    reservation_id = "reservation-1"
    data = {
        "records": {
            reservation_id: {
                "account_snapshot": {
                    CONF_TTLOCK_REGION: "eu",
                    CONF_TTLOCK_CLIENT_ID: "client",
                    CONF_TTLOCK_CLIENT_SECRET: "secret",
                    CONF_TTLOCK_USERNAME: "owner@example.com",
                    CONF_TTLOCK_ACCESS_TOKEN: "access",
                    CONF_TTLOCK_REFRESH_TOKEN: "refresh",
                },
                "locks": {"101": {"keyboard_pwd_id": 7001}},
            }
        },
        "tokens": {},
    }
    load = AsyncMock(return_value=data)
    save = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(GuestyTTLockStorage, "async_load", load)
    monkeypatch.setattr(GuestyTTLockStorage, "async_save", save)
    monkeypatch.setattr(GuestyTTLockStorage, "async_remove", remove)
    remote = SimpleNamespace(
        async_list_passcodes=AsyncMock(
            return_value=[
                {
                    "keyboardPwdId": 7001,
                    "keyboardPwd": "712345",
                    "keyboardPwdName": "Manually managed",
                }
            ]
        ),
        async_delete_passcode=AsyncMock(),
    )
    monkeypatch.setattr(
        ttlock.TTLockApiClient, "from_hass", lambda *args, **kwargs: remote
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    complete = await async_remove_stored_ttlock_passcodes(hass, entry)

    assert complete is True
    remote.async_delete_passcode.assert_not_awaited()
    remove.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_entry_removal_erases_ttlock_credentials_after_api_failure(
    hass, monkeypatch
) -> None:
    """Deleted config entries never leave unreachable OAuth tombstones behind."""
    data = {
        "records": {
            "reservation-1": {
                "account_snapshot": {
                    CONF_TTLOCK_REGION: "eu",
                    CONF_TTLOCK_CLIENT_ID: "client",
                    CONF_TTLOCK_CLIENT_SECRET: "secret",
                    CONF_TTLOCK_USERNAME: "owner@example.com",
                    CONF_TTLOCK_ACCESS_TOKEN: "access",
                    CONF_TTLOCK_REFRESH_TOKEN: "refresh",
                },
                "locks": {"101": {"keyboard_pwd_id": 7001}},
            }
        },
        "tokens": {},
    }
    remove = AsyncMock()
    monkeypatch.setattr(GuestyTTLockStorage, "async_load", AsyncMock(return_value=data))
    monkeypatch.setattr(GuestyTTLockStorage, "async_save", AsyncMock())
    monkeypatch.setattr(GuestyTTLockStorage, "async_remove", remove)
    remote = SimpleNamespace(
        async_list_passcodes=AsyncMock(side_effect=TTLockApiError("offline"))
    )
    monkeypatch.setattr(
        ttlock.TTLockApiClient, "from_hass", lambda *args, **kwargs: remote
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    complete = await async_remove_stored_ttlock_passcodes(hass, entry)

    assert complete is False
    remove.assert_awaited_once_with()
