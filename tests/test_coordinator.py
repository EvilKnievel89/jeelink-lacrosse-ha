"""Verhaltens-Tests für den Coordinator.

Der Import des Pakets gelingt dank der HA-Import-Stubs aus conftest.py (bzw. dem
echten HA-Core). Das konkrete Laufzeitverhalten wird hier pro Test auf
Coordinator-Modulebene gepatcht (Store, Timer, Serial-Reader) – damit laufen die
Tests sowohl in der Stub-Umgebung als auch in einem echten HA-Dev-Setup gleich.

Geprüft werden u. a. die ggü. v2 korrigierten Fehler: unregister_listener-Crash,
gemerktes Timer-Unsub-Handle, Store statt entry.options, neue/schwache Batterie.
"""
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.jeelink_lacrosse.coordinator import (
    JeeLinkCoordinator,
    SensorState,
)
from custom_components.jeelink_lacrosse.const import (
    CONF_DEVICE, CONF_BAUD, CONF_SENSORS, CONF_LACROSSE_ID,
    OFFLINE_THRESHOLD_MINUTES,
)
from custom_components.jeelink_lacrosse.protocol import parse_line

COORD = "custom_components.jeelink_lacrosse.coordinator"
_INTERVAL = {}   # zeichnet async_track_time_interval-Aufrufe auf


class _FakeStore:
    """Ersatz für homeassistant.helpers.storage.Store."""

    def __init__(self, hass, version, key):
        self.version = version
        self.key = key
        self.preset = None          # von Tests vorab setzbar (gespeicherte Daten)
        self.saved = []             # async_save-Aufrufe
        self.delay_saved = []       # async_delay_save-Aufrufe

    async def async_load(self):
        return self.preset

    async def async_save(self, data):
        self.saved.append(data)

    def async_delay_save(self, data_func, delay):
        self.delay_saved.append((data_func(), delay))


def _fake_interval(hass, action, interval, **kwargs):
    unsub = MagicMock(name="unsub_interval")
    _INTERVAL["action"] = action
    _INTERVAL["interval"] = interval
    _INTERVAL["unsub"] = unsub
    return unsub


@contextlib.contextmanager
def _env():
    """Patcht Store, Timer und Serial-Reader auf Coordinator-Modulebene."""
    with patch(f"{COORD}.Store", _FakeStore), \
         patch(f"{COORD}.async_track_time_interval", _fake_interval), \
         patch(f"{COORD}.JeeLinkSerialReader") as MockReader:
        MockReader.return_value.async_start = AsyncMock()
        MockReader.return_value.async_stop = AsyncMock()
        yield MockReader


def _make_entry():
    entry = MagicMock(name="config_entry")
    entry.entry_id = "abc123"
    entry.data = {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
    entry.options = {
        CONF_SENSORS: {
            "bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Badezimmer"},
        },
    }
    return entry


def _make_hass():
    hass = MagicMock(name="hass")
    hass.config_entries.async_update_entry = MagicMock()
    return hass


# --- Tests -------------------------------------------------------------------

async def test_async_start_loads_state_and_stores_unsub():
    with _env() as MockReader:
        coord = JeeLinkCoordinator(_make_hass(), _make_entry())
        coord._store.preset = {"last_seen": {"bad": 111.0}, "unknown_ids": [7]}
        await coord.async_start()

    assert "bad" in coord.sensors
    assert coord.sensors["bad"].lacrosse_id == 56
    assert coord.sensors["bad"].last_seen == 111.0     # aus dem Store geladen
    assert coord.unknown_ids == {7}
    # Reader wurde mit den Verbindungsdaten aus entry.data erzeugt
    _, kwargs = MockReader.call_args
    assert kwargs["device"] == "/dev/ttyUSB0"
    assert kwargs["baud"] == 57600
    coord._reader.async_start.assert_awaited_once()
    # Unsub-Handle wurde gespeichert (Fix ggü. v2)
    assert coord._unsub_interval is _INTERVAL["unsub"]


async def test_on_measurement_updates_state_and_battery_fields():
    with _env():
        coord = JeeLinkCoordinator(_make_hass(), _make_entry())
        await coord.async_start()
        # HUM=165 -> low_battery True, humidity 37 (maskiert); STATUS=1 -> new_battery False
        m = parse_line("OK 9 56 1 4 156 165")
        assert m is not None
        await coord._on_measurement(m)

    state = coord.sensors["bad"]
    assert state.temperature == 18.0
    assert state.humidity == 37
    assert state.low_battery is True
    assert state.new_battery is False
    assert state.available is True
    assert state.last_seen > 0
    # State wurde debounced in den Store geschrieben (nicht in entry.options!)
    assert coord._store.delay_saved, "async_delay_save sollte aufgerufen worden sein"
    coord.hass.config_entries.async_update_entry.assert_not_called()


async def test_unknown_id_is_tracked():
    with _env():
        coord = JeeLinkCoordinator(_make_hass(), _make_entry())
        await coord.async_start()
        await coord._on_measurement(parse_line("OK 9 99 1 4 156 37"))  # ID 99 unkonfiguriert

    assert 99 in coord.unknown_ids


async def test_register_and_unregister_listener_no_crash():
    """v2-Bug: unregister_listener rief list.discard() auf -> AttributeError."""
    coord = JeeLinkCoordinator(_make_hass(), _make_entry())

    calls = []
    cb = lambda: calls.append(1)

    coord.register_listener("bad", cb)
    assert coord._listeners["bad"] == [cb]

    coord.unregister_listener("bad", cb)         # darf NICHT werfen
    assert coord._listeners["bad"] == []

    # Doppeltes/unbekanntes Unregister ist ebenfalls harmlos
    coord.unregister_listener("bad", cb)
    coord.unregister_listener("unbekannt", cb)


async def test_listener_fires_on_measurement():
    fired = []
    with _env():
        coord = JeeLinkCoordinator(_make_hass(), _make_entry())
        await coord.async_start()
        coord.register_listener("bad", lambda: fired.append(1))
        await coord._on_measurement(parse_line("OK 9 56 1 4 156 37"))
    assert fired == [1]


async def test_is_available_uses_offline_threshold():
    coord = JeeLinkCoordinator(_make_hass(), _make_entry())
    coord.sensors["bad"] = SensorState(56, "Badezimmer")

    assert coord.is_available("bad") is False        # nie gesehen

    coord.sensors["bad"].last_seen = time.time()
    assert coord.is_available("bad") is True         # gerade gesehen

    coord.sensors["bad"].last_seen = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60 + 10)
    assert coord.is_available("bad") is False         # älter als Schwelle


async def test_async_stop_cancels_timer_and_flushes_store():
    with _env():
        coord = JeeLinkCoordinator(_make_hass(), _make_entry())
        await coord.async_start()
        unsub = coord._unsub_interval
        reader = coord._reader
        await coord.async_stop()

    unsub.assert_called_once()                 # Timer abgemeldet (kein Leak/Stacking)
    assert coord._unsub_interval is None
    reader.async_stop.assert_awaited_once()
    assert coord._store.saved, "async_stop sollte den State final flushen (async_save)"
