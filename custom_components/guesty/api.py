"""Guesty Open API client."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_BASE_URL,
    API_MAX_RETRIES,
    API_RETRY_BASE_DELAY,
    API_RETRY_MAX_DELAY,
    LISTING_FIELDS,
    OAUTH_URL,
    RESERVATION_FIELDS,
    TOKEN_REFRESH_MARGIN,
    WEBHOOK_EVENTS,
)
from .models import GuestyListing, GuestyReservation, build_reservation_filters

_LOGGER = logging.getLogger(__name__)

PAGE_LIMIT = 100
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class GuestyAuthError(Exception):
    """Raised when Guesty authentication fails."""


class GuestyApiError(Exception):
    """Raised when a Guesty API request fails."""


class GuestyPermissionError(GuestyApiError):
    """Raised when credentials work but API access is denied."""


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
        self._access_token = token
        self._token_expires_at = token_expires_at
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
        await self._async_ensure_token(force_refresh=True)
        try:
            await self._async_paginate(
                "/listings",
                params={
                    "fields": LISTING_FIELDS,
                    "limit": "1",
                },
            )
        except GuestyAuthError:
            raise
        except GuestyPermissionError as err:
            _LOGGER.warning("Guesty token valid but listings access denied: %s", err)
        except GuestyApiError as err:
            _LOGGER.warning("Guesty listings check failed after auth: %s", err)
        return "guesty_account"

    async def async_get_listings(self) -> list[GuestyListing]:
        """Fetch all listings from the Guesty account."""
        await self._async_ensure_token()
        raw_listings = await self._async_paginate(
            "/listings",
            params={"fields": LISTING_FIELDS},
        )
        return [GuestyListing.from_api(item) for item in raw_listings]

    async def async_get_reservations(
        self,
        days_past: int,
        days_future: int,
        *,
        updated_since: datetime | None = None,
    ) -> list[GuestyReservation]:
        """Fetch reservations for the configured sync window."""
        await self._async_ensure_token()
        filters = build_reservation_filters(
            days_past,
            days_future,
            updated_since=updated_since,
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

    async def async_get_reservation(self, reservation_id: str) -> GuestyReservation | None:
        """Fetch a single reservation by id."""
        await self._async_ensure_token()
        data = await self._async_request(
            "GET",
            f"/reservations/{reservation_id}",
            params={"fields": RESERVATION_FIELDS},
        )
        if isinstance(data, dict):
            return GuestyReservation.from_api(data)
        return None

    async def async_register_webhook(self, url: str) -> str:
        """Register Guesty webhooks and return the webhook id."""
        await self._async_ensure_token()
        payload = {"url": url, "events": list(WEBHOOK_EVENTS)}
        data = await self._async_request(
            "POST",
            "/webhooks",
            json_body=payload,
        )
        if not isinstance(data, dict) or "_id" not in data:
            raise GuestyApiError("Unexpected webhook registration response")
        return data["_id"]

    async def async_unregister_webhook(self, webhook_id: str) -> None:
        """Remove a Guesty webhook subscription."""
        await self._async_ensure_token()
        await self._async_request("DELETE", f"/webhooks/{webhook_id}")

    async def _async_ensure_token(self, force_refresh: bool = False) -> None:
        """Ensure a valid access token is available."""
        if (
            not force_refresh
            and self._access_token
            and self._token_expires_at is not None
        ):
            from homeassistant.util import dt as dt_util

            if (
                dt_util.utcnow().timestamp()
                < self._token_expires_at - TOKEN_REFRESH_MARGIN.total_seconds()
            ):
                return
        await self._async_refresh_token()

    async def _async_refresh_token(self) -> None:
        """Request a new OAuth access token."""
        payload = {
            "grant_type": "client_credentials",
            "scope": "open-api",
            "client_id": self._client_id.strip(),
            "client_secret": self._client_secret.strip(),
        }
        async with self._session.post(
            OAUTH_URL,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as response:
            body = await response.text()
            if response.status != 200:
                _LOGGER.error(
                    "Guesty auth failed (%s): %s",
                    response.status,
                    body[:500],
                )
                if response.status in {400, 401, 403}:
                    raise GuestyAuthError(
                        f"Authentication failed ({response.status}): {body[:200]}"
                    )
                raise GuestyApiError(
                    f"Token request failed ({response.status}): {body[:200]}"
                )

            try:
                data = json.loads(body)
            except json.JSONDecodeError as err:
                raise GuestyApiError("Invalid token response from Guesty") from err

            if "access_token" not in data:
                raise GuestyAuthError(f"No access_token in response: {body[:200]}")
            self._access_token = data["access_token"]
            from homeassistant.util import dt as dt_util

            self._token_expires_at = (
                dt_util.utcnow().timestamp() + int(data.get("expires_in", 86400))
            )

    async def _async_paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages for a Guesty endpoint."""
        results: list[dict[str, Any]] = []
        skip = 0
        query = dict(params or {})
        query["limit"] = str(PAGE_LIMIT)

        while True:
            query["skip"] = str(skip)
            page = await self._async_request("GET", path, params=query)
            items = self._normalize_results(page)
            if not items:
                break
            results.extend(items)
            if len(items) < PAGE_LIMIT:
                break
            skip += PAGE_LIMIT

        return results

    @staticmethod
    def _normalize_results(data: Any) -> list[dict[str, Any]]:
        """Normalize Guesty paginated API responses."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return results
            if data.get("_id"):
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

        last_error: Exception | None = None
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                return await self._async_request_once(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    retry_auth=retry_auth,
                )
            except GuestyAuthError:
                raise
            except GuestyApiError as err:
                last_error = err
                if attempt >= API_MAX_RETRIES:
                    break
                delay = min(
                    API_RETRY_BASE_DELAY * (2**attempt),
                    API_RETRY_MAX_DELAY,
                )
                _LOGGER.debug(
                    "Retrying Guesty request %s %s in %.1fs (%s)",
                    method,
                    path,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
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
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        async with self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
        ) as response:
            body = await response.text()
            self._capture_rate_limit_headers(response.headers)

            if response.status in {401, 403} and retry_auth:
                await self._async_refresh_token()
                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                ) as retry_response:
                    retry_body = await retry_response.text()
                    self._capture_rate_limit_headers(retry_response.headers)
                    if retry_response.status == 401:
                        raise GuestyAuthError(
                            f"Authentication failed ({retry_response.status}): "
                            f"{retry_body[:200]}"
                        )
                    if retry_response.status == 403:
                        raise GuestyPermissionError(
                            f"Permission denied ({retry_response.status}): "
                            f"{retry_body[:200]}"
                        )
                    if retry_response.status >= 400:
                        raise GuestyApiError(
                            f"Request failed ({retry_response.status}): {retry_body}"
                        )
                    return self._parse_response_body(retry_body)

            if response.status == 401:
                raise GuestyAuthError(
                    f"Authentication failed ({response.status}): {body[:200]}"
                )
            if response.status == 403:
                raise GuestyPermissionError(
                    f"Permission denied ({response.status}): {body[:200]}"
                )

            if response.status in RETRYABLE_STATUS_CODES:
                delay = self._retry_delay_from_response(response)
                raise GuestyApiError(
                    f"Retryable error ({response.status}), retry in {delay:.1f}s: {body}"
                )

            if response.status >= 400:
                raise GuestyApiError(f"Request failed ({response.status}): {body}")

            return self._parse_response_body(body)

    @staticmethod
    def _parse_response_body(body: str) -> Any:
        """Parse an API response body."""
        if not body:
            return []
        return json.loads(body)

    def _capture_rate_limit_headers(self, headers: Any) -> None:
        """Store rate limit headers for diagnostics."""
        remaining = headers.get("ratelimit-remaining") or headers.get(
            "x-ratelimit-remaining-day"
        )
        if remaining is not None:
            try:
                self.last_rate_limit_remaining = int(remaining)
            except (TypeError, ValueError):
                pass

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
