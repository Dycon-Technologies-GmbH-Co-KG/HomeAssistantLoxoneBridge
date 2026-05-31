"""Bidirectional bridge between Home Assistant and Loxone.

This module handles:
1. HA → Loxone: Syncing HA entity states to Loxone Virtual Inputs
2. Loxone → HA: Receiving commands from Loxone Virtual Outputs via webhook
"""
from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv6Address, ip_address
import json
import logging
import socket
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

_IpAddress = IPv4Address | IPv6Address

# Default entity filter: sync everything except internal/noisy entities
DEFAULT_EXCLUDE_DOMAINS = {
    "automation", "script", "scene", "group", "input_boolean",
    "persistent_notification", "device_tracker", "zone", "sun",
    "weather", "update", "button", "event", "calendar",
    "loxone_bridge",  # Prevent feedback loops
}

# Minimum seconds between pushes for the same entity
_MIN_PUSH_INTERVAL = 2.0
# Minimum seconds between retries after Loxone returned 403/404 for a VI.
_VI_NOT_FOUND_RETRY_INTERVAL = 60.0
# Process live state changes before the slower initial snapshot.
_STATE_CHANGE_PRIORITY = 0
_INITIAL_STATE_PRIORITY = 10
# Keep Loxone VI updates responsive; the Miniserver is expected to be local.
_VI_UPDATE_TIMEOUT = 3.0
_HA_TO_LOXONE_WORKERS = 4


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
        self._ha_to_loxone_queue: asyncio.PriorityQueue[
            tuple[int, int, dict[str, Any]]
        ] = asyncio.PriorityQueue()
        self._queue_tasks: list[asyncio.Task] = []
        self._queue_sequence = 0
        self._queued_state_versions: dict[str, int] = {}
        self._entity_push_locks: dict[str, asyncio.Lock] = {}
        self._miniserver_host = api.host
        self._miniserver_source_ips: set[_IpAddress] | None = None
        # Track 403/404 responses for missing Virtual Inputs without blocking forever.
        self._vi_not_found_retry_at: dict[str, float] = {}
        # Rate-limit: last push timestamp per entity
        self._last_push_time: dict[str, float] = {}
        self._last_pushed_values: dict[str, str | int | float] = {}

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

        for task in self._queue_tasks:
            task.cancel()
        if self._queue_tasks:
            await asyncio.gather(*self._queue_tasks, return_exceptions=True)
            self._queue_tasks.clear()

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
        self._queue_tasks = [
            asyncio.ensure_future(self._process_ha_to_loxone_queue(worker_id))
            for worker_id in range(_HA_TO_LOXONE_WORKERS)
        ]
        self._queue_current_ha_states()
        _LOGGER.debug("HA → Loxone sync started")

    @callback
    def _on_ha_state_changed(self, event: Event) -> None:
        """Handle HA state change events."""
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")

        self._queue_ha_state_for_sync(
            entity_id,
            new_state,
            priority=_STATE_CHANGE_PRIORITY,
        )

    @callback
    def _queue_current_ha_states(self) -> None:
        """Queue the current HA states so Loxone gets an initial snapshot."""
        queued = 0
        for state in self.hass.states.async_all():
            if self._queue_ha_state_for_sync(
                state.entity_id,
                state,
                priority=_INITIAL_STATE_PRIORITY,
            ):
                queued += 1

        _LOGGER.debug("Queued %d current HA states for Loxone sync", queued)

    @callback
    def _queue_ha_state_for_sync(
        self,
        entity_id: str,
        state: Any,
        *,
        priority: int,
    ) -> bool:
        """Queue an HA state for Loxone sync when it is syncable."""
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False

        if not self._is_state_syncable(entity_id, state):
            return False

        self._queue_sequence += 1
        version = self._queued_state_versions.get(entity_id, 0) + 1
        self._queued_state_versions[entity_id] = version
        self._ha_to_loxone_queue.put_nowait(
            (
                priority,
                self._queue_sequence,
                {"entity_id": entity_id, "state": state, "version": version},
            )
        )
        return True

    def _is_state_syncable(self, entity_id: str, state: Any) -> bool:
        """Return whether a Home Assistant state should be synced to Loxone."""
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False

        domain = entity_id.split(".")[0]
        if domain in DEFAULT_EXCLUDE_DOMAINS:
            return False

        return not state.attributes.get("loxone_uuid")

    async def _process_ha_to_loxone_queue(self, worker_id: int) -> None:
        """Process queued HA state changes and push to Loxone."""
        while True:
            queue_item = await self._ha_to_loxone_queue.get()
            try:
                _, _, item = queue_item
                entity_id = item["entity_id"]
                state = item["state"]
                version = item["version"]

                lock = self._entity_push_locks.setdefault(entity_id, asyncio.Lock())
                async with lock:
                    if version != self._queued_state_versions.get(entity_id):
                        continue

                    if not self._is_state_syncable(entity_id, state):
                        continue

                    await self._push_state_to_loxone(entity_id, state)
            except Exception as err:
                _LOGGER.error(
                    "Error processing HA→Loxone queue worker %d: %s",
                    worker_id,
                    err,
                )
            finally:
                self._ha_to_loxone_queue.task_done()

    async def _push_state_to_loxone(self, entity_id: str, state: Any) -> None:
        """Push an HA entity state to Loxone via Virtual HTTPS Input.

        Loxone Virtual Inputs can receive values via HTTPS:
          https://<miniserver>/dev/sps/io/<vi_name>/<value>

        The naming convention for auto-mapped Virtual Inputs:
          vi_<entity_id_with_underscores>
        """
        vi_name = self._get_virtual_input_name(entity_id)

        now = time.monotonic()
        retry_at = self._vi_not_found_retry_at.get(vi_name)
        if retry_at is not None and now < retry_at:
            return
        if retry_at is not None:
            self._vi_not_found_retry_at.pop(vi_name, None)

        value = self._convert_ha_state_to_loxone(entity_id, state)
        if value is None:
            return

        # Rate-limit repeated identical values, but never suppress a changed state.
        last = self._last_push_time.get(entity_id, 0.0)
        if (
            now - last < _MIN_PUSH_INTERVAL
            and self._last_pushed_values.get(entity_id) == value
        ):
            return

        try:
            result = await self.api.async_send_http_command_result(
                vi_name,
                str(value),
                timeout=_VI_UPDATE_TIMEOUT,
            )
            if result.success:
                self._last_push_time[entity_id] = time.monotonic()
                self._last_pushed_values[entity_id] = value
                self._vi_not_found_retry_at.pop(vi_name, None)
                _LOGGER.debug(
                    "Pushed %s = %s to Loxone VI '%s'",
                    entity_id,
                    value,
                    vi_name,
                )
            elif result.effective_status in (403, 404):
                self._vi_not_found_retry_at[vi_name] = (
                    time.monotonic() + _VI_NOT_FOUND_RETRY_INTERVAL
                )
                _LOGGER.debug(
                    "Loxone VI '%s' returned code %s for %s; retrying in %.0f seconds",
                    vi_name,
                    result.effective_status,
                    entity_id,
                    _VI_NOT_FOUND_RETRY_INTERVAL,
                )
            else:
                _LOGGER.warning(
                    "Failed to push %s = %s to Loxone VI '%s' "
                    "(http_status=%s, loxone_code=%s, error=%s)",
                    entity_id,
                    value,
                    vi_name,
                    result.status,
                    result.loxone_code,
                    result.error,
                )
        except Exception as err:
            _LOGGER.warning(
                "Failed to push %s to Loxone VI '%s': %s",
                entity_id,
                vi_name,
                err,
            )

    def _get_virtual_input_name(self, entity_id: str) -> str:
        """Get the Loxone Virtual Input name for an HA entity."""
        if entity_id in self._virtual_input_map:
            return self._virtual_input_map[entity_id]
        # Auto-generated name: replace dots with underscores
        return f"vi_{entity_id.replace('.', '_')}"

    @staticmethod
    def _convert_ha_state_to_loxone(
        entity_id: str,
        state: Any,
    ) -> str | int | float | None:
        """Convert HA state to a Loxone-compatible value."""
        state_value = state.state
        domain = entity_id.split(".")[0]

        if domain == "switch":
            if state_value == STATE_ON:
                return 1
            if state_value == STATE_OFF:
                return 0

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
        _LOGGER.info("Loxone → HA webhook registered")

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

        if not await self._is_request_from_miniserver(request):
            return web.json_response({"error": "forbidden"}, status=403)

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

    async def _is_request_from_miniserver(self, request: Any) -> bool:
        """Return True if the webhook request originates from the Miniserver."""
        remote_ip = self._get_request_remote_ip(request)
        if remote_ip is None:
            _LOGGER.warning("Rejected Loxone webhook without a source IP")
            return False

        allowed_ips = await self._get_miniserver_source_ips()
        if remote_ip in allowed_ips:
            return True

        # Hostnames may resolve to a different address after DHCP/DNS changes.
        if self._parse_ip_address(self._miniserver_host) is None:
            allowed_ips = await self._get_miniserver_source_ips(refresh=True)
            if remote_ip in allowed_ips:
                return True

        _LOGGER.warning(
            "Rejected Loxone webhook from %s; expected Miniserver host %s",
            remote_ip,
            self._miniserver_host,
        )
        return False

    async def _get_miniserver_source_ips(
        self,
        *,
        refresh: bool = False,
    ) -> set[_IpAddress]:
        """Return normalized IP addresses for the configured Miniserver host."""
        if self._miniserver_source_ips is not None and not refresh:
            return self._miniserver_source_ips

        self._miniserver_source_ips = await self._resolve_miniserver_source_ips()
        return self._miniserver_source_ips

    async def _resolve_miniserver_source_ips(self) -> set[_IpAddress]:
        """Resolve the configured Miniserver host to allowed source IPs."""
        host = self._miniserver_host.strip()
        lookup_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        configured_ip = self._parse_ip_address(lookup_host)
        if configured_ip is not None:
            return {configured_ip}

        try:
            addr_infos = await self.hass.async_add_executor_job(
                socket.getaddrinfo,
                lookup_host,
                None,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except socket.gaierror as err:
            _LOGGER.error(
                "Cannot resolve configured Loxone Miniserver host '%s': %s",
                host,
                err,
            )
            return set()

        resolved_ips: set[_IpAddress] = set()
        for addr_info in addr_infos:
            sockaddr = addr_info[4]
            if not sockaddr:
                continue
            resolved_ip = self._parse_ip_address(sockaddr[0])
            if resolved_ip is not None:
                resolved_ips.add(resolved_ip)

        if not resolved_ips:
            _LOGGER.error(
                "Configured Loxone Miniserver host '%s' did not resolve to an IP",
                host,
            )

        return resolved_ips

    @staticmethod
    def _get_request_remote_ip(request: Any) -> _IpAddress | None:
        """Return the normalized remote IP address reported by Home Assistant."""
        remote = getattr(request, "remote", None)
        remote_ip = LoxoneBridge._parse_ip_address(remote)
        if remote_ip is not None:
            return remote_ip

        transport = getattr(request, "transport", None)
        if transport is None:
            return None

        peername = transport.get_extra_info("peername")
        if not peername:
            return None

        return LoxoneBridge._parse_ip_address(peername[0])

    @staticmethod
    def _parse_ip_address(value: Any) -> _IpAddress | None:
        """Parse and normalize an IP address string."""
        if not isinstance(value, str) or not value:
            return None

        try:
            parsed_ip = ip_address(value.split("%", 1)[0])
        except ValueError:
            return None

        if isinstance(parsed_ip, IPv6Address) and parsed_ip.ipv4_mapped:
            return parsed_ip.ipv4_mapped

        return parsed_ip

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
