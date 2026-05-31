"""Climate platform for Loxone Bridge (IRoomController)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
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

# Loxone operating modes
LOXONE_MODE_MAP = {
    0: HVACMode.AUTO,      # Automatic
    1: HVACMode.AUTO,      # Comfort timer (automatic)
    2: HVACMode.HEAT,      # Building protection (manual heating)
    3: HVACMode.COOL,      # Manual cooling
    4: HVACMode.OFF,       # Manual off
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone climate entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "climate":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))
        entities.append(
            LoxoneClimate(coordinator, uuid, control, room_name, cat_name)
        )

    async_add_entities(entities)
    _LOGGER.debug("Added %d climate entities", len(entities))


class LoxoneClimate(ClimateEntity):
    """Representation of a Loxone IRoomController."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the climate entity."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"

        states = control.get("states", {})
        self._temp_actual_uuid = states.get("tempActual", "")
        self._temp_target_uuid = states.get("tempTarget", "")
        self._mode_uuid = states.get("mode", states.get("operatingMode", ""))
        self._active_uuid = states.get("active", uuid)

        # Temperature limits from control details
        details = control.get("details", {})
        self._attr_min_temp = details.get("minTemp", 5.0)
        self._attr_max_temp = details.get("maxTemp", 35.0)
        self._attr_target_temperature_step = details.get("step", 0.5)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model=self._control.get("type", "IRoomController"),
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
        # Add Loxone mode number
        mode = self.coordinator.get_state(self._mode_uuid)
        if mode is not None:
            attrs["loxone_mode"] = int(mode)
        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.api.connected

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        temp = self.coordinator.get_state(self._temp_actual_uuid)
        if temp is not None:
            try:
                return round(float(temp), 1)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        temp = self.coordinator.get_state(self._temp_target_uuid)
        if temp is not None:
            try:
                return round(float(temp), 1)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        mode = self.coordinator.get_state(self._mode_uuid)
        if mode is not None:
            return LOXONE_MODE_MAP.get(int(mode), HVACMode.AUTO)
        return HVACMode.AUTO

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        active = self.coordinator.get_state(self._active_uuid)
        if active is not None:
            val = float(active)
            if val > 0:
                return HVACAction.HEATING
            if val < 0:
                return HVACAction.COOLING
            return HVACAction.IDLE
        return None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.api.async_send_command(
                self._uuid, f"setManualTemperature/{temp}"
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        mode_map = {
            HVACMode.AUTO: 0,
            HVACMode.HEAT: 2,
            HVACMode.COOL: 3,
            HVACMode.OFF: 4,
        }
        loxone_mode = mode_map.get(hvac_mode, 0)
        await self.coordinator.api.async_send_command(
            self._uuid, f"setOperatingMode/{loxone_mode}"
        )

    async def async_turn_on(self) -> None:
        """Turn the climate on (Auto mode)."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        """Turn the climate off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_added_to_hass(self) -> None:
        """Register for state updates."""
        for state_uuid in [
            self._temp_actual_uuid,
            self._temp_target_uuid,
            self._mode_uuid,
            self._active_uuid,
        ]:
            if state_uuid:
                self.coordinator.register_entity_callback(
                    state_uuid, self._handle_state_update
                )

    @callback
    def _handle_state_update(self) -> None:
        """Handle a push state update."""
        self.async_write_ha_state()
