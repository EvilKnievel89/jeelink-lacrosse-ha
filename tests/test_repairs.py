"""Tests für den Repairs-Flow (ID-Neuzuweisung nach Batteriewechsel).

Geprüft werden:
- deterministische/deduplizierende Issue-IDs (inkl. Rückwärts-Parsing als Fallback),
- Issue-Erzeugung mit den richtigen Repairs-Parametern,
- der Fix-Flow: Formular -> Auswahl -> coordinator.reassign_id + Issue löschen,
- Abbruch-Pfade (Eintrag nicht geladen, Lage bereits erledigt),
- die Verdrahtung Coordinator -> Repairs (Offline-Sensor + unbekannte ID).

Die HA-/voluptuous-Bausteine kommen aus den conftest-Stubs (bzw. echtem HA).
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.jeelink_lacrosse import repairs
from custom_components.jeelink_lacrosse.const import (
    CONF_DEVICE, CONF_BAUD, CONF_SENSORS, CONF_LACROSSE_ID, DOMAIN,
    OFFLINE_THRESHOLD_MINUTES,
)
from custom_components.jeelink_lacrosse.coordinator import (
    JeeLinkCoordinator, SensorState,
)


# --- Test-Doubles ------------------------------------------------------------

class FakeCoordinator:
    """Minimaler Coordinator-Ersatz für die Flow-Tests."""

    def __init__(self) -> None:
        self.sensors: dict[str, SensorState] = {}
        self.unknown_ids: set[int] = set()
        self._available: dict[str, bool] = {}
        self.reassign_id = AsyncMock()

    def is_available(self, slug: str) -> bool:
        return self._available.get(slug, False)


def _hass_with(entry_id: str, coordinator) -> SimpleNamespace:
    """hass-Stub, dessen .data den Coordinator unter (DOMAIN, entry_id) führt."""
    data = {DOMAIN: {entry_id: coordinator}} if coordinator is not None else {DOMAIN: {}}
    return SimpleNamespace(data=data)


def _flow(entry_id, new_id, coordinator, issue_id="id_replacement_e1_99"):
    flow = repairs.JeeLinkIdReplacementRepairFlow(entry_id, new_id, issue_id)
    flow.hass = _hass_with(entry_id, coordinator)
    return flow


# --- Issue-ID ----------------------------------------------------------------

def test_issue_id_is_deterministic_and_id_specific():
    assert repairs._issue_id("e1", 99) == "id_replacement_e1_99"
    assert repairs._issue_id("e1", None) == "id_replacement_e1_scan"
    # Unterschiedliche IDs -> unterschiedliche Issues (kein Überschreiben)
    assert repairs._issue_id("e1", 99) != repairs._issue_id("e1", 100)


@pytest.mark.parametrize("entry_id,new_id", [("e1", 99), ("abc123def", None), ("x", 5)])
def test_parse_issue_id_roundtrip(entry_id, new_id):
    issue_id = repairs._issue_id(entry_id, new_id)
    assert repairs._parse_issue_id(issue_id) == (entry_id, new_id)


# --- Issue-Erzeugung ---------------------------------------------------------

async def test_create_issue_sets_fixable_and_placeholders():
    offline = {"bad": SensorState(56, "Badezimmer"), "kel": SensorState(12, "Keller")}
    with patch.object(repairs.ir, "async_create_issue") as create:
        await repairs.async_create_id_replacement_issue(
            MagicMock(), "e1", 99, offline
        )

    create.assert_called_once()
    args, kwargs = create.call_args
    assert args[1] == DOMAIN
    assert args[2] == "id_replacement_e1_99"
    assert kwargs["is_fixable"] is True
    assert kwargs["translation_key"] == "id_replacement"
    assert kwargs["translation_placeholders"]["new_id"] == "99"
    # Offline-Namen sortiert in den Platzhalter
    assert kwargs["translation_placeholders"]["offline_sensors"] == "Badezimmer, Keller"
    assert kwargs["data"] == {"entry_id": "e1", "new_id": 99, "issue_id": "id_replacement_e1_99"}


async def test_create_issue_scan_uses_dash_placeholder():
    with patch.object(repairs.ir, "async_create_issue") as create:
        await repairs.async_create_id_replacement_issue(
            MagicMock(), "e1", None, {"bad": SensorState(56, "Bad")}
        )
    _, kwargs = create.call_args
    assert kwargs["translation_placeholders"]["new_id"] == "—"
    assert create.call_args[0][2] == "id_replacement_e1_scan"


# --- Fix-Flow-Erzeugung ------------------------------------------------------

async def test_create_fix_flow_uses_data():
    flow = await repairs.async_create_fix_flow(
        MagicMock(), "id_replacement_e1_99",
        {"entry_id": "e1", "new_id": 99, "issue_id": "id_replacement_e1_99"},
    )
    assert isinstance(flow, repairs.JeeLinkIdReplacementRepairFlow)
    assert flow._entry_id == "e1"
    assert flow._new_id == 99


async def test_create_fix_flow_falls_back_to_issue_id_without_data():
    flow = await repairs.async_create_fix_flow(
        MagicMock(), "id_replacement_e1_scan", None
    )
    assert flow._entry_id == "e1"
    assert flow._new_id is None


# --- Fix-Flow-Verhalten ------------------------------------------------------

async def test_flow_shows_form_then_reassigns_on_submit():
    coord = FakeCoordinator()
    coord.sensors = {"bad": SensorState(56, "Badezimmer")}
    coord._available = {"bad": False}     # offline
    flow = _flow("e1", 99, coord)

    # 1) Einstieg -> Formular mit dem Offline-Sensor und der neuen ID
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["offline_sensors"] == "Badezimmer"
    assert result["description_placeholders"]["new_ids"] == "99"

    # 2) Auswahl absenden -> reassign_id + Issue löschen + Flow beenden
    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result2 = await flow.async_step_confirm({"sensor": "bad", "new_id": "99"})

    coord.reassign_id.assert_awaited_once_with("bad", 99)
    delete.assert_called_once_with(flow.hass, DOMAIN, "id_replacement_e1_99")
    assert result2["type"] == "create_entry"


async def test_flow_scan_offers_unknown_ids():
    coord = FakeCoordinator()
    coord.sensors = {"bad": SensorState(56, "Badezimmer")}
    coord._available = {"bad": False}
    coord.unknown_ids = {100, 99}
    flow = _flow("e1", None, coord, issue_id="id_replacement_e1_scan")

    result = await flow.async_step_init()
    assert result["type"] == "form"
    # sortierte unbekannte IDs als Kandidaten
    assert result["description_placeholders"]["new_ids"] == "99, 100"


async def test_flow_aborts_when_entry_not_loaded():
    flow = _flow("e1", 99, coordinator=None)
    result = await flow.async_step_init()
    assert result["type"] == "abort"
    assert result["reason"] == "entry_not_loaded"


async def test_flow_resolves_and_deletes_issue_when_nothing_to_do():
    coord = FakeCoordinator()
    coord.sensors = {"bad": SensorState(56, "Badezimmer")}
    coord._available = {"bad": True}      # wieder online -> nichts zu tun
    flow = _flow("e1", 99, coord)

    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "already_resolved"
    delete.assert_called_once_with(flow.hass, DOMAIN, "id_replacement_e1_99")


# --- Coordinator -> Repairs (Verdrahtung) ------------------------------------

def _coord_entry():
    entry = MagicMock(name="config_entry")
    entry.entry_id = "abc123"
    entry.data = {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
    entry.options = {CONF_SENSORS: {"bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"}}}
    return entry


async def test_offline_sensor_plus_unknown_id_creates_issue():
    """Offline-Sensor + bekannte unbekannte ID -> Coordinator legt Issue an."""
    coord = JeeLinkCoordinator(MagicMock(), _coord_entry())
    state = SensorState(56, "Bad")
    state.last_seen = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60 + 100)
    state.available = True
    coord.sensors = {"bad": state}
    coord.unknown_ids = {99}

    with patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()) as issue:
        await coord._async_check_offline_sensors()

    issue.assert_awaited_once()
    # offline-Mapping enthält den Sensor; new_id ist beim periodischen Scan None
    args, _ = issue.call_args
    assert args[2] is None
    assert "bad" in args[3]


async def test_no_issue_when_no_unknown_ids():
    """Offline-Sensor, aber keine unbekannte ID -> kein Issue."""
    coord = JeeLinkCoordinator(MagicMock(), _coord_entry())
    state = SensorState(56, "Bad")
    state.last_seen = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60 + 100)
    coord.sensors = {"bad": state}
    coord.unknown_ids = set()

    with patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()) as issue:
        await coord._async_check_offline_sensors()

    issue.assert_not_awaited()
