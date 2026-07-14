"""Constants for the Guesty integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "guesty"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_TOKEN_EXPIRES_AT: Final = "token_expires_at"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_LISTING_SYNC_INTERVAL: Final = "listing_sync_interval"
CONF_RESERVATION_DAYS_PAST: Final = "reservation_days_past"
CONF_RESERVATION_DAYS_FUTURE: Final = "reservation_days_future"
CONF_STALE_THRESHOLD_HOURS: Final = "stale_threshold_hours"
CONF_EXPOSE_GUEST_DETAILS: Final = "expose_guest_details"
CONF_ACCESS_ENABLED: Final = "access_enabled"
CONF_ACCESS_CUSTOM_FIELD: Final = "access_custom_field"
CONF_ACCESS_LOGO_URL: Final = "access_logo_url"
CONF_ACCESS_FAVICON_URL: Final = "access_favicon_url"
CONF_ACCESS_EARLY_MINUTES: Final = "access_early_minutes"
CONF_ACCESS_LATE_MINUTES: Final = "access_late_minutes"
CONF_ACCESS_LISTINGS: Final = "access_listings"
CONF_ACCESS_LOCK_MAPPINGS: Final = "access_lock_mappings"
CONF_ACCESS_LOCK_1: Final = "access_lock_1"
CONF_ACCESS_LOCK_1_NAME: Final = "access_lock_1_name"
CONF_ACCESS_LOCK_1_NAME_EN: Final = "access_lock_1_name_en"
CONF_ACCESS_LOCK_1_NAME_ES: Final = "access_lock_1_name_es"
CONF_ACCESS_LOCK_1_NAME_FR: Final = "access_lock_1_name_fr"
CONF_ACCESS_LOCK_2: Final = "access_lock_2"
CONF_ACCESS_LOCK_2_NAME: Final = "access_lock_2_name"
CONF_ACCESS_LOCK_2_NAME_EN: Final = "access_lock_2_name_en"
CONF_ACCESS_LOCK_2_NAME_ES: Final = "access_lock_2_name_es"
CONF_ACCESS_LOCK_2_NAME_FR: Final = "access_lock_2_name_fr"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_GUESTY_WEBHOOK_ID: Final = "guesty_webhook_id"

DEFAULT_SCAN_INTERVAL: Final = 300
DEFAULT_LISTING_SYNC_INTERVAL: Final = 86400
DEFAULT_RESERVATION_DAYS_PAST: Final = 30
DEFAULT_RESERVATION_DAYS_FUTURE: Final = 365
DEFAULT_STALE_THRESHOLD_HOURS: Final = 6
DEFAULT_EXPOSE_GUEST_DETAILS: Final = False
DEFAULT_ACCESS_ENABLED: Final = False
DEFAULT_ACCESS_CUSTOM_FIELD: Final = "Door access link"
DEFAULT_ACCESS_LOGO_URL: Final = ""
DEFAULT_ACCESS_FAVICON_URL: Final = ""
DEFAULT_ACCESS_EARLY_MINUTES: Final = 0
DEFAULT_ACCESS_LATE_MINUTES: Final = 0

# Use a quicker listing fallback only while push updates are unavailable.
WEBHOOK_INACTIVE_LISTING_SYNC_INTERVAL: Final = 900
WEBHOOK_DEBOUNCE_SECONDS: Final = 0.75

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
API_MAX_PAGES: Final = 1000
API_REQUEST_TIMEOUT: Final = 30.0

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

WEBHOOK_SUBSCRIPTION_EVENTS: Final = (
    "reservation.new",
    "reservation.updated",
    "listing.new",
    "listing.updated",
    "listing.removed",
)

# Accept newer payload names defensively, but only subscribe to event names
# documented by Guesty's Open API.
WEBHOOK_EVENTS: Final = (
    *WEBHOOK_SUBSCRIPTION_EVENTS,
    "reservation.created.v2",
    "reservation.updated.v2",
)

EVENT_OCCUPANCY_CHANGED: Final = "guesty_occupancy_changed"
EVENT_DOOR_ACCESS: Final = "guesty_door_access"

ACCESS_URL_PATH: Final = "/api/guesty/access"
ACCESS_TOKEN_BYTES: Final = 32
ACCESS_ACTION_NONCE_SECONDS: Final = 120
ACCESS_UNLOCK_COOLDOWN_SECONDS: Final = 5
ACCESS_RATE_LIMIT_WINDOW_SECONDS: Final = 60
ACCESS_RATE_LIMIT_MAX_ACTIONS: Final = 10
ACCESS_MAX_REQUEST_BYTES: Final = 4096

SENSOR_OCCUPANCY: Final = "occupancy"
SENSOR_CURRENT_GUEST: Final = "current_guest"
SENSOR_ACCESS_LINK: Final = "access_link"
SENSOR_SYNC_STATUS: Final = "sync_status"

SYNC_STATUS_OK: Final = "ok"
SYNC_STATUS_DEGRADED: Final = "degraded"
SYNC_STATUS_ERROR: Final = "error"
