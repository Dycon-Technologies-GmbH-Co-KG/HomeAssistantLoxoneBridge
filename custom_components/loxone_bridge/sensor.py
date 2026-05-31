"""Sensor platform for Loxone Bridge."""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfSpeed,
)
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

# Map Loxone analog format hints to HA sensor attributes
UNIT_MAP = {
    "°C": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "°F": (UnitOfTemperature.FAHRENHEIT, SensorDeviceClass.TEMPERATURE),
    "%": (PERCENTAGE, SensorDeviceClass.HUMIDITY),
    "ppm": ("ppm", SensorDeviceClass.CO2),
    "W": (UnitOfPower.WATT, SensorDeviceClass.POWER),
    "kW": (UnitOfPower.KILO_WATT, SensorDeviceClass.POWER),
    "Wh": (UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY),
    "kWh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
    "km/h": (UnitOfSpeed.KILOMETERS_PER_HOUR, SensorDeviceClass.WIND_SPEED),
    "lux": ("lx", SensorDeviceClass.ILLUMINANCE),
    "Lux": ("lx", SensorDeviceClass.ILLUMINANCE),
}

# State names extracted as additional sensor entities from complex controls
# (e.g. IRoomControllerV2).  Maps state_name → (display suffix, format hint).
_EXTRA_SENSOR_STATES: dict[str, tuple[str, str]] = {
    "temperature": ("Temperature", "%.1f °C"),
    "tempActual": ("Temperature", "%.1f °C"),
    "humidity": ("Humidity", "%.1f %"),
    "humidityActual": ("Humidity", "%.1f %"),
    "co2": ("CO2", "%.0f ppm"),
    "co2Actual": ("CO2", "%.0f ppm"),
}

# State names that require runtime validation because Loxone declares
# them in the structure file even when no physical sensor is connected.
# Entities for these states are only created when the coordinator already
# holds a non-zero value (0 ppm CO2 is physically impossible – ambient
# air is ~400 ppm – so 0.0 reliably indicates missing hardware).
_OPTIONAL_HARDWARE_STATES: set[str] = {"co2", "co2Actual"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Loxone sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: LoxoneCoordinator = data["coordinator"]
    api = coordinator.api

    entities = []
    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        if LOXONE_CONTROL_MAP.get(control_type) != "sensor":
            continue

        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))

        if control_type == "TextState":
            entities.append(LoxoneTextSensor(coordinator, uuid, control, room_name, cat_name))
        elif control_type in ("Meter", "EnergyFlowMonitor"):
            # Meter controls expose ``actual`` (power) and ``total`` (energy)
            entities.extend(
                _create_meter_entities(coordinator, uuid, control, room_name, cat_name)
            )
        else:
            entities.append(LoxoneAnalogSensor(coordinator, uuid, control, room_name, cat_name))

    # Discover additional sensor values from complex controls
    # (e.g. temperature, humidity, CO2 from IRoomControllerV2).
    # Only scan control types that actually contain these sub-sensors.
    # Loxone structure files include state keys (like co2Actual) even when
    # no physical sensor is connected – restricting by type prevents ghost entities.
    _COMPLEX_CONTROL_TYPES = {"IRoomController", "IRoomControllerV2"}
    seen_state_uuids: set[str] = {e._state_uuid for e in entities if hasattr(e, "_state_uuid")}

    for uuid, control in api.controls.items():
        control_type = control.get("type", "")
        room_name = api.get_room_name(control.get("room", ""))
        cat_name = api.get_category_name(control.get("cat", ""))
        base_name = control.get("name", "")

        # --- direct states (only from known complex controls) ---
        if control_type in _COMPLEX_CONTROL_TYPES:
            for state_name, state_uuid in control.get("states", {}).items():
                if state_name not in _EXTRA_SENSOR_STATES or state_uuid in seen_state_uuids:
                    continue
                # Skip optional-hardware states (e.g. CO2) when no real
                # sensor is connected.  Loxone always declares these in the
                # structure; the value stays at 0.0 without hardware.
                # 0 ppm CO2 is physically impossible (ambient ~400 ppm).
                if state_name in _OPTIONAL_HARDWARE_STATES:
                    current = coordinator.get_state(state_uuid)
                    if current is None or current == 0.0:
                        _LOGGER.debug(
                            "Skipping ghost %s entity for %s (value=%s)",
                            state_name, base_name, current,
                        )
                        continue
                suffix, fmt = _EXTRA_SENSOR_STATES[state_name]
                sub_control = {
                    "name": f"{base_name} {suffix}",
                    "type": "InfoOnlyAnalog",
                    "states": {"value": state_uuid},
                    "details": {"format": fmt},
                    "room": control.get("room", ""),
                    "cat": control.get("cat", ""),
                }
                entities.append(
                    LoxoneAnalogSensor(coordinator, state_uuid, sub_control, room_name, cat_name)
                )
                seen_state_uuids.add(state_uuid)

        # --- subControls (nested inside IRoomControllerV2 etc.) ---
        for _sub_uuid, sub_ctrl in control.get("subControls", {}).items():
            sub_type = sub_ctrl.get("type", "")
            if sub_type not in ("InfoOnlyAnalog", "InfoOnlyDigital"):
                continue
            sub_state_uuid = sub_ctrl.get("states", {}).get("value", "")
            if not sub_state_uuid or sub_state_uuid in seen_state_uuids:
                continue
            sub_entity_ctrl = {
                "name": f"{base_name} {sub_ctrl.get('name', _sub_uuid)}",
                "type": sub_type,
                "states": {"value": sub_state_uuid},
                "details": sub_ctrl.get("details", {}),
                "room": control.get("room", ""),
                "cat": control.get("cat", ""),
            }
            entities.append(
                LoxoneAnalogSensor(
                    coordinator, sub_state_uuid, sub_entity_ctrl, room_name, cat_name
                )
            )
            seen_state_uuids.add(sub_state_uuid)

    async_add_entities(entities)
    _LOGGER.debug("Added %d sensor entities", len(entities))


def _create_meter_entities(
    coordinator: LoxoneCoordinator,
    uuid: str,
    control: dict[str, Any],
    room_name: str,
    cat_name: str,
) -> list[SensorEntity]:
    """Create sensor entities for a Loxone Meter / EnergyFlowMonitor.

    These controls expose:
      ``actual`` – current power consumption/production (W / kW)
      ``total``  – accumulated energy (Wh / kWh)
    """
    entities: list[SensorEntity] = []
    states = control.get("states", {})
    base_name = control.get("name", uuid)

    if "actual" in states:
        actual_control = {
            "name": f"{base_name}",
            "type": control.get("type", "Meter"),
            "states": {"value": states["actual"]},
            "details": {"format": control.get("details", {}).get("actualFormat", "%.1f W")},
            "room": control.get("room", ""),
            "cat": control.get("cat", ""),
        }
        entities.append(
            LoxoneAnalogSensor(coordinator, f"{uuid}_actual", actual_control, room_name, cat_name)
        )

    if "total" in states:
        total_control = {
            "name": f"{base_name} Total",
            "type": control.get("type", "Meter"),
            "states": {"value": states["total"]},
            "details": {"format": control.get("details", {}).get("totalFormat", "%.1f kWh")},
            "room": control.get("room", ""),
            "cat": control.get("cat", ""),
        }
        entities.append(
            LoxoneAnalogSensor(coordinator, f"{uuid}_total", total_control, room_name, cat_name)
        )

    # Fallback: if no specific sub-states, use ``value`` or the control UUID
    if not entities:
        entities.append(
            LoxoneAnalogSensor(coordinator, uuid, control, room_name, cat_name)
        )

    return entities


class LoxoneAnalogSensor(SensorEntity):
    """Representation of a Loxone analog sensor (InfoOnlyAnalog)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"
        self._state_uuid = control.get("states", {}).get("value", uuid)

        # Detect unit and device class from Loxone format
        fmt = control.get("details", {}).get("format", "")
        self._detect_unit_and_class(fmt, control.get("name", ""))

    def _detect_unit_and_class(self, fmt: str, name: str) -> None:
        """Detect unit and device class from Loxone format string.

        Loxone format strings look like ``%.1f kWh``, ``<v.1> °C``,
        ``%.0f %%`` etc.  We strip the format specifier first, then
        match the remaining suffix against known units (longest first
        to avoid partial hits like 'W' matching inside 'kWh').
        """
        if fmt:
            # Strip Loxone (<v.N>) and printf (%…f/d/s) specifiers
            clean = re.sub(r'<v(\.\d+)?>', '', fmt)
            clean = re.sub(r'%[\d.]*[dfs%]', '', clean)
            clean = clean.strip()

            # Match longest unit key first to prevent partial matches
            for unit_str, (unit, device_class) in sorted(
                UNIT_MAP.items(), key=lambda x: -len(x[0])
            ):
                if clean == unit_str or clean.endswith(unit_str):
                    self._attr_native_unit_of_measurement = unit
                    self._attr_device_class = device_class
                    if device_class == SensorDeviceClass.ENERGY:
                        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
                    return

        # Heuristic based on name
        name_lower = name.lower()
        if "temp" in name_lower:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        elif "humid" in name_lower or "feucht" in name_lower:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.HUMIDITY
        elif "power" in name_lower or "leistung" in name_lower:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
        elif "energy" in name_lower or "energie" in name_lower or "zähler" in name_lower:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif "co2" in name_lower or "kohlendioxid" in name_lower:
            self._attr_native_unit_of_measurement = "ppm"
            self._attr_device_class = SensorDeviceClass.CO2

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model=self._control.get("type", "Sensor"),
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
    def native_value(self) -> float | None:
        """Return the sensor value."""
        state = self.coordinator.get_state(self._state_uuid)
        if state is not None:
            try:
                return round(float(state), 2)
            except (ValueError, TypeError):
                return None
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


class LoxoneTextSensor(SensorEntity):
    """Representation of a Loxone text state sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LoxoneCoordinator,
        uuid: str,
        control: dict[str, Any],
        room_name: str,
        cat_name: str,
    ) -> None:
        """Initialize the text sensor."""
        self.coordinator = coordinator
        self._uuid = uuid
        self._control = control
        self._room_name = room_name
        self._cat_name = cat_name
        self._attr_name = control.get("name", uuid)
        self._attr_unique_id = f"loxone_{uuid}"
        self._state_uuid = control.get("states", {}).get("textAndIcon", uuid)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._uuid)},
            name=self._attr_name,
            manufacturer="Loxone",
            model="TextState",
            suggested_area=self._room_name,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        return {
            ATTR_UUID: self._uuid,
            ATTR_ROOM: self._room_name,
            ATTR_CATEGORY: self._cat_name,
            ATTR_CONTROL_TYPE: "TextState",
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.api.connected

    @property
    def native_value(self) -> str | None:
        """Return the text value."""
        state = self.coordinator.get_state(self._state_uuid)
        if state is not None:
            return str(state)
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
