"""Small HTTPS client for the Loxone user-management web services."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    LOXONE_EPOCH,
    LOXONE_EXPIRATION_ACTION_DELETE,
    LOXONE_MAX_RETRIES,
    LOXONE_MAX_RESPONSE_BYTES,
    LOXONE_PRIVILEGED_GROUP_RIGHTS,
    LOXONE_REQUEST_TIMEOUT,
    LOXONE_USER_STATE_TIMESPAN,
)


class LoxoneApiError(Exception):
    """Raised when a Loxone user-management request fails."""

    def __init__(self, message: str, code: int | None = None) -> None:
        """Initialize an API error with an optional Loxone result code."""
        super().__init__(message)
        self.code = code


class LoxoneAuthError(LoxoneApiError):
    """Raised when Loxone rejects the configured service account."""


class LoxoneCodeConflictError(LoxoneApiError):
    """Raised when an access code is already in use on the Miniserver."""


def normalize_loxone_url(value: str) -> str:
    """Validate and normalize an HTTPS Miniserver or reverse-proxy URL."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A credential-free HTTPS Loxone URL is required")
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def loxone_server_id(url: str, username: str) -> str:
    """Return a stable non-secret identifier for a configured Miniserver."""
    normalized = f"{normalize_loxone_url(url)}\0{username.strip().lower()}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:20]


def loxone_timestamp(value: datetime) -> int:
    """Convert a datetime to Loxone seconds since 2009-01-01 UTC."""
    epoch = dt_util.parse_datetime(LOXONE_EPOCH)
    if epoch is None:
        raise RuntimeError("Invalid Loxone epoch constant")
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.UTC)
    return int((value.astimezone(dt_util.UTC) - epoch).total_seconds())


class LoxoneApiClient:
    """Access the official Loxone user-management endpoints over HTTPS."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize a Loxone client."""
        self._session = session
        self._base_url = normalize_loxone_url(base_url)
        self._username = username.strip()
        self._password = password
        if not self._username or not self._password:
            raise ValueError("Loxone username and password are required")
        self._auth = aiohttp.BasicAuth(self._username, self._password)

    @classmethod
    def from_hass(
        cls,
        hass: Any,
        base_url: str,
        username: str,
        password: str,
    ) -> LoxoneApiClient:
        """Create a client with Home Assistant's shared HTTP session."""
        return cls(
            async_get_clientsession(hass),
            base_url,
            username,
            password,
        )

    async def async_get_groups(self) -> list[dict[str, str]]:
        """Return configurable Loxone user groups."""
        value, _code = await self._async_request("getgrouplist")
        if not isinstance(value, list):
            raise LoxoneApiError("Loxone returned an invalid group list")
        groups: list[dict[str, str]] = []
        for item in value[:1000]:
            if not isinstance(item, dict):
                continue
            # Fail closed: only normal groups with an explicit rights mask are
            # selectable. Loxone Config and User management rights would turn a
            # short-lived door guest into an administrator or user manager.
            try:
                group_type = int(item["type"])
                user_rights = int(item["userRights"])
            except (KeyError, TypeError, ValueError):
                continue
            if group_type != 0 or user_rights & LOXONE_PRIVILEGED_GROUP_RIGHTS:
                continue
            group_uuid = item.get("uuid")
            name = item.get("name")
            if isinstance(group_uuid, str) and isinstance(name, str):
                groups.append({"uuid": group_uuid, "name": name})
        return groups

    async def async_get_user(self, user_uuid: str) -> dict[str, Any] | None:
        """Return a full Loxone user record, or None when it no longer exists."""
        try:
            value, code = await self._async_request(
                f"getuser/{quote(user_uuid, safe='')}",
                accepted_codes={200, 400, 404, 500},
            )
        except LoxoneApiError as err:
            if getattr(err, "code", None) in {400, 404}:
                return None
            raise
        if code == 200:
            return value if isinstance(value, dict) else None
        if code in {400, 404} or (code == 500 and self._is_not_found(value)):
            return None
        raise LoxoneApiError("Loxone could not verify whether the user exists", code)

    async def async_find_user_by_userid(self, user_id: str) -> dict[str, Any] | None:
        """Recover a user after an ambiguous create response or interrupted save."""
        value, _code = await self._async_request(
            f"checkuserid/{quote(user_id, safe='')}"
        )
        if value in ({}, None, ""):
            return None
        if not isinstance(value, dict):
            raise LoxoneApiError("Loxone returned an invalid userid lookup")
        user_uuid = value.get("uuid")
        if not isinstance(user_uuid, str) or not user_uuid:
            return None
        details = await self.async_get_user(user_uuid)
        if details is None:
            return None
        return details if details.get("userid") == user_id else None

    async def async_add_or_update_user(
        self,
        *,
        user_uuid: str | None,
        name: str,
        user_id: str,
        group_uuids: list[str],
        valid_from: datetime,
        valid_until: datetime,
    ) -> str:
        """Create or update one time-limited Loxone user and return its UUID."""
        payload: dict[str, Any] = {
            "name": name,
            "userid": user_id,
            "changePassword": False,
            "userState": LOXONE_USER_STATE_TIMESPAN,
            "validFrom": loxone_timestamp(valid_from),
            "validUntil": loxone_timestamp(valid_until),
            "expirationAction": LOXONE_EXPIRATION_ACTION_DELETE,
            "usergroups": group_uuids,
        }
        if user_uuid:
            payload["uuid"] = user_uuid
        encoded = quote(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            safe="",
        )
        value, _code = await self._async_request(f"addoredituser/{encoded}")
        returned_uuid = value.get("uuid") if isinstance(value, dict) else value
        if not isinstance(returned_uuid, str) or not returned_uuid:
            raise LoxoneApiError("Loxone did not return the user UUID")
        return returned_uuid

    async def async_set_access_code(self, user_uuid: str, code: str) -> None:
        """Assign a numeric access code, rejecting every non-unique result."""
        if not code.isdigit() or not 2 <= len(code) <= 8:
            raise ValueError("Loxone access codes must contain 2 to 8 digits")
        _value, result_code = await self._async_request(
            f"updateuseraccesscode/{quote(user_uuid, safe='')}/{quote(code, safe='')}",
            accepted_codes={200, 201, 409},
        )
        if result_code in {201, 409}:
            raise LoxoneCodeConflictError(
                "The access code is already in use on the Loxone Miniserver"
            )

    async def async_delete_user(self, user_uuid: str) -> None:
        """Delete a managed Loxone user, ignoring an already absent user."""
        try:
            value, code = await self._async_request(
                f"deleteuser/{quote(user_uuid, safe='')}",
                accepted_codes={200, 400, 404, 500},
            )
        except LoxoneApiError as err:
            if getattr(err, "code", None) in {400, 404}:
                return
            raise
        if code == 200 or code in {400, 404}:
            return
        if code == 500 and self._is_not_found(value):
            return
        raise LoxoneApiError("Loxone did not confirm deletion of the user", code)

    @staticmethod
    def _is_not_found(value: Any) -> bool:
        """Recognize only explicit absent-user errors, never generic code 500."""
        if isinstance(value, dict):
            text = " ".join(str(item) for item in value.values())
        else:
            text = str(value or "")
        normalized = " ".join(text.lower().split())
        return "not found" in normalized or "unknown user" in normalized

    async def _async_request(
        self,
        command: str,
        *,
        accepted_codes: set[int] | None = None,
    ) -> tuple[Any, int]:
        """Run one authenticated web-service command with bounded retries."""
        accepted = accepted_codes or {200}
        last_error: Exception | None = None
        for attempt in range(LOXONE_MAX_RETRIES + 1):
            try:
                async with self._session.get(
                    f"{self._base_url}/jdev/sps/{command}",
                    auth=self._auth,
                    allow_redirects=False,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=LOXONE_REQUEST_TIMEOUT),
                ) as response:
                    raw_body = await response.content.read(
                        LOXONE_MAX_RESPONSE_BYTES + 1
                    )
                    if len(raw_body) > LOXONE_MAX_RESPONSE_BYTES:
                        raise LoxoneApiError("Loxone response exceeded the size limit")
                    body = raw_body.decode(
                        response.charset or "utf-8", errors="replace"
                    )
                    if response.status in {401, 403}:
                        raise LoxoneAuthError(
                            "Loxone rejected the service account or its user-management rights"
                        )
                    if response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                        )
                    if response.status >= 400:
                        raise LoxoneApiError(
                            f"Loxone HTTP request failed ({response.status})",
                            response.status,
                        )
                    value, code = self._decode_response(body)
                    if code in {401, 403, 429}:
                        raise LoxoneAuthError(f"Loxone denied user management ({code})")
                    if code not in accepted:
                        raise LoxoneApiError(
                            f"Loxone command failed ({code})",
                            code,
                        )
                    return value, code
            except LoxoneAuthError:
                raise
            except LoxoneApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_error = err
                if attempt >= LOXONE_MAX_RETRIES:
                    break
                await asyncio.sleep(2**attempt)
        raise LoxoneApiError(
            "Could not connect to the Loxone Miniserver"
        ) from last_error

    @staticmethod
    def _decode_response(body: str) -> tuple[Any, int]:
        """Decode the Loxone LL response envelope and nested JSON value."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as err:
            raise LoxoneApiError("Loxone returned invalid JSON") from err
        envelope = data.get("LL") if isinstance(data, dict) else None
        if not isinstance(envelope, dict):
            raise LoxoneApiError("Loxone returned an invalid response envelope")
        try:
            code = int(envelope.get("Code"))
        except (TypeError, ValueError) as err:
            raise LoxoneApiError(
                "Loxone response did not contain a result code"
            ) from err
        value = envelope.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        return value, code
