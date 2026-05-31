"""Cover (Jalousie/Gate/Window) platform for Loxone Bridge."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
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
    """Set up Loxone cover entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "cover":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))

        if control_type == "Jalousie":
            entities.append(LoxoneJalousie(coordinator, uuid, control, room_name, cat_name))
        elif control_type == "Gate":
            entities.append(LoxoneGate(coordinator, uuid, control, room_name, cat_name))
        else:
            entities.append(LoxoneCover(coordinator, uuid, control, room_name, cat_name))

    async_add_entities(entities)
    _LOGGER.debug("Added %d cover entities", len(entities))


class LoxoneCover(CoverEntity):
    """Representation of a basic Loxone cover."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.BLIND

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the cover."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"
        states = control.get("states", {})
        self._position_uuid = states.get("position", uuid)
        self._up_uuid = states.get("up", "")
        self._down_uuid = states.get("down", "")

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model=self._control.get("type", "Cover"),
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
    def supported_features(self) -> CoverEntityFeature:
        """Return supported features."""
        return (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

    @property
    def current_cover_position(self) -> int | None:
        """Return the current cover position (0=closed, 100=open).

        Loxone uses 0=open, 100=closed, so we invert.
        """
        position = self.coordinator.get_state(self._position_uuid)
        if position is not None:
            return 100 - int(float(position) * 100)
        return None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is closed."""
        pos = self.current_cover_position
        if pos is not None:
            return pos == 0
        return None

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is opening."""
        up = self.coordinator.get_state(self._up_uuid)
        return bool(up) and up != 0

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is closing."""
        down = self.coordinator.get_state(self._down_uuid)
        return bool(down) and down != 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self.coordinator.api.async_send_command(self._uuid, "up")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self.coordinator.api.async_send_command(self._uuid, "down")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self.coordinator.api.async_send_command(self._uuid, "stop")

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set cover position (0=closed, 100=open → Loxone inverted)."""
        position = kwargs.get(ATTR_POSITION, 0)
        # Invert: HA 100=open → Loxone 0=open
        loxone_pos = 100 - position
        await self.coordinator.api.async_send_command(
            self._uuid, f"manualPosition/{loxone_pos}"
        )

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        for state_uuid in [self._position_uuid, self._up_uuid, self._down_uuid]:
            if state_uuid:
                self.coordinator.register_entity_callback(
                    state_uuid, self._handle_state_update
                )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()


class LoxoneJalousie(LoxoneCover):
    """Representation of a Loxone Jalousie with tilt support."""

    _attr_device_class = CoverDeviceClass.SHUTTER

    def __init__(self, coordinator, uuid, control, room_name, cat_name) -> None:
        """Initialize the jalousie."""
        super().__init__(coordinator, uuid, control, room_name, cat_name)
        self._shade_uuid = control.get("states", {}).get("shadePosition", "")

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Return supported features including tilt."""
        features = super().supported_features
        if self._shade_uuid:
            features |= (
                CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
            )
        return features

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return the current tilt position."""
        shade = self.coordinator.get_state(self._shade_uuid)
        if shade is not None:
            return 100 - int(float(shade) * 100)
        return None

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the tilt."""
        await self.coordinator.api.async_send_command(self._uuid, "shade/up")

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the tilt."""
        await self.coordinator.api.async_send_command(self._uuid, "shade/down")

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Set tilt position."""
        tilt = kwargs.get(ATTR_TILT_POSITION, 0)
        loxone_tilt = 100 - tilt
        await self.coordinator.api.async_send_command(
            self._uuid, f"manualShadePosition/{loxone_tilt}"
        )

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        await super().async_added_to_hass()
        if self._shade_uuid:
            self.coordinator.register_entity_callback(
                self._shade_uuid, self._handle_state_update
            )


class LoxoneGate(LoxoneCover):
    """Representation of a Loxone gate.

    Unlike Jalousie (0=open, 1=closed), Gate uses 0=closed, 1=open
    which matches the HA convention directly (no inversion needed).
    """

    _attr_device_class = CoverDeviceClass.GATE

    @property
    def current_cover_position(self) -> int | None:
        """Return the current gate position (0=closed, 100=open).

        Loxone Gate: 0=closed, 1=open — same direction as HA, no inversion.
        """
        position = self.coordinator.get_state(self._position_uuid)
        if position is not None:
            return int(float(position) * 100)
        return None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the gate is closed."""
        pos = self.current_cover_position
        if pos is not None:
            return pos == 0
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the gate."""
        await self.coordinator.api.async_send_command(self._uuid, "open")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the gate."""
        await self.coordinator.api.async_send_command(self._uuid, "close")
