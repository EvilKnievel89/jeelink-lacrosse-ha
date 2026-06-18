"""Options Flow: Sensoren nachträglich hinzufügen, bearbeiten, entfernen.

Schreibt das Sensor-Mapping nach ``entry.options[CONF_SENSORS]``. Eine
Options-Änderung lädt den Eintrag über den Update-Listener in __init__.py neu
(frischer Coordinator mit den neuen Sensoren).

Die HA-freien Transformationen liegen in :mod:`_sensor_config`; hier nur Formulare,
Fehleranzeige und der (optionale) Zugriff auf den Coordinator, um beim Hinzufügen
die zuletzt empfangenen, noch unbekannten IDs als Hilfestellung anzuzeigen.

config_entry wird seit HA 2024.11 vom Flow-Manager bereitgestellt (Property), daher
KEIN ``__init__``, das es selbst setzt (das ist inzwischen nicht mehr erlaubt).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow
from homeassistant.helpers import selector

from . import _sensor_config as sc
from .const import CONF_LACROSSE_ID, CONF_SENSORS, DOMAIN

_LOGGER = logging.getLogger(__name__)

FRIENDLY_NAME = sc.FRIENDLY_NAME
CONF_SENSOR = "sensor"

# LaCrosse-IT+-Sende-ID passt in ein Byte (Validierung in _sensor_config)
_ID_FIELD = vol.All(vol.Coerce(int), vol.Range(min=sc.ID_MIN, max=sc.ID_MAX))


class JeeLinkOptionsFlow(OptionsFlow):
    """Verwaltung der konfigurierten LaCrosse-Sensoren."""

    _edit_slug: str | None = None

    # --- Menü ---------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        menu = ["add_sensor"]
        if self._sensors():
            menu += ["edit_sensor", "remove_sensor"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    # --- Hinzufügen ---------------------------------------------------------

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        errors: dict[str, str] = {}
        if user_input is not None:
            lacrosse_id = sc.parse_id(user_input[CONF_LACROSSE_ID])
            if lacrosse_id is None:
                errors["base"] = "invalid_id"
            else:
                try:
                    new_options = sc.add_sensor(
                        self.config_entry.options,
                        lacrosse_id,
                        user_input[FRIENDLY_NAME],
                    )
                except sc.SensorConfigError as err:
                    errors["base"] = err.error_key
                else:
                    return self.async_create_entry(title="", data=new_options)

        # Dropdown der zuletzt empfangenen, noch unbekannten IDs (mit letzten
        # Messwerten als Auswahlhilfe). custom_value lässt zusätzlich eine ID von
        # Hand eintippen – z. B. bei Ersteinrichtung, bevor der Sensor gehört wurde.
        options = [
            selector.SelectOptionDict(value=value, label=label)
            for value, label in self._unknown_id_options().items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_LACROSSE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(FRIENDLY_NAME): str,
            }
        )
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=schema,
            errors=errors,
        )

    # --- Bearbeiten ---------------------------------------------------------

    async def async_step_edit_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._sensors():
            return await self.async_step_init()
        if user_input is not None:
            self._edit_slug = user_input[CONF_SENSOR]
            return await self.async_step_edit_details()
        schema = vol.Schema(
            {vol.Required(CONF_SENSOR): vol.In(sc.sensor_labels(self.config_entry.options))}
        )
        return self.async_show_form(step_id="edit_sensor", data_schema=schema)

    async def async_step_edit_details(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        slug = self._edit_slug
        sensors = self._sensors()
        if slug not in sensors:
            return await self.async_step_init()

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                new_options = sc.update_sensor(
                    self.config_entry.options,
                    slug,
                    lacrosse_id=user_input[CONF_LACROSSE_ID],
                    friendly_name=user_input[FRIENDLY_NAME],
                )
            except sc.SensorConfigError as err:
                errors["base"] = err.error_key
            else:
                return self.async_create_entry(title="", data=new_options)

        current = sensors[slug]
        schema = vol.Schema(
            {
                vol.Required(CONF_LACROSSE_ID, default=current[CONF_LACROSSE_ID]): _ID_FIELD,
                vol.Required(FRIENDLY_NAME, default=current[FRIENDLY_NAME]): str,
            }
        )
        return self.async_show_form(
            step_id="edit_details",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": current[FRIENDLY_NAME]},
        )

    # --- Entfernen ----------------------------------------------------------

    async def async_step_remove_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._sensors():
            return await self.async_step_init()
        if user_input is not None:
            new_options = sc.remove_sensor(
                self.config_entry.options, user_input[CONF_SENSOR]
            )
            return self.async_create_entry(title="", data=new_options)
        schema = vol.Schema(
            {vol.Required(CONF_SENSOR): vol.In(sc.sensor_labels(self.config_entry.options))}
        )
        return self.async_show_form(step_id="remove_sensor", data_schema=schema)

    # --- Hilfen -------------------------------------------------------------

    def _sensors(self) -> dict:
        return self.config_entry.options.get(CONF_SENSORS, {})

    def _unknown_id_options(self) -> dict[str, str]:
        """``{str(id): Label}`` der gesehenen, noch unbekannten IDs (oder leer)."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return {}
        return coordinator.unknown_id_options()
