"""Batterie-Status-Entities.

- Batterie schwach: aus HUM-Bit 7 (echte Schwachbatterie-Warnung).
- Neue Batterie: aus STATUS-Bit 7 (kurz nach Batteriewechsel) – als Diagnose.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

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
    """Je konfiguriertem Sensor eine Schwachbatterie- und eine Neu-Batterie-Entity."""
    coordinator: JeeLinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for slug in coordinator.sensors:
        entities.append(JeeLinkLowBatterySensor(coordinator, entry, slug))
        entities.append(JeeLinkNewBatterySensor(coordinator, entry, slug))
    async_add_entities(entities)


class JeeLinkLowBatterySensor(JeeLinkEntity, BinarySensorEntity):
    """Batterie schwach (device_class BATTERY: on = schwach)."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator: JeeLinkCoordinator, entry, slug: str) -> None:
        super().__init__(coordinator, entry, slug)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_low_battery"

    @property
    def is_on(self) -> bool:
        return self._coordinator.sensors[self._slug].low_battery


class JeeLinkNewBatterySensor(JeeLinkEntity, BinarySensorEntity):
    """Frisch eingelegte Batterie (Diagnose) – kurz nach Batteriewechsel gesetzt."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "New battery"

    def __init__(self, coordinator: JeeLinkCoordinator, entry, slug: str) -> None:
        super().__init__(coordinator, entry, slug)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_new_battery"

    @property
    def is_on(self) -> bool:
        return self._coordinator.sensors[self._slug].new_battery
