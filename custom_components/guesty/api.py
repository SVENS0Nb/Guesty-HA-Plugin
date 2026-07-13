"""Guesty Open API client."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_BASE_URL,
    API_MAX_PAGES,
    API_MAX_RETRIES,
    API_REQUEST_TIMEOUT,
    API_RETRY_BASE_DELAY,
    API_RETRY_MAX_DELAY,
    LISTING_FIELDS,
    OAUTH_URL,
    RESERVATION_FIELDS,
    TOKEN_REFRESH_MARGIN,
    WEBHOOK_SUBSCRIPTION_EVENTS,
)
from .models import GuestyListing, GuestyReservation, build_reservation_filters

_LOGGER = logging.getLogger(__name__)

PAGE_LIMIT = 100
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def is_safe_resource_id(value: Any) -> bool:
    """Return whether a value is safe to insert into an API URL path."""
    return isinstance(value, str) and RESOURCE_ID_PATTERN.fullmatch(value) is not None


class GuestyAuthError(Exception):
    """Raised when Guesty authentication fails."""


class GuestyApiError(Exception):
    """Raised when a Guesty API request fails."""


class GuestyRetryableError(GuestyApiError):
    """Raised when a Guesty API request can be retried."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Initialize a retryable API error."""
        super().__init__(message)
        self.retry_after = retry_after


class GuestyPermissionError(GuestyApiError):
    """Raised when credentials work but API access is denied."""


class GuestyNotFoundError(GuestyApiError):
    """Raised when a Guesty resource no longer exists."""


class GuestyApiClient:
    """Async client for the Guesty Open API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        token: str | None = None,
        token_expires_at: float | None = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = token if isinstance(token, str) and token else None
        self._token_expires_at = (
            float(token_expires_at)
            if isinstance(token_expires_at, (int, float))
            and not isinstance(token_expires_at, bool)
            else None
        )
        self._token_lock = asyncio.Lock()
        self.last_rate_limit_remaining: int | None = None

    @classmethod
    def from_hass(
        cls,
        hass: Any,
        client_id: str,
        client_secret: str,
        token: str | None = None,
        token_expires_at: float | None = None,
    ) -> GuestyApiClient:
        """Create a client using Home Assistant's shared aiohttp session."""
        return cls(
            async_get_clientsession(hass),
            client_id,
            client_secret,
            token,
            token_expires_at,
        )

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._access_token

    @property
    def token_expires_at(self) -> float | None:
        """Return the token expiry timestamp."""
        return self._token_expires_at

    async def async_validate_credentials(self) -> str:
        """Validate credentials and return the account id."""
        await self._async_ensure_token()
        await self._async_request(
            "GET",
            "/listings",
            params={
                "fields": LISTING_FIELDS,
                "limit": "1",
            },
        )
        return hashlib.sha256(self._client_id.strip().encode()).hexdigest()

    async def async_get_listings(self) -> list[GuestyListing]:
        """Fetch all listings from the Guesty account."""
        await self._async_ensure_token()
        raw_listings = await self._async_paginate(
            "/listings",
            params={"fields": LISTING_FIELDS},
        )
        listings: list[GuestyListing] = []
        for item in raw_listings:
            try:
                listings.append(GuestyListing.from_api(item))
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignoring an invalid Guesty listing response")
        return listings

    async def async_get_reservations(
        self,
        days_past: int,
        days_future: int,
        *,
        updated_since: datetime | None = None,
        listing_ids: Collection[str] | None = None,
    ) -> list[GuestyReservation]:
        """Fetch reservations for the configured sync window."""
        await self._async_ensure_token()
        filters = build_reservation_filters(
            days_past,
            days_future,
            updated_since=updated_since,
            listing_ids=listing_ids,
        )
        raw_reservations = await self._async_paginate(
            "/reservations",
            params={
                "fields": RESERVATION_FIELDS,
                "filters": json.dumps(filters, separators=(",", ":")),
                "sort": "_id",
            },
        )

        reservations: list[GuestyReservation] = []
        for item in raw_reservations:
            reservation = GuestyReservation.from_api(item)
            if reservation:
                reservations.append(reservation)
        return reservations

    async def async_get_reservation(
        self, reservation_id: str
    ) -> GuestyReservation | None:
        """Fetch a single reservation by id."""
        self._validate_resource_id(reservation_id, "reservation")
        await self._async_ensure_token()
        try:
            data = await self._async_request(
                "GET",
                f"/reservations/{reservation_id}",
                params={"fields": RESERVATION_FIELDS},
            )
        except GuestyNotFoundError:
            return None
        if isinstance(data, dict):
            return GuestyReservation.from_api(data)
        return None

    async def async_register_webhook(self, url: str) -> str:
        """Register Guesty webhooks and return the webhook id."""
        await self._async_ensure_token()
        payload = {"url": url, "events": list(WEBHOOK_SUBSCRIPTION_EVENTS)}
        data = await self._async_request(
            "POST",
            "/webhooks",
            json_body=payload,
        )
        if not isinstance(data, dict) or not is_safe_resource_id(data.get("_id")):
            raise GuestyApiError("Unexpected webhook registration response")
        return data["_id"]

    async def async_webhook_matches(self, webhook_id: str, url: str) -> bool:
        """Return whether the stored remote webhook is still usable."""
        self._validate_resource_id(webhook_id, "webhook")
        await self._async_ensure_token()
        data = await self._async_request("GET", "/webhooks")
        for item in self._normalize_results(data):
            remote_id = item.get("_id") or item.get("id")
            if remote_id != webhook_id:
                continue

            remote_url = item.get("url")
            if isinstance(remote_url, str) and remote_url != url:
                return False

            events = item.get("events")
            if isinstance(events, list) and not set(
                WEBHOOK_SUBSCRIPTION_EVENTS
            ).issubset(events):
                return False

            if item.get("active") is False or item.get("enabled") is False:
                return False
            return True
        return False

    async def async_unregister_webhook(self, webhook_id: str) -> None:
        """Remove a Guesty webhook subscription."""
        self._validate_resource_id(webhook_id, "webhook")
        await self._async_ensure_token()
        await self._async_request("DELETE", f"/webhooks/{webhook_id}")

    def _has_valid_token(self) -> bool:
        """Return whether the current token is usable beyond the refresh margin."""
        if not self._access_token or self._token_expires_at is None:
            return False
        from homeassistant.util import dt as dt_util

        return (
            dt_util.utcnow().timestamp()
            < self._token_expires_at - TOKEN_REFRESH_MARGIN.total_seconds()
        )

    async def _async_ensure_token(
        self,
        force_refresh: bool = False,
        *,
        invalid_token: str | None = None,
    ) -> None:
        """Ensure a valid access token is available."""
        if not force_refresh and self._has_valid_token():
            return

        async with self._token_lock:
            if (
                invalid_token is not None
                and self._access_token != invalid_token
                and self._has_valid_token()
            ):
                return
            if not force_refresh and self._has_valid_token():
                return
            await self._async_refresh_token_locked()

    async def _async_refresh_token(self) -> None:
        """Request a new OAuth access token."""
        await self._async_ensure_token(force_refresh=True)

    async def _async_refresh_token_locked(self) -> None:
        """Request a token while the token lock is held."""
        last_error: GuestyApiError | None = None
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                await self._async_refresh_token_once()
                return
            except GuestyAuthError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                last_error = GuestyApiError("Token request connection failed")
            except GuestyRetryableError as err:
                last_error = err
            except GuestyApiError:
                raise

            if attempt >= API_MAX_RETRIES:
                break
            delay = self._retry_delay(last_error, attempt)
            _LOGGER.debug("Retrying Guesty token request in %.1fs", delay)
            await asyncio.sleep(delay)

        if last_error is None:
            raise GuestyApiError("Token request failed")
        raise last_error

    async def _async_refresh_token_once(self) -> None:
        """Perform one OAuth token request."""
        payload = {
            "grant_type": "client_credentials",
            "scope": "open-api",
            "client_id": self._client_id.strip(),
            "client_secret": self._client_secret.strip(),
        }
        async with self._session.post(
            OAUTH_URL,
            data=payload,
            timeout=aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as response:
            body = await response.text()
            if response.status != 200:
                _LOGGER.error("Guesty auth failed with HTTP %s", response.status)
                if response.status in {400, 401, 403}:
                    raise GuestyAuthError(f"Authentication failed ({response.status})")
                if response.status in RETRYABLE_STATUS_CODES:
                    raise GuestyRetryableError(
                        f"Token request failed ({response.status})",
                        self._retry_delay_from_response(response),
                    )
                raise GuestyApiError(f"Token request failed ({response.status})")

            try:
                data = json.loads(body)
            except json.JSONDecodeError as err:
                raise GuestyApiError("Invalid token response from Guesty") from err

            access_token = data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise GuestyAuthError("Token response did not contain an access token")
            try:
                expires_in = int(data.get("expires_in", 86400))
            except (TypeError, ValueError) as err:
                raise GuestyApiError("Invalid token expiry from Guesty") from err
            if expires_in <= 0:
                raise GuestyApiError("Invalid token expiry from Guesty")

            self._access_token = access_token
            from homeassistant.util import dt as dt_util

            self._token_expires_at = dt_util.utcnow().timestamp() + expires_in

    async def _async_paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages for a Guesty endpoint."""
        results: list[dict[str, Any]] = []
        skip = 0
        query = dict(params or {})
        try:
            page_limit = int(query.get("limit", PAGE_LIMIT))
        except (TypeError, ValueError):
            page_limit = PAGE_LIMIT
        page_limit = min(max(page_limit, 1), PAGE_LIMIT)
        query["limit"] = str(page_limit)
        previous_fingerprint: bytes | None = None

        for _page_number in range(API_MAX_PAGES):
            query["skip"] = str(skip)
            page = await self._async_request("GET", path, params=query)
            items = self._normalize_results(page)
            if not items:
                break

            fingerprint = hashlib.sha256(
                json.dumps(items, sort_keys=True, default=str).encode()
            ).digest()
            if fingerprint == previous_fingerprint:
                raise GuestyApiError("Guesty pagination did not advance")
            previous_fingerprint = fingerprint

            results.extend(items)
            if len(items) < page_limit:
                break
            skip += page_limit
        else:
            raise GuestyApiError("Guesty pagination exceeded the safety limit")

        return results

    @staticmethod
    def _normalize_results(data: Any) -> list[dict[str, Any]]:
        """Normalize Guesty paginated API responses."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]
            if data.get("_id") or data.get("id"):
                return [data]
        return []

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """Perform an authenticated API request with retries."""
        if not self._access_token:
            await self._async_ensure_token()

        last_error: GuestyApiError | None = None
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                return await self._async_request_once(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    retry_auth=retry_auth,
                )
            except (GuestyAuthError, GuestyPermissionError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                last_error = GuestyApiError("Request connection failed")
            except GuestyRetryableError as err:
                last_error = err
            except GuestyApiError:
                raise

            if attempt >= API_MAX_RETRIES:
                break
            delay = self._retry_delay(last_error, attempt)
            _LOGGER.debug(
                "Retrying Guesty request %s %s in %.1fs (%s)",
                method,
                path,
                delay,
                last_error,
            )
            await asyncio.sleep(delay)

        if last_error is None:
            raise GuestyApiError("Request failed")
        raise last_error

    async def _async_request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """Perform a single authenticated API request."""
        url = f"{API_BASE_URL}{path}"
        request_token = self._access_token
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {request_token}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        async with self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT),
        ) as response:
            body = await response.text()
            self._capture_rate_limit_headers(response.headers)

            if response.status == 401 and retry_auth:
                await self._async_ensure_token(
                    force_refresh=True,
                    invalid_token=request_token,
                )
                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT),
                ) as retry_response:
                    retry_body = await retry_response.text()
                    self._capture_rate_limit_headers(retry_response.headers)
                    if retry_response.status == 401:
                        raise GuestyAuthError(
                            f"Authentication failed ({retry_response.status})"
                        )
                    if retry_response.status == 403:
                        raise GuestyPermissionError(
                            f"Permission denied ({retry_response.status})"
                        )
                    if retry_response.status == 404:
                        raise GuestyNotFoundError("Resource not found (404)")
                    if retry_response.status in RETRYABLE_STATUS_CODES:
                        raise GuestyRetryableError(
                            f"Retryable error ({retry_response.status})",
                            self._retry_delay_from_response(retry_response),
                        )
                    if retry_response.status >= 400:
                        raise GuestyApiError(
                            f"Request failed ({retry_response.status})"
                        )
                    return self._parse_response_body(retry_body)

            if response.status == 401:
                raise GuestyAuthError(f"Authentication failed ({response.status})")
            if response.status == 403:
                raise GuestyPermissionError(f"Permission denied ({response.status})")
            if response.status == 404:
                raise GuestyNotFoundError("Resource not found (404)")

            if response.status in RETRYABLE_STATUS_CODES:
                delay = self._retry_delay_from_response(response)
                raise GuestyRetryableError(
                    f"Retryable error ({response.status})",
                    delay,
                )

            if response.status >= 400:
                raise GuestyApiError(f"Request failed ({response.status})")

            return self._parse_response_body(body)

    @staticmethod
    def _parse_response_body(body: str) -> Any:
        """Parse an API response body."""
        if not body:
            return []
        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            raise GuestyApiError("Invalid JSON response from Guesty") from err

    @staticmethod
    def _retry_delay(error: GuestyApiError, attempt: int) -> float:
        """Return the delay for a retryable request failure."""
        if isinstance(error, GuestyRetryableError) and error.retry_after is not None:
            return min(max(error.retry_after, 0.0), API_RETRY_MAX_DELAY)
        return min(
            API_RETRY_BASE_DELAY * (2**attempt),
            API_RETRY_MAX_DELAY,
        )

    def _capture_rate_limit_headers(self, headers: Any) -> None:
        """Store rate limit headers for diagnostics."""
        values = []
        for name in (
            "RateLimit-Remaining",
            "X-RateLimit-Remaining-Second",
            "X-RateLimit-Remaining-Minute",
            "X-RateLimit-Remaining-Hour",
            "X-RateLimit-Remaining-Day",
        ):
            remaining = headers.get(name)
            if remaining is None:
                continue
            try:
                values.append(int(remaining))
            except (TypeError, ValueError):
                pass
        if values:
            self.last_rate_limit_remaining = min(values)

    @staticmethod
    def _validate_resource_id(value: str, resource: str) -> None:
        """Reject resource identifiers that are unsafe in URL paths."""
        if not is_safe_resource_id(value):
            raise GuestyApiError(f"Invalid {resource} id")

    @staticmethod
    def _retry_delay_from_response(response: aiohttp.ClientResponse) -> float:
        """Determine retry delay from response headers."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), API_RETRY_BASE_DELAY)
            except ValueError:
                pass
        return API_RETRY_BASE_DELAY
