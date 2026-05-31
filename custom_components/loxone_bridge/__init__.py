"""The Loxone Bridge integration.

Provides bidirectional communication between Home Assistant and Loxone Miniserver.
- Loxone controls are exposed as HA entities (lights, switches, covers, sensors, climate)
- HA entity states are pushed to Loxone Virtual Inputs
- Loxone can control HA entities via webhook (Virtual HTTP Outputs)
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .bridge import LoxoneBridge
from .const import (
    CONF_SYNC_HA_TO_LOXONE,
    CONF_SYNC_LOXONE_TO_HA,
    CONF_USE_TLS,
    CONF_VERIFY_SSL,
    DEFAULT_USE_TLS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import LoxoneCoordinator
from .loxone_api import LoxoneApi

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Loxone Bridge component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Loxone Bridge from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    use_tls = entry.data.get(CONF_USE_TLS, DEFAULT_USE_TLS)
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    sync_ha_to_loxone = entry.data.get(CONF_SYNC_HA_TO_LOXONE, True)
    sync_loxone_to_ha = entry.data.get(CONF_SYNC_LOXONE_TO_HA, True)

    # Create API client with TLS enabled by default
    api = LoxoneApi(host, port, username, password, use_tls=use_tls, verify_ssl=verify_ssl)

    # Create coordinator
    coordinator = LoxoneCoordinator(hass, api)

    # Connect and load structure
    if not await coordinator.async_setup():
        _LOGGER.error("Failed to connect to Loxone Miniserver at %s:%s", host, port)
        await api.async_disconnect()
        return False

    # Create bridge for bidirectional sync
    bridge = LoxoneBridge(
        hass, api, entry.entry_id,
        sync_ha_to_loxone=sync_ha_to_loxone,
        sync_loxone_to_ha=sync_loxone_to_ha,
    )

    # Store references
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "bridge": bridge,
    }

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start the bridge after platforms are set up
    await bridge.async_start()

    # Register services
    await _async_register_services(hass)

    _LOGGER.info(
        "Loxone Bridge setup complete: %s:%s (%d controls)",
        host,
        port,
        len(api.controls),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        return True

    # Stop bridge
    bridge: LoxoneBridge = data["bridge"]
    await bridge.async_stop()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Shutdown coordinator
    coordinator: LoxoneCoordinator = data["coordinator"]
    await coordinator.async_shutdown()

    # Clean up
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    async def handle_send_command(call: ServiceCall) -> None:
        """Handle the send_command service."""
        uuid = call.data.get("uuid", "")
        command = call.data.get("command", "")

        for entry_data in hass.data[DOMAIN].values():
            if isinstance(entry_data, dict) and "api" in entry_data:
                api: LoxoneApi = entry_data["api"]
                await api.async_send_command(uuid, command)
                return

    async def handle_get_webhook_url(call: ServiceCall) -> None:
        """Handle the get_webhook_url service – fires an event with the URL."""
        for entry_data in hass.data[DOMAIN].values():
            if isinstance(entry_data, dict) and "bridge" in entry_data:
                bridge: LoxoneBridge = entry_data["bridge"]
                hass.bus.async_fire(
                    f"{DOMAIN}_webhook_url",
                    {"webhook_url": bridge.webhook_url},
                )
                _LOGGER.info("Webhook URL: %s", bridge.webhook_url)
                return

    async def handle_generate_loxone_config(call: ServiceCall) -> None:
        """Generate Loxone Virtual Input/Output configuration snippets."""
        for entry_data in hass.data[DOMAIN].values():
            if isinstance(entry_data, dict) and "bridge" in entry_data:
                bridge: LoxoneBridge = entry_data["bridge"]
                vi_config = bridge.generate_loxone_virtual_inputs_config()
                vo_config = bridge.generate_loxone_virtual_outputs_config()
                hass.bus.async_fire(
                    f"{DOMAIN}_config",
                    {
                        "virtual_inputs": vi_config[:50],  # Limit for event size
                        "virtual_outputs": vo_config[:50],
                        "total_inputs": len(vi_config),
                        "total_outputs": len(vo_config),
                    },
                )
                _LOGGER.info(
                    "Generated config: %d Virtual Inputs, %d Virtual Outputs",
                    len(vi_config),
                    len(vo_config),
                )
                return

    # Register services (only once)
    if not hass.services.has_service(DOMAIN, "send_command"):
        hass.services.async_register(DOMAIN, "send_command", handle_send_command)
    if not hass.services.has_service(DOMAIN, "get_webhook_url"):
        hass.services.async_register(DOMAIN, "get_webhook_url", handle_get_webhook_url)
    if not hass.services.has_service(DOMAIN, "generate_loxone_config"):
        hass.services.async_register(
            DOMAIN, "generate_loxone_config", handle_generate_loxone_config
        )
