"""Constants for the Guesty integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "guesty"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_LISTING_SYNC_INTERVAL: Final = "listing_sync_interval"
CONF_RESERVATION_DAYS_PAST: Final = "reservation_days_past"
CONF_RESERVATION_DAYS_FUTURE: Final = "reservation_days_future"
CONF_STALE_THRESHOLD_HOURS: Final = "stale_threshold_hours"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_GUESTY_WEBHOOK_ID: Final = "guesty_webhook_id"

DEFAULT_SCAN_INTERVAL: Final = 300
DEFAULT_LISTING_SYNC_INTERVAL: Final = 86400
DEFAULT_RESERVATION_DAYS_PAST: Final = 30
DEFAULT_RESERVATION_DAYS_FUTURE: Final = 365
DEFAULT_STALE_THRESHOLD_HOURS: Final = 6

MIN_SCAN_INTERVAL: Final = 60
MAX_SCAN_INTERVAL: Final = 3600

API_BASE_URL: Final = "https://open-api.guesty.com/v1"
OAUTH_URL: Final = "https://open-api.guesty.com/oauth2/token"

DEFAULT_CHECK_IN_TIME: Final = "15:00"
DEFAULT_CHECK_OUT_TIME: Final = "11:00"

ACTIVE_RESERVATION_STATUSES: Final = frozenset(
    {
        "confirmed",
        "reserved",
        "checked_in",
        "checked-in",
        "in_house",
        "in-house",
    }
)

INACTIVE_RESERVATION_STATUSES: Final = frozenset(
    {
        "canceled",
        "cancelled",
        "closed",
        "declined",
        "expired",
    }
)

STORAGE_VERSION: Final = 2
STORAGE_KEY: Final = "guesty_cache"

TOKEN_REFRESH_MARGIN: Final = timedelta(minutes=30)

API_MAX_RETRIES: Final = 3
API_RETRY_BASE_DELAY: Final = 1.0
API_RETRY_MAX_DELAY: Final = 60.0

LISTING_FIELDS: Final = (
    "_id nickname title defaultCheckInTime defaultCheckOutTime timezone pms.active"
)

RESERVATION_FIELDS: Final = (
    "_id listingId status confirmationCode "
    "checkIn checkOut checkInDateLocalized checkOutDateLocalized "
    "plannedArrival plannedDeparture lastUpdatedAt "
    "listing.defaultCheckInTime listing.defaultCheckOutTime "
    "guest.fullName"
)

WEBHOOK_EVENTS: Final = (
    "reservation.new",
    "reservation.updated",
    "listing.new",
    "listing.updated",
    "listing.removed",
)

EVENT_OCCUPANCY_CHANGED: Final = "guesty_occupancy_changed"

SENSOR_OCCUPANCY: Final = "occupancy"
SENSOR_SYNC_STATUS: Final = "sync_status"

SYNC_STATUS_OK: Final = "ok"
SYNC_STATUS_DEGRADED: Final = "degraded"
SYNC_STATUS_ERROR: Final = "error"
