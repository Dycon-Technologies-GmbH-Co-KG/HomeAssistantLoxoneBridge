"""Switch platform for Loxone Bridge."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CATEGORY,
    ATTR_CONTROL_TYPE,
    ATTR_ROOM,
    ATTR_UUID,
    DOMAIN,
    LOXONE_CONTROL_MAP,
)
from .coordinator import LoxoneCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "switch":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))

        if control_type == "TimedSwitch":
            entities.append(LoxoneTimedSwitch(coordinator, uuid, control, room_name, cat_name))
        elif control_type == "Pushbutton":
            entities.append(LoxonePushbutton(coordinator, uuid, control, room_name, cat_name))
        else:
            entities.append(LoxoneSwitch(coordinator, uuid, control, room_name, cat_name))

    async_add_entities(entities)
    _LOGGER.debug("Added %d switch entities", len(entities))


class LoxoneSwitch(SwitchEntity):
    """Representation of a Loxone switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"
        self._state_uuid = control.get("states", {}).get("active", uuid)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model=self._control.get("type", "Switch"),
            suggested_area=self._room_name,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        return {
            ATTR_UUID: self._uuid,
            ATTR_ROOM: self._room_name,
            ATTR_CATEGORY: self._cat_name,
            ATTR_CONTROL_TYPE: self._control.get("type"),
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.api.connected

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        state = self.coordinator.get_state(self._state_uuid)
        return bool(state) and state != 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.coordinator.api.async_send_command(self._uuid, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.coordinator.api.async_send_command(self._uuid, "off")

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        self.coordinator.register_entity_callback(
            self._state_uuid, self._handle_state_update
        )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()


class LoxoneTimedSwitch(LoxoneSwitch):
    """Representation of a Loxone timed switch (Treppenlichtschalter)."""

    def __init__(self, coordinator, uuid, control, room_name, cat_name) -> None:
        """Initialize the timed switch."""
        super().__init__(coordinator, uuid, control, room_name, cat_name)
        self._deactivation_delay_uuid = control.get("states", {}).get(
            "deactivationDelay", ""
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes including remaining time."""
        attrs = super().extra_state_attributes
        delay = self.coordinator.get_state(self._deactivation_delay_uuid)
        if delay is not None:
            attrs["remaining_time"] = delay
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pulse the timed switch."""
        await self.coordinator.api.async_send_command(self._uuid, "pulse")


class LoxonePushbutton(LoxoneSwitch):
    """Representation of a Loxone pushbutton."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pulse the pushbutton."""
        await self.coordinator.api.async_send_command(self._uuid, "pulse")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release the pushbutton."""
        await self.coordinator.api.async_send_command(self._uuid, "off")
