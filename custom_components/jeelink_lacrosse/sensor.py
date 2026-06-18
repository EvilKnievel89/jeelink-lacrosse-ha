"""Temperatur- und Feuchte-Entities."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator
from .entity import JeeLinkEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Je konfiguriertem Sensor eine Temperatur- und eine Feuchte-Entity anlegen."""
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for slug in coordinator.sensors:
        entities.append(JeeLinkTemperatureSensor(coordinator, entry, slug))
        entities.append(JeeLinkHumiditySensor(coordinator, entry, slug))
    async_add_entities(entities)


class JeeLinkTemperatureSensor(JeeLinkEntity, SensorEntity):
    """Temperatur eines LaCrosse-IT+-Sensors."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: JeeLinkCoordinator, entry, slug: str) -> None:
        super().__init__(coordinator, entry, slug)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_temperature"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.sensors[self._slug].temperature


class JeeLinkHumiditySensor(JeeLinkEntity, SensorEntity):
    """Relative Luftfeuchte eines LaCrosse-IT+-Sensors (None ohne Feuchtesensor)."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: JeeLinkCoordinator, entry, slug: str) -> None:
        super().__init__(coordinator, entry, slug)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_humidity"

    @property
    def native_value(self) -> int | None:
        return self._coordinator.sensors[self._slug].humidity
