"""Tests for Guesty data models and occupancy logic."""

from __future__ import annotations

from datetime import datetime
import zoneinfo

import pytest

from custom_components.guesty import models

TZ = zoneinfo.ZoneInfo("Europe/Berlin")
FIXED_NOW = datetime(2026, 7, 13, 14, 30, tzinfo=TZ)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    """Keep reservation-window tests deterministic."""
    monkeypatch.setattr(models.dt_util, "now", lambda: FIXED_NOW)


def _listing() -> models.GuestyListing:
    return models.GuestyListing(
        id="listing-1",
        title="Test Wohnung",
        nickname="Test",
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )


def _reservation(
    *,
    reservation_id: str = "res-1",
    check_in_date: str | None = "2026-07-13",
    check_out_date: str | None = "2026-07-16",
    check_in_utc: str | None = None,
    check_out_utc: str | None = None,
    planned_arrival: str | None = None,
    planned_departure: str | None = None,
    status: str = "confirmed",
) -> models.GuestyReservation:
    return models.GuestyReservation(
        id=reservation_id,
        listing_id="listing-1",
        status=status,
        confirmation_code="GY-TEST",
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        check_in_utc=check_in_utc,
        check_out_utc=check_out_utc,
        planned_arrival=planned_arrival,
        planned_departure=planned_departure,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name="Max Mustermann",
        last_updated_at=None,
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 7, 13, 14, 59, tzinfo=TZ), "vacant"),
        (datetime(2026, 7, 13, 15, 0, tzinfo=TZ), "occupied"),
        (datetime(2026, 7, 16, 11, 0, tzinfo=TZ), "vacant"),
    ],
)
def test_occupancy_boundaries(moment, expected) -> None:
    """Check-in is inclusive and checkout is exclusive."""
    occupancy = models.calculate_listing_occupancy(
        _listing(),
        [_reservation()],
        moment,
    )
    assert occupancy.status == expected


def test_reservation_codes_are_parsed_but_not_written_to_general_cache() -> None:
    """Native Keycodes and custom fields stay out of the general cache."""
    reservation = models.GuestyReservation.from_api(
        {
            "_id": "res-keycode",
            "listingId": "listing-1",
            "status": "confirmed",
            "checkIn": "2026-07-20T13:00:00Z",
            "checkOut": "2026-07-22T09:00:00Z",
            "customFields": [
                {"fieldId": "65fab102a5284d73c6206db0", "value": "712346"}
            ],
            "notes": {"keyCode": 712345},
        }
    )

    assert reservation is not None
    assert reservation.key_code == "712345"
    assert reservation.key_code_observed is True
    assert reservation.custom_fields == {"65fab102a5284d73c6206db0": "712346"}
    assert reservation.custom_fields_observed is True
    assert reservation.legacy_key_code is None
    assert "key_code" not in reservation.to_dict()
    cached = models.GuestyReservation.from_dict(reservation.to_dict())
    assert cached.key_code is None
    assert cached.key_code_observed is False
    assert cached.custom_fields == {}
    assert cached.custom_fields_observed is False
    assert cached.legacy_key_code is None


def test_missing_live_notes_is_an_authoritative_empty_keycode() -> None:
    """Guesty's omission of an empty notes object must still allow generation."""
    reservation = models.GuestyReservation.from_api(
        {
            "_id": "res-empty-keycode",
            "listing": {"_id": "listing-1"},
            "checkIn": "2026-07-15",
            "checkOut": "2026-07-16",
        }
    )

    assert reservation is not None
    assert reservation.key_code is None
    assert reservation.key_code_observed is True


def test_planned_arrival_overrides_default() -> None:
    """Planned arrival changes the occupancy transition."""
    reservation = _reservation(planned_arrival="13:00")
    before = models.calculate_listing_occupancy(
        _listing(),
        [reservation],
        datetime(2026, 7, 13, 12, 59, tzinfo=TZ),
    )
    after = models.calculate_listing_occupancy(
        _listing(),
        [reservation],
        datetime(2026, 7, 13, 13, 0, tzinfo=TZ),
    )
    assert before.status == "vacant"
    assert after.status == "occupied"


def test_planned_times_override_stale_utc_timestamps() -> None:
    """Manual Guesty time changes win even if checkIn/checkOut stay unchanged."""
    reservation = _reservation(
        check_in_utc="2026-07-13T13:00:00Z",
        check_out_utc="2026-07-16T09:00:00Z",
        planned_arrival="12:00",
        planned_departure="14:00",
    )

    assert reservation.check_in_datetime(_listing()) == datetime(
        2026, 7, 13, 12, 0, tzinfo=TZ
    )
    assert reservation.check_out_datetime(_listing()) == datetime(
        2026, 7, 16, 14, 0, tzinfo=TZ
    )


def test_invalid_planned_time_falls_back_to_utc_timestamp() -> None:
    """One malformed optional override cannot replace a valid Guesty timestamp."""
    reservation = _reservation(
        check_in_utc="2026-07-13T13:00:00Z",
        planned_arrival="invalid",
    )

    assert reservation.check_in_datetime(_listing()) == datetime(
        2026, 7, 13, 13, 0, tzinfo=zoneinfo.ZoneInfo("UTC")
    )


def test_invalid_local_date_with_planned_time_falls_back_to_utc() -> None:
    """A malformed localized date cannot hide a valid UTC timestamp."""
    reservation = _reservation(
        check_in_date="invalid",
        check_in_utc="2026-07-13T13:00:00Z",
        planned_arrival="12:00",
    )

    assert reservation.check_in_datetime(_listing()) == datetime(
        2026, 7, 13, 13, 0, tzinfo=zoneinfo.ZoneInfo("UTC")
    )


def test_utc_timestamps_are_supported_without_localized_dates() -> None:
    """UTC-only reservations drive occupancy and survive cache pruning."""
    reservation = _reservation(
        reservation_id="res-utc",
        check_in_date=None,
        check_out_date=None,
        check_in_utc="2026-07-13T11:00:00.000Z",
        check_out_utc="2026-07-16T08:00:00.000Z",
    )
    occupancy = models.calculate_listing_occupancy(
        _listing(),
        [reservation],
        datetime(2026, 7, 13, 13, 0, tzinfo=TZ),
    )
    merged = models.merge_reservations(
        [reservation],
        [],
        days_past=30,
        days_future=365,
    )
    assert occupancy.status == "occupied"
    assert merged == [reservation]


def test_old_utc_only_reservation_is_pruned() -> None:
    """UTC-only records no longer remain in cache forever."""
    reservation = _reservation(
        check_in_date=None,
        check_out_date=None,
        check_in_utc="2025-01-01T11:00:00Z",
        check_out_utc="2025-01-02T08:00:00Z",
    )
    assert (
        models.merge_reservations(
            [reservation],
            [],
            days_past=30,
            days_future=365,
        )
        == []
    )


def test_cancelled_reservation_removed_on_merge() -> None:
    """Cancelled reservations are removed during incremental merge."""
    merged = models.merge_reservations(
        [_reservation()],
        [_reservation(status="cancelled")],
        days_past=30,
        days_future=365,
    )
    assert merged == []


def test_invalid_date_range_is_ignored() -> None:
    """A checkout before check-in cannot create occupancy or calendar overlap."""
    reservation = _reservation(
        check_in_date="2026-07-16",
        check_out_date="2026-07-13",
    )
    occupancy = models.calculate_listing_occupancy(
        _listing(),
        [reservation],
        FIXED_NOW,
    )
    assert occupancy.status == "vacant"
    assert not models.reservation_overlaps_range(
        reservation,
        _listing(),
        datetime(2026, 7, 1, tzinfo=TZ),
        datetime(2026, 8, 1, tzinfo=TZ),
    )


def test_overlapping_current_reservations_are_deterministic() -> None:
    """API ordering cannot change the selected current reservation."""
    earlier = _reservation(reservation_id="a", check_in_date="2026-07-12")
    later = _reservation(reservation_id="b", check_in_date="2026-07-13")
    first = models.calculate_listing_occupancy(
        _listing(), [later, earlier], FIXED_NOW
    ).current_reservation
    second = models.calculate_listing_occupancy(
        _listing(), [earlier, later], FIXED_NOW
    ).current_reservation
    assert first == second == earlier


def test_listing_accepts_id_without_private_id() -> None:
    """Listing fallback titles work with Guesty's alternative id field."""
    listing = models.GuestyListing.from_api({"id": "listing-id", "pms": {}})
    assert listing.id == "listing-id"
    assert listing.title == "listing-id"


def test_partial_listing_webhook_preserves_existing_fields() -> None:
    """Sparse listing updates cannot erase names, times, or timezone data."""
    existing = _listing()
    listing = models.GuestyListing.from_api(
        {"_id": "listing-1", "nickname": "Renamed"},
        fallback=existing,
    )

    assert listing.nickname == "Renamed"
    assert listing.title == existing.title
    assert listing.timezone == existing.timezone
    assert listing.default_check_in_time == existing.default_check_in_time


def test_reservation_without_id_is_ignored() -> None:
    """Malformed API records cannot crash a complete sync."""
    assert (
        models.GuestyReservation.from_api(
            {
                "listingId": "listing-1",
                "checkIn": "2026-07-13T12:00:00Z",
                "checkOut": "2026-07-14T10:00:00Z",
            }
        )
        is None
    )


def test_next_transition_returns_check_in() -> None:
    """The scheduler sees the nearest upcoming check-in."""
    transition = models.get_next_transition(
        _listing(),
        [_reservation()],
        datetime(2026, 7, 10, 10, 0, tzinfo=TZ),
    )
    assert transition == datetime(2026, 7, 13, 15, 0, tzinfo=TZ)


def test_unknown_timezone_uses_home_assistant_timezone(monkeypatch) -> None:
    """Unknown Guesty timezones retain an aware fallback datetime."""
    listing = _listing()
    listing.timezone = "Invalid/Timezone"
    default_timezone = models.dt_util.DEFAULT_TIME_ZONE
    monkeypatch.setattr(models.dt_util, "get_time_zone", lambda value: None)

    assert _reservation().check_in_datetime(listing).tzinfo == default_timezone
