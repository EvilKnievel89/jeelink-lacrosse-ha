"""Repairs: ID-Neuzuweisung nach Batteriewechsel.

LaCrosse-IT+-Sensoren würfeln ihre Sende-ID bei jedem Batteriewechsel neu aus.
Fällt ein konfigurierter Sensor offline und taucht danach eine neue, mehrfach
empfangene ID auf, ist das fast immer derselbe Sensor mit frischer Batterie.

Pro Config-Entry gibt es GENAU EIN konsolidiertes Issue (kein Issue je ID –
das würde in dichten Umgebungen mehrere Meldungen erzeugen). Der Coordinator
ermittelt die Kandidaten (siehe ``JeeLinkCoordinator.replacement_candidates``);
dieser Flow lässt den Nutzer den Offline-Sensor und die neue ID wählen – mit
letztem Messwert je Kandidat als Unterscheidungshilfe.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import _sensor_config as sc
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_ID_PREFIX = "id_replacement"
NEW_SENSOR_ISSUE_PREFIX = "new_sensor"

# Schlüssel im Fix-Flow-Formular
CONF_SENSOR = "sensor"
CONF_NEW_ID = "new_id"
FRIENDLY_NAME = sc.FRIENDLY_NAME


def _issue_id(entry_id: str) -> str:
    """Ein konsolidiertes Issue pro Eintrag (idempotent, keine Duplikate)."""
    return f"{ISSUE_ID_PREFIX}_{entry_id}"


def _entry_id_from_issue(issue_id: str) -> str:
    """entry_id zurückgewinnen (Fallback, falls HA kein data mitgibt)."""
    return issue_id[len(ISSUE_ID_PREFIX) + 1:]


def _new_sensor_issue_id(entry_id: str) -> str:
    """Ein konsolidiertes "Neuer Sensor"-Issue pro Eintrag."""
    return f"{NEW_SENSOR_ISSUE_PREFIX}_{entry_id}"


async def async_create_id_replacement_issue(
    hass: HomeAssistant,
    entry_id: str,
    offline: dict[str, Any],
) -> None:
    """Konsolidiertes Repairs-Issue erzeugen/aktualisieren (offline + Kandidaten)."""
    offline_names = ", ".join(
        sorted(state.friendly_name for state in offline.values())
    )
    issue_id = _issue_id(entry_id)

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="id_replacement",
        translation_placeholders={"offline_sensors": offline_names},
        data={"entry_id": entry_id, "issue_id": issue_id},
    )


def async_delete_id_replacement_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Issue zurückziehen, sobald die Lage erledigt ist (Reconcile)."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id))


async def async_create_new_sensor_issue(
    hass: HomeAssistant,
    entry_id: str,
    candidate_ids: list[int],
) -> None:
    """Konsolidiertes Issue für neu erkannte, noch nicht konfigurierte Sensoren.

    Ein Issue pro Eintrag (nicht je ID), das die gefundenen IDs auflistet; der
    Fix-Flow lässt den Nutzer eine ID wählen und benennen (-> als Sensor anlegen).
    """
    issue_id = _new_sensor_issue_id(entry_id)
    new_ids = ", ".join(str(uid) for uid in candidate_ids)

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="new_sensor",
        translation_placeholders={"new_ids": new_ids},
        data={"entry_id": entry_id, "issue_id": issue_id},
    )


def async_delete_new_sensor_issue(hass: HomeAssistant, entry_id: str) -> None:
    """"Neuer Sensor"-Issue zurückziehen, sobald nichts Neues mehr ansteht."""
    ir.async_delete_issue(hass, DOMAIN, _new_sensor_issue_id(entry_id))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Vom Repairs-Framework aufgerufen, um den passenden Fix-Flow zu erzeugen."""
    data = data or {}
    if issue_id.startswith(NEW_SENSOR_ISSUE_PREFIX):
        entry_id = data.get("entry_id") or issue_id[len(NEW_SENSOR_ISSUE_PREFIX) + 1:]
        return JeeLinkNewSensorRepairFlow(entry_id, issue_id)
    entry_id = data.get("entry_id") or _entry_id_from_issue(issue_id)
    return JeeLinkIdReplacementRepairFlow(entry_id, issue_id)


class JeeLinkIdReplacementRepairFlow(RepairsFlow):
    """Fix-Flow: eine neu erkannte ID einem bestehenden (Offline-)Sensor zuweisen."""

    def __init__(self, entry_id: str | None, issue_id: str) -> None:
        self._entry_id = entry_id
        self._issue_id = issue_id

    def _coordinator(self):
        """Aktuellen Coordinator zum Eintrag holen (oder None, wenn entladen)."""
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")

        # Auswahl frisch aus dem Live-Zustand ableiten – die Lage kann sich seit der
        # Issue-Erstellung geändert haben.
        offline = {
            slug: state
            for slug, state in coordinator.sensors.items()
            if not coordinator.is_available(slug) and state.last_seen > 0
        }
        candidate_ids = coordinator.replacement_candidates()

        if not offline or not candidate_ids:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_abort(reason="already_resolved")

        if user_input is not None:
            slug = user_input[CONF_SENSOR]
            new_id = int(user_input[CONF_NEW_ID])
            await coordinator.reassign_id(slug, new_id)
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        sensor_options = {
            slug: self._sensor_label(state) for slug, state in offline.items()
        }
        id_options = {str(uid): coordinator.candidate_label(uid) for uid in candidate_ids}

        sensor_field = (
            vol.Required(CONF_SENSOR, default=next(iter(sensor_options)))
            if len(sensor_options) == 1
            else vol.Required(CONF_SENSOR)
        )
        id_field = (
            vol.Required(CONF_NEW_ID, default=next(iter(id_options)))
            if len(id_options) == 1
            else vol.Required(CONF_NEW_ID)
        )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    sensor_field: vol.In(sensor_options),
                    id_field: vol.In(id_options),
                }
            ),
            description_placeholders={
                "offline_sensors": ", ".join(sensor_options.values()),
            },
        )

    @staticmethod
    def _sensor_label(state) -> str:
        """Offline-Sensor mit zuletzt gemessener Temperatur (Matching-Hilfe)."""
        if state.temperature is not None:
            return f"{state.friendly_name} (zuletzt {state.temperature:.1f} °C)"
        return state.friendly_name


class JeeLinkNewSensorRepairFlow(RepairsFlow):
    """Fix-Flow: einen neu erkannten, noch unbekannten Sensor benennen und anlegen."""

    def __init__(self, entry_id: str | None, issue_id: str) -> None:
        self._entry_id = entry_id
        self._issue_id = issue_id

    def _coordinator(self):
        """Aktuellen Coordinator zum Eintrag holen (oder None, wenn entladen)."""
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")

        # Auswahl frisch aus dem Live-Zustand ableiten – die Lage kann sich seit der
        # Issue-Erstellung geändert haben.
        candidate_ids = coordinator.new_sensor_candidates()
        if not candidate_ids:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_abort(reason="already_resolved")

        errors: dict[str, str] = {}
        if user_input is not None:
            new_id = int(user_input[CONF_NEW_ID])
            try:
                await coordinator.add_sensor(new_id, user_input[FRIENDLY_NAME])
            except sc.SensorConfigError as err:
                errors["base"] = err.error_key
            else:
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                return self.async_create_entry(title="", data={})

        id_options = {str(uid): coordinator.candidate_label(uid) for uid in candidate_ids}
        id_field = (
            vol.Required(CONF_NEW_ID, default=next(iter(id_options)))
            if len(id_options) == 1
            else vol.Required(CONF_NEW_ID)
        )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    id_field: vol.In(id_options),
                    vol.Required(FRIENDLY_NAME): str,
                }
            ),
            errors=errors,
            description_placeholders={"new_ids": ", ".join(id_options.values())},
        )
