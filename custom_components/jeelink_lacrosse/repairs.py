"""Repairs: ID-Neuzuweisung nach Batteriewechsel.

LaCrosse-IT+-Sensoren würfeln ihre Sende-ID bei jedem Batteriewechsel neu aus.
Fällt ein konfigurierter Sensor offline und taucht zeitnah eine neue, unbekannte
ID auf, ist das fast immer derselbe Sensor mit frischer Batterie. Statt den Sensor
komplett neu einzurichten (und Historie/Entitäten zu verlieren), erzeugt der
Coordinator ein Repairs-Issue; der hier definierte Fix-Flow lässt den Nutzer die
neue ID dem bestehenden Sensor zuweisen.

Vertrag mit dem Coordinator:
- ``async_create_id_replacement_issue(hass, entry_id, new_id, offline)`` legt das
  Issue an (idempotent über eine deterministische Issue-ID).
- Der Fix-Flow ruft am Ende ``coordinator.reassign_id(slug, new_id)`` auf.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_ID_PREFIX = "id_replacement"
_SCAN_SUFFIX = "scan"

# Schlüssel im Fix-Flow-Formular
CONF_SENSOR = "sensor"
CONF_NEW_ID = "new_id"


def _issue_id(entry_id: str, new_id: int | None) -> str:
    """Deterministische, deduplizierende Issue-ID.

    Pro neuer ID ein eigenes Issue; der periodische Scan (new_id=None) teilt sich
    ein gemeinsames Issue. So wird dasselbe Issue bei wiederholter Erkennung nur
    aktualisiert statt dupliziert.
    """
    suffix = _SCAN_SUFFIX if new_id is None else str(new_id)
    return f"{ISSUE_ID_PREFIX}_{entry_id}_{suffix}"


def _parse_issue_id(issue_id: str) -> tuple[str, int | None]:
    """Fallback: entry_id/new_id aus der Issue-ID rekonstruieren.

    Format: ``id_replacement_<entry_id>_<suffix>``. Config-Entry-IDs enthalten
    keine Unterstriche, daher ist das Aufsplitten eindeutig. Wird nur genutzt,
    falls HA dem Fix-Flow kein ``data`` mitgibt (ältere Cores).
    """
    suffix = issue_id.rsplit("_", 1)[-1]
    entry_id = issue_id[len(ISSUE_ID_PREFIX) + 1 : -(len(suffix) + 1)]
    new_id = None if suffix == _SCAN_SUFFIX else int(suffix)
    return entry_id, new_id


async def async_create_id_replacement_issue(
    hass: HomeAssistant,
    entry_id: str,
    new_id: int | None,
    offline: dict[str, Any],
) -> None:
    """Repairs-Issue erzeugen: Offline-Sensor(en) + (neue) unbekannte ID(s).

    ``offline`` ist ein Mapping slug -> SensorState. Es wird nur für die
    Beschreibungstexte verwendet; die tatsächlichen Auswahlmöglichkeiten ermittelt
    der Fix-Flow zur Laufzeit frisch aus dem Coordinator.
    """
    offline_names = ", ".join(
        sorted(state.friendly_name for state in offline.values())
    )
    issue_id = _issue_id(entry_id, new_id)

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="id_replacement",
        translation_placeholders={
            "offline_sensors": offline_names,
            "new_id": "—" if new_id is None else str(new_id),
        },
        data={"entry_id": entry_id, "new_id": new_id, "issue_id": issue_id},
    )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Vom Repairs-Framework aufgerufen, um den Fix-Flow zu erzeugen."""
    data = data or {}
    entry_id = data.get("entry_id")
    new_id = data.get("new_id")
    if entry_id is None:
        entry_id, new_id = _parse_issue_id(issue_id)
    return JeeLinkIdReplacementRepairFlow(entry_id, new_id, issue_id)


class JeeLinkIdReplacementRepairFlow(RepairsFlow):
    """Fix-Flow: eine neu erkannte ID einem bestehenden (Offline-)Sensor zuweisen."""

    def __init__(
        self, entry_id: str | None, new_id: int | None, issue_id: str
    ) -> None:
        self._entry_id = entry_id
        self._new_id = new_id
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
            # Eintrag (noch) nicht geladen -> Issue stehen lassen, später neuer Versuch.
            return self.async_abort(reason="entry_not_loaded")

        # Auswahl frisch aus dem Live-Zustand ableiten – die Lage kann sich seit der
        # Issue-Erstellung geändert haben.
        offline = {
            slug: state
            for slug, state in coordinator.sensors.items()
            if not coordinator.is_available(slug)
        }
        # Scan-Fall: nur plausible neue IDs (nach Offline-Gehen aufgetaucht),
        # nicht jede je gehörte Fremd-ID.
        candidate_ids = (
            [self._new_id]
            if self._new_id is not None
            else coordinator.replacement_candidates()
        )

        if not offline or not candidate_ids:
            # Hat sich erledigt (Sensor wieder online / ID verschwunden).
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_abort(reason="already_resolved")

        if user_input is not None:
            slug = user_input[CONF_SENSOR]
            new_id = int(user_input[CONF_NEW_ID])
            await coordinator.reassign_id(slug, new_id)
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        sensor_options = {
            slug: state.friendly_name for slug, state in offline.items()
        }
        id_options = {str(i): str(i) for i in candidate_ids}

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
                "new_ids": ", ".join(id_options.values()),
            },
        )
