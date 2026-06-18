"""
JeeLinkCoordinator: Verbindet serial_reader mit HA-Entities.
Verwaltet Sensor-State, last_seen, unbekannte IDs.
"""
from __future__ import annotations
import copy
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_DEVICE, CONF_BAUD, DEFAULT_BAUD,
    CONF_SENSORS, CONF_LACROSSE_ID,
    OFFLINE_THRESHOLD_MINUTES,
    CHECK_INTERVAL_MINUTES,
)
from .protocol import LaCrosseMeasurement
from .serial_reader import JeeLinkSerialReader

_LOGGER = logging.getLogger(__name__)

# Transienter Laufzeit-State (last_seen, unknown_ids) wird im Store gehalten,
# NICHT in entry.options (das würde über den Update-Listener einen Reload triggern).
STORAGE_VERSION = 1
PERSIST_DELAY_SECONDS = 300   # debounced save


class SensorState:
    """Aktueller Zustand eines einzelnen Sensors im Speicher."""

    def __init__(self, lacrosse_id: int, friendly_name: str) -> None:
        self.lacrosse_id = lacrosse_id
        self.friendly_name = friendly_name
        self.temperature: float | None = None
        self.humidity: int | None = None
        self.low_battery: bool = False   # echte Schwachbatterie (HUM-Bit 7)
        self.new_battery: bool = False   # frisch eingelegt (STATUS-Bit 7)
        self.last_seen: float = 0.0
        self.available: bool = False


class JeeLinkCoordinator:
    """Zentrale Datenklasse für die JeeLink-Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.sensors: dict[str, SensorState] = {}
        # Unbekannte (unkonfigurierte) IDs -> Zeitpunkt der Ersterfassung. Der
        # Zeitstempel erlaubt es, beim ID-Replacement nur IDs anzubieten, die ERST
        # NACH dem Offline-Gehen eines Sensors auftauchten (echter Batteriewechsel),
        # statt dauerhaft mithörender Fremd-Sensoren.
        self.unknown_ids: dict[int, float] = {}
        self._reader: JeeLinkSerialReader | None = None
        self._listeners: dict[str, list[callable]] = {}
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsub_interval: callable | None = None

    # --- Start / Stop -------------------------------------------------------

    async def async_start(self) -> None:
        """Integration initialisieren und Serial-Reader starten."""
        options = self.entry.options

        # Transienten State aus dem Store laden
        stored = await self._store.async_load() or {}
        last_seen_map: dict[str, float] = stored.get("last_seen", {})
        self.unknown_ids = self._load_unknown_ids(stored.get("unknown_ids"))

        # Sensoren aus der (User-)Konfiguration aufbauen
        for slug, cfg in options.get(CONF_SENSORS, {}).items():
            state = SensorState(
                lacrosse_id=cfg[CONF_LACROSSE_ID],
                friendly_name=cfg["friendly_name"],
            )
            state.last_seen = last_seen_map.get(slug, 0.0)
            self.sensors[slug] = state

        # Bereits konfigurierte IDs nicht weiter als "unbekannt" führen. Sonst
        # bleibt die ID nach dem Anlegen über den Options-Flow in unknown_ids
        # hängen (falscher Hinweis im "Sensor hinzufügen"-Dialog, unnötige
        # Replacement-Erkennung). Greift bei jedem (Re-)Start, also genau dann,
        # wenn eine Options-Änderung wirksam wird.
        configured_ids = {state.lacrosse_id for state in self.sensors.values()}
        stale_unknown = configured_ids & set(self.unknown_ids)
        if stale_unknown:
            for cid in stale_unknown:
                self.unknown_ids.pop(cid, None)
            _LOGGER.debug(
                "Konfigurierte IDs aus unknown_ids entfernt: %s", sorted(stale_unknown)
            )

        # Verbindungsdaten stehen in entry.data (Config Flow), Sensoren in entry.options
        self._reader = JeeLinkSerialReader(
            device=self.entry.data[CONF_DEVICE],
            baud=self.entry.data.get(CONF_BAUD, DEFAULT_BAUD),
            on_measurement=self._on_measurement,
        )
        await self._reader.async_start()

        # Periodische Prüfung – Unsub-Handle MERKEN (Fix ggü. v2)
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_check_offline_sensors,
            timedelta(minutes=CHECK_INTERVAL_MINUTES),
        )

        _LOGGER.info(
            "JeeLink Coordinator gestartet. %d Sensoren geladen.", len(self.sensors)
        )

    async def async_stop(self) -> None:
        """Reader/Timer stoppen und State final persistieren."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._reader:
            await self._reader.async_stop()
        # Letzten Stand synchron flushen (Store-Delay-Save würde sonst verloren gehen)
        await self._store.async_save(self._data_to_store())

    # Hinweis: Ein Reload bei Options-Änderung wird in __init__.py via
    # config_entries.async_reload ausgelöst (frischer Coordinator). Eine eigene
    # async_options_updated-Methode (manuelles stop/start) entfällt damit bewusst.

    # --- Measurement-Callback ----------------------------------------------

    async def _on_measurement(self, m: LaCrosseMeasurement) -> None:
        """Verarbeitet eine eingehende Messung."""
        slug = self._slug_for_id(m.sensor_id)

        if slug is not None:
            state = self.sensors[slug]
            state.temperature = m.temperature
            state.humidity = m.humidity
            state.low_battery = m.low_battery
            state.new_battery = m.new_battery
            state.last_seen = time.time()
            state.available = True

            for cb in self._listeners.get(slug, []):
                cb()

            # Debounced in den Store schreiben (kein entry.options, kein Reload)
            self._store.async_delay_save(self._data_to_store, PERSIST_DELAY_SECONDS)
        else:
            if m.sensor_id not in self.unknown_ids:
                self.unknown_ids[m.sensor_id] = time.time()   # Ersterfassung merken
                _LOGGER.info("Neue unbekannte LaCrosse-ID empfangen: %d", m.sensor_id)
                self._store.async_delay_save(self._data_to_store, PERSIST_DELAY_SECONDS)
                await self._async_check_for_replacement(m.sensor_id)

    @callback
    def _data_to_store(self) -> dict:
        """Snapshot des transienten States für den Store."""
        return {
            "last_seen": {slug: s.last_seen for slug, s in self.sensors.items()},
            # JSON-Keys müssen Strings sein -> id als str, beim Laden zurück nach int
            "unknown_ids": {str(uid): ts for uid, ts in self.unknown_ids.items()},
        }

    @staticmethod
    def _load_unknown_ids(raw) -> dict[int, float]:
        """unknown_ids aus dem Store laden.

        Neues Format: {id: first_seen}. Abwärtskompatibel zum alten Format
        (reine Liste von IDs) – dort ist die Ersterfassung unbekannt (0.0).
        """
        if isinstance(raw, dict):
            return {int(k): float(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return {int(i): 0.0 for i in raw}
        return {}

    # --- Listener-Registrierung --------------------------------------------

    def register_listener(self, slug: str, callback: callable) -> None:
        self._listeners.setdefault(slug, []).append(callback)

    def unregister_listener(self, slug: str, callback: callable) -> None:
        # Fix ggü. v2: _listeners-Werte sind Listen -> list.remove(), nicht .discard()
        listeners = self._listeners.get(slug)
        if listeners and callback in listeners:
            listeners.remove(callback)

    # --- Hilfsfunktionen ----------------------------------------------------

    def _slug_for_id(self, lacrosse_id: int) -> str | None:
        for slug, state in self.sensors.items():
            if state.lacrosse_id == lacrosse_id:
                return slug
        return None

    def is_available(self, slug: str) -> bool:
        """Verfügbarkeit aus derselben Konstante wie die Offline-Erkennung."""
        state = self.sensors.get(slug)
        if not state or state.last_seen == 0:
            return False
        return (time.time() - state.last_seen) < OFFLINE_THRESHOLD_MINUTES * 60

    # --- Offline-Erkennung und ID-Neuzuweisung ------------------------------

    def _offline_sensors(self) -> dict[str, SensorState]:
        """Konfigurierte Sensoren, die schon empfangen wurden, aber jetzt als
        offline gelten (länger als die Schwelle still)."""
        offline_threshold = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60)
        return {
            slug: state for slug, state in self.sensors.items()
            if state.last_seen > 0 and state.last_seen < offline_threshold
        }

    def replacement_candidates(self) -> list[int]:
        """Plausible neue IDs für einen Batteriewechsel.

        Nur unbekannte IDs, die ERST NACH dem letzten Empfang eines jetzt offline
        Sensors auftauchten. Dadurch werden dauerhaft mithörende Fremd-Sensoren
        (deren ID schon vor dem Offline-Gehen bekannt war) nicht angeboten.
        """
        offline = self._offline_sensors()
        if not offline:
            return []
        earliest_offline = min(state.last_seen for state in offline.values())
        return sorted(
            uid for uid, first_seen in self.unknown_ids.items()
            if first_seen >= earliest_offline
        )

    async def _async_check_offline_sensors(self, _now=None) -> None:
        offline = self._offline_sensors()

        # Issue nur, wenn es plausible neue IDs gibt (nach Offline-Gehen aufgetaucht)
        if offline and self.replacement_candidates():
            from .repairs import async_create_id_replacement_issue
            await async_create_id_replacement_issue(
                self.hass, self.entry.entry_id, None, offline
            )

        for slug, state in self.sensors.items():
            was_available = state.available
            state.available = self.is_available(slug)
            if was_available and not state.available:
                _LOGGER.info(
                    "Sensor '%s' ist offline (keine Daten > %d min)",
                    slug, OFFLINE_THRESHOLD_MINUTES,
                )
                for cb in self._listeners.get(slug, []):
                    cb()

    async def _async_check_for_replacement(self, new_id: int) -> None:
        # new_id ist gerade frisch aufgetaucht -> per se ein Kandidat. Wenn ein
        # Sensor offline ist, ist das fast sicher derselbe nach Batteriewechsel.
        offline = self._offline_sensors()
        if offline:
            from .repairs import async_create_id_replacement_issue
            await async_create_id_replacement_issue(
                self.hass, self.entry.entry_id, new_id, offline
            )

    async def reassign_id(self, slug: str, new_lacrosse_id: int) -> None:
        """
        ID-Neuzuweisung nach Batteriewechsel (vom Repairs-Flow aufgerufen).
        Das ID-Mapping ist echte Konfiguration -> schreibt entry.options.
        Der dadurch ausgelöste Reload ist hier gewollt und selten.
        """
        _LOGGER.info("Weise Sensor '%s' neue LaCrosse-ID %d zu", slug, new_lacrosse_id)

        if slug in self.sensors:
            self.sensors[slug].lacrosse_id = new_lacrosse_id
            self.sensors[slug].last_seen = 0.0
            self.sensors[slug].available = False

        self.unknown_ids.pop(new_lacrosse_id, None)
        await self._store.async_save(self._data_to_store())

        new_options = copy.deepcopy(dict(self.entry.options))
        if slug in new_options.get(CONF_SENSORS, {}):
            new_options[CONF_SENSORS][slug][CONF_LACROSSE_ID] = new_lacrosse_id

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
