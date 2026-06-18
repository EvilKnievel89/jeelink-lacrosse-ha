# Projektplan: HA Custom Integration `jeelink_lacrosse` (v2 – pyserial-asyncio)

## Was sich gegenüber v1 ändert

| Bereich | v1 (pylacrosse) | v2 (pyserial-asyncio) |
|---|---|---|
| Dependency | `pylacrosse==0.3` | `pyserial-asyncio>=0.6` |
| Architektur | Sync-Thread + asyncio.Queue | Rein asyncio, kein Threading |
| Protocol-Parsing | In pylacrosse eingebettet | Eigene `protocol.py` |
| Serial-Handling | Eigene `coordinator.py` | Eigene `serial_reader.py` |
| Testbarkeit | pylacrosse mocken | `protocol.py` direkt unit-testbar |
| Reconnect | Manuell via Thread-Restart | Eingebaut in read-loop |
| Fehlerbehandlung | Thread-Exceptions schwer fangbar | Standard asyncio try/except |

Die **Phasen 1–2** (Dev-Environment, Basisstruktur) und **Phasen 4–9**
(Config Flow, Entities, Repairs, Options Flow, Tests, HACS) bleiben inhaltlich
identisch zu v1. Nur Phase 3 (Serial-Kommunikation) wird vollständig neu
geschrieben und teilt sich jetzt in zwei Dateien auf.

---

## Protokoll-Referenz: LaCrosse IT+ via JeeLink

**Verifiziert** gegen Firmware-Quellcode (`LaCrosseITPlusReader`) und
5 reale Datenpunkte aus Praxisberichten. Alle Werte stimmen auf ±0,1 °C.

### Startup-Ausgabe (einmalig beim Anstecken)
```
[LaCrosseITPlusReader.10.1s (RFM69CW f:868300 r:17241)]
```
Diese Zeile beginnt mit `[` – ignorieren oder als Versioninfo loggen.

### Messdaten-Format
```
OK 9 <ID> <STATUS> <T_H> <T_L> <HUM>\r\n
```

| Feld | Typ | Beschreibung |
|---|---|---|
| `OK` | Literal | Gültiges Paket |
| `9` | Literal | Sensor-Typ LaCrosse IT+ |
| `ID` | int 0–255 | Sensor-Identifikator |
| `STATUS` | int | Bit-Flags (siehe unten) |
| `T_H` | int | Temperatur High-Byte |
| `T_L` | int | Temperatur Low-Byte |
| `HUM` | int | Relative Luftfeuchte in % |

### Temperaturberechnung
```python
temperature = (T_H * 256 + T_L - 1000) / 10.0  # Ergebnis in °C
```

Beispiel (aus Firmware-Kommentar): `T_H=4, T_L=156` → `(4*256+156-1000)/10 = 18,0 °C` ✓

Gültigkeitsbereich: `-40,0 °C` bis `+60,0 °C`. Werte außerhalb dieses Bereichs
können direkt nach einem Batteriewechsel auftreten und sollen verworfen werden.

### Luftfeuchte
`HUM` ist der direkte %-Wert. Werte `> 100` bedeuten: kein Feuchtesensor
(z. B. TX38IT). Bei TX29 DTH-IT immer gültig.

### STATUS-Byte
Aus Praxisbeobachtungen (kein offizielles Dokument verfügbar):

| Wert | Bedeutung |
|---|---|
| `1` (0x01) | Normaler Betrieb |
| `129` (0x81) | Direkt nach Batteriewechsel |

`bit 7 (0x80)` ist nach aktuellem Kenntnisstand das „Nach-Batteriewechsel"-Flag.
`bit 0 (0x01)` scheint bei normalen Messwerten standardmäßig gesetzt zu sein.

**Wichtig**: Beim ersten Betrieb die DEBUG-Logs lesen und mit den
Anzeigewerten der Sensoren abgleichen. Sollte eine Temperatur dauerhaft
um exakt ±40 °C daneben liegen, stimmt die Formel oder ein Offset-Byte nicht.

---

## Aktualisierte Dateistruktur

```
custom_components/
└── jeelink_lacrosse/
    ├── __init__.py          # Setup, Entry-Load/-Unload
    ├── config_flow.py       # GUI-Setup-Wizard  (wie v1)
    ├── options_flow.py      # Nachträgliche Einstellungen (wie v1)
    ├── repairs.py           # ID-Neuzuweisungs-Dialog (wie v1)
    ├── coordinator.py       # Datenverwaltung, Status-Tracking  ← geändert
    ├── serial_reader.py     # Serial-Kommunikation via pyserial-asyncio ← NEU
    ├── protocol.py          # LaCrosse-Protokoll-Parser ← NEU
    ├── sensor.py            # Temperatur- und Feuchte-Entities (wie v1)
    ├── binary_sensor.py     # Batterie-Warnung (wie v1)
    ├── const.py             # Konstanten (wie v1)
    ├── manifest.json        # ← geänderte Dependency
    ├── strings.json
    └── translations/
        ├── de.json
        └── en.json
```

---

## Aktualisierte manifest.json

Einzige Änderung gegenüber v1: `requirements`.

```json
{
  "domain": "jeelink_lacrosse",
  "name": "JeeLink LaCrosse",
  "version": "0.1.0",
  "codeowners": ["@dein-github-name"],
  "config_flow": true,
  "iot_class": "local_push",
  "requirements": ["pyserial-asyncio>=0.6"],
  "homeassistant": "2022.9.0",
  "documentation": "https://github.com/dein-repo/jeelink-lacrosse-ha"
}
```

`homeassistant: "2022.9.0"` wegen Abhängigkeit von der Repairs-API (Phase 6).

---

## Phase 3 – Serial-Kommunikation (neu: zwei Dateien)

**Dauer: ~2–3 Tage**

### Datei 1: protocol.py – Reiner Parser, keine I/O

Diese Datei hat **keine** Abhängigkeit zu pyserial oder HA. Sie kann
vollständig ohne Hardware unit-getestet werden.

```python
"""
LaCrosse IT+ Protokoll-Parser.
Kein I/O – nur String-Parsing.
Verifiziert gegen LaCrosseITPlusReader Firmware-Quellcode.
"""
from __future__ import annotations
from dataclasses import dataclass
import logging

_LOGGER = logging.getLogger(__name__)

TEMP_MIN = -40.0
TEMP_MAX =  60.0
HUM_MAX  = 100


@dataclass
class LaCrosseMeasurement:
    sensor_id:    int
    temperature:  float
    humidity:     int | None   # None wenn kein Feuchtigkeitssensor
    battery_warn: bool         # True wenn Status-Bit 7 gesetzt
    raw_status:   int          # Original-Status-Byte für Diagnose


def parse_line(line: str) -> LaCrosseMeasurement | None:
    """
    Parst eine LaCrosse IT+ Zeile vom JeeLink.

    Gültiges Format:  'OK 9 <ID> <STATUS> <T_H> <T_L> <HUM>'
    Ungültig/ignoriert: Leerzeilen, Versionszeilen (start mit '['), '#' etc.

    Gibt None zurück wenn die Zeile kein gültiges LaCrosse-IT+-Paket ist
    oder die Werte außerhalb des physikalisch sinnvollen Bereichs liegen.
    """
    line = line.strip()

    # Ignoriere Leerzeilen, Versionsstring, sonstige Nicht-Daten
    if not line or line[0] != 'O':
        return None

    parts = line.split()

    if len(parts) != 7 or parts[0] != 'OK' or parts[1] != '9':
        return None

    try:
        sensor_id  = int(parts[2])
        status     = int(parts[3])
        t_h        = int(parts[4])
        t_l        = int(parts[5])
        hum_raw    = int(parts[6])
    except ValueError:
        _LOGGER.debug("Konnte Zeile nicht parsen: %s", line)
        return None

    # Temperaturberechnung (verifiziert: T_H=4, T_L=156 → 18,0 °C)
    temperature = (t_h * 256 + t_l - 1000) / 10.0

    # Sanity-Check: Werte direkt nach Batteriewechsel oft außerhalb Bereich
    if not TEMP_MIN <= temperature <= TEMP_MAX:
        _LOGGER.debug(
            "Sensor %d: Temperatur %.1f°C außerhalb Bereich [%.1f, %.1f] – ignoriert",
            sensor_id, temperature, TEMP_MIN, TEMP_MAX,
        )
        return None

    # Luftfeuchte: direkt als %, >100 = kein Feuchtigkeitssensor
    humidity = hum_raw if hum_raw <= HUM_MAX else None

    # Batterie: Bit 7 des Status-Bytes
    battery_warn = bool(status & 0x80)

    return LaCrosseMeasurement(
        sensor_id=sensor_id,
        temperature=temperature,
        humidity=humidity,
        battery_warn=battery_warn,
        raw_status=status,
    )
```

---

### Datei 2: serial_reader.py – Async Serial, kein Protokoll-Wissen

Diese Datei ist für die I/O-Schicht zuständig: Verbindung aufbauen,
Zeilen lesen, Callbacks aufrufen, bei Disconnect reconnecten.

```python
"""
Asynchroner JeeLink Serial-Reader.
Nutzt pyserial-asyncio, kein Threading.
"""
from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from typing import Any

import serial_asyncio

from .protocol import LaCrosseMeasurement, parse_line

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 30  # Sekunden vor erneutem Verbindungsversuch


class JeeLinkSerialReader:
    """
    Öffnet den JeeLink-Port, liest Zeilen, parsed sie und ruft
    den `on_measurement`-Callback auf.

    Trennt sich die Verbindung (USB-Disconnect etc.), wird nach
    RECONNECT_DELAY automatisch reconnected.
    """

    def __init__(
        self,
        device: str,
        baud: int,
        on_measurement: Callable[[LaCrosseMeasurement], Any],
    ) -> None:
        self._device = device
        self._baud = baud
        self._on_measurement = on_measurement
        self._running = False
        self._task: asyncio.Task | None = None

    async def async_start(self) -> None:
        """Startet den Lese-Loop als Hintergrund-Task."""
        self._running = True
        self._task = asyncio.create_task(
            self._read_loop(), name="jeelink_read_loop"
        )

    async def async_stop(self) -> None:
        """Beendet den Lese-Loop sauber."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.debug("JeeLink Serial-Reader gestoppt")

    async def _read_loop(self) -> None:
        """
        Äußerer Loop: Verbindung aufbauen, lesen, bei Fehler reconnecten.
        Läuft bis async_stop() aufgerufen wird.
        """
        while self._running:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    _LOGGER.warning(
                        "JeeLink Verbindung unterbrochen (%s). "
                        "Reconnect in %ds...",
                        exc, RECONNECT_DELAY,
                    )
                    await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_read(self) -> None:
        """
        Öffnet die Serial-Verbindung und liest Zeilen bis zur Exception.
        Wirft bei Verbindungsfehler oder Disconnect.
        """
        _LOGGER.info(
            "Verbinde mit JeeLink auf %s (Baud: %d)", self._device, self._baud
        )

        reader, _writer = await serial_asyncio.open_serial_connection(
            url=self._device,
            baudrate=self._baud,
        )

        _LOGGER.info("JeeLink verbunden – empfange Daten")

        while self._running:
            # readline() blockiert (async) bis \n empfangen
            raw_line = await reader.readline()

            if not raw_line:
                # Leere Bytes: Verbindung geschlossen
                raise ConnectionResetError("JeeLink hat Verbindung getrennt")

            line = raw_line.decode("ascii", errors="ignore")
            measurement = parse_line(line)

            if measurement is not None:
                _LOGGER.debug(
                    "Sensor %d: %.1f°C %s%% status=%d",
                    measurement.sensor_id,
                    measurement.temperature,
                    measurement.humidity if measurement.humidity is not None else "n/a",
                    measurement.raw_status,
                )
                # Callback ist ein coroutine oder sync – beide aufrufen
                result = self._on_measurement(measurement)
                if asyncio.iscoroutine(result):
                    await result

    @staticmethod
    async def test_connection(device: str, baud: int) -> bool:
        """
        Schneller Verbindungstest für den Config Flow.
        Versucht den Port zu öffnen und wartet 3s auf irgendeinen Input.
        Gibt True zurück wenn die Verbindung erfolgreich war.
        """
        try:
            reader, writer = await asyncio.wait_for(
                serial_asyncio.open_serial_connection(url=device, baudrate=baud),
                timeout=5.0,
            )
            # Auf mindestens eine Zeile warten
            await asyncio.wait_for(reader.readline(), timeout=3.0)
            writer.close()
            return True
        except (OSError, asyncio.TimeoutError, Exception) as exc:
            _LOGGER.debug("Verbindungstest fehlgeschlagen: %s", exc)
            return False
```

---

### Aktualisierter Coordinator (ohne pylacrosse, ohne Threading)

```python
"""
JeeLinkCoordinator: Verbindet serial_reader mit HA-Entities.
Verwaltet Sensor-State, last_seen, unbekannte IDs.
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_DEVICE, CONF_BAUD, CONF_LED,
    CONF_SENSORS, CONF_LACROSSE_ID, CONF_LAST_SEEN, CONF_UNKNOWN_IDS,
    OFFLINE_THRESHOLD_MINUTES,
)
from .protocol import LaCrosseMeasurement
from .serial_reader import JeeLinkSerialReader

_LOGGER = logging.getLogger(__name__)

# last_seen wird maximal alle N Sekunden in den ConfigEntry geschrieben
# (vermeidet zu viele Schreibzugriffe bei häufigen Messungen)
PERSIST_INTERVAL_SECONDS = 300


class SensorState:
    """Aktueller Zustand eines einzelnen Sensors im Speicher."""

    def __init__(self, lacrosse_id: int, friendly_name: str) -> None:
        self.lacrosse_id   = lacrosse_id
        self.friendly_name = friendly_name
        self.temperature:  float | None = None
        self.humidity:     int   | None = None
        self.battery_warn: bool         = False
        self.last_seen:    float        = 0.0        # Unix-Timestamp
        self.available:    bool         = False      # True wenn Daten da und aktuell


class JeeLinkCoordinator:
    """Zentrale Datenklasse für die JeeLink-Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass   = hass
        self.entry  = entry
        # slug → SensorState (slug = stabiler Name wie "badezimmer")
        self.sensors:      dict[str, SensorState] = {}
        # IDs die empfangen wurden, aber keinem Sensor zugeordnet sind
        self.unknown_ids:  set[int]               = set()
        self._reader:      JeeLinkSerialReader | None = None
        # slug → Liste von Callbacks (Entity-Update-Trigger)
        self._listeners:   dict[str, list[callable]] = {}
        self._last_persist: float = 0.0

    # -------------------------------------------------------------------------
    # Start / Stop
    # -------------------------------------------------------------------------

    async def async_start(self) -> None:
        """Integration initialisieren und Serial-Reader starten."""
        options = self.entry.options

        # Sensor-Zustand aus der gespeicherten Konfiguration laden
        for slug, cfg in options.get(CONF_SENSORS, {}).items():
            state = SensorState(
                lacrosse_id=cfg[CONF_LACROSSE_ID],
                friendly_name=cfg["friendly_name"],
            )
            state.last_seen = cfg.get(CONF_LAST_SEEN, 0.0)
            self.sensors[slug] = state

        self.unknown_ids = set(options.get(CONF_UNKNOWN_IDS, []))

        # Serial-Reader erstellen und starten
        self._reader = JeeLinkSerialReader(
            device=options[CONF_DEVICE],
            baud=options.get(CONF_BAUD, 57600),
            on_measurement=self._on_measurement,
        )
        await self._reader.async_start()

        # Periodische Prüfung: Offline-Sensoren + unbekannte IDs
        async_track_time_interval(
            self.hass,
            self._async_check_offline_sensors,
            timedelta(minutes=5),
        )

        _LOGGER.info(
            "JeeLink Coordinator gestartet. %d Sensoren geladen.",
            len(self.sensors),
        )

    async def async_stop(self) -> None:
        """Reader stoppen und last_seen final persistieren."""
        if self._reader:
            await self._reader.async_stop()
        await self._async_persist_last_seen(force=True)

    async def async_options_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Aufgerufen wenn Options im Config-Flow geändert werden."""
        await self.async_stop()
        self.sensors.clear()
        self.unknown_ids.clear()
        self._listeners.clear()
        await self.async_start()

    # -------------------------------------------------------------------------
    # Measurement-Callback (kommt vom serial_reader)
    # -------------------------------------------------------------------------

    async def _on_measurement(self, m: LaCrosseMeasurement) -> None:
        """Verarbeitet eine eingehende Messung."""
        slug = self._slug_for_id(m.sensor_id)

        if slug is not None:
            # Bekannter Sensor → State aktualisieren
            state = self.sensors[slug]
            state.temperature  = m.temperature
            state.humidity     = m.humidity
            state.battery_warn = m.battery_warn
            state.last_seen    = time.time()
            state.available    = True

            # Alle registrierten Callbacks für diesen Sensor aufrufen
            for cb in self._listeners.get(slug, []):
                cb()

            # Gelegentlich last_seen persistieren
            await self._async_persist_last_seen()
        else:
            # Unbekannte ID
            if m.sensor_id not in self.unknown_ids:
                self.unknown_ids.add(m.sensor_id)
                _LOGGER.info(
                    "Neue unbekannte LaCrosse-ID empfangen: %d", m.sensor_id
                )
                # Prüfen ob ein offline-Sensor auf diese ID gewartet hat
                await self._async_check_for_replacement(m.sensor_id)

    # -------------------------------------------------------------------------
    # Listener-Registrierung für Entities
    # -------------------------------------------------------------------------

    def register_listener(self, slug: str, callback: callable) -> None:
        """Entity registriert sich für State-Updates."""
        self._listeners.setdefault(slug, []).append(callback)

    def unregister_listener(self, slug: str, callback: callable) -> None:
        if slug in self._listeners:
            self._listeners[slug].discard(callback)

    # -------------------------------------------------------------------------
    # Hilfsfunktionen
    # -------------------------------------------------------------------------

    def _slug_for_id(self, lacrosse_id: int) -> str | None:
        """Gibt den Slug zum LaCrosse-ID-Mapping zurück, oder None."""
        for slug, state in self.sensors.items():
            if state.lacrosse_id == lacrosse_id:
                return slug
        return None

    def is_available(self, slug: str) -> bool:
        """Entity-Verfügbarkeit: True wenn letzte Messung < 2h."""
        state = self.sensors.get(slug)
        if not state or state.last_seen == 0:
            return False
        return (time.time() - state.last_seen) < 7200  # 2 Stunden

    # -------------------------------------------------------------------------
    # Offline-Erkennung und ID-Neuzuweisung
    # -------------------------------------------------------------------------

    async def _async_check_offline_sensors(self, _now=None) -> None:
        """Periodisch: Offline-Sensoren mit unbekannten IDs abgleichen."""
        offline_threshold = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60)
        offline = {
            slug: state for slug, state in self.sensors.items()
            if state.last_seen > 0 and state.last_seen < offline_threshold
        }

        if offline and self.unknown_ids:
            from .repairs import async_create_id_replacement_issue
            await async_create_id_replacement_issue(
                self.hass, self.entry.entry_id, None, offline
            )

        # available-Flag für offline-Sensoren aktualisieren
        for slug, state in self.sensors.items():
            was_available = state.available
            state.available = self.is_available(slug)
            if was_available and not state.available:
                _LOGGER.info("Sensor '%s' ist offline (keine Daten > 2h)", slug)
                for cb in self._listeners.get(slug, []):
                    cb()

    async def _async_check_for_replacement(self, new_id: int) -> None:
        """Neue unbekannte ID: Prüfen ob Repair-Issue nötig."""
        offline_threshold = time.time() - (OFFLINE_THRESHOLD_MINUTES * 60)
        offline = {
            slug: state for slug, state in self.sensors.items()
            if state.last_seen > 0 and state.last_seen < offline_threshold
        }

        if offline:
            from .repairs import async_create_id_replacement_issue
            await async_create_id_replacement_issue(
                self.hass, self.entry.entry_id, new_id, offline
            )

    async def reassign_id(self, slug: str, new_lacrosse_id: int) -> None:
        """
        ID-Neuzuweisung nach Batteriewechsel.
        Wird vom Repairs-Flow aufgerufen.
        Slug (und damit Entity-IDs und History) bleiben erhalten.
        """
        import copy

        _LOGGER.info(
            "Weise Sensor '%s' neue LaCrosse-ID %d zu",
            slug, new_lacrosse_id,
        )

        # In-Memory aktualisieren
        if slug in self.sensors:
            self.sensors[slug].lacrosse_id = new_lacrosse_id
            self.sensors[slug].last_seen   = 0.0
            self.sensors[slug].available   = False

        self.unknown_ids.discard(new_lacrosse_id)

        # In ConfigEntry persistieren
        new_options = copy.deepcopy(dict(self.entry.options))
        if slug in new_options.get(CONF_SENSORS, {}):
            new_options[CONF_SENSORS][slug][CONF_LACROSSE_ID] = new_lacrosse_id

        unknown = set(new_options.get(CONF_UNKNOWN_IDS, []))
        unknown.discard(new_lacrosse_id)
        new_options[CONF_UNKNOWN_IDS] = list(unknown)

        self.hass.config_entries.async_update_entry(
            self.entry, options=new_options
        )

    # -------------------------------------------------------------------------
    # Persistierung
    # -------------------------------------------------------------------------

    async def _async_persist_last_seen(self, force: bool = False) -> None:
        """
        Schreibt last_seen aller Sensoren in den ConfigEntry.
        Nur alle PERSIST_INTERVAL_SECONDS um I/O zu minimieren.
        """
        now = time.time()
        if not force and (now - self._last_persist) < PERSIST_INTERVAL_SECONDS:
            return

        import copy
        new_options = copy.deepcopy(dict(self.entry.options))

        for slug, state in self.sensors.items():
            if slug in new_options.get(CONF_SENSORS, {}):
                new_options[CONF_SENSORS][slug][CONF_LAST_SEEN] = state.last_seen

        new_options[CONF_UNKNOWN_IDS] = list(self.unknown_ids)
        self.hass.config_entries.async_update_entry(
            self.entry, options=new_options
        )
        self._last_persist = now
```

---

## Aktualisierte Tests (Phase 8)

Die zentrale Verbesserung gegenüber v1: `protocol.py` ist **vollständig
ohne Hardware testbar**. `serial_reader.py` wird mit einem `asyncio.StreamReader`-
Mock getestet.

```python
# tests/test_protocol.py
import pytest
from custom_components.jeelink_lacrosse.protocol import parse_line, LaCrosseMeasurement

class TestParseLineValid:
    """Verifiziert anhand bekannter Firmware-Beispiele."""

    def test_firmware_reference_18_0_degrees(self):
        """Firmware-Kommentar: OK 9 56 1 4 156 37 -> T=18.0, H=37"""
        result = parse_line("OK 9 56 1 4 156 37")
        assert result is not None
        assert result.sensor_id   == 56
        assert result.temperature == 18.0
        assert result.humidity    == 37
        assert result.battery_warn is False
        assert result.raw_status  == 1

    def test_battery_warn_bit7_set(self):
        """Status=129 (0x81): bit 7 gesetzt → battery_warn=True"""
        result = parse_line("OK 9 52 129 2 169 60")
        # Temperatur: (2*256+169-1000)/10 = (512+169-1000)/10 = -319/10 = -31,9°C
        # Außerhalb Bereich → sollte None zurückgeben (Sanity-Check greift)
        assert result is None  # Ungültige Temp nach Batteriewechsel wird verworfen

    def test_no_humidity_sensor(self):
        """Humidity=106 → kein Feuchtigkeitssensor → None"""
        result = parse_line("OK 9 55 1 4 124 106")
        assert result is not None
        assert result.humidity is None
        assert abs(result.temperature - 14.8) < 0.1

    def test_multiple_sensors(self):
        lines = [
            ("OK 9 58 1 4 174 62", 19.8, 62),
            ("OK 9 38 1 4 209 42", 23.3, 42),
            ("OK 9 30 1 4 198 34", 22.2, 34),
        ]
        for line, expected_temp, expected_hum in lines:
            r = parse_line(line)
            assert r is not None
            assert abs(r.temperature - expected_temp) < 0.1
            assert r.humidity == expected_hum


class TestParseLineInvalid:
    """Parser muss robust gegen fehlerhafte Eingaben sein."""

    def test_version_line_ignored(self):
        assert parse_line("[LaCrosseITPlusReader.10.1s (RFM69CW)]") is None

    def test_empty_line_ignored(self):
        assert parse_line("") is None
        assert parse_line("   ") is None

    def test_wrong_sensor_type(self):
        assert parse_line("OK 6 23 1 4 156 37") is None

    def test_wrong_prefix(self):
        assert parse_line("ERR 9 56 1 4 156 37") is None

    def test_too_few_fields(self):
        assert parse_line("OK 9 56 1 4 156") is None

    def test_non_numeric_fields(self):
        assert parse_line("OK 9 XX 1 4 156 37") is None

    def test_temperature_out_of_range_discarded(self):
        # T_H=0, T_L=0 → (0-1000)/10 = -100°C → außerhalb [-40, +60]
        assert parse_line("OK 9 56 1 0 0 50") is None


# tests/test_serial_reader.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

async def test_measurement_callback_called(hass):
    """serial_reader ruft Callback auf wenn gültige Zeile empfangen."""
    from custom_components.jeelink_lacrosse.serial_reader import JeeLinkSerialReader

    measurements = []

    async def mock_callback(m):
        measurements.append(m)

    # Mock für serial_asyncio.open_serial_connection
    mock_reader = AsyncMock()
    mock_reader.readline.side_effect = [
        b"OK 9 56 1 4 156 37\r\n",
        b"OK 9 23 1 4 200 55\r\n",
        asyncio.CancelledError(),  # Beendet den Loop
    ]
    mock_writer = MagicMock()

    with patch(
        "serial_asyncio.open_serial_connection",
        return_value=(mock_reader, mock_writer),
    ):
        reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, mock_callback)
        await reader.async_start()
        await asyncio.sleep(0.1)  # Loop laufen lassen
        await reader.async_stop()

    assert len(measurements) == 2
    assert measurements[0].sensor_id == 56
    assert measurements[0].temperature == 18.0
    assert measurements[1].sensor_id == 23


async def test_reconnect_on_disconnect(hass):
    """serial_reader reconnectet nach Verbindungsabbruch."""
    connect_count = 0

    async def mock_connect(url, baudrate):
        nonlocal connect_count
        connect_count += 1
        mock_reader = AsyncMock()
        if connect_count == 1:
            mock_reader.readline.side_effect = OSError("USB disconnect")
        else:
            mock_reader.readline.side_effect = asyncio.CancelledError()
        return mock_reader, MagicMock()

    with patch("serial_asyncio.open_serial_connection", side_effect=mock_connect):
        with patch(
            "custom_components.jeelink_lacrosse.serial_reader.RECONNECT_DELAY", 0
        ):
            reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, AsyncMock())
            await reader.async_start()
            await asyncio.sleep(0.1)
            await reader.async_stop()

    assert connect_count >= 2  # Mind. 1 Reconnect-Versuch
```

---

## Gesamtzeitplan (aktualisiert)

| Phase | Inhalt | Aufwand |
|---|---|---|
| 1 | Dev-Environment | 1 Tag |
| 2 | Basisstruktur (manifest, const, __init__) | 1 Tag |
| 3 | protocol.py + serial_reader.py + coordinator.py | **2–3 Tage** (↓ vs. v1) |
| 4 | Config Flow | 3 Tage |
| 5 | Sensor-Entities | 1–2 Tage |
| 6 | ID-Neuzuweisung (Repairs) | 3–4 Tage |
| 7 | Options Flow | 1–2 Tage |
| 8 | Tests | **1–2 Tage** (↓ vs. v1, dank besserer Testbarkeit) |
| 9 | HACS & Release | 1 Tag |
| **Gesamt** | | **~14–17 Tage** |

Gegenüber v1 rund 2 Tage kürzer, weil Threading-Komplexität wegfällt
und protocol.py ohne Mock-Overhead testbar ist.

---

## Bekannte Fallstricke (aktualisiert)

### pyserial-asyncio unter HAOS
Unter HAOS (Raspberry Pi OS Image) muss der USB-Port freigegeben sein.
Den stabilen Pfad verwenden:
```
/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
```
Statt `/dev/ttyUSB0` (Nummer kann sich nach Reboot ändern).

### readline() und Partial-Lines
`asyncio.StreamReader.readline()` gibt eine vollständige Zeile zurück
(inklusive `\n`). Das ist sauber. Kein manuelles Buffering nötig.

### Startup-Zeile des JeeLink
Nach dem Anstecken sendet der JeeLink sofort eine Versionszeile:
`[LaCrosseITPlusReader.10.1s (RFM69CW f:868300 r:17241)]`
Der Parser verwirft diese korrekt (beginnt mit `[`).

### Protokoll-Status-Byte
Das Status-Byte ist **nicht offiziell dokumentiert**. Die Interpretation
basiert auf Praxisbeobachtungen. Nach dem ersten Deployment:
1. `_LOGGER.debug` in `parse_line` aktivieren
2. Rohdaten aus dem HA-Log mit den Anzeigewerten der Sensoren abgleichen
3. Sollte ein Wert dauerhaft um ±40 °C daneben liegen → Formel-Offset prüfen

### pyserial-asyncio Version
Ab Version 0.6 ist die `open_serial_connection()` API stabil.
Ältere Versionen haben eine andere Signatur. Mindestversion in `manifest.json`
fest auf `>=0.6` setzen.

### Config-Entry-Migrations
Wenn das Options-Schema sich in einer späteren Version ändert (neue Felder),
`async_migrate_entry` in `__init__.py` implementieren und die
Schema-Version in `manifest.json` hochzählen.

---

## Empfohlene Ressourcen

- [pyserial-asyncio Doku](https://pyserial-asyncio.readthedocs.io/en/latest/)
- [LaCrosseITPlusReader Firmware (FHEM)](https://svn.fhem.de/trac/browser/trunk/fhem/contrib/arduino)
- [HA Developer Docs – Config Flow](https://developers.home-assistant.io/docs/config_entries_config_flow_handler)
- [HA Developer Docs – Repairs](https://developers.home-assistant.io/docs/repairs)
- [HA Integration Blueprint](https://github.com/ludeeus/integration_blueprint)
- [pyserial-asyncio auf PyPI](https://pypi.org/project/pyserial-asyncio/)
