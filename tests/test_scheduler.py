"""Tests for exact check-in and checkout transition scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zoneinfo

import pytest

from custom_components.guesty.models import GuestyListing, GuestyReservation
from custom_components.guesty import scheduler as scheduler_module
from custom_components.guesty.scheduler import GuestyTransitionScheduler

TZ = zoneinfo.ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=TZ)


def _listing() -> GuestyListing:
    return GuestyListing(
        id="listing-1",
        title="Listing",
        nickname=None,
        default_check_in_time="15:00",
        default_check_out_time="11:00",
        timezone="Europe/Berlin",
        active=True,
    )


def _reservation(
    check_in: str = "2026-07-14",
    check_out: str = "2026-07-16",
) -> GuestyReservation:
    return GuestyReservation(
        id="reservation-1",
        listing_id="listing-1",
        status="confirmed",
        confirmation_code=None,
        check_in_date=check_in,
        check_out_date=check_out,
        check_in_utc=None,
        check_out_utc=None,
        planned_arrival=None,
        planned_departure=None,
        listing_default_check_in=None,
        listing_default_check_out=None,
        guest_name=None,
        last_updated_at=None,
    )


@pytest.mark.asyncio
async def test_scheduler_tracks_and_runs_next_transition(hass, monkeypatch) -> None:
    """The nearest check-in schedules one tracked Home Assistant callback."""
    callback_holder = {}
    unsubscribe = MagicMock()

    def track(hass, callback, point_in_time):
        callback_holder["callback"] = callback
        callback_holder["time"] = point_in_time
        return unsubscribe

    monkeypatch.setattr(scheduler_module.dt_util, "now", lambda: NOW)
    monkeypatch.setattr(scheduler_module, "async_track_point_in_time", track)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[_reservation()],
        )
    )
    on_transition = AsyncMock()
    scheduler = GuestyTransitionScheduler(hass, coordinator, on_transition)

    scheduler.async_schedule()

    assert callback_holder["time"] == datetime(2026, 7, 14, 15, 0, tzinfo=TZ)
    callback_holder["callback"](callback_holder["time"])
    await hass.async_block_till_done()
    on_transition.assert_awaited_once_with()
    assert scheduler._unsub is None


def test_scheduler_cancels_previous_and_ignores_distant_transitions(
    hass, monkeypatch
) -> None:
    """Rescheduling cancels the prior timer and caps the scheduling horizon."""
    monkeypatch.setattr(scheduler_module.dt_util, "now", lambda: NOW)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            listings={"listing-1": _listing()},
            reservations=[_reservation(check_in="2026-09-01", check_out="2026-09-03")],
        )
    )
    scheduler = GuestyTransitionScheduler(hass, coordinator, AsyncMock())
    unsubscribe = MagicMock()
    scheduler._unsub = unsubscribe

    scheduler.async_schedule()

    unsubscribe.assert_called_once_with()
    assert scheduler._unsub is None
    assert datetime(2026, 9, 1, tzinfo=TZ) - NOW > timedelta(days=30)
