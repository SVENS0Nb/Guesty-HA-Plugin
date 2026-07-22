"""Guesty webhook handler."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components import webhook

from .api import GuestyApiError, GuestyAuthError, GuestyNotFoundError

if TYPE_CHECKING:
    from .coordinator import GuestyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 1_000_000


def _webhook_secret_key(secret: str) -> bytes | None:
    """Decode a Standard Webhooks/Svix secret without accepting empty keys."""
    encoded = secret.removeprefix("whsec_").strip()
    if not encoded:
        return None
    try:
        key = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        key = encoded.encode()
    return key if len(key) >= 16 else None


def verify_webhook_signature(
    headers: Any,
    body: bytes,
    secret: str,
    *,
    now: float | None = None,
) -> str | None:
    """Verify Standard Webhooks or legacy Svix headers and return message id."""
    from .const import WEBHOOK_SIGNATURE_TOLERANCE_SECONDS

    message_id = headers.get("webhook-id") or headers.get("svix-id")
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    signatures = headers.get("webhook-signature") or headers.get("svix-signature")
    if not all(
        isinstance(value, str) and value
        for value in (message_id, timestamp, signatures)
    ):
        return None

    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        return None
    current_time = time.time() if now is None else now
    if abs(current_time - timestamp_value) > WEBHOOK_SIGNATURE_TOLERANCE_SECONDS:
        return None

    key = _webhook_secret_key(secret)
    if key is None:
        return None
    signed = f"{message_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for item in signatures.split():
        version, separator, candidate = item.partition(",")
        if separator and version == "v1" and hmac.compare_digest(candidate, expected):
            return message_id
    return None


async def async_setup_webhook(
    hass: Any,
    entry: Any,
    coordinator: GuestyDataUpdateCoordinator,
) -> str | None:
    """Register the Home Assistant webhook endpoint."""
    from .const import (
        CONF_GUESTY_WEBHOOK_SECRET,
        CONF_WEBHOOK_ID,
        DOMAIN,
        WEBHOOK_EVENTS,
        WEBHOOK_SIGNATURE_TOLERANCE_SECONDS,
    )

    seen_message_ids: dict[str, int] = {}
    processing_message_ids: set[str] = set()

    async def handle_webhook(
        hass: Any, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Handle incoming Guesty webhook payloads."""
        if (getattr(request, "content_length", None) or 0) > MAX_WEBHOOK_BODY_BYTES:
            return web.Response(status=413, body="payload too large")
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return web.Response(status=413, body="payload too large")
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            return web.Response(status=413, body="payload too large")

        secret = entry.data.get(CONF_GUESTY_WEBHOOK_SECRET)
        if not isinstance(secret, str) or not secret:
            _LOGGER.warning(
                "Guesty webhook rejected because no signing secret is loaded"
            )
            return web.Response(status=503, body="webhook verification unavailable")
        message_id = verify_webhook_signature(request.headers, body, secret)
        if message_id is None:
            _LOGGER.warning("Guesty webhook received an invalid signature")
            return web.Response(status=401, body="invalid signature")

        now = int(time.time())
        cutoff = now - WEBHOOK_SIGNATURE_TOLERANCE_SECONDS
        for seen_id, seen_at in tuple(seen_message_ids.items()):
            if seen_at < cutoff:
                seen_message_ids.pop(seen_id, None)
        if message_id in seen_message_ids or message_id in processing_message_ids:
            return web.Response(status=202)

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            _LOGGER.warning("Guesty webhook received invalid JSON")
            return web.Response(status=400, body="invalid json")

        if not isinstance(payload, dict):
            _LOGGER.warning("Guesty webhook received a non-object JSON payload")
            return web.Response(status=400, body="invalid payload")

        event = (payload.get("event") or payload.get("type") or "").lower()
        if event not in WEBHOOK_EVENTS:
            _LOGGER.debug("Ignoring unsupported Guesty webhook event %r", event)
            return web.Response(status=202)

        # Reserve the id while it is being queued to collapse concurrent
        # duplicates. Only mark it as successfully seen after the coordinator
        # accepted it so a failed request can be retried by Guesty.
        processing_message_ids.add(message_id)
        try:
            await coordinator.async_handle_webhook(payload)
        except BaseException:
            processing_message_ids.discard(message_id)
            raise
        processing_message_ids.discard(message_id)
        seen_message_ids[message_id] = now
        return web.Response(status=202)

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = webhook.async_generate_id()
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_WEBHOOK_ID: webhook_id},
        )

    # Ensure reloads always bind the stable URL to the current coordinator.
    webhook.async_unregister(hass, webhook_id)
    webhook.async_register(
        hass,
        DOMAIN,
        f"Guesty {entry.title}",
        webhook_id,
        handle_webhook,
        allowed_methods=("POST",),
    )

    return webhook_id


async def async_register_guesty_webhook(
    hass: Any,
    entry: Any,
    client: Any,
    webhook_id: str,
) -> str | None:
    """Register the HA webhook URL with Guesty."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    from .const import (
        CONF_GUESTY_WEBHOOK_ID,
        CONF_GUESTY_WEBHOOK_SECRET,
        CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID,
    )

    try:
        base_url = get_url(hass, prefer_external=True, allow_internal=False)
    except NoURLAvailableError:
        _LOGGER.warning(
            "No external Home Assistant URL available; Guesty webhooks not registered"
        )
        return None

    webhook_url = f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"
    existing_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)
    stored_secret = entry.data.get(CONF_GUESTY_WEBHOOK_SECRET)
    migration_id = entry.data.get(CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID)
    try:
        guesty_webhook_id = await client.async_ensure_webhook(
            webhook_url,
            existing_id,
        )
        if (
            guesty_webhook_id == existing_id
            and isinstance(stored_secret, str)
            and len(stored_secret.strip()) >= 16
        ):
            # The secret belongs to this unchanged remote subscription. Avoid a
            # needless API lookup and keep working during Guesty API outages.
            webhook_secret = stored_secret.strip()
        else:
            try:
                webhook_secret = await client.async_get_webhook_secret(webhook_url)
            except GuestyNotFoundError:
                if migration_id == guesty_webhook_id:
                    # This exact subscription was already recreated once. Keep
                    # polling and retry the secret lookup after the next reload,
                    # but never enter a delete/create loop.
                    raise
                _LOGGER.info("Guesty webhook has no signing secret; recreating it once")
                try:
                    await client.async_unregister_webhook(guesty_webhook_id)
                except GuestyNotFoundError:
                    pass
                guesty_webhook_id = await client.async_register_webhook(webhook_url)

                # Persist the new id and one-time marker before the second
                # lookup. A failure or restart cannot cause repeated recreation.
                migration_data = {
                    **entry.data,
                    CONF_GUESTY_WEBHOOK_ID: guesty_webhook_id,
                    CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID: guesty_webhook_id,
                }
                migration_data.pop(CONF_GUESTY_WEBHOOK_SECRET, None)
                hass.config_entries.async_update_entry(entry, data=migration_data)
                webhook_secret = await client.async_get_webhook_secret(webhook_url)
    except GuestyNotFoundError as err:
        _LOGGER.warning(
            "Guesty webhook signing secret is not available after a safe "
            "one-time migration; polling remains active: %s",
            err,
        )
        return None
    except (GuestyApiError, GuestyAuthError) as err:
        _LOGGER.warning("Failed to register Guesty webhook: %s", err)
        return None

    updated_data = {
        **entry.data,
        CONF_GUESTY_WEBHOOK_ID: guesty_webhook_id,
        CONF_GUESTY_WEBHOOK_SECRET: webhook_secret,
    }
    updated_data.pop(CONF_GUESTY_WEBHOOK_SECRET_MIGRATION_ID, None)
    hass.config_entries.async_update_entry(
        entry,
        data=updated_data,
    )
    _LOGGER.info("Guesty webhook registered successfully")
    return guesty_webhook_id
