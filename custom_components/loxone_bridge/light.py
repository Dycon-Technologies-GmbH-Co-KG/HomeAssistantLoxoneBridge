"""Light platform for Loxone Bridge."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone light entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "light":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))

        if control_type in ("Dimmer",):
            entities.append(LoxoneDimmer(coordinator, uuid, control, room_name, cat_name))
        elif control_type in ("ColorPickerV2",):
            entities.append(LoxoneColorLight(coordinator, uuid, control, room_name, cat_name))
        elif control_type in ("LightController", "LightControllerV2"):
            entities.append(LoxoneLightController(coordinator, uuid, control, room_name, cat_name))
        else:
            entities.append(LoxoneLight(coordinator, uuid, control, room_name, cat_name))

    async_add_entities(entities)
    _LOGGER.debug("Added %d light entities", len(entities))


class LoxoneLightBase(LightEntity):
    """Base class for Loxone light entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the light."""
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
            model=self._control.get("type", "Light"),
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

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        self.coordinator.register_entity_callback(
            self._state_uuid, self._handle_state_update
        )
        # Also register sub-states (position, color, activeMoods, etc.)
        for state_name, state_uuid in self._control.get("states", {}).items():
            if state_uuid != self._state_uuid:
                self.coordinator.register_entity_callback(
                    state_uuid, self._handle_state_update
                )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()


class LoxoneLight(LoxoneLightBase):
    """Representation of a simple Loxone light (on/off)."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        state = self.coordinator.get_state(self._state_uuid)
        return bool(state) and state != 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        await self.coordinator.api.async_send_command(self._uuid, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.api.async_send_command(self._uuid, "off")


class LoxoneDimmer(LoxoneLightBase):
    """Representation of a Loxone dimmer."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator, uuid, control, room_name, cat_name) -> None:
        """Initialize the dimmer."""
        super().__init__(coordinator, uuid, control, room_name, cat_name)
        self._position_uuid = control.get("states", {}).get("position", uuid)

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        state = self.coordinator.get_state(self._state_uuid)
        if state is not None:
            return bool(state) and state != 0
        position = self.coordinator.get_state(self._position_uuid)
        return position is not None and position > 0

    @property
    def brightness(self) -> int | None:
        """Return the brightness (0-255)."""
        position = self.coordinator.get_state(self._position_uuid)
        if position is not None:
            return int(float(position) * 2.55)
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        if ATTR_BRIGHTNESS in kwargs:
            pct = int(kwargs[ATTR_BRIGHTNESS] / 2.55)
            await self.coordinator.api.async_send_command(self._uuid, str(pct))
        else:
            await self.coordinator.api.async_send_command(self._uuid, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.api.async_send_command(self._uuid, "off")


class LoxoneColorLight(LoxoneLightBase):
    """Representation of a Loxone color light."""

    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.BRIGHTNESS}

    def __init__(self, coordinator, uuid, control, room_name, cat_name) -> None:
        """Initialize the color light."""
        super().__init__(coordinator, uuid, control, room_name, cat_name)
        self._color_uuid = control.get("states", {}).get("color", uuid)

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        state = self.coordinator.get_state(self._state_uuid)
        return bool(state) and state != 0

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the HS color."""
        color = self.coordinator.get_state(self._color_uuid)
        if color and isinstance(color, str):
            # Loxone sends "hsv(h,s,v)" format
            try:
                parts = color.replace("hsv(", "").replace(")", "").split(",")
                return (float(parts[0]), float(parts[1]))
            except (ValueError, IndexError):
                pass
        return None

    @property
    def brightness(self) -> int | None:
        """Return the brightness."""
        color = self.coordinator.get_state(self._color_uuid)
        if color and isinstance(color, str):
            try:
                parts = color.replace("hsv(", "").replace(")", "").split(",")
                return int(float(parts[2]) * 2.55)
            except (ValueError, IndexError):
                pass
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on with optional color/brightness."""
        if ATTR_HS_COLOR in kwargs or ATTR_BRIGHTNESS in kwargs:
            hs = kwargs.get(ATTR_HS_COLOR, self.hs_color or (0, 0))
            bright = kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255)
            h, s = hs
            v = int(bright / 2.55)
            await self.coordinator.api.async_send_command(
                self._uuid, f"hsv({int(h)},{int(s)},{v})"
            )
        else:
            await self.coordinator.api.async_send_command(self._uuid, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.api.async_send_command(self._uuid, "off")


class LoxoneLightController(LoxoneLightBase):
    """Representation of a Loxone LightController (mood-based)."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator, uuid, control, room_name, cat_name) -> None:
        """Initialize the light controller."""
        super().__init__(coordinator, uuid, control, room_name, cat_name)
        self._active_mood_uuid = control.get("states", {}).get("activeMoods", "")

    @property
    def is_on(self) -> bool:
        """Return True if any mood is active (not mood 778 = all off)."""
        moods_raw = self.coordinator.get_state(self._active_mood_uuid)
        if moods_raw is not None:
            try:
                mood_list = json.loads(moods_raw) if isinstance(moods_raw, str) else moods_raw
                if isinstance(mood_list, list):
                    return mood_list != [778]  # 778 = all off in Loxone
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        # Fallback to active state (simple Light controls)
        state = self.coordinator.get_state(self._state_uuid)
        if state is not None:
            return bool(state) and state != 0
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on (activate default mood)."""
        await self.coordinator.api.async_send_command(self._uuid, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off (all off mood)."""
        await self.coordinator.api.async_send_command(self._uuid, "off")
