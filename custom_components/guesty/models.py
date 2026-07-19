"""Data models and occupancy helpers for Guesty."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_RESERVATION_STATUSES,
    DEFAULT_CHECK_IN_TIME,
    DEFAULT_CHECK_OUT_TIME,
    INACTIVE_RESERVATION_STATUSES,
)

_LOGGER = logging.getLogger(__name__)


def _parse_time(value: str | None, default: str) -> time:
    """Parse HH:MM time strings from Guesty."""
    raw = (value or default).strip()
    try:
        hour, minute = raw.split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        hour, minute = default.split(":", 1)
        return time(int(hour), int(minute))


def _parse_optional_time(value: str | None) -> time | None:
    """Parse an explicit Guesty time without inventing a fallback value."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # Guesty documents HH:MM, while accepting seconds here makes the parser
        # robust to equivalent ISO time responses.
        return time.fromisoformat(value.strip()).replace(tzinfo=None)
    except ValueError:
        return None


def _combine_date_time(
    day: date,
    clock: time,
    timezone: str | None,
) -> datetime:
    """Combine localized date and time in the listing timezone."""
    tz = dt_util.get_time_zone(timezone) if timezone else dt_util.DEFAULT_TIME_ZONE
    if tz is None:
        _LOGGER.warning(
            "Unknown Guesty listing timezone %r; using Home Assistant timezone",
            timezone,
        )
        tz = dt_util.DEFAULT_TIME_ZONE
    return datetime.combine(day, clock, tzinfo=tz)


def _parse_utc_datetime(value: str | None) -> datetime | None:
    """Parse Guesty UTC datetime strings."""
    if not value:
        return None
    try:
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt_util.UTC)
        return parsed
    except (ValueError, TypeError):
        return None


@dataclass(slots=True)
class GuestyListing:
    """A Guesty property listing."""

    id: str
    title: str
    nickname: str | None
    default_check_in_time: str
    default_check_out_time: str
    timezone: str | None
    active: bool

    @property
    def display_name(self) -> str:
        """Return the best available display name."""
        return self.nickname or self.title or self.id

    @classmethod
    def from_api(
        cls,
        data: dict[str, Any],
        fallback: GuestyListing | None = None,
    ) -> GuestyListing:
        """Create a listing from API data."""
        listing_id = data.get("_id") or data.get("id")
        if not listing_id:
            raise ValueError("Listing payload missing id")
        pms = data.get("pms") or {}
        if not isinstance(pms, dict):
            pms = {}
        if "active" in pms:
            active = bool(pms["active"])
        elif "active" in data:
            active = bool(data["active"])
        else:
            active = fallback.active if fallback else True
        return cls(
            id=str(listing_id),
            title=data.get("title")
            or (fallback.title if fallback else str(listing_id)),
            nickname=(
                data.get("nickname")
                if "nickname" in data
                else fallback.nickname
                if fallback
                else None
            ),
            default_check_in_time=data.get("defaultCheckInTime")
            or (fallback.default_check_in_time if fallback else None)
            or DEFAULT_CHECK_IN_TIME,
            default_check_out_time=data.get("defaultCheckOutTime")
            or (fallback.default_check_out_time if fallback else None)
            or DEFAULT_CHECK_OUT_TIME,
            timezone=(
                data.get("timezone")
                if "timezone" in data
                else fallback.timezone
                if fallback
                else None
            ),
            active=active,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize listing for storage."""
        return {
            "id": self.id,
            "title": self.title,
            "nickname": self.nickname,
            "default_check_in_time": self.default_check_in_time,
            "default_check_out_time": self.default_check_out_time,
            "timezone": self.timezone,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuestyListing:
        """Deserialize listing from storage."""
        return cls(
            id=data["id"],
            title=data["title"],
            nickname=data.get("nickname"),
            default_check_in_time=data.get("default_check_in_time")
            or DEFAULT_CHECK_IN_TIME,
            default_check_out_time=data.get("default_check_out_time")
            or DEFAULT_CHECK_OUT_TIME,
            timezone=data.get("timezone"),
            active=data.get("active", True),
        )


@dataclass(slots=True)
class GuestyReservation:
    """A Guesty reservation."""

    id: str
    listing_id: str
    status: str
    confirmation_code: str | None
    check_in_date: str | None
    check_out_date: str | None
    check_in_utc: str | None
    check_out_utc: str | None
    planned_arrival: str | None
    planned_departure: str | None
    listing_default_check_in: str | None
    listing_default_check_out: str | None
    guest_name: str | None
    last_updated_at: str | None
    key_code: str | None = None
    key_code_observed: bool = False
    custom_fields: dict[str, Any] = field(default_factory=dict)
    custom_fields_observed: bool = False
    legacy_key_code: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> GuestyReservation | None:
        """Create a reservation from API data."""
        reservation_id = data.get("_id") or data.get("id")
        listing_id = data.get("listingId") or (data.get("listing") or {}).get("_id")
        if not reservation_id or not listing_id:
            return None

        check_in = data.get("checkInDateLocalized")
        check_out = data.get("checkOutDateLocalized")
        check_in_utc = data.get("checkIn")
        check_out_utc = data.get("checkOut")

        if not ((check_in and check_out) or (check_in_utc and check_out_utc)):
            return None

        listing = data.get("listing") or {}
        guest = data.get("guest") or {}
        notes = data.get("notes") or {}
        if not isinstance(notes, dict):
            notes = {}
        raw_custom_fields = data.get("customFields")
        custom_fields: dict[str, Any] = {}
        if isinstance(raw_custom_fields, list):
            for item in raw_custom_fields:
                if not isinstance(item, dict):
                    continue
                field_id = item.get("fieldId")
                if isinstance(field_id, str) and field_id:
                    custom_fields[field_id] = item.get("value")
        return cls(
            id=str(reservation_id),
            listing_id=str(listing_id),
            status=(data.get("status") or "").lower(),
            confirmation_code=data.get("confirmationCode"),
            check_in_date=check_in,
            check_out_date=check_out,
            check_in_utc=check_in_utc,
            check_out_utc=check_out_utc,
            planned_arrival=data.get("plannedArrival"),
            planned_departure=data.get("plannedDeparture"),
            listing_default_check_in=listing.get("defaultCheckInTime"),
            listing_default_check_out=listing.get("defaultCheckOutTime"),
            guest_name=guest.get("fullName"),
            last_updated_at=data.get("lastUpdatedAt"),
            # The configured reservation custom field is selected only after its
            # reference has been resolved to an account-specific field ID.
            key_code=None,
            key_code_observed=False,
            custom_fields=custom_fields,
            custom_fields_observed=isinstance(raw_custom_fields, list),
            # Keep the former built-in Keycode only in memory for a one-time,
            # non-rotating migration into the configured custom field.
            legacy_key_code=(
                str(notes["keyCode"]).strip()
                if notes.get("keyCode") is not None
                else None
            ),
        )

    def is_active_status(self) -> bool:
        """Return whether the reservation should affect occupancy."""
        return self.status in ACTIVE_RESERVATION_STATUSES

    def is_inactive_status(self) -> bool:
        """Return whether the reservation was cancelled or closed."""
        return self.status in INACTIVE_RESERVATION_STATUSES

    def check_in_datetime(self, listing: GuestyListing) -> datetime:
        """Return check-in datetime with UTC and localized fallbacks."""
        planned_time = _parse_optional_time(self.planned_arrival)
        if self.check_in_date and planned_time is not None:
            # Guesty exposes manual check-in time changes through
            # plannedArrival. It overrides the otherwise useful checkIn UTC
            # timestamp, which can still contain the former default time.
            try:
                return _combine_date_time(
                    date.fromisoformat(self.check_in_date),
                    planned_time,
                    listing.timezone,
                )
            except (TypeError, ValueError):
                pass

        utc_dt = _parse_utc_datetime(self.check_in_utc)
        if utc_dt is not None:
            return utc_dt

        if not self.check_in_date:
            raise ValueError(f"Reservation {self.id} has no check-in date")

        check_in_time = _parse_time(
            self.planned_arrival
            or self.listing_default_check_in
            or listing.default_check_in_time,
            DEFAULT_CHECK_IN_TIME,
        )
        return _combine_date_time(
            date.fromisoformat(self.check_in_date),
            check_in_time,
            listing.timezone,
        )

    def check_out_datetime(self, listing: GuestyListing) -> datetime:
        """Return check-out datetime with UTC and localized fallbacks."""
        planned_time = _parse_optional_time(self.planned_departure)
        if self.check_out_date and planned_time is not None:
            # plannedDeparture has the same override semantics as arrival.
            try:
                return _combine_date_time(
                    date.fromisoformat(self.check_out_date),
                    planned_time,
                    listing.timezone,
                )
            except (TypeError, ValueError):
                pass

        utc_dt = _parse_utc_datetime(self.check_out_utc)
        if utc_dt is not None:
            return utc_dt

        if not self.check_out_date:
            raise ValueError(f"Reservation {self.id} has no check-out date")

        check_out_time = _parse_time(
            self.planned_departure
            or self.listing_default_check_out
            or listing.default_check_out_time,
            DEFAULT_CHECK_OUT_TIME,
        )
        return _combine_date_time(
            date.fromisoformat(self.check_out_date),
            check_out_time,
            listing.timezone,
        )

    def is_occupied_at(self, moment: datetime, listing: GuestyListing) -> bool:
        """Return whether the listing is occupied at a given moment."""
        if not self.is_active_status():
            return False
        check_in, check_out = self.stay_datetimes(listing)
        return check_in <= moment < check_out

    def stay_datetimes(self, listing: GuestyListing) -> tuple[datetime, datetime]:
        """Return and validate the reservation interval."""
        check_in = self.check_in_datetime(listing)
        check_out = self.check_out_datetime(listing)
        if check_out <= check_in:
            raise ValueError(f"Reservation {self.id} has an invalid date range")
        return check_in, check_out

    def to_dict(self) -> dict[str, Any]:
        """Serialize reservation for storage."""
        return {
            "id": self.id,
            "listing_id": self.listing_id,
            "status": self.status,
            "confirmation_code": self.confirmation_code,
            "check_in_date": self.check_in_date,
            "check_out_date": self.check_out_date,
            "check_in_utc": self.check_in_utc,
            "check_out_utc": self.check_out_utc,
            "planned_arrival": self.planned_arrival,
            "planned_departure": self.planned_departure,
            "listing_default_check_in": self.listing_default_check_in,
            "listing_default_check_out": self.listing_default_check_out,
            "guest_name": self.guest_name,
            "last_updated_at": self.last_updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuestyReservation:
        """Deserialize reservation from storage."""
        return cls(
            id=data["id"],
            listing_id=data["listing_id"],
            status=data.get("status", ""),
            confirmation_code=data.get("confirmation_code"),
            check_in_date=data.get("check_in_date"),
            check_out_date=data.get("check_out_date"),
            check_in_utc=data.get("check_in_utc"),
            check_out_utc=data.get("check_out_utc"),
            planned_arrival=data.get("planned_arrival"),
            planned_departure=data.get("planned_departure"),
            listing_default_check_in=data.get("listing_default_check_in"),
            listing_default_check_out=data.get("listing_default_check_out"),
            guest_name=data.get("guest_name"),
            last_updated_at=data.get("last_updated_at"),
            # Access codes are intentionally never restored from the general
            # Guesty cache. The private Loxone store owns them until access expires.
            key_code=None,
            key_code_observed=False,
            custom_fields={},
            custom_fields_observed=False,
            legacy_key_code=None,
        )


@dataclass(slots=True)
class ListingOccupancy:
    """Occupancy state for a listing."""

    listing: GuestyListing
    status: str
    current_reservation: GuestyReservation | None
    next_reservation: GuestyReservation | None
    next_check_in: datetime | None
    next_check_out: datetime | None

    @property
    def is_occupied(self) -> bool:
        """Return whether the listing is currently occupied."""
        return self.status == "occupied"


def calculate_listing_occupancy(
    listing: GuestyListing,
    reservations: list[GuestyReservation],
    moment: datetime | None = None,
) -> ListingOccupancy:
    """Calculate occupancy for a listing at a given moment."""
    now = moment or dt_util.now()
    listing_reservations = [
        reservation
        for reservation in reservations
        if reservation.listing_id == listing.id and reservation.is_active_status()
    ]

    current_candidates: list[tuple[datetime, datetime, str, GuestyReservation]] = []
    upcoming: list[tuple[datetime, GuestyReservation]] = []

    for reservation in listing_reservations:
        try:
            check_in, check_out = reservation.stay_datetimes(listing)
        except (TypeError, ValueError):
            _LOGGER.debug("Skipping invalid reservation %s", reservation.id)
            continue

        if check_in <= now < check_out:
            current_candidates.append(
                (check_in, check_out, reservation.id, reservation)
            )
        elif check_in > now:
            upcoming.append((check_in, reservation))

    current = (
        min(current_candidates, key=lambda item: (item[0], item[1], item[2]))[3]
        if current_candidates
        else None
    )

    next_reservation: GuestyReservation | None = None
    next_check_in: datetime | None = None
    next_check_out: datetime | None = None

    if upcoming:
        upcoming.sort(key=lambda item: item[0])
        next_check_in, next_reservation = upcoming[0]
        next_check_out = next_reservation.check_out_datetime(listing)

    status = "occupied" if current else "vacant"
    return ListingOccupancy(
        listing=listing,
        status=status,
        current_reservation=current,
        next_reservation=next_reservation,
        next_check_in=next_check_in,
        next_check_out=next_check_out,
    )


def get_next_transition(
    listing: GuestyListing,
    reservations: list[GuestyReservation],
    moment: datetime | None = None,
) -> datetime | None:
    """Return the next occupancy state transition for a listing."""
    now = moment or dt_util.now()
    occupancy = calculate_listing_occupancy(listing, reservations, now)
    transitions: list[datetime] = []

    if occupancy.current_reservation:
        try:
            transitions.append(
                occupancy.current_reservation.check_out_datetime(listing)
            )
        except (TypeError, ValueError):
            pass

    if occupancy.next_check_in:
        transitions.append(occupancy.next_check_in)

    future = [transition for transition in transitions if transition > now]
    return min(future) if future else None


def reservation_overlaps_range(
    reservation: GuestyReservation,
    listing: GuestyListing,
    start: datetime,
    end: datetime,
) -> bool:
    """Return whether a reservation overlaps a datetime range."""
    try:
        check_in, check_out = reservation.stay_datetimes(listing)
    except (TypeError, ValueError):
        return False
    return start < end and check_in < end and check_out > start


def build_reservation_filters(
    days_past: int,
    days_future: int,
    *,
    updated_since: datetime | None = None,
    listing_ids: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Build Guesty reservation API filters for the sync window."""
    today = dt_util.now().date()
    start_date = (today - timedelta(days=days_past)).isoformat()
    end_date = (today + timedelta(days=days_future)).isoformat()
    filters: list[dict[str, Any]] = [
        {
            "operator": "$gte",
            "field": "checkOutDateLocalized",
            "value": start_date,
        },
        {
            "operator": "$lte",
            "field": "checkInDateLocalized",
            "value": end_date,
        },
    ]

    if listing_ids:
        filters.append(
            {
                "operator": "$in",
                "field": "listingId",
                "value": sorted(set(listing_ids)),
            }
        )

    if updated_since is None:
        filters.insert(
            0,
            {
                "operator": "$in",
                "field": "status",
                "value": sorted(ACTIVE_RESERVATION_STATUSES),
            },
        )
    else:
        filters.append(
            {
                "operator": "$gte",
                "field": "lastUpdatedAt",
                "value": updated_since.isoformat(),
            }
        )

    return filters


def merge_reservations(
    existing: list[GuestyReservation],
    updates: list[GuestyReservation],
    *,
    days_past: int,
    days_future: int,
) -> list[GuestyReservation]:
    """Merge reservation updates into the cached collection."""
    by_id = {reservation.id: reservation for reservation in existing}
    today = dt_util.now().date()
    window_start = today - timedelta(days=days_past)
    window_end = today + timedelta(days=days_future)

    for reservation in updates:
        if reservation.is_inactive_status():
            by_id.pop(reservation.id, None)
            continue
        if not reservation.is_active_status():
            continue
        by_id[reservation.id] = reservation

    pruned: list[GuestyReservation] = []
    for reservation in by_id.values():
        checkout_day = _reservation_day(
            reservation.check_out_date,
            reservation.check_out_utc,
        )
        checkin_day = _reservation_day(
            reservation.check_in_date,
            reservation.check_in_utc,
        )
        if checkout_day is None:
            _LOGGER.debug(
                "Dropping reservation %s with no valid checkout date",
                reservation.id,
            )
            continue
        if checkout_day >= window_start and (
            checkin_day is None or checkin_day <= window_end
        ):
            pruned.append(reservation)

    return sorted(pruned, key=lambda reservation: reservation.id)


def _reservation_day(localized: str | None, utc_value: str | None) -> date | None:
    """Return a reservation day from localized or UTC input."""
    if localized:
        try:
            return date.fromisoformat(localized)
        except (TypeError, ValueError):
            pass
    parsed = _parse_utc_datetime(utc_value)
    return parsed.date() if parsed else None
