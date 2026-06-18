"""Gemeinsame Basis für JeeLink-Entities.

Stellt Geräte-Zuordnung, Verfügbarkeit und den Push-Listener-Lifecycle bereit.
Wird von sensor.py und binary_sensor.py über Mehrfachvererbung mit der jeweiligen
Plattform-Entity (SensorEntity / BinarySensorEntity) kombiniert.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator


class JeeLinkEntity:
    """Mixin: Geräte-Info, Verfügbarkeit und Listener-An-/Abmeldung."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: JeeLinkCoordinator, entry, slug: str
    ) -> None:
        self._coordinator = coordinator
        self._slug = slug
        state = coordinator.sensors[slug]
        # Temperatur, Feuchte und Batterie eines Sensors bilden EIN Gerät.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{slug}")},
            name=state.friendly_name,
            manufacturer="LaCrosse",
            model="IT+",
        )

    @property
    def available(self) -> bool:
        return self._coordinator.is_available(self._slug)

    async def async_added_to_hass(self) -> None:
        # Push-Updates: Coordinator ruft bei jeder Messung async_write_ha_state auf.
        self._coordinator.register_listener(self._slug, self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_listener(self._slug, self.async_write_ha_state)
