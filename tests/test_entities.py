"""Tests for Guesty calendar and sensor entities."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import zoneinfo

import pytest
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory

from custom_components.guesty.calendar import (
    GuestyReservationCalendar,
    _add_listing_entities as add_calendar_entities,
)
from custom_components.guesty.const import CONF_EXPOSE_GUEST_DETAILS
from custom_components.guesty.models import (
    GuestyListing,
    GuestyReservation,
    calculate_listing_occupancy,
)
from custom_components.guesty.sensor import (
    GuestyAccessLinkSensor,
    GuestyCurrentGuestSensor,
    GuestyKeycodeStatusSensor,
    GuestyLoxonePinStatusSensor,
    GuestyTTLockPinStatusSensor,
    GuestyOccupancySensor,
    _add_listing_entities as add_sensor_entities,
)

TZ = zoneinfo.ZoneInfo("Europe/Berlin")


def _listing() -> GuestyListing:
    return GuestyListing(
        id="listing-1",
        title="Private address",
        nickname="Private nickname",
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )


def _reservation() -> GuestyReservation:
    return GuestyReservation(
        id="reservation-1",
        listing_id="listing-1",
        status="confirmed",
        confirmation_code="SECRET-CODE",
        check_in_date="2026-07-13",
        check_out_date="2026-07-16",
        check_in_utc=None,
        check_out_utc=None,
        planned_arrival=None,
        planned_departure=None,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name="Private Guest",
        last_updated_at=None,
    )


def _coordinator(
    *,
    expose_details: bool = False,
    moment: datetime | None = None,
):
    listing = _listing()
    reservation = _reservation()
    occupancy = calculate_listing_occupancy(
        listing,
        [reservation],
        moment or datetime(2026, 7, 12, 12, 0, tzinfo=TZ),
    )
    data = SimpleNamespace(
        listings={listing.id: listing},
        reservations=[reservation],
        occupancy={listing.id: occupancy},
        data_stale=False,
        cache_age_minutes=0.0,
        last_sync="2026-07-13T12:00:00+00:00",
    )
    entry = SimpleNamespace(
        entry_id="entry-id",
        options={CONF_EXPOSE_GUEST_DETAILS: expose_details},
    )
    return SimpleNamespace(
        data=data,
        config_entry=entry,
        last_update_success=True,
        async_add_listener=lambda listener, context=None: lambda: None,
        get_listing_reservations=lambda listing_id: [reservation],
    )


def _access_manager(snapshot: dict | None = None):
    """Return the small access-manager surface used by sensor entities."""
    return SimpleNamespace(
        listing_access_snapshot=lambda _listing_id: (
            snapshot or {"status": "no_reservation"}
        ),
        async_add_listener=lambda _listener: lambda: None,
    )


def _loxone_manager(snapshot: dict | None = None):
    """Return the small Loxone-manager surface used by status entities."""
    return SimpleNamespace(
        listing_status_snapshot=lambda _listing_id: (
            snapshot
            or {
                "guesty_status": "no_reservation",
                "loxone_status": "no_reservation",
            }
        ),
        async_add_listener=lambda _listener: lambda: None,
    )


@pytest.mark.asyncio
async def test_calendar_hides_guest_details_by_default(hass) -> None:
    """Calendar event text is privacy-safe unless the user opts in."""
    entity = GuestyReservationCalendar(_coordinator(), "listing-1")
    event = entity.event
    events = await entity.async_get_events(
        hass,
        datetime(2026, 7, 1, tzinfo=TZ),
        datetime(2026, 8, 1, tzinfo=TZ),
    )

    assert event is not None
    assert event.summary == "Reserved"
    assert "Private Guest" not in (event.description or "")
    assert "SECRET-CODE" not in (event.description or "")
    assert len(events) == 1


def test_calendar_can_expose_guest_details_explicitly() -> None:
    """The privacy option restores useful live calendar details."""
    entity = GuestyReservationCalendar(_coordinator(expose_details=True), "listing-1")
    event = entity.event
    assert event is not None
    assert event.summary == "Private Guest"
    assert "SECRET-CODE" in (event.description or "")


def test_calendar_notifies_event_listeners(monkeypatch) -> None:
    """Future-event subscribers are refreshed after coordinator updates."""
    entity = GuestyReservationCalendar(_coordinator(), "listing-1")
    listener_update = MagicMock()
    state_update = MagicMock()
    monkeypatch.setattr(
        entity,
        "async_update_event_listeners",
        listener_update,
        raising=False,
    )
    monkeypatch.setattr(CoordinatorEntity, "_handle_coordinator_update", state_update)

    entity._handle_coordinator_update()

    listener_update.assert_called_once_with()
    state_update.assert_called_once_with()


def test_sensor_hides_and_does_not_record_guest_details_by_default() -> None:
    """Guest PII is neither exposed nor included in recorder attributes."""
    entity = GuestyOccupancySensor(_coordinator(), "listing-1")
    attributes = entity.extra_state_attributes

    assert "current_guest" not in attributes
    assert "current_confirmation_code" not in attributes
    assert "listing_title" not in attributes
    assert "listing_nickname" not in attributes
    assert "current_guest" in entity._unrecorded_attributes


def test_sensor_exposes_unrecorded_details_only_after_opt_in() -> None:
    """Opted-in guest details remain excluded from recorder history."""
    entity = GuestyOccupancySensor(_coordinator(expose_details=True), "listing-1")
    attributes = entity.extra_state_attributes

    assert attributes["next_guest"] == "Private Guest"
    assert attributes["next_confirmation_code"] == "SECRET-CODE"
    assert "next_guest" in entity._unrecorded_attributes


def test_current_guest_sensor_requires_explicit_privacy_opt_ins() -> None:
    """The guest name state is disabled and unavailable without consent."""
    entity = GuestyCurrentGuestSensor(
        _coordinator(
            moment=datetime(2026, 7, 14, 12, 0, tzinfo=TZ),
        ),
        "listing-1",
    )

    assert entity.native_value is None
    assert not entity.available
    assert entity.entity_registry_enabled_default is False


def test_current_guest_sensor_exposes_only_the_active_guest() -> None:
    """An opted-in sensor shows the current guest but not a future guest."""
    current = GuestyCurrentGuestSensor(
        _coordinator(
            expose_details=True,
            moment=datetime(2026, 7, 14, 12, 0, tzinfo=TZ),
        ),
        "listing-1",
    )
    upcoming = GuestyCurrentGuestSensor(
        _coordinator(expose_details=True),
        "listing-1",
    )

    assert current.native_value == "Private Guest"
    assert current.available
    assert upcoming.native_value is None


def test_access_link_sensor_exposes_live_unrecorded_url_after_enable() -> None:
    """The diagnostic sensor shows a verified link without recorder history."""
    coordinator = _coordinator(expose_details=True)
    reservation = coordinator.data.reservations[0]
    manager = _access_manager(
        {
            "status": "synced",
            "access_url": "https://ha.test/api/guesty/access/entry/token",
            "reservation": reservation,
            "access_start": datetime(2026, 7, 13, 15, 0, tzinfo=TZ),
            "access_end": datetime(2026, 7, 16, 11, 0, tzinfo=TZ),
            "access_active": True,
            "field_synced": True,
            "write_verified": True,
        }
    )
    entity = GuestyAccessLinkSensor(coordinator, manager, "listing-1")

    assert entity.native_value == "synced"
    assert entity.entity_registry_enabled_default is False
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.extra_state_attributes == {
        "access_active": True,
        "field_synced": True,
        "write_verified": True,
        "access_url": "https://ha.test/api/guesty/access/entry/token",
        "access_start": "2026-07-13T15:00:00+02:00",
        "access_end": "2026-07-16T11:00:00+02:00",
        "reservation_status": "confirmed",
        "guest_name": "Private Guest",
    }
    assert {"access_url", "guest_name"} <= entity._unrecorded_attributes


def test_access_link_sensor_hides_guest_name_without_privacy_opt_in() -> None:
    """The link diagnostic reuses the existing guest-detail privacy setting."""
    coordinator = _coordinator()
    manager = _access_manager(
        {
            "status": "pending",
            "reservation": coordinator.data.reservations[0],
            "access_active": False,
        }
    )
    entity = GuestyAccessLinkSensor(coordinator, manager, "listing-1")

    assert entity.native_value == "pending"
    assert "guest_name" not in entity.extra_state_attributes


def test_keycode_status_sensors_report_each_destination_without_secrets() -> None:
    """Guesty and Loxone delivery have independent privacy-safe states."""
    coordinator = _coordinator()
    manager = _loxone_manager(
        {
            "guesty_status": "synced",
            "loxone_status": "scheduled",
            "access_start": datetime(2026, 7, 13, 15, 0, tzinfo=TZ),
            "access_end": datetime(2026, 7, 16, 11, 0, tzinfo=TZ),
            "provision_at": datetime(2026, 7, 13, 9, 0, tzinfo=TZ),
            "reservation_status": "confirmed",
            "field_synced": True,
            "loxone_user_created": False,
            "data_stale": False,
        }
    )
    guesty_entity = GuestyKeycodeStatusSensor(coordinator, manager, "listing-1")
    loxone_entity = GuestyLoxonePinStatusSensor(coordinator, manager, "listing-1")

    assert guesty_entity.native_value == "synced"
    assert loxone_entity.native_value == "scheduled"
    assert guesty_entity.entity_category is EntityCategory.DIAGNOSTIC
    assert loxone_entity.entity_registry_enabled_default is True
    assert guesty_entity.extra_state_attributes == {
        "data_stale": False,
        "field_synced": True,
        "loxone_user_created": False,
        "reservation_status": "confirmed",
        "access_start": "2026-07-13T15:00:00+02:00",
        "access_end": "2026-07-16T11:00:00+02:00",
        "provision_at": "2026-07-13T09:00:00+02:00",
    }
    assert "code" not in guesty_entity.extra_state_attributes
    assert "guest_name" not in guesty_entity.extra_state_attributes


def test_ttlock_status_sensor_reports_partial_delivery_without_pin() -> None:
    """TTLock exposes per-lock progress but never the reservation PIN."""
    coordinator = _coordinator()
    manager = _loxone_manager(
        {
            "ttlock_status": "partial",
            "mapped_locks": 3,
            "provisioned_locks": 2,
            "access_start": datetime(2026, 7, 13, 15, 0, tzinfo=TZ),
            "access_end": datetime(2026, 7, 16, 11, 0, tzinfo=TZ),
            "provision_at": datetime(2026, 7, 13, 9, 0, tzinfo=TZ),
            "reservation_status": "confirmed",
            "data_stale": False,
        }
    )
    entity = GuestyTTLockPinStatusSensor(coordinator, manager, "listing-1")

    assert entity.native_value == "partial"
    assert entity.extra_state_attributes["mapped_locks"] == 3
    assert entity.extra_state_attributes["provisioned_locks"] == 2
    assert "code" not in entity.extra_state_attributes


def test_new_listings_create_entities_once_during_runtime() -> None:
    """Coordinator updates add new sensors and calendars without a reload."""
    coordinator = _coordinator()
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            access_manager=_access_manager(),
            loxone_manager=_loxone_manager(),
            sensor_listing_ids=set(),
            calendar_listing_ids=set(),
        )
    )
    add_sensors = MagicMock()
    add_calendars = MagicMock()

    add_sensor_entities(coordinator, entry, add_sensors)
    add_calendar_entities(coordinator, entry, add_calendars)
    add_sensor_entities(coordinator, entry, add_sensors)
    add_calendar_entities(coordinator, entry, add_calendars)

    add_sensors.assert_called_once()
    add_calendars.assert_called_once()
    assert len(add_sensors.call_args.args[0]) == 5
    assert entry.runtime_data.sensor_listing_ids == {"listing-1"}
    assert entry.runtime_data.calendar_listing_ids == {"listing-1"}


def test_removed_listing_entities_become_unavailable() -> None:
    """A removed listing is recognized immediately without stale states."""
    coordinator = _coordinator()
    sensor = GuestyOccupancySensor(coordinator, "listing-1")
    guest_sensor = GuestyCurrentGuestSensor(coordinator, "listing-1")
    access_sensor = GuestyAccessLinkSensor(coordinator, _access_manager(), "listing-1")
    calendar = GuestyReservationCalendar(coordinator, "listing-1")

    coordinator.data.listings.clear()
    coordinator.data.occupancy.clear()

    assert not sensor.available
    assert not guest_sensor.available
    assert not access_sensor.available
    assert not calendar.available
