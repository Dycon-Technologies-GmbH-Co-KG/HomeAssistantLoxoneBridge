"""Bidirectional bridge between Home Assistant and Loxone.

This module handles:
1. HA → Loxone: Syncing HA entity states to Loxone Virtual Inputs
2. Loxone → HA: Receiving commands from Loxone Virtual Outputs via webhook
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from homeassistant.components.webhook import (
    async_generate_url,
    async_register,
    async_unregister,
)
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, WEBHOOK_ID
from .loxone_api import LoxoneApi

_LOGGER = logging.getLogger(__name__)

# Default entity filter: sync everything except internal/noisy entities
DEFAULT_EXCLUDE_DOMAINS = {
    "automation", "script", "scene", "group", "input_boolean",
    "persistent_notification", "device_tracker", "zone", "sun",
    "weather", "update", "button", "event", "calendar",
    "loxone_bridge",  # Prevent feedback loops
}

# Stop retrying a Virtual Input after this many consecutive failures
_MAX_VI_FAILURES = 3
# Minimum seconds between pushes for the same entity
_MIN_PUSH_INTERVAL = 2.0


class LoxoneBridge:
    """Manages bidirectional state sync between HA and Loxone."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: LoxoneApi,
        entry_id: str,
        sync_ha_to_loxone: bool = True,
        sync_loxone_to_ha: bool = True,
    ) -> None:
        """Initialize the bridge."""
        self.hass = hass
        self.api = api
        self._entry_id = entry_id
        self._sync_ha_to_loxone = sync_ha_to_loxone
        self._sync_loxone_to_ha = sync_loxone_to_ha
        self._state_listener = None
        self._webhook_id = f"{WEBHOOK_ID}_{entry_id[:8]}"
        self._virtual_input_map: dict[str, str] = {}  # ha entity_id -> loxone vi name
        self._virtual_output_map: dict[str, str] = {}  # loxone cmd -> ha service call
        self._ha_to_loxone_queue: asyncio.Queue = asyncio.Queue()
        self._queue_task: asyncio.Task | None = None
        # Track failed Virtual Input pushes to stop retrying non-existent VIs
        self._vi_failure_count: dict[str, int] = {}
        self._vi_blocked: set[str] = set()
        # Rate-limit: last push timestamp per entity
        self._last_push_time: dict[str, float] = {}

    @property
    def webhook_id(self) -> str:
        """Return the webhook ID."""
        return self._webhook_id

    @property
    def webhook_url(self) -> str:
        """Return the full webhook URL."""
        return async_generate_url(self.hass, self._webhook_id)

    async def async_start(self) -> None:
        """Start the bridge (both directions)."""
        if self._sync_ha_to_loxone:
            await self._start_ha_to_loxone()

        if self._sync_loxone_to_ha:
            await self._start_loxone_to_ha()

        _LOGGER.info("Loxone Bridge started (HA→Lox: %s, Lox→HA: %s)",
                      self._sync_ha_to_loxone, self._sync_loxone_to_ha)

    async def async_stop(self) -> None:
        """Stop the bridge."""
        if self._state_listener:
            self._state_listener()
            self._state_listener = None

        if self._queue_task and not self._queue_task.done():
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass

        try:
            async_unregister(self.hass, self._webhook_id)
        except ValueError:
            pass

        _LOGGER.info("Loxone Bridge stopped")

    # ==========================================
    # HA → Loxone: Push HA states to Virtual Inputs
    # ==========================================

    async def _start_ha_to_loxone(self) -> None:
        """Start syncing HA entity states to Loxone Virtual Inputs."""
        self._state_listener = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._on_ha_state_changed
        )
        self._queue_task = asyncio.ensure_future(self._process_ha_to_loxone_queue())
        _LOGGER.debug("HA → Loxone sync started")

    @callback
    def _on_ha_state_changed(self, event: Event) -> None:
        """Handle HA state change events."""
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")

        if not new_state or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        # Skip internal domains
        domain = entity_id.split(".")[0]
        if domain in DEFAULT_EXCLUDE_DOMAINS:
            return

        # Skip loxone_bridge entities to prevent loops
        if new_state.attributes.get("loxone_uuid"):
            return

        self._ha_to_loxone_queue.put_nowait(
            {"entity_id": entity_id, "state": new_state}
        )

    async def _process_ha_to_loxone_queue(self) -> None:
        """Process queued HA state changes and push to Loxone."""
        while True:
            try:
                item = await self._ha_to_loxone_queue.get()
                entity_id = item["entity_id"]
                state = item["state"]

                await self._push_state_to_loxone(entity_id, state)
                self._ha_to_loxone_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Error processing HA→Loxone queue: %s", err)

    async def _push_state_to_loxone(self, entity_id: str, state: Any) -> None:
        """Push an HA entity state to Loxone via Virtual HTTPS Input.

        Loxone Virtual Inputs can receive values via HTTPS:
          https://<miniserver>/dev/sps/io/<vi_name>/<value>

        The naming convention for auto-mapped Virtual Inputs:
          vi_<entity_id_with_underscores>
        """
        vi_name = self._get_virtual_input_name(entity_id)

        # Skip entities whose VI has been permanently blocked (doesn't exist)
        if vi_name in self._vi_blocked:
            return

        # Rate-limit: skip if pushed too recently
        now = time.monotonic()
        last = self._last_push_time.get(entity_id, 0.0)
        if now - last < _MIN_PUSH_INTERVAL:
            return

        value = self._convert_ha_state_to_loxone(entity_id, state)
        if value is None:
            return

        try:
            result = await self.api.async_send_http_command(vi_name, str(value))
            if result is not None:
                self._last_push_time[entity_id] = now
                # Reset failure count on success
                self._vi_failure_count.pop(vi_name, None)
                _LOGGER.debug("Pushed %s = %s to Loxone VI '%s'", entity_id, value, vi_name)
            else:
                # Track failures; block after N consecutive failures
                count = self._vi_failure_count.get(vi_name, 0) + 1
                self._vi_failure_count[vi_name] = count
                if count >= _MAX_VI_FAILURES:
                    self._vi_blocked.add(vi_name)
                    _LOGGER.info(
                        "Blocking VI '%s' after %d failures (entity %s)",
                        vi_name, count, entity_id,
                    )
        except Exception:
            pass  # Already logged inside async_send_http_command

    def _get_virtual_input_name(self, entity_id: str) -> str:
        """Get the Loxone Virtual Input name for an HA entity."""
        if entity_id in self._virtual_input_map:
            return self._virtual_input_map[entity_id]
        # Auto-generated name: replace dots with underscores
        return f"vi_{entity_id.replace('.', '_')}"

    @staticmethod
    def _convert_ha_state_to_loxone(entity_id: str, state: Any) -> str | float | None:
        """Convert HA state to a Loxone-compatible value."""
        state_value = state.state
        domain = entity_id.split(".")[0]

        # Binary states
        if state_value in (STATE_ON, "on", "home", "open", "detected", "True"):
            return 1
        if state_value in (STATE_OFF, "off", "not_home", "closed", "clear", "False"):
            return 0

        # Numeric states
        try:
            return float(state_value)
        except (ValueError, TypeError):
            pass

        # Text states – URL-encode for Loxone
        return state_value

    def set_virtual_input_mapping(self, entity_id: str, vi_name: str) -> None:
        """Set a custom Virtual Input mapping."""
        self._virtual_input_map[entity_id] = vi_name

    # ==========================================
    # Loxone → HA: Receive commands via Webhook
    # ==========================================

    async def _start_loxone_to_ha(self) -> None:
        """Start receiving commands from Loxone via webhook."""
        async_register(
            self.hass,
            DOMAIN,
            f"Loxone Bridge ({self._entry_id[:8]})",
            self._webhook_id,
            self._handle_webhook,
        )
        _LOGGER.info(
            "Loxone → HA webhook registered: %s", self.webhook_url
        )

    async def _handle_webhook(
        self,
        hass: HomeAssistant,
        webhook_id: str,
        request: Any,
    ) -> Any:
        """Handle incoming webhook from Loxone Virtual HTTP Output.

        Loxone can send HTTP requests to this webhook to control HA entities.

        Expected JSON body:
        {
            "entity_id": "light.living_room",
            "action": "turn_on",
            "data": {"brightness": 255}
        }

        Or simplified:
        {
            "entity_id": "switch.garden_pump",
            "state": "on"
        }

        Or batch:
        {
            "commands": [
                {"entity_id": "light.kitchen", "action": "turn_on"},
                {"entity_id": "switch.fan", "state": "off"}
            ]
        }
        """
        from aiohttp import web

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            # Try query parameters as fallback
            body = dict(request.query)

        _LOGGER.debug("Webhook received: %s", body)

        try:
            # Handle batch commands
            if "commands" in body:
                results = []
                for cmd in body["commands"]:
                    result = await self._execute_ha_command(cmd)
                    results.append(result)
                return web.json_response({"results": results})

            # Handle single command
            result = await self._execute_ha_command(body)
            return web.json_response(result)

        except Exception as err:
            _LOGGER.error("Webhook error: %s", err)
            return web.json_response(
                {"error": str(err)}, status=500
            )

    async def _execute_ha_command(self, command: dict) -> dict:
        """Execute a single HA command from Loxone."""
        entity_id = command.get("entity_id", "")
        action = command.get("action", command.get("service"))
        state = command.get("state")
        data = command.get("data", {})

        if not entity_id:
            return {"error": "entity_id required"}

        domain = entity_id.split(".")[0]

        # Simple state-based commands
        if state is not None and not action:
            if str(state).lower() in ("1", "on", "true", "open", "home"):
                action = "turn_on"
            elif str(state).lower() in ("0", "off", "false", "close", "away"):
                action = "turn_off"
            elif str(state).lower() == "toggle":
                action = "toggle"
            else:
                # Try to set a numeric value
                try:
                    numeric_val = float(state)
                    if domain == "light":
                        action = "turn_on"
                        data["brightness"] = int(numeric_val * 2.55)  # 0-100 → 0-255
                    elif domain == "cover":
                        action = "set_cover_position"
                        data["position"] = int(numeric_val)
                    elif domain == "climate":
                        action = "set_temperature"
                        data["temperature"] = numeric_val
                    elif domain in ("number", "input_number"):
                        action = "set_value"
                        data["value"] = numeric_val
                    else:
                        action = "turn_on"
                except (ValueError, TypeError):
                    return {"error": f"Cannot interpret state: {state}"}

        if not action:
            return {"error": "action or state required"}

        # Determine the correct domain for the service call
        service_domain = domain
        if action in ("turn_on", "turn_off", "toggle") and domain in (
            "light", "switch", "fan", "media_player",
        ):
            service_domain = domain
        elif action in ("turn_on", "turn_off", "toggle"):
            service_domain = "homeassistant"

        # Add entity_id to data
        service_data = {"entity_id": entity_id, **data}

        try:
            await self.hass.services.async_call(
                service_domain, action, service_data, blocking=True
            )
            _LOGGER.info("Executed: %s.%s for %s", service_domain, action, entity_id)
            return {"success": True, "entity_id": entity_id, "action": action}
        except Exception as err:
            _LOGGER.error("Failed to execute %s.%s: %s", service_domain, action, err)
            return {"error": str(err), "entity_id": entity_id}

    # ==========================================
    # Helper: Generate Loxone Config Snippets
    # ==========================================

    def generate_loxone_virtual_inputs_config(self) -> list[dict]:
        """Generate a list of Virtual Input configs for Loxone Config.

        This can be used to help users set up the Loxone side.
        Returns info about which Virtual Inputs should be created in Loxone Config.
        """
        configs = []
        states = self.hass.states.async_all()
        for state in states:
            entity_id = state.entity_id
            domain = entity_id.split(".")[0]
            if domain in DEFAULT_EXCLUDE_DOMAINS:
                continue

            vi_name = self._get_virtual_input_name(entity_id)
            configs.append({
                "name": vi_name,
                "entity_id": entity_id,
                "type": "analog" if domain in ("sensor", "number", "input_number", "climate") else "digital",
                "description": f"HA entity: {state.attributes.get('friendly_name', entity_id)}",
            })
        return configs

    def generate_loxone_virtual_outputs_config(self) -> list[dict]:
        """Generate a list of Virtual Output configs for Loxone Config.

        Returns info about which Virtual Outputs should be created in Loxone Config
        to control HA entities from Loxone.
        """
        configs = []
        states = self.hass.states.async_all()
        for state in states:
            entity_id = state.entity_id
            domain = entity_id.split(".")[0]

            # Only for controllable entities
            if domain not in ("light", "switch", "cover", "climate", "fan",
                              "media_player", "lock", "number", "input_number"):
                continue

            configs.append({
                "name": f"vo_{entity_id.replace('.', '_')}",
                "entity_id": entity_id,
                "webhook_url": self.webhook_url,
                "on_command": f'{self.webhook_url}?entity_id={entity_id}&state=on',
                "off_command": f'{self.webhook_url}?entity_id={entity_id}&state=off',
                "description": f"Control HA: {state.attributes.get('friendly_name', entity_id)}",
            })
        return configs
