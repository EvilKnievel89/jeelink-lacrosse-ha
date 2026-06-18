"""Tests für den Repairs-Flow (ID-Neuzuweisung nach Batteriewechsel).

Geprüft werden:
- ein konsolidiertes Issue pro Eintrag (keine Duplikate),
- Issue-Erzeugung mit den richtigen Repairs-Parametern + Reconcile-Löschung,
- der Fix-Flow: Formular -> Auswahl -> coordinator.reassign_id + Issue löschen,
- Abbruch-Pfade (Eintrag nicht geladen, Lage bereits erledigt),
- die Verdrahtung Coordinator -> Repairs inkl. "etabliert"-Filter (kein Issue für
  einmalige Einschalt-/Rauschpakete).

Die HA-/voluptuous-Bausteine kommen aus den conftest-Stubs (bzw. echtem HA).
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.jeelink_lacrosse import _sensor_config as sc
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
        self.unknown_ids: dict[int, dict] = {}
        self._available: dict[str, bool] = {}
        self.candidates: list[int] = []        # was replacement_candidates() liefert
        self.new_candidates: list[int] = []    # was new_sensor_candidates() liefert
        self.reassign_id = AsyncMock()
        self.add_sensor = AsyncMock()

    def is_available(self, slug: str) -> bool:
        return self._available.get(slug, False)

    def replacement_candidates(self) -> list[int]:
        return self.candidates

    def new_sensor_candidates(self) -> list[int]:
        return self.new_candidates

    def candidate_label(self, uid: int) -> str:
        return f"{uid}"


def _hass_with(entry_id: str, coordinator) -> SimpleNamespace:
    """hass-Stub, dessen .data den Coordinator unter (DOMAIN, entry_id) führt."""
    data = {DOMAIN: {entry_id: coordinator}} if coordinator is not None else {DOMAIN: {}}
    return SimpleNamespace(data=data)


def _flow(entry_id, coordinator, issue_id=None):
    issue_id = issue_id or f"id_replacement_{entry_id}"
    flow = repairs.JeeLinkIdReplacementRepairFlow(entry_id, issue_id)
    flow.hass = _hass_with(entry_id, coordinator)
    return flow


def _new_sensor_flow(entry_id, coordinator, issue_id=None):
    issue_id = issue_id or f"new_sensor_{entry_id}"
    flow = repairs.JeeLinkNewSensorRepairFlow(entry_id, issue_id)
    flow.hass = _hass_with(entry_id, coordinator)
    return flow


def _offline_state(name="Badezimmer", lacrosse_id=56, temp=21.5):
    state = SensorState(lacrosse_id, name)
    state.last_seen = 100.0      # schon mal gesehen (>0)
    state.temperature = temp
    return state


def _schema_field_options(result, field):
    """Aus dem (Stub-)Schema die vol.In-Optionen eines Feldes ziehen."""
    schema = result["data_schema"].schema
    for key, val in schema.items():
        if getattr(key, "schema", None) == field:
            return getattr(val, "container", None)
    return None


# --- Issue-ID ----------------------------------------------------------------

def test_issue_id_single_per_entry_and_roundtrip():
    assert repairs._issue_id("e1") == "id_replacement_e1"
    assert repairs._entry_id_from_issue("id_replacement_e1") == "e1"


# --- Issue-Erzeugung / -Löschung ---------------------------------------------

async def test_create_issue_sets_fixable_and_placeholders():
    offline = {"bad": SensorState(56, "Badezimmer"), "kel": SensorState(12, "Keller")}
    with patch.object(repairs.ir, "async_create_issue") as create:
        await repairs.async_create_id_replacement_issue(MagicMock(), "e1", offline)

    create.assert_called_once()
    args, kwargs = create.call_args
    assert args[1] == DOMAIN
    assert args[2] == "id_replacement_e1"
    assert kwargs["is_fixable"] is True
    assert kwargs["translation_key"] == "id_replacement"
    # Offline-Namen sortiert in den Platzhalter
    assert kwargs["translation_placeholders"]["offline_sensors"] == "Badezimmer, Keller"
    assert kwargs["data"] == {"entry_id": "e1", "issue_id": "id_replacement_e1"}


def test_delete_issue_uses_single_id():
    with patch.object(repairs.ir, "async_delete_issue") as delete:
        repairs.async_delete_id_replacement_issue(MagicMock(), "e1")
    delete.assert_called_once()
    assert delete.call_args[0][2] == "id_replacement_e1"


# --- Fix-Flow-Erzeugung ------------------------------------------------------

async def test_create_fix_flow_uses_data():
    flow = await repairs.async_create_fix_flow(
        MagicMock(), "id_replacement_e1",
        {"entry_id": "e1", "issue_id": "id_replacement_e1"},
    )
    assert isinstance(flow, repairs.JeeLinkIdReplacementRepairFlow)
    assert flow._entry_id == "e1"


async def test_create_fix_flow_falls_back_to_issue_id_without_data():
    flow = await repairs.async_create_fix_flow(MagicMock(), "id_replacement_e1", None)
    assert flow._entry_id == "e1"


# --- Fix-Flow-Verhalten ------------------------------------------------------

async def test_flow_shows_form_then_reassigns_on_submit():
    coord = FakeCoordinator()
    coord.sensors = {"bad": _offline_state()}
    coord._available = {"bad": False}
    coord.candidates = [99]
    flow = _flow("e1", coord)

    # 1) Einstieg -> Formular mit dem Offline-Sensor und dem Kandidaten
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["offline_sensors"].startswith("Badezimmer")
    assert set(_schema_field_options(result, "new_id")) == {"99"}

    # 2) Auswahl absenden -> reassign_id + Issue löschen + Flow beenden
    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result2 = await flow.async_step_confirm({"sensor": "bad", "new_id": "99"})

    coord.reassign_id.assert_awaited_once_with("bad", 99)
    delete.assert_called_once_with(flow.hass, DOMAIN, "id_replacement_e1")
    assert result2["type"] == "create_entry"


async def test_flow_offers_only_replacement_candidates():
    coord = FakeCoordinator()
    coord.sensors = {"bad": _offline_state()}
    coord._available = {"bad": False}
    # Fremd-IDs sind bekannt, aber nur die gefilterten Kandidaten werden angeboten
    coord.unknown_ids = {1: {}, 16: {}, 99: {}, 100: {}}
    coord.candidates = [99, 100]
    flow = _flow("e1", coord)

    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert set(_schema_field_options(result, "new_id")) == {"99", "100"}


async def test_flow_aborts_when_entry_not_loaded():
    flow = _flow("e1", coordinator=None)
    result = await flow.async_step_init()
    assert result["type"] == "abort"
    assert result["reason"] == "entry_not_loaded"


async def test_flow_resolves_and_deletes_issue_when_nothing_to_do():
    coord = FakeCoordinator()
    coord.sensors = {"bad": _offline_state()}
    coord._available = {"bad": True}      # wieder online -> nichts zu tun
    coord.candidates = []
    flow = _flow("e1", coord)

    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "already_resolved"
    delete.assert_called_once_with(flow.hass, DOMAIN, "id_replacement_e1")


# --- Coordinator -> Repairs (Verdrahtung) ------------------------------------

def _coord_entry():
    entry = MagicMock(name="config_entry")
    entry.entry_id = "abc123"
    entry.data = {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
    entry.options = {CONF_SENSORS: {"bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"}}}
    return entry


def _offline_coord():
    coord = JeeLinkCoordinator(MagicMock(), _coord_entry())
    state = SensorState(56, "Bad")
    state.last_seen = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60 + 100)
    coord.sensors = {"bad": state}
    return coord


def _online_coord():
    """Coordinator mit einem aktuell online Sensor (keine Offline-Lage)."""
    coord = JeeLinkCoordinator(MagicMock(), _coord_entry())
    state = SensorState(56, "Bad")
    state.last_seen = time.time()
    coord.sensors = {"bad": state}
    return coord


async def test_offline_plus_established_candidate_creates_issue():
    """Offline-Sensor + mehrfach empfangene neue ID -> Coordinator legt Issue an."""
    coord = _offline_coord()
    now = time.time()
    coord.unknown_ids = {
        88: {"first_seen": now, "last_seen": now, "count": 3, "temperature": 22.0}
    }
    with patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()) as create, \
         patch.object(repairs, "async_delete_id_replacement_issue") as delete:
        await coord._async_check_offline_sensors()

    create.assert_awaited_once()
    args, _ = create.call_args
    assert args[1] == coord.entry.entry_id
    assert "bad" in args[2]
    delete.assert_not_called()


async def test_oneshot_unknown_id_is_not_a_candidate():
    """Einmal empfangene ID (Einschalt-/Rauschburst) -> kein Issue, Reconcile löscht."""
    coord = _offline_coord()
    now = time.time()
    coord.unknown_ids = {
        88: {"first_seen": now, "last_seen": now, "count": 1, "temperature": -33.1}
    }
    with patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()) as create, \
         patch.object(repairs, "async_delete_id_replacement_issue") as delete:
        await coord._async_check_offline_sensors()

    create.assert_not_awaited()
    delete.assert_called_once()


async def test_no_issue_when_no_unknown_ids():
    """Offline-Sensor, aber keine unbekannte ID -> kein Issue."""
    coord = _offline_coord()
    coord.unknown_ids = {}
    with patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()) as create, \
         patch.object(repairs, "async_delete_id_replacement_issue"):
        await coord._async_check_offline_sensors()

    create.assert_not_awaited()


# --- "Neuer Sensor"-Issue: Erzeugung / Löschung ------------------------------

async def test_create_new_sensor_issue_sets_fixable_and_placeholders():
    with patch.object(repairs.ir, "async_create_issue") as create:
        await repairs.async_create_new_sensor_issue(MagicMock(), "e1", [77, 88])

    create.assert_called_once()
    args, kwargs = create.call_args
    assert args[1] == DOMAIN
    assert args[2] == "new_sensor_e1"
    assert kwargs["is_fixable"] is True
    assert kwargs["translation_key"] == "new_sensor"
    assert kwargs["translation_placeholders"]["new_ids"] == "77, 88"
    assert kwargs["data"] == {"entry_id": "e1", "issue_id": "new_sensor_e1"}


def test_delete_new_sensor_issue_uses_single_id():
    with patch.object(repairs.ir, "async_delete_issue") as delete:
        repairs.async_delete_new_sensor_issue(MagicMock(), "e1")
    delete.assert_called_once()
    assert delete.call_args[0][2] == "new_sensor_e1"


# --- Fix-Flow-Dispatch (Replacement vs. Neuer Sensor) ------------------------

async def test_create_fix_flow_dispatches_new_sensor():
    flow = await repairs.async_create_fix_flow(
        MagicMock(), "new_sensor_e1",
        {"entry_id": "e1", "issue_id": "new_sensor_e1"},
    )
    assert isinstance(flow, repairs.JeeLinkNewSensorRepairFlow)
    assert flow._entry_id == "e1"


async def test_create_fix_flow_new_sensor_falls_back_to_issue_id():
    flow = await repairs.async_create_fix_flow(MagicMock(), "new_sensor_e1", None)
    assert isinstance(flow, repairs.JeeLinkNewSensorRepairFlow)
    assert flow._entry_id == "e1"


# --- "Neuer Sensor"-Fix-Flow-Verhalten ---------------------------------------

async def test_new_sensor_flow_shows_form_then_adds_on_submit():
    coord = FakeCoordinator()
    coord.new_candidates = [77]
    flow = _new_sensor_flow("e1", coord)

    # 1) Einstieg -> Formular mit dem erkannten Kandidaten + Namensfeld
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert set(_schema_field_options(result, "new_id")) == {"77"}
    assert result["description_placeholders"]["new_ids"] == "77"

    # 2) Absenden -> coordinator.add_sensor + Issue löschen + Flow beenden
    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result2 = await flow.async_step_confirm(
            {"new_id": "77", "friendly_name": "Wohnzimmer"}
        )

    coord.add_sensor.assert_awaited_once_with(77, "Wohnzimmer")
    delete.assert_called_once_with(flow.hass, DOMAIN, "new_sensor_e1")
    assert result2["type"] == "create_entry"


async def test_new_sensor_flow_shows_error_on_duplicate_id():
    coord = FakeCoordinator()
    coord.new_candidates = [77]
    coord.add_sensor = AsyncMock(side_effect=sc.DuplicateIdError())
    flow = _new_sensor_flow("e1", coord)

    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result = await flow.async_step_confirm(
            {"new_id": "77", "friendly_name": "Wohnzimmer"}
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "id_in_use"
    delete.assert_not_called()


async def test_new_sensor_flow_aborts_when_entry_not_loaded():
    flow = _new_sensor_flow("e1", coordinator=None)
    result = await flow.async_step_init()
    assert result["type"] == "abort"
    assert result["reason"] == "entry_not_loaded"


async def test_new_sensor_flow_resolves_when_nothing_to_do():
    coord = FakeCoordinator()
    coord.new_candidates = []      # nichts mehr offen
    flow = _new_sensor_flow("e1", coord)

    with patch.object(repairs.ir, "async_delete_issue") as delete:
        result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "already_resolved"
    delete.assert_called_once_with(flow.hass, DOMAIN, "new_sensor_e1")


# --- Coordinator: new_sensor_candidates + Verdrahtung ------------------------

def test_new_sensor_candidates_excludes_replacement_candidates():
    """Eine Batteriewechsel-ID erscheint nur als Ersatz, nicht als 'neuer Sensor'."""
    coord = _offline_coord()
    now = time.time()
    earliest_offline = coord.sensors["bad"].last_seen
    coord.unknown_ids = {
        # nach dem Offline-Gehen aufgetaucht -> Batteriewechsel-Kandidat:
        88: {"first_seen": now, "last_seen": now, "count": 3, "temperature": 22.0},
        # schon vorher vorhanden -> eigenständiger neuer Sensor:
        77: {"first_seen": earliest_offline - 50, "last_seen": now, "count": 3,
             "temperature": 20.0},
    }
    assert coord.replacement_candidates() == [88]
    assert coord.new_sensor_candidates() == [77]


def test_new_sensor_candidates_ignores_oneshot_and_stale():
    coord = _online_coord()
    now = time.time()
    stale = now - (OFFLINE_THRESHOLD_MINUTES * 60 + 100)
    coord.unknown_ids = {
        10: {"first_seen": now, "last_seen": now, "count": 1, "temperature": 5.0},   # Einmal-Paket
        20: {"first_seen": now, "last_seen": stale, "count": 5, "temperature": 5.0}, # verstummt
        30: {"first_seen": now, "last_seen": now, "count": 3, "temperature": 5.0},   # gültig
    }
    assert coord.new_sensor_candidates() == [30]


async def test_check_creates_new_sensor_issue_for_established_id():
    """Mehrfach empfangene unbekannte ID ohne Offline-Bezug -> new_sensor-Issue."""
    coord = _online_coord()
    now = time.time()
    coord.unknown_ids = {
        77: {"first_seen": now, "last_seen": now, "count": 3, "temperature": 20.0}
    }
    with patch.object(repairs, "async_create_new_sensor_issue", new=AsyncMock()) as create_new, \
         patch.object(repairs, "async_delete_new_sensor_issue") as delete_new, \
         patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()), \
         patch.object(repairs, "async_delete_id_replacement_issue"):
        await coord._async_check_offline_sensors()

    create_new.assert_awaited_once()
    args, _ = create_new.call_args
    assert args[1] == coord.entry.entry_id
    assert args[2] == [77]
    delete_new.assert_not_called()


async def test_check_deletes_new_sensor_issue_when_none():
    """Keine etablierten unbekannten IDs -> Reconcile zieht das Issue zurück."""
    coord = _online_coord()
    coord.unknown_ids = {}
    with patch.object(repairs, "async_create_new_sensor_issue", new=AsyncMock()) as create_new, \
         patch.object(repairs, "async_delete_new_sensor_issue") as delete_new, \
         patch.object(repairs, "async_create_id_replacement_issue", new=AsyncMock()), \
         patch.object(repairs, "async_delete_id_replacement_issue"):
        await coord._async_check_offline_sensors()

    create_new.assert_not_awaited()
    delete_new.assert_called_once()
