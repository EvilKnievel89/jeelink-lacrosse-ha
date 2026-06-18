"""Verhaltens-Tests für den Coordinator.

Der volle Home-Assistant-Core lässt sich in dieser Umgebung nicht installieren
(C-Extension-Abhängigkeit ohne Compiler). Die wenigen HA-Symbole, die der
Coordinator nutzt, werden daher durch schlanke Stubs ersetzt. Damit lässt sich
die *Logik* des Coordinators echt prüfen – insbesondere die ggü. v2 korrigierten
Fehler (unregister_listener-Crash, Timer-Unsub, Store statt entry.options,
neue/schwache Batterie-Felder).
"""
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

# --- HA-Stubs in sys.modules registrieren, BEVOR coordinator importiert wird ---

_INTERVAL = {}   # zeichnet async_track_time_interval-Aufrufe auf


class _FakeStore:
    """Minimaler Ersatz für homeassistant.helpers.storage.Store."""

    def __init__(self, hass, version, key):
        self.version = version
        self.key = key
        self.preset = None          # von Tests vorab setzbar (simuliert gespeicherte Daten)
        self.saved = []             # async_save-Aufrufe
        self.delay_saved = []       # async_delay_save-Aufrufe

    async def async_load(self):
        return self.preset

    async def async_save(self, data):
        self.saved.append(data)

    def async_delay_save(self, data_func, delay):
        self.delay_saved.append((data_func(), delay))


def _async_track_time_interval(hass, action, interval, **kwargs):
    unsub = MagicMock(name="unsub_interval")
    _INTERVAL["action"] = action
    _INTERVAL["interval"] = interval
    _INTERVAL["unsub"] = unsub
    return unsub


def _install_ha_stubs():
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    config_entries = types.ModuleType("homeassistant.config_entries")
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    storage = types.ModuleType("homeassistant.helpers.storage")

    class HomeAssistant:  # nur als Typname benötigt (PEP 563: Annotations sind Strings)
        ...

    class ConfigEntry:
        ...

    core.HomeAssistant = HomeAssistant
    core.callback = lambda fn: fn            # @callback = no-op-Decorator
    config_entries.ConfigEntry = ConfigEntry
    event.async_track_time_interval = _async_track_time_interval
    storage.Store = _FakeStore

    ha.core = core
    ha.config_entries = config_entries
    ha.helpers = helpers
    helpers.event = event
    helpers.storage = storage

    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.config_entries": config_entries,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.storage": storage,
    })


_install_ha_stubs()

from custom_components.jeelink_lacrosse.coordinator import (  # noqa: E402
    JeeLinkCoordinator,
    SensorState,
)
from custom_components.jeelink_lacrosse.const import (  # noqa: E402
    CONF_DEVICE, CONF_BAUD, CONF_SENSORS, CONF_LACROSSE_ID,
    OFFLINE_THRESHOLD_MINUTES,
)
from custom_components.jeelink_lacrosse.protocol import parse_line  # noqa: E402


# --- Helfer ------------------------------------------------------------------

def _make_entry():
    entry = MagicMock(name="config_entry")
    entry.entry_id = "abc123"
    entry.options = {
        CONF_DEVICE: "/dev/ttyUSB0",
        CONF_BAUD: 57600,
        CONF_SENSORS: {
            "bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Badezimmer"},
        },
    }
    return entry


def _make_hass():
    hass = MagicMock(name="hass")
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _patched_reader():
    """Patcht den Serial-Reader weg, damit kein echter Port geöffnet wird."""
    p = patch("custom_components.jeelink_lacrosse.coordinator.JeeLinkSerialReader")
    MockReader = p.start()
    instance = MockReader.return_value
    instance.async_start = AsyncMock()
    instance.async_stop = AsyncMock()
    return p, MockReader, instance


# --- Tests -------------------------------------------------------------------

async def test_async_start_loads_state_and_stores_unsub():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    coord._store.preset = {"last_seen": {"bad": 111.0}, "unknown_ids": [7]}

    p, MockReader, reader = _patched_reader()
    try:
        await coord.async_start()
    finally:
        p.stop()

    assert "bad" in coord.sensors
    assert coord.sensors["bad"].lacrosse_id == 56
    assert coord.sensors["bad"].last_seen == 111.0     # aus dem Store geladen
    assert coord.unknown_ids == {7}
    reader.async_start.assert_awaited_once()
    # Unsub-Handle wurde gespeichert (Fix ggü. v2)
    assert coord._unsub_interval is _INTERVAL["unsub"]
    MockReader.assert_called_once()


async def test_on_measurement_updates_state_and_battery_fields():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    p, _, _ = _patched_reader()
    try:
        await coord.async_start()

        # HUM=165 -> low_battery True, humidity 37 (maskiert); STATUS=1 -> new_battery False
        m = parse_line("OK 9 56 1 4 156 165")
        assert m is not None
        await coord._on_measurement(m)
    finally:
        p.stop()

    state = coord.sensors["bad"]
    assert state.temperature == 18.0
    assert state.humidity == 37
    assert state.low_battery is True
    assert state.new_battery is False
    assert state.available is True
    assert state.last_seen > 0
    # State wurde debounced in den Store geschrieben (nicht in entry.options!)
    assert coord._store.delay_saved, "async_delay_save sollte aufgerufen worden sein"
    hass.config_entries.async_update_entry.assert_not_called()


async def test_unknown_id_is_tracked():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    p, _, _ = _patched_reader()
    try:
        await coord.async_start()
        m = parse_line("OK 9 99 1 4 156 37")   # ID 99 ist nicht konfiguriert
        await coord._on_measurement(m)
    finally:
        p.stop()

    assert 99 in coord.unknown_ids


async def test_register_and_unregister_listener_no_crash():
    """v2-Bug: unregister_listener rief list.discard() auf -> AttributeError."""
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)

    calls = []
    cb = lambda: calls.append(1)

    coord.register_listener("bad", cb)
    assert coord._listeners["bad"] == [cb]

    # Darf NICHT werfen und muss den Callback entfernen
    coord.unregister_listener("bad", cb)
    assert coord._listeners["bad"] == []

    # Doppeltes/unbekanntes Unregister ist ebenfalls harmlos
    coord.unregister_listener("bad", cb)
    coord.unregister_listener("unbekannt", cb)


async def test_listener_fires_on_measurement():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    p, _, _ = _patched_reader()
    fired = []
    try:
        await coord.async_start()
        coord.register_listener("bad", lambda: fired.append(1))
        await coord._on_measurement(parse_line("OK 9 56 1 4 156 37"))
    finally:
        p.stop()
    assert fired == [1]


async def test_is_available_uses_offline_threshold():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    coord.sensors["bad"] = SensorState(56, "Badezimmer")

    # nie gesehen -> nicht verfügbar
    assert coord.is_available("bad") is False

    # gerade gesehen -> verfügbar
    coord.sensors["bad"].last_seen = time.time()
    assert coord.is_available("bad") is True

    # älter als die Offline-Schwelle -> nicht verfügbar
    coord.sensors["bad"].last_seen = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60 + 10)
    assert coord.is_available("bad") is False


async def test_async_stop_cancels_timer_and_flushes_store():
    hass, entry = _make_hass(), _make_entry()
    coord = JeeLinkCoordinator(hass, entry)
    p, _, reader = _patched_reader()
    try:
        await coord.async_start()
        unsub = coord._unsub_interval
        await coord.async_stop()
    finally:
        p.stop()

    unsub.assert_called_once()                 # Timer abgemeldet (kein Leak/Stacking)
    assert coord._unsub_interval is None
    reader.async_stop.assert_awaited_once()
    assert coord._store.saved, "async_stop sollte den State final flushen (async_save)"
