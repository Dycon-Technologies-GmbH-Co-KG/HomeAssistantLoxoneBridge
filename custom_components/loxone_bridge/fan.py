"""Fan platform for Loxone Bridge (Ventilation controls)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

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

# Loxone ventilation named levels (ordered low → high)
ORDERED_NAMED_SPEEDS = ["low", "medium", "high", "auto"]

# Loxone ``level`` state value ↔ named speed
_LEVEL_TO_NAME = {1: "low", 2: "medium", 3: "high", 4: "auto"}
_NAME_TO_LEVEL = {v: k for k, v in _LEVEL_TO_NAME.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone fan entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "fan":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))
        entities.append(
            LoxoneVentilation(coordinator, uuid, control, room_name, cat_name)
        )

    async_add_entities(entities)
    _LOGGER.debug("Added %d fan entities", len(entities))


class LoxoneVentilation(FanEntity):
    """Representation of a Loxone Ventilation control.

    Loxone ``level`` state values:
      0 = off
      1 = low    (25 %)
      2 = medium (50 %)
      3 = high   (75 %)
      4 = auto   (100 %)

    Some setups only expose a ``speed`` state whose value already is a
    percentage (0-100).  The entity detects which state is available and
    converts accordingly.
    """

    _attr_has_entity_name = True
    _attr_speed_count = len(ORDERED_NAMED_SPEEDS)
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = ORDERED_NAMED_SPEEDS

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the fan entity."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"

        states = control.get("states", {})
        # Prefer ``level`` (integer 0-4, standard Ventilation state).
        # Fall back to ``speed`` only when ``level`` is absent.
        self._level_uuid = states.get("level", "")
        self._speed_uuid = states.get("speed", "")
        self._use_level = bool(self._level_uuid)
        self._primary_uuid = self._level_uuid or self._speed_uuid or uuid

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model="Ventilation",
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
        raw = self._raw_value
        if raw is not None:
            attrs["loxone_level" if self._use_level else "loxone_speed"] = raw
        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.api.connected

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _raw_value(self) -> float | None:
        """Return the raw numeric value from Loxone."""
        state = self.coordinator.get_state(self._primary_uuid)
        if state is not None:
            try:
                return float(state)
            except (ValueError, TypeError):
                return None
        return None

    def _level_to_percentage(self, level: int) -> int:
        """Convert a Loxone integer level (1-4) to a HA percentage."""
        clamped = max(1, min(level, len(ORDERED_NAMED_SPEEDS)))
        return ordered_list_item_to_percentage(
            ORDERED_NAMED_SPEEDS, ORDERED_NAMED_SPEEDS[clamped - 1]
        )

    # ------------------------------------------------------------------
    # HA state properties
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool | None:
        """Return True if the fan is on."""
        raw = self._raw_value
        if raw is not None:
            return raw > 0
        return None

    @property
    def percentage(self) -> int | None:
        """Return the current speed as a percentage (0-100)."""
        raw = self._raw_value
        if raw is None:
            return None
        if raw == 0:
            return 0
        if self._use_level:
            return self._level_to_percentage(int(raw))
        # ``speed`` state: treat as direct percentage 0-100
        return max(0, min(100, int(raw)))

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode name."""
        raw = self._raw_value
        if raw is None or raw == 0:
            return None
        if self._use_level:
            return _LEVEL_TO_NAME.get(int(raw))
        # Map percentage back to nearest named speed
        try:
            return percentage_to_ordered_list_item(
                ORDERED_NAMED_SPEEDS, max(1, min(100, int(raw)))
            )
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed by percentage."""
        if percentage == 0:
            await self.async_turn_off()
            return
        if self._use_level:
            named = percentage_to_ordered_list_item(ORDERED_NAMED_SPEEDS, percentage)
            loxone_val = _NAME_TO_LEVEL[named]
        else:
            loxone_val = percentage
        await self.coordinator.api.async_send_command(
            self._uuid, str(loxone_val)
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the fan preset mode."""
        if preset_mode not in _NAME_TO_LEVEL:
            return
        if self._use_level:
            await self.coordinator.api.async_send_command(
                self._uuid, str(_NAME_TO_LEVEL[preset_mode])
            )
        else:
            pct = ordered_list_item_to_percentage(ORDERED_NAMED_SPEEDS, preset_mode)
            await self.coordinator.api.async_send_command(
                self._uuid, str(pct)
            )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            # Default: auto
            val = "4" if self._use_level else "100"
            await self.coordinator.api.async_send_command(self._uuid, val)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        await self.coordinator.api.async_send_command(self._uuid, "0")

    # ------------------------------------------------------------------
    # State update wiring
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        self.coordinator.register_entity_callback(
            self._primary_uuid, self._handle_state_update
        )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()
