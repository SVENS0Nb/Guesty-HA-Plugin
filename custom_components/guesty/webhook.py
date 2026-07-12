"""Guesty webhook handler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components import webhook

if TYPE_CHECKING:
    from .coordinator import GuestyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_webhook(
    hass: Any,
    entry: Any,
    coordinator: GuestyDataUpdateCoordinator,
) -> str | None:
    """Register the Home Assistant webhook endpoint."""
    from .const import CONF_WEBHOOK_ID, DOMAIN

    async def handle_webhook(
        hass: Any, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Handle incoming Guesty webhook payloads."""
        try:
            payload = await request.json()
        except Exception:
            _LOGGER.warning("Guesty webhook received invalid JSON")
            return webhook.Response(status=400, body="invalid json")

        _LOGGER.debug(
            "Guesty webhook received: %s",
            payload.get("event") or payload.get("type"),
        )
        await coordinator.async_handle_webhook(payload)
        return webhook.Response(status=200)

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = webhook.async_register(
            hass,
            DOMAIN,
            f"Guesty {entry.title}",
            handle_webhook,
            allowed_methods=("POST",),
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_WEBHOOK_ID: webhook_id},
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
        try:
            base_url = get_url(hass, prefer_external=False, allow_internal=True)
        except NoURLAvailableError:
            _LOGGER.warning(
                "No Home Assistant URL available; Guesty webhooks not registered"
            )
            return None

    webhook_url = f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"
    existing_id = entry.data.get(CONF_GUESTY_WEBHOOK_ID)

    if existing_id:
        try:
            await client.async_unregister_webhook(existing_id)
        except Exception:
            _LOGGER.debug("Could not unregister previous Guesty webhook")

    try:
        guesty_webhook_id = await client.async_register_webhook(webhook_url)
    except Exception as err:
        _LOGGER.warning("Failed to register Guesty webhook: %s", err)
        return None

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_GUESTY_WEBHOOK_ID: guesty_webhook_id},
    )
    _LOGGER.info("Guesty webhook registered at %s", webhook_url)
    return guesty_webhook_id
