"""Guesty webhook handler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components import webhook

from .api import GuestyApiError, GuestyAuthError

if TYPE_CHECKING:
    from .coordinator import GuestyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 1_000_000


async def async_setup_webhook(
    hass: Any,
    entry: Any,
    coordinator: GuestyDataUpdateCoordinator,
) -> str | None:
    """Register the Home Assistant webhook endpoint."""
    from .const import CONF_WEBHOOK_ID, DOMAIN, WEBHOOK_EVENTS

    async def handle_webhook(
        hass: Any, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Handle incoming Guesty webhook payloads."""
        if (getattr(request, "content_length", None) or 0) > MAX_WEBHOOK_BODY_BYTES:
            return web.Response(status=413, body="payload too large")
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            _LOGGER.warning("Guesty webhook received invalid JSON")
            return web.Response(status=400, body="invalid json")

        if not isinstance(payload, dict):
            _LOGGER.warning("Guesty webhook received a non-object JSON payload")
            return web.Response(status=400, body="invalid payload")

        event = (payload.get("event") or payload.get("type") or "").lower()
        if event not in WEBHOOK_EVENTS:
            _LOGGER.debug("Ignoring unsupported Guesty webhook event %r", event)
            return web.Response(status=202)

        hass.async_create_task(
            coordinator.async_handle_webhook(payload),
            f"guesty_webhook_{event}",
        )
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

    from .const import CONF_GUESTY_WEBHOOK_ID

    try:
        base_url = get_url(hass, prefer_external=True, allow_internal=False)
    except NoURLAvailableError:
        _LOGGER.warning(
            "No external Home Assistant URL available; Guesty webhooks not registered"
        )
        return None

    webhook_url = f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"
    existing_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)
    try:
        guesty_webhook_id = await client.async_ensure_webhook(
            webhook_url,
            existing_id,
        )
    except (GuestyApiError, GuestyAuthError) as err:
        _LOGGER.warning("Failed to register Guesty webhook: %s", err)
        return None

    if existing_id and existing_id != guesty_webhook_id:
        try:
            await client.async_unregister_webhook(existing_id)
        except (GuestyApiError, GuestyAuthError):
            _LOGGER.debug("Could not remove the obsolete Guesty webhook")

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_GUESTY_WEBHOOK_ID: guesty_webhook_id},
    )
    _LOGGER.info("Guesty webhook registered successfully")
    return guesty_webhook_id
