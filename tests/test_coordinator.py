"""Tests for Guesty coordinator decisions and webhook deduplication."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guesty.api import (
    GuestyAuthError,
    GuestyKeyCodeReadResult,
    GuestyPermissionError,
    GuestyRetryableError,
)
from custom_components.guesty.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_LOXONE_ENABLED,
    CONF_LOXONE_LISTING_MAPPINGS,
    CONF_PIN_CUSTOM_ENABLED,
    CONF_PIN_NATIVE_ENABLED,
    CONF_TTLOCK_ENABLED,
    CONF_TTLOCK_LISTING_MAPPINGS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SYNC_STATUS_DEGRADED,
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


def _mapped_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={
            CONF_LOXONE_ENABLED: True,
            CONF_LOXONE_LISTING_MAPPINGS: {"listing-1": {}},
        },
    )


def _empty_cache() -> dict:
    return {
        "listings": {},
        "reservations": [],
        "access_token": None,
        "token_expires_at": None,
        "token_retry_at": None,
        "last_sync": None,
        "last_listing_sync": None,
        "last_reservation_sync": None,
        "last_full_reservation_sync": None,
        "last_incremental_sync": None,
        "last_error": None,
        "pending_reservation_webhooks": {},
        "last_webhook_received_at": None,
        "last_webhook_processed_at": None,
        "last_webhook_failure_reason": None,
    }


def _coordinator(
    hass,
    client=None,
    storage=None,
    entry=None,
) -> GuestyDataUpdateCoordinator:
    entry = entry or _entry()
    entry.add_to_hass(hass)
    storage = storage or SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    return GuestyDataUpdateCoordinator(
        hass,
        entry,
        client or SimpleNamespace(),
        storage,
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
async def test_failed_pin_enrichment_forces_next_shared_refresh_to_be_full(
    hass,
    monkeypatch,
) -> None:
    """A persisted optional-read failure cannot age out of incremental results."""
    cache = _empty_cache()
    cache.update(
        {
            "last_full_reservation_sync": NOW.isoformat(),
            "pin_enrichment_retry_needed": True,
        }
    )
    storage = SimpleNamespace(async_load=AsyncMock(return_value=cache))
    instance = _coordinator(hass, storage=storage)
    expected = object()
    fetch = AsyncMock(return_value=expected)
    monkeypatch.setattr(instance, "_async_fetch_data", fetch)

    result = await instance._async_update_data()

    assert result is expected
    fetch.assert_awaited_once_with(full_reservation_sync=True)


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
async def test_fresh_token_permission_failure_starts_reauthentication(hass) -> None:
    """A permission denial that survives token refresh requires intervention."""
    client = SimpleNamespace(
        async_get_listings=AsyncMock(
            side_effect=GuestyPermissionError("denied", status_code=403)
        ),
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
async def test_oauth_rate_limit_is_persisted_before_empty_cache_retry(hass) -> None:
    """First setup exits quickly while preserving Guesty's OAuth cooldown."""
    retry_at = 2_000_900.0
    client = SimpleNamespace(
        access_token=None,
        token_expires_at=None,
        token_retry_at=retry_at,
        async_get_listings=AsyncMock(
            side_effect=GuestyRetryableError(
                "Guesty token request deferred by rate limit",
                900.0,
                status_code=429,
                endpoint="oauth2",
            )
        ),
        async_get_reservations=AsyncMock(return_value=[]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    with pytest.raises(UpdateFailed):
        await instance._async_fetch_data(full_reservation_sync=True)

    saved = storage.async_save.await_args.args[0]
    assert saved["token_retry_at"] == retry_at
    assert saved["last_error"] == "Guesty token request deferred by rate limit"


@pytest.mark.asyncio
async def test_oauth_rate_limit_starts_from_cache_in_degraded_state(
    hass, monkeypatch
) -> None:
    """Cached entities remain available when OAuth is rate-limited at startup."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    retry_at = 2_000_900.0
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_sync": NOW.isoformat(),
            "last_listing_sync": NOW.isoformat(),
            "last_reservation_sync": NOW.isoformat(),
            "last_full_reservation_sync": NOW.isoformat(),
            "last_incremental_sync": NOW.isoformat(),
        }
    )
    client = SimpleNamespace(
        access_token=None,
        token_expires_at=None,
        token_retry_at=retry_at,
        async_get_reservations=AsyncMock(
            side_effect=GuestyRetryableError(
                "Guesty token request deferred by rate limit",
                900.0,
                status_code=429,
                endpoint="oauth2",
            )
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    result = await instance._async_fetch_data(full_reservation_sync=False)

    assert set(result.listings) == {"listing-1"}
    assert result.sync_status == SYNC_STATUS_DEGRADED
    assert result.data_stale is True
    assert storage.async_save.await_args.args[0]["token_retry_at"] == retry_at


@pytest.mark.asyncio
async def test_successful_api_sync_aborts_stale_reauthentication_flow(hass) -> None:
    """A proven live recovery clears Home Assistant's stale repair issue."""
    entry = _entry()
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=2_000_000.0,
        token_retry_at=None,
        async_get_listings=AsyncMock(return_value=[_listing()]),
        async_get_reservations=AsyncMock(return_value=[]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=_empty_cache()),
        async_save=AsyncMock(),
    )
    instance = GuestyDataUpdateCoordinator(hass, entry, client, storage)

    await instance._async_fetch_data(full_reservation_sync=True)

    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert not any(
        flow["flow_id"] == form["flow_id"]
        for flow in hass.config_entries.flow.async_progress()
    )


@pytest.mark.asyncio
async def test_degraded_cached_sync_keeps_reauthentication_flow(
    hass, monkeypatch
) -> None:
    """Cached data alone is never proof that authentication recovered."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    entry = _entry()
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_sync": NOW.isoformat(),
            "last_listing_sync": NOW.isoformat(),
            "last_reservation_sync": NOW.isoformat(),
            "last_full_reservation_sync": NOW.isoformat(),
            "last_incremental_sync": NOW.isoformat(),
        }
    )
    client = SimpleNamespace(
        access_token=None,
        token_expires_at=None,
        token_retry_at=9_876_543_210.0,
        async_get_reservations=AsyncMock(
            side_effect=GuestyRetryableError(
                "Guesty token request deferred by rate limit",
                900.0,
                status_code=429,
                endpoint="oauth2",
            )
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    abort = MagicMock(wraps=hass.config_entries.flow.async_abort)
    monkeypatch.setattr(hass.config_entries.flow, "async_abort", abort)
    instance = GuestyDataUpdateCoordinator(hass, entry, client, storage)

    result = await instance._async_fetch_data(full_reservation_sync=False)

    assert result.sync_status == SYNC_STATUS_DEGRADED
    abort.assert_not_called()
    assert any(
        flow["flow_id"] == form["flow_id"]
        for flow in entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    )


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
    assert storage.async_save.await_args.args[0]["last_listing_sync"] is not None


@pytest.mark.asyncio
async def test_duplicate_reservation_webhooks_are_coalesced(hass, monkeypatch) -> None:
    """Guesty's duplicate notifications cannot trigger duplicate API calls."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    instance = _coordinator(hass)
    instance._async_apply_reservation_webhook = AsyncMock(return_value=(True, None))
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
    assert (
        instance.reservation_webhook_received_at("65f19af19824d7e6ff848f11") is not None
    )


@pytest.mark.asyncio
async def test_reservation_webhook_is_durable_before_worker_runs(
    hass, monkeypatch
) -> None:
    """A verified handoff reaches atomic storage before HTTP acknowledgement."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 3600
    )
    cache = _empty_cache()
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)

    await instance.async_handle_webhook(
        {
            "event": "reservation.updated",
            "reservation": {"_id": "reservation-1"},
        }
    )

    record = cache["pending_reservation_webhooks"]["reservation-1"]
    assert record["generation"] == 1
    assert record["attempt_count"] == 0
    assert instance.webhook_diagnostics()["pending_reservations"] == 1
    assert "reservation-1" not in str(instance.webhook_diagnostics())
    storage.async_save.assert_awaited()
    await instance.async_shutdown()


@pytest.mark.asyncio
async def test_webhook_retry_state_survives_normal_poll_interval(
    hass, monkeypatch
) -> None:
    """A failed targeted read is persisted for a one-minute fast retry."""
    cache = _empty_cache()
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)
    received_at = NOW
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )

    await instance._async_persist_reservation_webhook(
        "reservation-1",
        received_at,
        inactive_hint=False,
    )
    await instance._async_update_webhook_queue_result(
        "reservation-1",
        expected_generation=1,
        success=False,
        reason="reservation_not_visible",
    )

    record = cache["pending_reservation_webhooks"]["reservation-1"]
    assert record["attempt_count"] == 1
    assert record["next_attempt_at"] == (received_at + timedelta(minutes=1)).isoformat()
    assert cache["last_webhook_failure_reason"] == "reservation_not_visible"


@pytest.mark.asyncio
async def test_newer_webhook_generation_owns_inflight_queue_result(hass) -> None:
    """An older targeted result cannot complete a newer verified change."""
    cache = _empty_cache()
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)

    await instance._async_persist_reservation_webhook(
        "reservation-1", NOW, inactive_hint=False
    )
    await instance._async_persist_reservation_webhook(
        "reservation-1", NOW + timedelta(seconds=1), inactive_hint=False
    )
    await instance._async_update_webhook_queue_result(
        "reservation-1",
        expected_generation=1,
        success=True,
        reason=None,
    )

    record = cache["pending_reservation_webhooks"]["reservation-1"]
    assert record["generation"] == 2
    assert record["next_attempt_at"] == (NOW + timedelta(seconds=1)).isoformat()


@pytest.mark.asyncio
async def test_pending_webhook_queue_resumes_after_restart(hass, monkeypatch) -> None:
    """Cached verified work starts one owned worker after Home Assistant reload."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 3600
    )
    cache = _empty_cache()
    cache["pending_reservation_webhooks"] = {
        "reservation-1": {
            "received_at": NOW.isoformat(),
            "next_attempt_at": (NOW + timedelta(minutes=1)).isoformat(),
            "attempt_count": 1,
            "generation": 2,
            "inactive_hint": False,
        }
    }
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)

    await instance.async_load_cached_data()

    assert instance.webhook_diagnostics()["pending_reservations"] == 1
    assert instance.reservation_webhook_received_at("reservation-1") == NOW
    assert instance._webhook_batch_task is not None
    await instance.async_shutdown()


@pytest.mark.asyncio
async def test_new_webhook_wakes_worker_waiting_for_later_retry(
    hass, monkeypatch
) -> None:
    """Fresh work is not delayed by an older item's minute-scale wait."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    now = datetime.now(timezone.utc)
    cache = _empty_cache()
    cache["pending_reservation_webhooks"] = {
        "reservation-old": {
            "received_at": now.isoformat(),
            "next_attempt_at": (now + timedelta(minutes=5)).isoformat(),
            "attempt_count": 5,
            "generation": 1,
            "inactive_hint": False,
        }
    }
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)
    instance._async_apply_reservation_webhook = AsyncMock(return_value=(True, None))
    instance._restore_webhook_queue_state(cache)
    await asyncio.sleep(0.01)

    await instance.async_handle_webhook(
        {
            "event": "reservation.updated",
            "reservation": {"_id": "reservation-new"},
        }
    )
    for _ in range(20):
        if any(
            item.args == ("reservation-new",)
            for item in instance._async_apply_reservation_webhook.await_args_list
        ):
            break
        await asyncio.sleep(0.01)

    instance._async_apply_reservation_webhook.assert_any_await("reservation-new")
    await instance.async_shutdown()


@pytest.mark.asyncio
async def test_signed_cancellation_revokes_cached_reservation_before_api_read(
    hass, monkeypatch
) -> None:
    """A signed inactive hint exposes cleanup immediately and confirms later."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 3600
    )
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    cache["reservations"] = [_reservation().to_dict()]
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, storage=storage)
    await instance.async_load_cached_data()

    await instance.async_handle_webhook(
        {
            "event": "reservation.updated",
            "reservation": {"_id": "reservation-1", "status": "canceled"},
        }
    )

    assert instance.data.reservations == []
    assert (
        cache["pending_reservation_webhooks"]["reservation-1"]["inactive_hint"] is True
    )
    await instance.async_shutdown()


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

    async def apply_reservation(reservation_id: str) -> tuple[bool, None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return True, None

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
async def test_reservation_burst_preserves_each_targeted_refresh(
    hass, monkeypatch
) -> None:
    """Bulk edits preserve one targeted refresh for every reservation."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.WEBHOOK_DEBOUNCE_SECONDS", 0
    )
    instance = _coordinator(hass)
    instance.async_refresh = AsyncMock()
    instance._async_apply_reservation_webhook = AsyncMock(return_value=(True, None))

    await asyncio.gather(
        instance.async_handle_webhook(
            {"event": "reservation.updated", "reservation": {"_id": "res-1"}}
        ),
        instance.async_handle_webhook(
            {"event": "reservation.updated", "reservation": {"_id": "res-2"}}
        ),
    )
    await _wait_webhook_worker(instance)

    instance.async_refresh.assert_not_awaited()
    assert instance._async_apply_reservation_webhook.await_count == 2
    assert {
        call.args[0]
        for call in instance._async_apply_reservation_webhook.await_args_list
    } == {"res-1", "res-2"}


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


@pytest.mark.asyncio
async def test_webhook_registration_recovers_with_bounded_backoff(
    hass, monkeypatch
) -> None:
    """A transient remote registration failure recovers without a reload."""
    instance = _coordinator(hass)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(delay: float) -> None:
        delays.append(delay)
        if delay == 3600:
            await asyncio.Event().wait()
        await real_sleep(0)

    register = AsyncMock(side_effect=[None, "remote-webhook"])
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.asyncio.sleep",
        _sleep,
    )
    monkeypatch.setattr(
        "custom_components.guesty.webhook.async_register_guesty_webhook",
        register,
    )

    instance.async_start_webhook_registration_recovery("local-webhook")
    task = instance._webhook_registration_task
    assert task is not None
    while register.await_count < 2 or len(delays) < 3:
        await real_sleep(0)

    assert delays[:3] == [300, 600, 3600]
    assert register.await_count == 2
    assert instance._webhook_active is True
    await instance.async_shutdown()
    assert instance._webhook_registration_task is None


@pytest.mark.asyncio
async def test_shutdown_cancels_webhook_registration_recovery(
    hass, monkeypatch
) -> None:
    """Config-entry unload cannot leak the registration retry task."""
    instance = _coordinator(hass)
    sleep_started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def _wait_forever(delay: float) -> None:
        if delay == 300:
            sleep_started.set()
            await asyncio.Event().wait()
            return
        await real_sleep(delay)

    register = AsyncMock()
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.asyncio.sleep",
        _wait_forever,
    )
    monkeypatch.setattr(
        "custom_components.guesty.webhook.async_register_guesty_webhook",
        register,
    )

    instance.async_start_webhook_registration_recovery("local-webhook")
    await sleep_started.wait()
    await instance.async_shutdown()

    register.assert_not_awaited()
    assert instance._webhook_registration_task is None


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
    today = datetime.now(timezone.utc).date()
    return GuestyReservation(
        id=reservation_id,
        listing_id="listing-1",
        status="confirmed",
        confirmation_code=None,
        check_in_date=(today + timedelta(days=1)).isoformat(),
        check_out_date=(today + timedelta(days=3)).isoformat(),
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


@pytest.mark.parametrize(
    "options",
    [
        {
            CONF_LOXONE_ENABLED: True,
            CONF_LOXONE_LISTING_MAPPINGS: {"listing-1": {}},
        },
        {
            CONF_TTLOCK_ENABLED: True,
            CONF_TTLOCK_LISTING_MAPPINGS: {"listing-1": {}},
        },
    ],
)
@pytest.mark.asyncio
async def test_mapped_active_reservations_receive_authoritative_v3_keycodes(
    hass,
    options,
) -> None:
    """Each enabled provider receives v3 Keycodes only for its mapped listings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options=options,
    )
    mapped_id = "507f1f77bcf86cd799439011"
    unmapped_id = "507f1f77bcf86cd799439012"
    inactive_id = "507f1f77bcf86cd799439013"
    mapped = _reservation(mapped_id)
    unmapped = _reservation(unmapped_id)
    unmapped.listing_id = "listing-2"
    inactive = _reservation(inactive_id)
    inactive.status = "canceled"
    client = SimpleNamespace(
        async_get_reservation_key_codes=AsyncMock(return_value={mapped_id: "799999"})
    )
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([mapped, unmapped, inactive])

    client.async_get_reservation_key_codes.assert_awaited_once_with({mapped_id})
    assert mapped.key_code == "799999"
    assert mapped.key_code_observed is True
    assert unmapped.key_code_observed is False
    assert inactive.key_code_observed is False


@pytest.mark.asyncio
async def test_disabled_native_keycode_source_skips_v3_enrichment(hass) -> None:
    """Disabling Keycode removes its additional read traffic and observations."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={
            CONF_LOXONE_ENABLED: True,
            CONF_LOXONE_LISTING_MAPPINGS: {"listing-1": {}},
            CONF_PIN_NATIVE_ENABLED: False,
        },
    )
    reservation = _reservation("507f1f77bcf86cd799439011")
    client = SimpleNamespace(async_get_reservation_key_codes=AsyncMock())
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([reservation])

    client.async_get_reservation_key_codes.assert_not_awaited()
    assert reservation.key_code_observed is False


@pytest.mark.asyncio
async def test_v3_observed_empty_keycode_remains_authoritative(hass) -> None:
    """An explicit empty v3 Keycode is distinct from a sparse response."""
    entry = _mapped_entry()
    reservation_id = "507f1f77bcf86cd799439011"
    reservation = _reservation(reservation_id, key_code="712345")
    client = SimpleNamespace(
        async_get_reservation_key_codes=AsyncMock(return_value={reservation_id: None})
    )
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([reservation])

    assert reservation.key_code is None
    assert reservation.key_code_observed is True


@pytest.mark.asyncio
async def test_empty_v3_projection_cannot_erase_populated_v2_keycode(hass) -> None:
    """A channel Keycode survives the sparse alternate reservation model."""
    entry = _mapped_entry()
    reservation_id = "507f1f77bcf86cd799439011"
    reservation = _reservation(reservation_id, key_code="700070#️⃣")
    reservation.key_code_route = "v2"
    client = SimpleNamespace(
        async_get_reservation_key_codes=AsyncMock(return_value={reservation_id: None})
    )
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([reservation])

    assert reservation.key_code == "700070#️⃣"
    assert reservation.key_code_observed is True
    assert reservation.key_code_route == "v2"


@pytest.mark.asyncio
async def test_sparse_v3_keycode_response_is_not_an_observed_deletion(hass) -> None:
    """A missing v3 reservation or notes object cannot revoke a private PIN."""
    entry = _mapped_entry()
    reservation_id = "507f1f77bcf86cd799439011"
    reservation = _reservation(reservation_id)
    client = SimpleNamespace(async_get_reservation_key_codes=AsyncMock(return_value={}))
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([reservation])

    assert reservation.key_code is None
    assert reservation.key_code_observed is False
    assert reservation.key_code_read_failed is True


@pytest.mark.asyncio
async def test_sparse_v3_channel_response_selects_observed_v2_keycode(hass) -> None:
    """A paired sparse V3 row identifies the successfully observed V2 surface."""
    entry = _mapped_entry()
    reservation_id = "507f1f77bcf86cd799439011"
    reservation = _reservation(reservation_id)
    reservation.key_code = None
    reservation.key_code_observed = True
    reservation.key_code_v2_observed = True
    reservation.key_code_route = None
    client = SimpleNamespace(
        async_get_reservation_key_codes=AsyncMock(
            return_value=GuestyKeyCodeReadResult(
                {},
                frozenset({reservation_id}),
            )
        )
    )
    instance = _coordinator(hass, client=client, entry=entry)

    await instance._async_enrich_native_keycodes([reservation])

    assert reservation.key_code is None
    assert reservation.key_code_observed is True
    assert reservation.key_code_v2_observed is True
    assert reservation.key_code_route == "v2"
    assert reservation.key_code_read_failed is False


@pytest.mark.asyncio
async def test_full_poll_enriches_mapped_reservations_from_v3(
    hass,
    monkeypatch,
) -> None:
    """The normal shared poll publishes v3 Keycodes to downstream managers."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    reservation_id = "507f1f77bcf86cd799439011"
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_listing_sync": NOW.isoformat(),
            "pin_enrichment_retry_needed": True,
        }
    )
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_reservations=AsyncMock(return_value=[_reservation(reservation_id)]),
        async_get_reservation_key_codes=AsyncMock(
            return_value={reservation_id: "799999"}
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    result = await instance._async_fetch_data(full_reservation_sync=True)

    client.async_get_reservation_key_codes.assert_awaited_once_with({reservation_id})
    assert result.reservations[0].key_code == "799999"
    assert result.reservations[0].key_code_observed is True
    saved_cache = storage.async_save.await_args.args[0]
    assert "key_code" not in saved_cache["reservations"][0]
    assert "pin_enrichment_retry_needed" not in saved_cache


@pytest.mark.asyncio
async def test_v3_keycode_failure_keeps_fresh_base_reservation_data(
    hass,
    monkeypatch,
) -> None:
    """Optional Keycode enrichment cannot make a successful poll stale."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    reservation_id = "507f1f77bcf86cd799439011"
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_listing_sync": NOW.isoformat(),
        }
    )
    reservation = _reservation(reservation_id)
    reservation.check_in_utc = "2026-08-20T15:00:00Z"
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_reservations=AsyncMock(return_value=[reservation]),
        async_get_reservation_key_codes=AsyncMock(
            side_effect=GuestyRetryableError("v3 unavailable")
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    result = await instance._async_fetch_data(full_reservation_sync=True)

    assert result.data_stale is False
    assert result.last_error is None
    assert result.reservations[0].check_in_utc == "2026-08-20T15:00:00Z"
    assert result.reservations[0].key_code_read_failed is True
    saved = storage.async_save.await_args.args[0]
    assert saved["last_reservation_sync"] == NOW.isoformat()
    assert saved["pin_enrichment_retry_needed"] is True


@pytest.mark.asyncio
async def test_channel_sparse_v3_result_does_not_force_repeated_full_polls(
    hass,
    monkeypatch,
) -> None:
    """An exact V2/V3 route pair clears the persistent enrichment retry marker."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow", lambda: NOW
    )
    reservation_id = "507f1f77bcf86cd799439011"
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_listing_sync": NOW.isoformat(),
            "pin_enrichment_retry_needed": True,
        }
    )
    reservation = _reservation(reservation_id)
    reservation.key_code = None
    reservation.key_code_observed = True
    reservation.key_code_v2_observed = True
    reservation.key_code_route = None
    reservation.custom_fields = {}
    reservation.custom_fields_observed = True
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_reservations=AsyncMock(return_value=[reservation]),
        async_get_reservation_key_codes=AsyncMock(
            return_value=GuestyKeyCodeReadResult(
                {},
                frozenset({reservation_id}),
            )
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    result = await instance._async_fetch_data(full_reservation_sync=True)

    current = result.reservations[0]
    assert current.key_code_route == "v2"
    assert current.key_code_read_failed is False
    saved = storage.async_save.await_args.args[0]
    assert "pin_enrichment_retry_needed" not in saved


@pytest.mark.asyncio
async def test_targeted_webhook_enriches_mapped_reservation_from_v3(hass) -> None:
    """A manual native Keycode edit reaches managers through one targeted read."""
    reservation_id = "507f1f77bcf86cd799439011"
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(return_value=_reservation(reservation_id)),
        async_get_reservation_key_codes=AsyncMock(
            return_value={reservation_id: "788888"}
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    await instance._async_apply_reservation_webhook(reservation_id)

    assert client.async_get_reservation.await_args_list == [
        call(reservation_id),
        call(
            reservation_id,
            include_key_code=True,
            include_custom_fields=True,
        ),
    ]
    client.async_get_reservation_key_codes.assert_awaited_once_with({reservation_id})
    assert instance.data.reservations[0].key_code == "788888"
    assert instance.data.reservations[0].key_code_observed is True


@pytest.mark.asyncio
async def test_targeted_webhook_exposes_custom_field_change(hass) -> None:
    """A manual custom-field edit reaches PIN reconciliation immediately."""
    reservation_id = "507f1f77bcf86cd799439011"
    base = _reservation(reservation_id)
    enriched = _reservation(reservation_id)
    enriched.custom_fields = {"field-id": "734567"}
    enriched.custom_fields_observed = True
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(side_effect=[base, enriched]),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "client", CONF_CLIENT_SECRET: "secret"},
        options={
            CONF_LOXONE_ENABLED: True,
            CONF_LOXONE_LISTING_MAPPINGS: {"listing-1": {}},
            CONF_PIN_NATIVE_ENABLED: False,
            CONF_PIN_CUSTOM_ENABLED: True,
        },
    )
    instance = _coordinator(hass, client=client, storage=storage, entry=entry)

    await instance._async_apply_reservation_webhook(reservation_id)

    assert instance.data.reservations[0].custom_fields == {"field-id": "734567"}
    assert instance.data.reservations[0].custom_fields_observed is True
    assert client.async_get_reservation.await_args_list == [
        call(reservation_id),
        call(
            reservation_id,
            include_key_code=False,
            include_custom_fields=True,
        ),
    ]


@pytest.mark.asyncio
async def test_cancellation_webhook_prunes_without_pin_enrichment(hass) -> None:
    """A cancellation takes the shortest path to provider cleanup."""
    reservation_id = "507f1f77bcf86cd799439011"
    active = _reservation(reservation_id)
    canceled = _reservation(reservation_id)
    canceled.status = "canceled"
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    cache["reservations"] = [active.to_dict()]
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(return_value=canceled),
        async_get_reservation_key_codes=AsyncMock(),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )
    listener = MagicMock()
    remove_listener = instance.async_add_listener(listener)

    await instance._async_apply_reservation_webhook(reservation_id)

    assert instance.data.reservations == []
    client.async_get_reservation.assert_awaited_once_with(reservation_id)
    client.async_get_reservation_key_codes.assert_not_awaited()
    listener.assert_called_once_with()
    remove_listener()


@pytest.mark.asyncio
async def test_targeted_pin_enrichment_failure_preserves_cancellation_fields(
    hass,
) -> None:
    """A PIN-only failure cannot discard a fresh reservation update."""
    reservation_id = "507f1f77bcf86cd799439011"
    reservation = _reservation(reservation_id)
    reservation.planned_arrival = "13:30"
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(
            side_effect=[
                reservation,
                GuestyRetryableError("PIN projection temporarily unavailable"),
            ]
        ),
        async_get_reservation_key_codes=AsyncMock(return_value={}),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    await instance._async_apply_reservation_webhook(reservation_id)

    current = instance.data.reservations[0]
    assert current.planned_arrival == "13:30"
    assert current.key_code_read_failed is True
    assert current.custom_fields_read_failed is True
    assert storage.async_save.await_args.args[0]["pin_enrichment_retry_needed"] is True


@pytest.mark.asyncio
async def test_targeted_reservation_error_stays_in_targeted_retry_queue(hass) -> None:
    """A failed exact read reports retry without starting an account scan."""
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(
            side_effect=GuestyRetryableError("temporary outage")
        )
    )
    instance = _coordinator(hass, client=client)
    instance.async_refresh = AsyncMock()

    result = await instance._async_apply_reservation_webhook("reservation-1")

    assert result == (False, "api_unavailable")
    instance.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_targeted_reservation_preserves_cache_for_targeted_retry(
    hass,
) -> None:
    """An eventually-consistent 404 cannot erase a prior safe snapshot."""
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    cache["reservations"] = [_reservation().to_dict()]
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    client = SimpleNamespace(async_get_reservation=AsyncMock(return_value=None))
    instance = _coordinator(hass, client=client, storage=storage)
    instance.async_refresh = AsyncMock()

    result = await instance._async_apply_reservation_webhook("reservation-1")

    assert result == (False, "reservation_not_visible")
    assert instance.data is None
    assert cache["reservations"] == [_reservation().to_dict()]
    instance.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_missing_pin_projection_keeps_base_update_fail_closed(
    hass,
) -> None:
    """A transient enrichment 404 cannot trigger blind PIN reconciliation."""
    reservation_id = "507f1f77bcf86cd799439011"
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    client = SimpleNamespace(
        async_get_reservation=AsyncMock(
            side_effect=[_reservation(reservation_id), None]
        ),
        async_get_reservation_key_codes=AsyncMock(
            side_effect=GuestyRetryableError("v3 unavailable")
        ),
    )
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(
        hass,
        client=client,
        storage=storage,
        entry=_mapped_entry(),
    )

    await instance._async_apply_reservation_webhook(reservation_id)

    current = instance.data.reservations[0]
    assert current.id == reservation_id
    assert current.key_code_read_failed is True
    assert current.custom_fields_read_failed is True
    assert storage.async_save.await_args.args[0]["pin_enrichment_retry_needed"] is True


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
async def test_new_listing_webhook_does_not_persist_opted_out_guest_details(
    hass,
) -> None:
    """Targeted new-listing reads obey the same cache privacy option as polling."""
    reservation = _reservation()
    reservation.guest_name = "Private Guest"
    reservation.confirmation_code = "PRIVATE-CODE"
    client = SimpleNamespace(
        access_token="token",
        token_expires_at=123.0,
        async_get_listings=AsyncMock(),
        async_get_reservations=AsyncMock(return_value=[reservation]),
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

    saved = storage.async_save.await_args.args[0]["reservations"][0]
    assert "guest_name" not in saved
    assert "confirmation_code" not in saved


@pytest.mark.asyncio
async def test_reservation_webhook_does_not_persist_opted_out_guest_details(
    hass,
) -> None:
    """A single-reservation webhook cannot restore PII removed from the cache."""
    reservation = _reservation()
    reservation.guest_name = "Private Guest"
    reservation.confirmation_code = "PRIVATE-CODE"
    cache = _empty_cache()
    cache["listings"] = {"listing-1": _listing().to_dict()}
    client = SimpleNamespace(async_get_reservation=AsyncMock(return_value=reservation))
    storage = SimpleNamespace(
        async_load=AsyncMock(return_value=cache),
        async_save=AsyncMock(),
    )
    instance = _coordinator(hass, client, storage)

    await instance._async_apply_reservation_webhook("reservation-1")

    saved = storage.async_save.await_args.args[0]["reservations"][0]
    assert "guest_name" not in saved
    assert "confirmation_code" not in saved
    assert instance.data.reservations[0].guest_name == "Private Guest"


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
async def test_targeted_reservation_webhook_preserves_global_stale_state(
    hass, monkeypatch
) -> None:
    """One fresh reservation cannot make an old account snapshot trustworthy."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow",
        lambda: NOW,
    )
    stale_sync = (NOW - timedelta(hours=7)).isoformat()
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "last_sync": stale_sync,
            "last_reservation_sync": stale_sync,
            "last_error": "Guesty temporarily unavailable",
        }
    )
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
    assert saved_cache["last_sync"] == stale_sync
    assert saved_cache["last_reservation_sync"] == stale_sync
    assert saved_cache["last_error"] == "Guesty temporarily unavailable"
    assert instance.data.data_stale is True
    assert instance.data.cache_age_minutes == 420.0
    assert instance.data.sync_status == SYNC_STATUS_DEGRADED
    assert instance.data.last_error == "Guesty temporarily unavailable"


@pytest.mark.asyncio
async def test_targeted_listing_webhook_does_not_refresh_reservation_age(
    hass, monkeypatch
) -> None:
    """A listing payload cannot reset the age of cached reservations."""
    monkeypatch.setattr(
        "custom_components.guesty.coordinator.dt_util.utcnow",
        lambda: NOW,
    )
    stale_sync = (NOW - timedelta(hours=7)).isoformat()
    cache = _empty_cache()
    cache.update(
        {
            "listings": {"listing-1": _listing().to_dict()},
            "reservations": [_reservation().to_dict()],
            "last_sync": stale_sync,
            "last_listing_sync": stale_sync,
            "last_reservation_sync": stale_sync,
        }
    )
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
        [
            {
                "event": "listing.updated",
                "listing": {
                    "_id": "listing-1",
                    "title": "Updated Apartment",
                    "timezone": "Europe/Berlin",
                },
            }
        ]
    )

    saved_cache = storage.async_save.await_args.args[0]
    assert saved_cache["last_sync"] == stale_sync
    assert saved_cache["last_listing_sync"] == stale_sync
    assert saved_cache["last_reservation_sync"] == stale_sync
    assert instance.data.data_stale is True
    assert instance.data.cache_age_minutes == 420.0
    assert instance.data.sync_status == SYNC_STATUS_DEGRADED


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
