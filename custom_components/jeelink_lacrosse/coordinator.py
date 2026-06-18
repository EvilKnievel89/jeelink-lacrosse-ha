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
    CONF_SENSORS, CONF_LACROSSE_ID, CONF_OFFLINE_THRESHOLD,
    DEFAULT_OFFLINE_THRESHOLD_MINUTES,
    CHECK_INTERVAL_MINUTES,
)
from .protocol import LaCrosseMeasurement
from .serial_reader import JeeLinkSerialReader

_LOGGER = logging.getLogger(__name__)

# Transienter Laufzeit-State (last_seen, unknown_ids) wird im Store gehalten,
# NICHT in entry.options (das würde über den Update-Listener einen Reload triggern).
STORAGE_VERSION = 1

# Eine neue, unbekannte ID muss MEHRFACH empfangen werden, bevor sie als
# Batteriewechsel-Kandidat zählt. So fallen einmalige Einschalt-/Rauschpakete
# (z. B. Power-on-Bursts fremder Sensoren mit new_batt-Bit) heraus.
MIN_REPLACEMENT_SIGHTINGS = 3


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
        # Offline-/Verfügbarkeits-Schwelle: konfigurierbar je Eintrag, sonst Default.
        # Eine Options-Änderung lädt den Eintrag neu (frischer Coordinator), daher
        # genügt das einmalige Lesen hier.
        self.offline_threshold_minutes: int = entry.options.get(
            CONF_OFFLINE_THRESHOLD, DEFAULT_OFFLINE_THRESHOLD_MINUTES
        )
        self.sensors: dict[str, SensorState] = {}
        # Unbekannte (unkonfigurierte) IDs -> Aufzeichnung je ID:
        #   {"first_seen", "last_seen", "count", "temperature"}.
        # Daraus werden Batteriewechsel-Kandidaten abgeleitet: eine ID gilt nur als
        # Kandidat, wenn sie NACH dem Offline-Gehen eines Sensors auftauchte,
        # MEHRFACH empfangen wurde (kein Einschalt-/Rauschburst) und noch aktiv ist.
        self.unknown_ids: dict[int, dict] = {}
        self._dirty = False                       # ungespeicherte State-Änderungen
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
        # Letzten Stand synchron flushen
        await self._store.async_save(self._data_to_store())
        self._dirty = False

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
            self._dirty = True
        else:
            self._track_unknown(m)

    def _track_unknown(self, m: LaCrosseMeasurement) -> None:
        """Unbekannte ID aufzeichnen: Ersterfassung, Häufigkeit, letzte Messwerte."""
        now = time.time()
        rec = self.unknown_ids.get(m.sensor_id)
        if rec is None:
            self.unknown_ids[m.sensor_id] = {
                "first_seen": now,
                "last_seen": now,
                "count": 1,
                "temperature": m.temperature,
                "humidity": m.humidity,
            }
            _LOGGER.info("Neue unbekannte LaCrosse-ID empfangen: %d", m.sensor_id)
        else:
            rec["last_seen"] = now
            rec["count"] += 1
            rec["temperature"] = m.temperature
            rec["humidity"] = m.humidity
        self._dirty = True

    @callback
    def _data_to_store(self) -> dict:
        """Snapshot des transienten States für den Store."""
        return {
            "last_seen": {slug: s.last_seen for slug, s in self.sensors.items()},
            # JSON-Keys müssen Strings sein -> id als str, beim Laden zurück nach int
            "unknown_ids": {str(uid): rec for uid, rec in self.unknown_ids.items()},
        }

    @staticmethod
    def _load_unknown_ids(raw) -> dict[int, dict]:
        """unknown_ids aus dem Store laden – abwärtskompatibel.

        Akzeptiert das aktuelle Format
        {id: {first_seen,last_seen,count,temperature,humidity}}, das ältere ohne
        ``humidity``, das frühere {id: first_seen} und das ursprüngliche [id, ...].
        """
        def _rec(first_seen=0.0, last_seen=0.0, count=0,
                 temperature=None, humidity=None) -> dict:
            return {
                "first_seen": float(first_seen),
                "last_seen": float(last_seen),
                "count": int(count),
                "temperature": temperature,
                "humidity": humidity,
            }

        if isinstance(raw, dict):
            out: dict[int, dict] = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[int(k)] = _rec(
                        v.get("first_seen", 0.0), v.get("last_seen", 0.0),
                        v.get("count", 0), v.get("temperature"), v.get("humidity"),
                    )
                else:  # altes {id: first_seen}
                    out[int(k)] = _rec(first_seen=v, last_seen=v)
            return out
        if isinstance(raw, list):
            return {int(i): _rec() for i in raw}
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
        return (time.time() - state.last_seen) < self.offline_threshold_minutes * 60

    # --- Offline-Erkennung und ID-Neuzuweisung ------------------------------

    def _offline_sensors(self) -> dict[str, SensorState]:
        """Konfigurierte Sensoren, die schon empfangen wurden, aber jetzt als
        offline gelten (länger als die Schwelle still)."""
        offline_threshold = time.time() - (self.offline_threshold_minutes * 60)
        return {
            slug: state for slug, state in self.sensors.items()
            if state.last_seen > 0 and state.last_seen < offline_threshold
        }

    def replacement_candidates(self) -> list[int]:
        """Plausible neue IDs für einen Batteriewechsel.

        Eine unbekannte ID zählt nur, wenn sie
          1. ERST NACH dem letzten Empfang eines jetzt offline Sensors auftauchte,
          2. MEHRFACH empfangen wurde (kein einmaliger Einschalt-/Rauschburst) und
          3. aktuell noch sendet (nicht selbst schon wieder verstummt).
        So fallen dauerhaft mithörende Fremd-Sensoren UND Einmal-Pakete heraus.
        """
        offline = self._offline_sensors()
        if not offline:
            return []
        earliest_offline = min(state.last_seen for state in offline.values())
        active_after = time.time() - (self.offline_threshold_minutes * 60)
        return sorted(
            uid for uid, rec in self.unknown_ids.items()
            if rec["first_seen"] >= earliest_offline
            and rec["count"] >= MIN_REPLACEMENT_SIGHTINGS
            and rec["last_seen"] >= active_after
        )

    def new_sensor_candidates(self) -> list[int]:
        """Plausibel NEUE (noch nicht konfigurierte) Sensoren – kein Batteriewechsel.

        Eine unbekannte ID zählt, wenn sie
          1. MEHRFACH empfangen wurde (kein einmaliger Einschalt-/Rauschburst) und
          2. aktuell noch sendet,
        aber NICHT bereits als Batteriewechsel-Kandidat eines offline Sensors gilt.
        Letztere laufen über das ``id_replacement``-Issue; der Ausschluss verhindert,
        dass dieselbe ID doppelt (als "neu" UND als "Ersatz") gemeldet wird.
        """
        active_after = time.time() - (self.offline_threshold_minutes * 60)
        replacement = set(self.replacement_candidates())
        return sorted(
            uid for uid, rec in self.unknown_ids.items()
            if uid not in replacement
            and rec["count"] >= MIN_REPLACEMENT_SIGHTINGS
            and rec["last_seen"] >= active_after
        )

    def candidate_label(self, uid: int) -> str:
        """Label eines Kandidaten inkl. letzter Messwerte (zur Unterscheidung mehrerer)."""
        return self._unknown_label(uid, self.unknown_ids.get(uid) or {})

    @staticmethod
    def _unknown_label(uid: int, rec: dict) -> str:
        """ID mit den zuletzt empfangenen Messwerten als Anzeige-/Auswahlhilfe."""
        parts: list[str] = []
        temp = rec.get("temperature")
        if temp is not None:
            parts.append(f"{temp:.1f} °C")
        hum = rec.get("humidity")
        if hum is not None:
            parts.append(f"{hum} %")
        return f"{uid} ({', '.join(parts)})" if parts else f"{uid}"

    def unknown_id_options(self) -> dict[str, str]:
        """``{str(id): Label}`` aller gesehenen unbekannten IDs (nach ID sortiert).

        Dient als Dropdown-Vorauswahl beim Hinzufügen eines Sensors: man wählt eine
        tatsächlich empfangene ID samt letzter Messwerte, statt eine Zahl zu raten.
        """
        return {str(uid): self.candidate_label(uid) for uid in sorted(self.unknown_ids)}

    async def _async_flush_store(self) -> None:
        """State periodisch persistieren. Ein per-Messung-Debounce würde bei
        Dauer-Sendeverkehr ständig zurückgesetzt und nie schreiben."""
        if self._dirty:
            await self._store.async_save(self._data_to_store())
            self._dirty = False

    async def _async_check_offline_sensors(self, _now=None) -> None:
        await self._async_flush_store()

        offline = self._offline_sensors()
        candidates = self.replacement_candidates()

        from .repairs import (
            async_create_id_replacement_issue,
            async_delete_id_replacement_issue,
            async_create_new_sensor_issue,
            async_delete_new_sensor_issue,
        )
        if offline and candidates:
            await async_create_id_replacement_issue(
                self.hass, self.entry.entry_id, offline
            )
        else:
            # Lage erledigt -> ggf. bestehendes Issue zurückziehen (Reconcile).
            async_delete_id_replacement_issue(self.hass, self.entry.entry_id)

        # Neu erkannte, noch nicht konfigurierte Sensoren separat melden (Reconcile).
        new_sensors = self.new_sensor_candidates()
        if new_sensors:
            await async_create_new_sensor_issue(
                self.hass, self.entry.entry_id, new_sensors
            )
        else:
            async_delete_new_sensor_issue(self.hass, self.entry.entry_id)

        for slug, state in self.sensors.items():
            was_available = state.available
            state.available = self.is_available(slug)
            if was_available and not state.available:
                _LOGGER.info(
                    "Sensor '%s' ist offline (keine Daten > %d min)",
                    slug, self.offline_threshold_minutes,
                )
                for cb in self._listeners.get(slug, []):
                    cb()

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
        self._dirty = False

        new_options = copy.deepcopy(dict(self.entry.options))
        if slug in new_options.get(CONF_SENSORS, {}):
            new_options[CONF_SENSORS][slug][CONF_LACROSSE_ID] = new_lacrosse_id

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)

    async def add_sensor(self, lacrosse_id: int, friendly_name: str) -> None:
        """
        Neuen Sensor anlegen (vom Repairs-Flow "Neuer Sensor erkannt" aufgerufen).
        Das ID-Mapping ist echte Konfiguration -> schreibt entry.options (löst über
        den Update-Listener einen Reload aus; selten und gewollt).

        Wirft ``_sensor_config.SensorConfigError`` bei belegter/ungültiger ID oder
        leerem Namen, BEVOR irgendetwas mutiert wird – der Flow zeigt dann den Fehler.
        """
        # Lazy-Import wie bei .repairs: _sensor_config wird sonst über das Paket-
        # __init__ (das den Coordinator importiert) zirkulär gezogen.
        from . import _sensor_config as sc

        new_options = sc.add_sensor(self.entry.options, lacrosse_id, friendly_name)

        _LOGGER.info(
            "Neuen Sensor '%s' (LaCrosse-ID %d) angelegt", friendly_name, lacrosse_id
        )
        self.unknown_ids.pop(lacrosse_id, None)
        await self._store.async_save(self._data_to_store())
        self._dirty = False
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
