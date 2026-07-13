"""Unit tests for Guesty occupancy logic."""

from __future__ import annotations

from datetime import datetime
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
import zoneinfo

ROOT = Path(__file__).resolve().parents[1]
GUESTY_PATH = ROOT / "custom_components" / "guesty"
TZ = zoneinfo.ZoneInfo("Europe/Berlin")
FIXED_NOW = datetime(2026, 7, 13, 14, 30, tzinfo=TZ)


def _load_module(name: str, path: Path, package: str) -> ModuleType:
    """Load a module file into a fake package hierarchy."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    module.__package__ = package
    spec.loader.exec_module(module)
    return module


def _load_models() -> ModuleType:
    """Load models with mocked Home Assistant dependencies."""
    dt_util = MagicMock()
    dt_util.UTC = zoneinfo.ZoneInfo("UTC")
    dt_util.DEFAULT_TIME_ZONE = TZ
    dt_util.get_time_zone = lambda value: zoneinfo.ZoneInfo(value)
    dt_util.now.return_value = FIXED_NOW
    dt_util.parse_datetime = lambda value: datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    sys.modules["homeassistant"] = ModuleType("homeassistant")
    sys.modules["homeassistant.util"] = ModuleType("homeassistant.util")
    sys.modules["homeassistant.util.dt"] = dt_util

    if "custom_components" not in sys.modules:
        sys.modules["custom_components"] = ModuleType("custom_components")
    if "custom_components.guesty" not in sys.modules:
        sys.modules["custom_components.guesty"] = ModuleType("custom_components.guesty")

    _load_module(
        "custom_components.guesty.const",
        GUESTY_PATH / "const.py",
        "custom_components.guesty",
    )
    return _load_module(
        "custom_components.guesty.models",
        GUESTY_PATH / "models.py",
        "custom_components.guesty",
    )


models = _load_models()


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
    check_in_date: str = "2026-07-13",
    check_out_date: str = "2026-07-16",
    planned_arrival: str | None = None,
    planned_departure: str | None = None,
    status: str = "confirmed",
) -> models.GuestyReservation:
    return models.GuestyReservation(
        id="res-1",
        listing_id="listing-1",
        status=status,
        confirmation_code="GY-TEST",
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        check_in_utc=None,
        check_out_utc=None,
        planned_arrival=planned_arrival,
        planned_departure=planned_departure,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name="Max Mustermann",
        last_updated_at=None,
    )


def test_vacant_before_check_in() -> None:
    """Listing is vacant before default check-in time."""
    listing = _listing()
    reservation = _reservation()
    occupancy = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 13, 14, 59, tzinfo=TZ),
    )
    assert occupancy.status == "vacant"


def test_occupied_after_check_in() -> None:
    """Listing becomes occupied at check-in time."""
    listing = _listing()
    reservation = _reservation()
    occupancy = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 13, 15, 0, tzinfo=TZ),
    )
    assert occupancy.status == "occupied"


def test_vacant_after_check_out() -> None:
    """Listing becomes vacant again after check-out."""
    listing = _listing()
    reservation = _reservation()
    occupancy = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 16, 11, 0, tzinfo=TZ),
    )
    assert occupancy.status == "vacant"


def test_planned_arrival_overrides_default() -> None:
    """Planned arrival changes the occupancy transition."""
    listing = _listing()
    reservation = _reservation(planned_arrival="13:00")
    before = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 13, 12, 59, tzinfo=TZ),
    )
    after = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 13, 13, 0, tzinfo=TZ),
    )
    assert before.status == "vacant"
    assert after.status == "occupied"


def test_utc_fallback() -> None:
    """UTC timestamps are used when localized dates are missing."""
    listing = _listing()
    reservation = models.GuestyReservation(
        id="res-utc",
        listing_id="listing-1",
        status="confirmed",
        confirmation_code=None,
        check_in_date=None,
        check_out_date=None,
        check_in_utc="2026-07-13T11:00:00.000Z",
        check_out_utc="2026-07-16T08:00:00.000Z",
        planned_arrival=None,
        planned_departure=None,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name=None,
        last_updated_at=None,
    )
    occupancy = models.calculate_listing_occupancy(
        listing,
        [reservation],
        datetime(2026, 7, 13, 13, 0, tzinfo=TZ),
    )
    assert occupancy.status == "occupied"


def test_cancelled_reservation_removed_on_merge() -> None:
    """Cancelled reservations are removed during merge."""
    existing = [_reservation()]
    cancelled = _reservation(status="cancelled")
    merged = models.merge_reservations(
        existing,
        [cancelled],
        days_past=30,
        days_future=365,
    )
    assert merged == []


def test_next_transition_returns_check_in() -> None:
    """Next transition is the upcoming check-in when vacant."""
    listing = _listing()
    reservation = _reservation()
    transition = models.get_next_transition(
        listing,
        [reservation],
        datetime(2026, 7, 10, 10, 0, tzinfo=TZ),
    )
    assert transition == datetime(2026, 7, 13, 15, 0, tzinfo=TZ)


def test_unknown_timezone_uses_home_assistant_timezone(monkeypatch) -> None:
    """Unknown Guesty timezone names retain aware datetime values."""
    listing = _listing()
    listing.timezone = "Invalid/Timezone"
    monkeypatch.setattr(models.dt_util, "get_time_zone", lambda value: None)

    check_in = _reservation().check_in_datetime(listing)

    assert check_in.tzinfo == TZ


def test_merge_tolerates_invalid_localized_dates() -> None:
    """Malformed API dates do not crash incremental cache pruning."""
    reservation = _reservation(check_in_date="invalid")

    merged = models.merge_reservations(
        [reservation],
        [],
        days_past=30,
        days_future=365,
    )

    assert merged == [reservation]
