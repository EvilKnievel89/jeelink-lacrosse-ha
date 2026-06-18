"""Config Flow für die JeeLink-LaCrosse-Integration (Phase 4).

Geführtes Setup: seriellen Port wählen (Dropdown erkannter Ports oder manuelle
Pfadeingabe), Baudrate festlegen, Verbindung testen und Eintrag anlegen.
Verbindungsdaten landen in entry.data; die Sensor-Zuordnung kommt später über
den Options Flow (Phase 7) in entry.options.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, CONF_DEVICE, CONF_BAUD, DEFAULT_BAUD
from .serial_reader import JeeLinkSerialReader, list_serial_ports

_LOGGER = logging.getLogger(__name__)

# Sentinel im Port-Dropdown für „Pfad manuell eingeben"
MANUAL_PATH = "__manual__"


class JeeLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup-Wizard für einen JeeLink-Stick."""

    VERSION = 1

    def __init__(self) -> None:
        self._baud: int = DEFAULT_BAUD

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erster Schritt: Port-Auswahl (Dropdown) + Baudrate."""
        if user_input is not None:
            device = user_input[CONF_DEVICE]
            self._baud = user_input[CONF_BAUD]
            if device == MANUAL_PATH:
                return await self.async_step_manual()
            return await self._async_validate_and_create(device, self._baud)

        # Verfügbare Ports im Executor ermitteln (blockierende I/O)
        ports = await self.hass.async_add_executor_job(list_serial_ports)
        choices = {**ports, MANUAL_PATH: "Pfad manuell eingeben"}

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): vol.In(choices),
                vol.Required(CONF_BAUD, default=DEFAULT_BAUD): cv.positive_int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manuelle Pfadeingabe (empfohlen: /dev/serial/by-id/...)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            return await self._async_validate_and_create(
                user_input[CONF_DEVICE], user_input[CONF_BAUD]
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): cv.string,
                vol.Required(CONF_BAUD, default=self._baud): cv.positive_int,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    async def _async_validate_and_create(
        self, device: str, baud: int
    ) -> ConfigFlowResult:
        """Doppelte verhindern, Verbindung testen, Eintrag anlegen."""
        await self.async_set_unique_id(device)
        self._abort_if_unique_id_configured()

        if not await JeeLinkSerialReader.test_connection(device, baud):
            # Bei Fehler in den manuellen Schritt mit vorbelegtem Pfad zurück,
            # damit der Nutzer Pfad/Baud korrigieren kann.
            schema = vol.Schema(
                {
                    vol.Required(CONF_DEVICE, default=device): cv.string,
                    vol.Required(CONF_BAUD, default=baud): cv.positive_int,
                }
            )
            return self.async_show_form(
                step_id="manual",
                data_schema=schema,
                errors={"base": "cannot_connect"},
            )

        return self.async_create_entry(
            title=f"JeeLink ({device})",
            data={CONF_DEVICE: device, CONF_BAUD: baud},
        )
