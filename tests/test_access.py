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
    client.async_resolve_custom_field.assert_awaited_once()


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
        GuestyApiError("stale field"),
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
    ("language", "title", "button"),
    [
        ("de", "Türzugang", "Haustür öffnen"),
        ("en", "Door access", "Open Haustür"),
        ("es", "Acceso a la puerta", "Abrir Haustür"),
        ("fr", "Accès à la porte", "Ouvrir Haustür"),
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
@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("de", "Haustür wurde geöffnet"),
        ("en", "Haustür was opened"),
        ("es", "Se abrió Haustür"),
        ("fr", "Haustür a été ouverte"),
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
