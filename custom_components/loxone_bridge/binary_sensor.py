"""Binary sensor platform for Loxone Bridge."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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

# Map Loxone control types to HA binary sensor device classes
BINARY_SENSOR_DEVICE_CLASS_MAP = {
    "InfoOnlyDigital": None,
    "Alarm": BinarySensorDeviceClass.SAFETY,
    "SmokeAlarm": BinarySensorDeviceClass.SMOKE,
    "PresenceDetector": BinarySensorDeviceClass.PRESENCE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone binary sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "binary_sensor":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))
        device_class = BINARY_SENSOR_DEVICE_CLASS_MAP.get(control_type)

        entities.append(
            LoxoneBinarySensor(
                coordinator, uuid, control, room_name, cat_name, device_class
            )
        )

    async_add_entities(entities)
    _LOGGER.debug("Added %d binary sensor entities", len(entities))


class LoxoneBinarySensor(BinarySensorEntity):
    """Representation of a Loxone binary sensor (InfoOnlyDigital, Alarm, etc.)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
        device_class: BinarySensorDeviceClass | None = None,
    ) -> None:
        """Initialize the binary sensor."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"
        self._attr_device_class = device_class
        self._state_uuid = control.get("states", {}).get("active", uuid)

        # Select the most appropriate state key per control type
        states = control.get("states", {})
        control_type = control.get("type", "")
        if control_type in ("Alarm", "SmokeAlarm"):
            # Alarm controls: ``level`` indicates alarm severity
            # (0 = no alarm / safe, ≥1 = alarm triggered / unsafe).
            # ``active`` would represent the armed state which is the
            # *normal* condition and must NOT be used for is_on.
            self._state_uuid = states.get("level", states.get("active", uuid))
        elif "value" in states:
            self._state_uuid = states["value"]
        elif "active" in states:
            self._state_uuid = states["active"]

        # Detect device class from name if not already set
        if not device_class:
            self._detect_device_class(control.get("name", ""))

    def _detect_device_class(self, name: str) -> None:
        """Detect device class from sensor name."""
        name_lower = name.lower()
        if any(w in name_lower for w in ("tür", "door", "fenster", "window")):
            self._attr_device_class = BinarySensorDeviceClass.DOOR
        elif any(w in name_lower for w in ("bewegung", "motion", "presence", "präsenz")):
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif any(w in name_lower for w in ("rauch", "smoke")):
            self._attr_device_class = BinarySensorDeviceClass.SMOKE
        elif any(w in name_lower for w in ("wasser", "water", "leck", "leak")):
            self._attr_device_class = BinarySensorDeviceClass.MOISTURE

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model=self._control.get("type", "BinarySensor"),
            suggested_area=self._room_name,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        attrs = {
            ATTR_UUID: self._uuid,
            ATTR_ROOM: self._room_name,
            ATTR_CATEGORY: self._cat_name,
            ATTR_CONTROL_TYPE: self._control.get("type"),
        }
        # Add text descriptions if available
        active_text = self._control.get("details", {}).get("text", {})
        if active_text:
            attrs["text_on"] = active_text.get("on", "")
            attrs["text_off"] = active_text.get("off", "")
        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.api.connected

    @property
    def is_on(self) -> bool | None:
        """Return True if the sensor is active."""
        state = self.coordinator.get_state(self._state_uuid)
        if state is not None:
            return bool(state) and state != 0
        return None

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        self.coordinator.register_entity_callback(
            self._state_uuid, self._handle_state_update
        )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()
