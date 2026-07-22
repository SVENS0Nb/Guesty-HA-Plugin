"""Async client for TTLock Open Platform reservation passcodes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import time
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    TTLOCK_API_BASE_URLS,
    TTLOCK_MAX_RESPONSE_BYTES,
    TTLOCK_MAX_RETRIES,
    TTLOCK_REQUEST_TIMEOUT,
    TTLOCK_TOKEN_REFRESH_MARGIN_SECONDS,
)

_AUTH_ERROR_CODES = {
    -2018,
    10000,
    10001,
    10002,
    10003,
    10004,
    10005,
    10006,
    10007,
    10008,
    10009,
    10010,
    10011,
    20001,
    20002,
    30001,
}


class TTLockApiError(Exception):
    """Raised when TTLock rejects an API operation."""

    def __init__(self, message: str, code: int | None = None) -> None:
        """Initialize an API error with an optional TTLock error code."""
        super().__init__(message)
        self.code = code


class TTLockAuthError(TTLockApiError):
    """Raised when TTLock credentials or permissions are invalid."""


class TTLockCodeConflictError(TTLockApiError):
    """Raised when the requested passcode is already assigned."""


class TTLockGatewayError(TTLockApiError):
    """Raised when a lock cannot be reached through a gateway."""


class TTLockRateLimitError(TTLockApiError):
    """Raised when the TTLock application request allowance is exhausted."""


class TTLockOperationPendingError(TTLockApiError):
    """Raised while TTLock has not completed a gateway operation yet."""


def ttlock_api_base_url(region: str) -> str:
    """Return an allow-listed TTLock API endpoint for one account region."""
    try:
        return TTLOCK_API_BASE_URLS[region]
    except KeyError as err:
        raise ValueError("Unsupported TTLock API region") from err


def ttlock_milliseconds(value: datetime) -> int:
    """Convert an aware datetime to Unix milliseconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.UTC)
    return int(value.astimezone(dt_util.UTC).timestamp() * 1000)


class TTLockApiClient:
    """Use TTLock Cloud API V3 through Home Assistant's shared session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        region: str,
        client_id: str,
        client_secret: str,
        username: str = "",
        access_token: str = "",
        refresh_token: str = "",
        token_expires_at: str | None = None,
    ) -> None:
        """Initialize the client without retaining the TTLock account password."""
        self._session = session
        self._base_url = ttlock_api_base_url(region)
        self.region = region
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.username = username.strip()
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.token_expires_at = token_expires_at
        self._token_lock = asyncio.Lock()
        if not self.client_id or not self.client_secret:
            raise ValueError("TTLock client ID and client secret are required")

    @classmethod
    def from_hass(cls, hass: Any, **kwargs: Any) -> TTLockApiClient:
        """Create a client with Home Assistant's shared HTTP session."""
        return cls(async_get_clientsession(hass), **kwargs)

    async def async_authenticate(self, username: str, password: str) -> None:
        """Exchange TTLock App credentials for tokens and discard the password."""
        username = username.strip()
        if not username or not password:
            raise TTLockAuthError("TTLock username and password are required")
        password_hash = hashlib.md5(  # noqa: S324
            password.encode(), usedforsecurity=False
        ).hexdigest()
        payload = await self._async_token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "username": username,
                "password": password_hash,
            }
        )
        self.username = username
        self._apply_token_response(payload)

    async def async_refresh_access_token(self) -> None:
        """Refresh an expired or nearly expired TTLock access token."""
        async with self._token_lock:
            if not self.refresh_token:
                raise TTLockAuthError("TTLock refresh token is unavailable")
            payload = await self._async_token_request(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                }
            )
            self._apply_token_response(payload)

    def token_snapshot(self) -> dict[str, str]:
        """Return token state for the integration's private store."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expires_at": self.token_expires_at or "",
        }

    async def async_list_locks(self) -> list[dict[str, Any]]:
        """Return all locks owned by the configured TTLock App account."""
        payload = await self._async_api_request(
            "/v3/lock/list",
            {"pageNo": 1, "pageSize": 10000, "type": 1},
        )
        items = payload.get("list") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise TTLockApiError("TTLock returned an invalid lock list")
        return [item for item in items if isinstance(item, dict)]

    async def async_list_passcodes(self, lock_id: int) -> list[dict[str, Any]]:
        """Return all passcodes of one lock with bounded pagination."""
        result: list[dict[str, Any]] = []
        for page in range(1, 101):
            payload = await self._async_api_request(
                "/v3/lock/listKeyboardPwd",
                {"lockId": lock_id, "pageNo": page, "pageSize": 100},
            )
            items = payload.get("list") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise TTLockApiError("TTLock returned an invalid passcode list")
            result.extend(item for item in items if isinstance(item, dict))
            pages = payload.get("pages")
            if len(items) < 100 or (isinstance(pages, int) and page >= pages):
                break
        return result

    async def async_add_passcode(
        self,
        *,
        lock_id: int,
        code: str,
        name: str,
        valid_from: datetime,
        valid_until: datetime,
    ) -> int:
        """Add a customized V4 passcode through a Wi-Fi gateway."""
        self._validate_code(code)
        payload = await self._async_api_request(
            "/v3/keyboardPwd/add",
            {
                "lockId": lock_id,
                "keyboardPwd": code,
                "keyboardPwdName": name,
                "startDate": ttlock_milliseconds(valid_from),
                "endDate": ttlock_milliseconds(valid_until),
                "addType": 2,
            },
            # Creating a passcode is not idempotent. If the response is lost,
            # the manager recovers the result by its reservation marker instead
            # of risking a duplicate through an automatic POST retry.
            retry_transport=False,
        )
        try:
            password_id = int(payload["keyboardPwdId"])
        except (KeyError, TypeError, ValueError) as err:
            raise TTLockApiError("TTLock did not return a passcode ID") from err
        return password_id

    async def async_change_passcode(
        self,
        *,
        lock_id: int,
        password_id: int,
        code: str,
        name: str,
        valid_from: datetime,
        valid_until: datetime,
    ) -> None:
        """Change code, name, and validity through a Wi-Fi gateway."""
        self._validate_code(code)
        await self._async_api_request(
            "/v3/keyboardPwd/change",
            {
                "lockId": lock_id,
                "keyboardPwdId": password_id,
                "keyboardPwdName": name,
                "newKeyboardPwd": code,
                "startDate": ttlock_milliseconds(valid_from),
                "endDate": ttlock_milliseconds(valid_until),
                "changeType": 2,
            },
        )

    async def async_delete_passcode(self, *, lock_id: int, password_id: int) -> None:
        """Delete one managed passcode through a Wi-Fi gateway."""
        await self._async_api_request(
            "/v3/keyboardPwd/delete",
            {
                "lockId": lock_id,
                "keyboardPwdId": password_id,
                "deleteType": 2,
            },
        )

    async def _async_token_request(self, form: dict[str, Any]) -> dict[str, Any]:
        """Run an OAuth request with bounded transport retries."""
        payload = await self._async_http_post("/oauth2/token", form)
        if not isinstance(payload, dict):
            raise TTLockAuthError("TTLock returned an invalid OAuth response")
        if "access_token" not in payload:
            code = self._error_code(payload)
            raise TTLockAuthError("TTLock rejected the configured account", code)
        return payload

    async def _async_api_request(
        self,
        path: str,
        form: dict[str, Any],
        *,
        allow_refresh: bool = True,
        retry_transport: bool = True,
    ) -> dict[str, Any]:
        """Run one authenticated TTLock API request."""
        if self._token_needs_refresh() and self.refresh_token:
            await self.async_refresh_access_token()
        if not self.access_token:
            raise TTLockAuthError("TTLock access token is unavailable")
        request_form = {
            "clientId": self.client_id,
            "accessToken": self.access_token,
            "date": int(time.time() * 1000),
            **form,
        }
        payload = await self._async_http_post(
            path, request_form, retry_transport=retry_transport
        )
        if not isinstance(payload, dict):
            raise TTLockApiError("TTLock returned an invalid API response")
        code = self._error_code(payload)
        if code == 0:
            return payload
        if code in {10003, 10004} and allow_refresh and self.refresh_token:
            await self.async_refresh_access_token()
            return await self._async_api_request(
                path,
                form,
                allow_refresh=False,
                retry_transport=retry_transport,
            )
        self._raise_api_error(payload, code)
        raise AssertionError("TTLock error classification returned unexpectedly")

    async def _async_http_post(
        self,
        path: str,
        form: dict[str, Any],
        *,
        retry_transport: bool = True,
    ) -> dict[str, Any]:
        """POST a form and decode a bounded JSON response."""
        last_error: Exception | None = None
        max_retries = TTLOCK_MAX_RETRIES if retry_transport else 0
        for attempt in range(max_retries + 1):
            try:
                async with self._session.post(
                    f"{self._base_url}{path}",
                    data=form,
                    allow_redirects=False,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=TTLOCK_REQUEST_TIMEOUT),
                ) as response:
                    raw = await response.content.read(TTLOCK_MAX_RESPONSE_BYTES + 1)
                    if len(raw) > TTLOCK_MAX_RESPONSE_BYTES:
                        raise TTLockApiError("TTLock response exceeded the size limit")
                    if response.status in {401, 403}:
                        raise TTLockAuthError("TTLock rejected the account")
                    if response.status == 429:
                        raise TTLockRateLimitError("TTLock rate limit exceeded", 30006)
                    if response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                        )
                    if response.status >= 400:
                        raise TTLockApiError(
                            f"TTLock HTTP request failed ({response.status})",
                            response.status,
                        )
                    try:
                        value = json.loads(raw.decode(response.charset or "utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as err:
                        raise TTLockApiError("TTLock returned invalid JSON") from err
                    if not isinstance(value, dict):
                        raise TTLockApiError("TTLock returned an invalid JSON object")
                    return value
            except (TTLockAuthError, TTLockRateLimitError, TTLockApiError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_error = err
                if attempt >= max_retries:
                    break
                await asyncio.sleep(2**attempt)
        raise TTLockApiError("Could not connect to TTLock") from last_error

    def _apply_token_response(self, payload: dict[str, Any]) -> None:
        """Validate and retain a successful token response."""
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise TTLockAuthError("TTLock did not return an access token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise TTLockAuthError("TTLock did not return a refresh token")
        try:
            expires_in = max(int(payload.get("expires_in", 0)), 60)
        except (TypeError, ValueError):
            expires_in = 60
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = (
            dt_util.utcnow() + timedelta(seconds=expires_in)
        ).isoformat()

    def _token_needs_refresh(self) -> bool:
        """Return whether a token is absent, expired, or near expiry."""
        if not self.access_token:
            return True
        if not self.token_expires_at:
            return False
        try:
            expires_at = dt_util.parse_datetime(self.token_expires_at)
        except (TypeError, ValueError):
            return True
        if expires_at is None or expires_at.tzinfo is None:
            return True
        return expires_at <= dt_util.utcnow() + timedelta(
            seconds=TTLOCK_TOKEN_REFRESH_MARGIN_SECONDS
        )

    @staticmethod
    def _error_code(payload: dict[str, Any]) -> int:
        """Return a normalized TTLock error code."""
        raw = payload.get("errcode", 0 if "keyboardPwdId" in payload else 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _error_text(payload: dict[str, Any]) -> str:
        """Return normalized non-secret error text for classification."""
        return (
            " ".join(str(payload.get(key, "")) for key in ("errmsg", "description"))
            .strip()
            .lower()
        )

    def _raise_api_error(self, payload: dict[str, Any], code: int) -> None:
        """Map TTLock error envelopes to stable integration exceptions."""
        text = self._error_text(payload)
        if code in _AUTH_ERROR_CODES:
            raise TTLockAuthError("TTLock denied the API operation", code)
        if code == 30006:
            raise TTLockRateLimitError("TTLock rate limit exceeded", code)
        if code == -2012:
            raise TTLockGatewayError("TTLock gateway is unavailable", code)
        if any(
            marker in text
            for marker in (
                "already exists",
                "already in use",
                "duplicate passcode",
                "same passcode",
                "密码已存在",
                "密码相同",
            )
        ):
            raise TTLockCodeConflictError("TTLock passcode is already in use", code)
        raise TTLockApiError(f"TTLock operation failed ({code})", code)

    @staticmethod
    def _validate_code(code: str) -> None:
        """Accept only the integration's six-digit reservation codes."""
        if not code.isascii() or not code.isdigit() or len(code) != 6:
            raise ValueError("TTLock reservation passcodes must contain six digits")
