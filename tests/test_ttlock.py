"""Tests for reservation-driven TTLock PIN provisioning."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty import ttlock
from custom_components.guesty.const import (
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
    CONF_TTLOCK_USERNAME,
    DOMAIN,
)
from custom_components.guesty.models import GuestyListing, GuestyReservation
from custom_components.guesty.ttlock import (
    GuestyTTLockManager,
    GuestyTTLockStorage,
    async_remove_stored_ttlock_passcodes,
)
from custom_components.guesty.ttlock_api import TTLockApiError, TTLockGatewayError

NOW = datetime.fromisoformat("2026-07-20T12:00:00+00:00")


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


def _manager(hass, monkeypatch, reservation: GuestyReservation):
    entry = MockConfigEntry(domain=DOMAIN, options=_options())
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[reservation],
            data_stale=False,
        )
    )
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
async def test_remote_duplicate_rotates_authoritative_guesty_code(
    hass, monkeypatch
) -> None:
    """A proven TTLock collision is delegated to the shared Guesty PIN owner."""
    reservation = _reservation()
    manager, _coordinator, pin_manager, remote = _manager(
        hass, monkeypatch, reservation
    )
    remote.async_list_passcodes.side_effect = None
    remote.async_list_passcodes.return_value = [
        {"keyboardPwdId": 999, "keyboardPwd": "712345", "keyboardPwdName": "Other"}
    ]

    await manager.async_reconcile()

    pin_manager.async_rotate_external_conflict.assert_awaited_once_with(
        reservation.id, "712345"
    )
    remote.async_add_passcode.assert_not_awaited()
    assert manager._records[reservation.id]["last_error"] == "code_conflict_rotated"


@pytest.mark.asyncio
async def test_repeated_remote_conflicts_are_rate_limited(hass, monkeypatch) -> None:
    """TTLock cannot cause an unbounded loop of authoritative Guesty writes."""
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

    assert pin_manager.async_rotate_external_conflict.await_count == 3
    record = manager._records[reservation.id]
    assert len(record["conflict_rotation_times"]) == 3
    assert record["last_error"] == "code_conflict"
    assert record["retry_at"]


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
