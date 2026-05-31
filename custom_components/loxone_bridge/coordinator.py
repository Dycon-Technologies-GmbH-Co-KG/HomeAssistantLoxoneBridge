"""Data update coordinator for Loxone Bridge integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL
from .loxone_api import LoxoneApi

_LOGGER = logging.getLogger(__name__)


class LoxoneCoordinator(DataUpdateCoordinator):
    """Coordinator that manages Loxone state data and push updates."""

    def __init__(self, hass: HomeAssistant, api: LoxoneApi) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.api = api
        self._value_states: dict[str, float] = {}
        self._text_states: dict[str, str] = {}
        self._entity_map: dict[str, list] = {}  # uuid -> list of entity callbacks
        self._unregister_value: Any = None
        self._unregister_text: Any = None
        self._unregister_connection: Any = None

    @property
    def value_states(self) -> dict[str, float]:
        """Return current value states."""
        return self._value_states

    @property
    def text_states(self) -> dict[str, str]:
        """Return current text states."""
        return self._text_states

    def get_state(self, uuid: str) -> float | str | None:
        """Get the current state for a UUID."""
        if uuid in self._value_states:
            return self._value_states[uuid]
        if uuid in self._text_states:
            return self._text_states[uuid]
        return None

    def register_entity_callback(self, uuid: str, callback_fn: Any) -> None:
        """Register an entity callback for a specific state UUID."""
        if uuid not in self._entity_map:
            self._entity_map[uuid] = []
        self._entity_map[uuid].append(callback_fn)

    def unregister_entity_callback(self, uuid: str, callback_fn: Any) -> None:
        """Unregister an entity callback."""
        if uuid in self._entity_map:
            try:
                self._entity_map[uuid].remove(callback_fn)
            except ValueError:
                pass

    async def async_setup(self) -> bool:
        """Set up the coordinator: connect, authenticate, load structure, start updates."""
        if not await self.api.async_connect():
            return False

        if not await self.api.async_authenticate():
            return False

        try:
            await self.api.async_get_structure()
        except Exception as err:  # noqa: BLE001
            # Structure load failure is non-fatal: entities won't be created
            # from the Loxone side, but the bridge (HA→Loxone, webhook) still works.
            _LOGGER.warning(
                "Could not load Loxone structure file: %s. "
                "Loxone entities will not appear in Home Assistant. "
                "Check the user's permissions on the Miniserver.",
                err,
            )

        # Register state callbacks for push updates
        self._unregister_value = self.api.register_state_callback(
            self._on_value_state_update
        )
        self._unregister_text = self.api.register_text_state_callback(
            self._on_text_state_update
        )
        self._unregister_connection = self.api.register_connection_callback(
            self._on_connection_change
        )

        await self.api.async_enable_status_updates()
        await self.api.async_start_listening()

        # Wait for the initial state burst from the Miniserver.
        # After enablebinstatusupdate the Miniserver pushes a full state
        # dump almost immediately over the local network.  Entity platforms
        # rely on this data to distinguish real hardware from ghost entries
        # (e.g. CO2 states that exist in the structure but have no sensor).
        for _ in range(10):
            if self._value_states:
                break
            await asyncio.sleep(0.5)

        return True

    @callback
    def _on_connection_change(self, connected: bool) -> None:
        """Handle a connection state change from the API.

        Calling async_set_updated_data triggers all registered entity listeners
        so every entity re-evaluates its `available` property immediately,
        rather than waiting for the next 60-second poll.
        """
        _LOGGER.debug("Loxone connection state changed: %s", "connected" if connected else "disconnected")
        self.async_set_updated_data(self._value_states)

    @callback
    def _on_value_state_update(self, uuid: str, value: float) -> None:
        """Handle a value state push update from Loxone."""
        self._value_states[uuid] = value
        if len(self._value_states) <= 3:
            _LOGGER.info(
                "Value state stored: %s = %s (total: %d, entity_map keys: %d, match: %s)",
                uuid, value, len(self._value_states), len(self._entity_map),
                uuid in self._entity_map,
            )
        self._notify_entities(uuid)

    @callback
    def _on_text_state_update(self, uuid: str, value: str) -> None:
        """Handle a text state push update from Loxone."""
        self._text_states[uuid] = value
        self._notify_entities(uuid)

    def _notify_entities(self, uuid: str) -> None:
        """Notify only the entities registered for this specific UUID.

        Previous versions also called ``async_set_updated_data`` here which
        triggered *every* entity listener on *every* state change – resulting
        in O(states × entities) state-writes per WebSocket burst and heavy
        SD-card / CPU load on both HA and the Miniserver.  Now only the
        entities that actually use *this* UUID are notified.
        """
        if uuid in self._entity_map:
            for cb in self._entity_map[uuid]:
                try:
                    cb()
                except Exception as err:
                    _LOGGER.error("Error notifying entity for %s: %s", uuid, err)

    async def _async_update_data(self) -> dict[str, float]:
        """Fallback polling update.

        The primary data flow is via WebSocket push. This polling fallback
        only returns cached state. Reconnection is handled by the API's own
        reconnect logic to avoid conflicting concurrent reconnect attempts.
        """
        return self._value_states

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        if self._unregister_value:
            self._unregister_value()
        if self._unregister_text:
            self._unregister_text()
        if self._unregister_connection:
            self._unregister_connection()
        await self.api.async_disconnect()
