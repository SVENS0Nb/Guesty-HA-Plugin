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
    API_MAX_RESPONSE_BYTES,
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
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")


def is_safe_resource_id(value: Any) -> bool:
    """Return whether a value is safe to insert into an API URL path."""
    return isinstance(value, str) and RESOURCE_ID_PATTERN.fullmatch(value) is not None


class GuestyAuthError(Exception):
    """Raised when Guesty authentication fails."""


class GuestyApiError(Exception):
    """Raised when a Guesty API request fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize an API error with optional structured HTTP context."""
        super().__init__(message)
        self.status_code = status_code


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


def is_custom_field_reference_error(error: Exception) -> bool:
    """Return whether an error specifically points to an invalid field reference."""
    if isinstance(error, GuestyNotFoundError):
        return True
    if not isinstance(error, GuestyApiError) or error.status_code not in {400, 422}:
        return False
    normalized = " ".join(str(error).lower().replace("_", " ").split())
    return any(
        marker in normalized
        for marker in ("custom field", "field id", "fieldid", "field definition")
    )


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

    async def async_resolve_custom_field(self, reference: str) -> str:
        """Resolve a Guesty custom field name, variable, or id."""
        value = reference.strip()
        if OBJECT_ID_PATTERN.fullmatch(value):
            return value

        await self._async_ensure_token()
        account = await self._async_request("GET", "/accounts/me")
        if not isinstance(account, dict):
            raise GuestyApiError("Unexpected Guesty account response")
        account_id = account.get("_id") or account.get("id")
        self._validate_resource_id(account_id, "account")

        data = await self._async_request("GET", f"/accounts/{account_id}/custom-fields")
        fields = self._normalize_results(data)
        if isinstance(data, dict) and not fields:
            for key in ("customFields", "fields"):
                items = data.get(key)
                if isinstance(items, list):
                    fields = [item for item in items if isinstance(item, dict)]
                    break

        normalized_reference = self._normalize_custom_field_name(value)
        matches: list[str] = []
        for field in fields:
            field_id = field.get("_id") or field.get("id") or field.get("fieldId")
            if not is_safe_resource_id(field_id):
                continue
            candidates = (
                field.get("displayName"),
                field.get("name"),
                field.get("variable"),
                field.get("key"),
            )
            if any(
                self._normalize_custom_field_name(candidate) == normalized_reference
                for candidate in candidates
                if isinstance(candidate, str)
            ):
                matches.append(field_id)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise GuestyApiError("Guesty custom field name is not unique")
        raise GuestyNotFoundError("Guesty custom field not found")

    async def async_update_reservation_custom_field(
        self,
        reservation_id: str,
        field_id: str,
        value: str,
    ) -> None:
        """Set and verify a reservation custom field using the v3 endpoint."""
        self._validate_resource_id(reservation_id, "reservation")
        self._validate_resource_id(field_id, "custom field")
        data = await self._async_request(
            "PUT",
            f"/reservations-v3/{reservation_id}/custom-fields",
            json_body={"customFields": [{"fieldId": field_id, "value": value}]},
        )
        if not isinstance(data, dict) or data.get("reservationId") != reservation_id:
            raise GuestyApiError("Guesty did not confirm the custom field update")
        custom_fields = data.get("customFields")
        if not isinstance(custom_fields, list):
            raise GuestyApiError("Guesty returned an invalid custom field response")
        for item in custom_fields:
            if (
                isinstance(item, dict)
                and item.get("fieldId") == field_id
                and item.get("value") == value
            ):
                break
        else:
            raise GuestyApiError("Guesty did not persist the custom field value")

        # Guesty's update response can acknowledge a request before the value is
        # visible on the reservation. Confirm it through the dedicated read
        # endpoint before treating a guest access link as synchronized.
        await self._async_verify_reservation_custom_field(
            reservation_id,
            field_id,
            value,
        )

    async def async_get_reservation_custom_field(
        self,
        reservation_id: str,
        field_id: str,
    ) -> Any | None:
        """Return one populated reservation custom-field value."""
        self._validate_resource_id(reservation_id, "reservation")
        self._validate_resource_id(field_id, "custom field")
        try:
            data = await self._async_request(
                "GET",
                f"/reservations-v3/{reservation_id}/custom-fields/{field_id}",
            )
        except GuestyNotFoundError:
            # Guesty uses 404 for an existing reservation whose optional field
            # has no populated value.
            return None

        if not isinstance(data, dict) or data.get("reservationId") != reservation_id:
            raise GuestyApiError("Guesty returned an invalid custom field response")
        custom_field = data.get("customField")
        if (
            not isinstance(custom_field, dict)
            or custom_field.get("fieldId") != field_id
        ):
            raise GuestyApiError("Guesty returned an invalid custom field response")
        return custom_field.get("value")

    async def _async_verify_reservation_custom_field(
        self,
        reservation_id: str,
        field_id: str,
        expected_value: str,
    ) -> None:
        """Verify a written reservation field with bounded eventual-consistency retries."""
        for attempt in range(3):
            try:
                data = await self._async_request(
                    "GET",
                    f"/reservations-v3/{reservation_id}/custom-fields/{field_id}",
                )
            except GuestyNotFoundError:
                data = None

            custom_field = data.get("customField") if isinstance(data, dict) else None
            if (
                isinstance(data, dict)
                and data.get("reservationId") == reservation_id
                and isinstance(custom_field, dict)
                and custom_field.get("fieldId") == field_id
                and custom_field.get("value") == expected_value
            ):
                return
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise GuestyApiError("Guesty did not persist the custom field value")

    async def async_delete_reservation_custom_field(
        self,
        reservation_id: str,
        field_id: str,
    ) -> None:
        """Delete a reservation custom field, ignoring already absent values."""
        self._validate_resource_id(reservation_id, "reservation")
        self._validate_resource_id(field_id, "custom field")
        try:
            await self._async_request(
                "DELETE",
                f"/reservations-v3/{reservation_id}/custom-fields/{field_id}",
            )
        except GuestyNotFoundError:
            return

    @staticmethod
    def _normalize_custom_field_name(value: str) -> str:
        """Normalize Guesty display names and {{variables}} for matching."""
        return re.sub(r"[^a-z0-9]", "", value.lower())

    async def async_register_webhook(self, url: str) -> str:
        """Register Guesty webhooks and return the webhook id."""
        await self._async_ensure_token()
        payload = {"url": url, "events": list(WEBHOOK_SUBSCRIPTION_EVENTS)}
        try:
            # Creating a webhook is not idempotent. A connection loss after
            # Guesty accepted the POST must not be followed by another POST.
            data = await self._async_request(
                "POST",
                "/webhooks",
                json_body=payload,
                retry_transport=False,
            )
        except GuestyRetryableError:
            existing = await self._async_find_webhook_by_url(url)
            if existing is None:
                raise
            return existing
        if not isinstance(data, dict) or not is_safe_resource_id(data.get("_id")):
            raise GuestyApiError("Unexpected webhook registration response")
        return data["_id"]

    async def _async_find_webhook_by_url(self, url: str) -> str | None:
        """Recover a webhook after an ambiguous non-idempotent create request."""
        data = await self._async_request("GET", "/webhooks")
        for item in self._normalize_results(data):
            if item.get("url") != url:
                continue
            webhook_id = item.get("_id") or item.get("id")
            if is_safe_resource_id(webhook_id):
                return webhook_id
        return None

    async def async_ensure_webhook(
        self,
        url: str,
        existing_id: str | None = None,
    ) -> str:
        """Reuse or repair the integration webhook before creating a new one."""
        await self._async_ensure_token()
        data = await self._async_request("GET", "/webhooks")
        webhooks = self._normalize_results(data)
        desired_events = set(WEBHOOK_SUBSCRIPTION_EVENTS)

        stored_candidate: dict[str, Any] | None = None
        if existing_id and is_safe_resource_id(existing_id):
            stored_candidate = next(
                (
                    item
                    for item in webhooks
                    if (item.get("_id") or item.get("id")) == existing_id
                ),
                None,
            )

        # Prefer an already-correct URL over stale local metadata. This avoids
        # deleting a recovered webhook and issuing another non-idempotent POST
        # after an earlier response was lost or the external HA URL changed.
        candidate = next(
            (
                item
                for item in webhooks
                if item.get("url") == url
                and (item.get("_id") or item.get("id")) == existing_id
            ),
            None,
        )
        if candidate is None:
            candidate = next(
                (item for item in webhooks if item.get("url") == url),
                None,
            )
        if candidate is None:
            candidate = stored_candidate

        if candidate is None:
            return await self.async_register_webhook(url)

        webhook_id = candidate.get("_id") or candidate.get("id")
        self._validate_resource_id(webhook_id, "webhook")
        if (
            stored_candidate is not None
            and stored_candidate is not candidate
            and stored_candidate.get("url") != url
        ):
            stale_id = stored_candidate.get("_id") or stored_candidate.get("id")
            if is_safe_resource_id(stale_id):
                try:
                    await self.async_unregister_webhook(stale_id)
                except (GuestyApiError, GuestyAuthError) as err:
                    _LOGGER.warning("Could not remove a stale Guesty webhook: %s", err)
        for duplicate in webhooks:
            duplicate_id = duplicate.get("_id") or duplicate.get("id")
            if (
                duplicate is candidate
                or duplicate.get("url") != url
                or duplicate_id == webhook_id
                or not is_safe_resource_id(duplicate_id)
            ):
                continue
            try:
                await self.async_unregister_webhook(duplicate_id)
            except (GuestyApiError, GuestyAuthError) as err:
                _LOGGER.warning("Could not remove a duplicate Guesty webhook: %s", err)
        remote_url = candidate.get("url")
        is_disabled = (
            candidate.get("active") is False or candidate.get("enabled") is False
        )
        if remote_url != url or is_disabled:
            # Guesty no longer permits changing a webhook URL in place. Recreate
            # disabled subscriptions as well so they are activated predictably.
            await self.async_unregister_webhook(webhook_id)
            return await self.async_register_webhook(url)

        remote_events = candidate.get("events")
        if not (
            isinstance(remote_events, list) and desired_events.issubset(remote_events)
        ):
            await self._async_request(
                "PUT",
                f"/webhooks/{webhook_id}",
                json_body={"events": list(WEBHOOK_SUBSCRIPTION_EVENTS)},
            )
        return webhook_id

    async def async_get_webhook_secret(self, url: str) -> str:
        """Return the signing secret Guesty assigned to a webhook URL."""
        await self._async_ensure_token()
        data = await self._async_request(
            "GET",
            "/webhooks-v2/secret",
            params={"url": url},
        )

        candidates: list[Any] = [data]
        if isinstance(data, dict):
            candidates.extend(data.get(key) for key in ("data", "result"))
        for candidate in candidates:
            if isinstance(candidate, str) and len(candidate.strip()) >= 16:
                return candidate.strip()
            if not isinstance(candidate, dict):
                continue
            for key in ("secret", "signingSecret", "signing_secret", "key"):
                value = candidate.get(key)
                if isinstance(value, str) and len(value.strip()) >= 16:
                    return value.strip()
        raise GuestyApiError("Guesty returned an invalid webhook signing secret")

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
            body = await self._async_read_response_text(response)
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
        retry_transport: bool = True,
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
                last_error = GuestyRetryableError("Request connection failed")
            except GuestyRetryableError as err:
                last_error = err
            except GuestyApiError:
                raise

            if not retry_transport or attempt >= API_MAX_RETRIES:
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
            body = await self._async_read_response_text(response)
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
                    retry_body = await self._async_read_response_text(retry_response)
                    self._capture_rate_limit_headers(retry_response.headers)
                    if retry_response.status == 401:
                        raise GuestyAuthError(
                            self._error_message(
                                "Authentication failed",
                                retry_response.status,
                                retry_body,
                                retry_response.headers,
                            )
                        )
                    if retry_response.status == 403:
                        raise GuestyPermissionError(
                            self._error_message(
                                "Permission denied",
                                retry_response.status,
                                retry_body,
                                retry_response.headers,
                            )
                        )
                    if retry_response.status == 404:
                        raise GuestyNotFoundError(
                            self._error_message(
                                "Resource not found",
                                retry_response.status,
                                retry_body,
                                retry_response.headers,
                            )
                        )
                    if retry_response.status in RETRYABLE_STATUS_CODES:
                        raise GuestyRetryableError(
                            f"Retryable error ({retry_response.status})",
                            self._retry_delay_from_response(retry_response),
                        )
                    if retry_response.status >= 400:
                        raise GuestyApiError(
                            self._error_message(
                                "Request failed",
                                retry_response.status,
                                retry_body,
                                retry_response.headers,
                            ),
                            retry_response.status,
                        )
                    return self._parse_response_body(retry_body)

            if response.status == 401:
                raise GuestyAuthError(
                    self._error_message(
                        "Authentication failed",
                        response.status,
                        body,
                        response.headers,
                    )
                )
            if response.status == 403:
                raise GuestyPermissionError(
                    self._error_message(
                        "Permission denied",
                        response.status,
                        body,
                        response.headers,
                    )
                )
            if response.status == 404:
                raise GuestyNotFoundError(
                    self._error_message(
                        "Resource not found",
                        response.status,
                        body,
                        response.headers,
                    )
                )

            if response.status in RETRYABLE_STATUS_CODES:
                delay = self._retry_delay_from_response(response)
                raise GuestyRetryableError(
                    f"Retryable error ({response.status})",
                    delay,
                )

            if response.status >= 400:
                raise GuestyApiError(
                    self._error_message(
                        "Request failed",
                        response.status,
                        body,
                        response.headers,
                    ),
                    response.status,
                )

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
    async def _async_read_response_text(response: aiohttp.ClientResponse) -> str:
        """Read one bounded API response to protect Home Assistant memory."""
        raw = await response.content.read(API_MAX_RESPONSE_BYTES + 1)
        if len(raw) > API_MAX_RESPONSE_BYTES:
            raise GuestyApiError("Guesty response exceeded the size limit")
        return raw.decode(response.charset or "utf-8", errors="replace")

    @staticmethod
    def _error_message(
        prefix: str,
        status: int,
        body: str,
        headers: Any,
    ) -> str:
        """Return bounded, non-secret Guesty error context for diagnostics."""
        details: list[str] = []
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            for key in ("message", "error", "error_description", "details"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    details.append(
                        GuestyApiClient._redact_error_detail(value.strip())[:300]
                    )
                elif isinstance(value, list):
                    safe_items = [
                        GuestyApiClient._redact_error_detail(str(item))[:120]
                        for item in value[:3]
                        if isinstance(item, (str, int, float, bool))
                    ]
                    if safe_items:
                        details.append(", ".join(safe_items))

        request_id = None
        for name in ("x-request-id", "X-Request-Id", "x-guesty-request-id"):
            value = headers.get(name) if headers is not None else None
            if isinstance(value, str) and value:
                request_id = value[:100]
                break

        message = f"{prefix} ({status})"
        if details:
            message += f": {'; '.join(dict.fromkeys(details))}"
        if request_id:
            message += f" [request_id={request_id}]"
        return message

    @staticmethod
    def _redact_error_detail(value: str) -> str:
        """Redact bearer access links if an API echoes the submitted value."""
        return re.sub(
            r"(?:https?://[^\s\"']+)?/api/guesty/access/[^\s\"']+",
            "[REDACTED_ACCESS_URL]",
            value,
            flags=re.IGNORECASE,
        )

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
