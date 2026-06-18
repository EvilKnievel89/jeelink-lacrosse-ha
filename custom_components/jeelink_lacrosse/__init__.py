"""JeeLink LaCrosse Integration.

Setup/Teardown eines Config-Entries: startet den Coordinator (Serial-Reader +
State-Verwaltung) und meldet ihn bei Options-Änderungen für einen sauberen
Reload an. Die Entity-Plattformen (sensor, binary_sensor) kommen in Phase 5 dazu.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import JeeLinkCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry einrichten: Coordinator erstellen und starten."""
    coordinator = JeeLinkCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry entladen: Plattformen entladen und Coordinator stoppen."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: JeeLinkCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_stop()
    return unload_ok


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bei Options-Änderung den Eintrag sauber neu laden (frischer Coordinator)."""
    await hass.config_entries.async_reload(entry.entry_id)
