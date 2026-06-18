# Projektplan: HA Custom Integration `jeelink_lacrosse` (v3 – pyserial-asyncio-fast + Protokoll-Fixes)

> **v3-Status:** Diese Fassung korrigiert die in einem Code-/Protokoll-/HA-API-Audit
> verifizierten Fehler aus v2. Architektur und Phaseneinteilung bleiben unverändert –
> es ändern sich Dependency, Batterie-/Feuchte-Parsing, Persistenz und einige
> Lifecycle-Details. Alle Korrekturen sind gegen Primärquellen belegt
> (LaCrosseITPlusReader-Firmware, `pylacrosse`, HA-Developer-Docs).

## Was sich gegenüber v2 ändert (Korrekturen aus dem Audit)

| # | Schweregrad | Bereich | v2 (fehlerhaft) | v3 (korrigiert) |
|---|---|---|---|---|
| 1 | **Blocker** | Dependency | `pyserial-asyncio>=0.6` | `pyserial-asyncio-fast>=0.16` – HA blockiert `pyserial-asyncio` ab Release **2026.7** |
| 2 | **Blocker** | Batterie-Flag | `battery_warn = status & 0x80` | STATUS-Bit 7 = **neue** Batterie; **Schwachbatterie = HUM-Bit 7** |
| 3 | Major | Feuchte | `hum_raw` direkt, `> 100` = kein Sensor | erst `& 0x7f` maskieren, dann `> 100` = kein Sensor |
| 4 | Major | manifest | `"homeassistant": "2022.9.0"` (kein gültiger Key) | Mindestversion in **`hacs.json`**; `integration_type`/`issue_tracker` ergänzt |
| 5 | Major | Persistenz | `last_seen` in `entry.options` → Reload-Schleife | `homeassistant.helpers.storage.Store` |
| 6 | Major | Tests | `test_battery_warn_bit7_set` schlägt fehl | korrekte Asserts + neue Batterie-/Feuchte-Vektoren |
| 7 | Major | Coordinator | `list.discard()` (AttributeError) | `list.remove()` mit Guard |
| 8 | Major | Coordinator | Timer-Unsub verworfen → Stacking/Leak | Unsub-Handle speichern und in `async_stop` aufrufen |
| 9 | Minor | Coordinator | `is_available` hardcodet 7200 s | aus `OFFLINE_THRESHOLD_MINUTES` abgeleitet |
| 10 | Minor | serial_reader | `readline()` ohne `ValueError`-Schutz; Writer nie geschlossen | `ValueError` überspringen; `writer.close()` im `finally` |

Die **Phasen 1–2** (Dev-Environment, Basisstruktur) und **Phasen 4–9**
(Config Flow, Entities, Repairs, Options Flow, Tests, HACS) bleiben strukturell
identisch. Phase 3 (Serial-Kommunikation) ist gegenüber v1 vollständig neu und
gegenüber v2 in den oben genannten Punkten korrigiert.

---

## Protokoll-Referenz: LaCrosse IT+ via JeeLink

**Verifiziert** gegen den Firmware-Quellcode (`LaCrosseITPlusReader`, `LaCrosse.cpp`
→ `GetFhemDataString()`) **und** die Referenz-Bibliothek `pylacrosse`. Byte-Position,
Temperaturformel und die Bit-Belegung der Status-/Feuchte-Bytes sind damit belegt –
nicht mehr „nur Praxisbeobachtung".

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
| `9` | Literal | Marker des LaCrosse-IT+-Decoders |
| `ID` | int 0–255 | Sensor-Identifikator |
| `STATUS` | int | Sensor-Subtyp + Neu-Batterie-Bit (siehe unten) |
| `T_H` | int | Temperatur High-Byte |
| `T_L` | int | Temperatur Low-Byte |
| `HUM` | int | Feuchte **+ Schwachbatterie-Bit in Bit 7** (siehe unten) |

### Temperaturberechnung (unverändert – verifiziert korrekt)
```python
temperature = (T_H * 256 + T_L - 1000) / 10.0  # Ergebnis in °C
```

Beispiel (aus Firmware-Kommentar): `T_H=4, T_L=156` → `(4*256+156-1000)/10 = 18,0 °C` ✓
Die Firmware kodiert `pTemp = Temperatur*10 + 1000` und gibt `pTemp >> 8` / `pTemp & 0xFF`
aus – die Umkehrung ist exakt obige Formel.

Gültigkeitsbereich: `-40,0 °C` bis `+60,0 °C`. **Wichtig:** Die Firmware filtert
selbst bereits mit genau dieser Grenze (`if (Temperature >= 60 || Temperature <= -40) return "";`),
d. h. außerhalbliegende Werte werden gar nicht erst gesendet. Der Host-seitige
Sanity-Check ist also ein Spiegel dieses Filters und darf **nicht** aufgeweitet
werden. Die spezifizierte Messspanne der TX29-/TX29DTH-IT-Familie (−39,9…+59,9 °C)
liegt innerhalb des Bereichs.

### STATUS-Byte (Feld 4) – KORRIGIERT
Die unteren Bits kodieren den **Sensor-Subtyp**, Bit 7 die **frisch eingelegte Batterie**:

| Wert | Bedeutung |
|---|---|
| `1` (0x01) | Subtyp „mit Feuchtesensor", normaler Betrieb |
| `2` (0x02) | Subtyp „ohne Feuchtesensor" |
| `129` (0x81) | wie 1, aber **gerade neue Batterie eingelegt** (`type \| 0x80`) |
| `130` (0x82) | wie 2, aber neue Batterie |

→ `new_battery = bool(STATUS & 0x80)`. **Das ist NICHT die Schwachbatterie-Warnung**,
sondern das Gegenteil: Es zeigt eine *frisch eingelegte* Batterie an und ist nur kurz
nach dem Wechsel gesetzt. Firmware: `field4 = NewBatteryFlag ? 129 : 1`;
`pylacrosse`: `new_battery = bool(data[2] & 0x80)`.

### HUM-Byte (Feld 6) – KORRIGIERT
Bit 7 des Feuchte-Bytes trägt das **Schwachbatterie-Flag**, die unteren 7 Bit die
Feuchte. Vor der Auswertung **maskieren**:

```python
low_battery = bool(HUM & 0x80)   # echte „Batterie schwach"-Warnung
humidity    = HUM & 0x7f         # tatsächlicher %-Wert
```

- Firmware: `byte hum = Humidity; if (WeakBatteryFlag) hum |= 0x80; print(hum);`
- `pylacrosse`: `humidity = data[5] & 0x7f`, `low_battery = bool(data[5] & 0x80)`
- „Kein Feuchtesensor" wird mit dem **maskierten** Wert `106` signalisiert
  (`106 & 0x7f == 106 > 100`). Ein echter 37 %-Wert bei schwacher Batterie kommt als
  `37 | 0x80 = 165` an → ohne Maskierung würde v2 das fälschlich als „kein Sensor"
  (`None`) werten und gültige Feuchte verwerfen.

> **Konsequenz für die Entities:** Der „Batterie-Warnung"-`binary_sensor` muss aus
> `low_battery` (HUM-Bit 7) gespeist werden – **nicht** aus `new_battery`.
> `new_battery` kann optional als Diagnose-Attribut surfacen.

---

## Aktualisierte Dateistruktur

```
<repo-root>/
├── hacs.json               # ← NEU: HACS-Metadaten inkl. HA-Mindestversion
└── custom_components/
    └── jeelink_lacrosse/
        ├── __init__.py          # Setup, Entry-Load/-Unload, Update-Listener
        ├── config_flow.py       # GUI-Setup-Wizard  (wie v1)
        ├── options_flow.py      # Nachträgliche Einstellungen (wie v1)
        ├── repairs.py           # ID-Neuzuweisungs-Dialog (wie v1)
        ├── coordinator.py       # Datenverwaltung, Status-Tracking, Store ← geändert
        ├── serial_reader.py     # Serial via pyserial-asyncio-fast ← geändert
        ├── protocol.py          # LaCrosse-Protokoll-Parser ← geändert
        ├── sensor.py            # Temperatur- und Feuchte-Entities (wie v1)
        ├── binary_sensor.py     # Batterie-Warnung ← liest low_battery (Anpassung ggü. v1)
        ├── const.py             # Konstanten (wie v1, + AVAILABILITY-/INTERVAL-Konstanten)
        ├── manifest.json        # ← geänderte Dependency + Felder
        ├── strings.json
        └── translations/
            ├── de.json
            └── en.json
```

---

## Aktualisierte manifest.json

```json
{
  "domain": "jeelink_lacrosse",
  "name": "JeeLink LaCrosse",
  "version": "0.1.0",
  "integration_type": "hub",
  "codeowners": ["@dein-github-name"],
  "config_flow": true,
  "iot_class": "local_push",
  "requirements": ["pyserial-asyncio-fast>=0.16"],
  "documentation": "https://github.com/dein-repo/jeelink-lacrosse-ha",
  "issue_tracker": "https://github.com/dein-repo/jeelink-lacrosse-ha/issues"
}
```

**Wichtige Änderungen ggü. v2:**
- `requirements`: `pyserial-asyncio-fast>=0.16` statt `pyserial-asyncio>=0.6`.
  HA blockiert die Installation von `pyserial-asyncio` ab **2026.7** (blockiert den
  Event-Loop, unmaintained). `pyserial-asyncio-fast` ist der von HA gepflegte
  Drop-in-Ersatz (`home-assistant-libs`).
- **`"homeassistant"` entfernt** – diesen Key gibt es im manifest-Schema nicht
  (er wird ignoriert und erzwingt keine Mindestversion).
- `integration_type: "hub"` ergänzt (JeeLink-Stick = Gateway zu mehreren Sensoren).
- `issue_tracker` ergänzt (empfohlen für HACS/Repairs).

### NEU: hacs.json (im Repo-Root)
Die HA-Mindestversion gehört bei Custom-Integrationen hierher, nicht ins manifest:

```json
{
  "name": "JeeLink LaCrosse",
  "homeassistant": "2024.11.0",
  "render_readme": true
}
```

Mindestversion **2024.11.0**: Die Repairs-API (Phase 6) wäre schon ab 2022.9
nutzbar, der Options-Flow (Phase 7) nutzt aber den modernen `OptionsFlow` ohne
selbst gesetztes `self.config_entry` – das wird seit 2024.11 vom Flow-Manager
bereitgestellt (das frühere manuelle Setzen ist inzwischen nicht mehr erlaubt).
Damit ist 2024.11 die bindende Untergrenze.

---

## Phase 3 – Serial-Kommunikation (zwei Dateien)

**Dauer: ~2–3 Tage**

### Datei 1: protocol.py – Reiner Parser, keine I/O

Keine Abhängigkeit zu pyserial oder HA → vollständig ohne Hardware unit-testbar.

```python
"""
LaCrosse IT+ Protokoll-Parser.
Kein I/O – nur String-Parsing.
Verifiziert gegen LaCrosseITPlusReader-Firmware und pylacrosse.
"""
from __future__ import annotations
from dataclasses import dataclass
import logging

_LOGGER = logging.getLogger(__name__)

TEMP_MIN = -40.0
TEMP_MAX =  60.0
HUM_MAX  = 100      # maskierte Werte > 100 (z. B. 106) = kein Feuchtesensor


@dataclass
class LaCrosseMeasurement:
    sensor_id:    int
    temperature:  float
    humidity:     int | None   # None wenn kein Feuchtesensor
    new_battery:  bool         # STATUS-Bit 7: frische Batterie gerade eingelegt
    low_battery:  bool         # HUM-Bit 7: Batterie schwach (echte Warnung)
    raw_status:   int          # Original-Status-Byte für Diagnose


def parse_line(line: str) -> LaCrosseMeasurement | None:
    """
    Parst eine LaCrosse IT+ Zeile vom JeeLink.

    Gültiges Format:  'OK 9 <ID> <STATUS> <T_H> <T_L> <HUM>'
    Ungültig/ignoriert: Leerzeilen, Versionszeilen (start mit '['), '#' etc.

    Gibt None zurück, wenn die Zeile kein gültiges LaCrosse-IT+-Paket ist
    oder die Temperatur außerhalb des physikalisch sinnvollen Bereichs liegt.
    """
    line = line.strip()   # entfernt auch das CRLF-'\r' (Firmware sendet \r\n)

    # Ignoriere Leerzeilen, Versionsstring, sonstige Nicht-Daten
    if not line or line[0] != 'O':
        return None

    parts = line.split()

    if len(parts) != 7 or parts[0] != 'OK' or parts[1] != '9':
        return None

    try:
        sensor_id = int(parts[2])
        status    = int(parts[3])
        t_h       = int(parts[4])
        t_l       = int(parts[5])
        hum_raw   = int(parts[6])
    except ValueError:
        _LOGGER.debug("Konnte Zeile nicht parsen: %s", line)
        return None

    # Temperatur (verifiziert: T_H=4, T_L=156 → 18,0 °C)
    temperature = (t_h * 256 + t_l - 1000) / 10.0

    # Sanity-Check – spiegelt den Firmware-internen Filter [-40, 60]
    if not TEMP_MIN <= temperature <= TEMP_MAX:
        _LOGGER.debug(
            "Sensor %d: Temperatur %.1f°C außerhalb Bereich [%.1f, %.1f] – ignoriert",
            sensor_id, temperature, TEMP_MIN, TEMP_MAX,
        )
        return None

    # HUM-Byte: Bit 7 = Schwachbatterie, untere 7 Bit = Feuchte
    low_battery = bool(hum_raw & 0x80)
    hum         = hum_raw & 0x7f
    humidity    = hum if hum <= HUM_MAX else None   # 106 = kein Feuchtesensor

    # STATUS-Byte: Bit 7 = frisch eingelegte Batterie (NICHT Schwachbatterie!)
    new_battery = bool(status & 0x80)

    return LaCrosseMeasurement(
        sensor_id=sensor_id,
        temperature=temperature,
        humidity=humidity,
        new_battery=new_battery,
        low_battery=low_battery,
        raw_status=status,
    )
```

---

### Datei 2: serial_reader.py – Async Serial, kein Protokoll-Wissen

```python
"""
Asynchroner JeeLink Serial-Reader.
Nutzt pyserial-asyncio-fast, kein Threading.
"""
from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from typing import Any

import serial                          # pyserial – für SerialException
import serial_asyncio_fast as serial_asyncio

from .protocol import LaCrosseMeasurement, parse_line

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 30  # Sekunden vor erneutem Verbindungsversuch


class JeeLinkSerialReader:
    """
    Öffnet den JeeLink-Port, liest Zeilen, parsed sie und ruft
    den `on_measurement`-Callback auf. Reconnectet bei Disconnect.
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
        """Äußerer Loop: verbinden, lesen, bei Fehler reconnecten."""
        while self._running:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    _LOGGER.warning(
                        "JeeLink Verbindung unterbrochen (%s). Reconnect in %ds...",
                        exc, RECONNECT_DELAY,
                    )
                    await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_read(self) -> None:
        """Öffnet die Serial-Verbindung und liest Zeilen bis zur Exception."""
        _LOGGER.info(
            "Verbinde mit JeeLink auf %s (Baud: %d)", self._device, self._baud
        )

        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._device,
            baudrate=self._baud,
        )
        _LOGGER.info("JeeLink verbunden – empfange Daten")

        try:
            while self._running:
                try:
                    raw_line = await reader.readline()
                except ValueError:
                    # readline() wirft ValueError, wenn in 64 KiB kein '\n' kommt
                    # (Müll/Partial). Überspringen, NICHT als Disconnect behandeln.
                    _LOGGER.debug("Überlange/ungültige Zeile verworfen")
                    continue

                if not raw_line:
                    raise ConnectionResetError("JeeLink hat Verbindung getrennt")

                line = raw_line.decode("ascii", errors="ignore")
                measurement = parse_line(line)
                if measurement is None:
                    continue

                _LOGGER.debug(
                    "Sensor %d: %.1f°C %s%% new_batt=%s low_batt=%s status=%d",
                    measurement.sensor_id,
                    measurement.temperature,
                    measurement.humidity if measurement.humidity is not None else "n/a",
                    measurement.new_battery,
                    measurement.low_battery,
                    measurement.raw_status,
                )
                result = self._on_measurement(measurement)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            # Transport in jedem Fall schließen (Reconnect, Cancel, Fehler)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # close() kann beim Disconnect selbst werfen
                pass

    @staticmethod
    async def test_connection(device: str, baud: int) -> bool:
        """
        Schneller Verbindungstest für den Config Flow.
        Öffnet den Port und wartet kurz auf irgendeinen Input.
        """
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                serial_asyncio.open_serial_connection(url=device, baudrate=baud),
                timeout=5.0,
            )
            await asyncio.wait_for(reader.readline(), timeout=3.0)
            return True
        except (OSError, asyncio.TimeoutError, serial.SerialException) as exc:
            _LOGGER.debug("Verbindungstest fehlgeschlagen: %s", exc)
            return False
        finally:
            if writer is not None:
                writer.close()
```

> **Hinweis zur Migration:** Durch `import serial_asyncio_fast as serial_asyncio`
> bleiben alle Aufruf-Stellen (`serial_asyncio.open_serial_connection(...)`)
> unverändert. Die Signatur `open_serial_connection(url=..., baudrate=...)`
> ist in `pyserial-asyncio-fast` identisch zu `pyserial-asyncio` 0.6.

---

### Aktualisierter Coordinator (Store-Persistenz, Lifecycle-Fixes)

```python
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
    CONF_DEVICE, CONF_BAUD,
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
        self.lacrosse_id   = lacrosse_id
        self.friendly_name = friendly_name
        self.temperature: float | None = None
        self.humidity:    int   | None = None
        self.low_battery: bool         = False   # echte Schwachbatterie (HUM-Bit 7)
        self.new_battery: bool         = False   # frisch eingelegt (STATUS-Bit 7)
        self.last_seen:   float        = 0.0
        self.available:   bool         = False


class JeeLinkCoordinator:
    """Zentrale Datenklasse für die JeeLink-Integration."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass  = hass
        self.entry = entry
        self.sensors:     dict[str, SensorState] = {}
        self.unknown_ids: set[int]               = set()
        self._reader:     JeeLinkSerialReader | None = None
        self._listeners:  dict[str, list[callable]]  = {}
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsub_interval: callable | None = None

    # --- Start / Stop -------------------------------------------------------

    async def async_start(self) -> None:
        """Integration initialisieren und Serial-Reader starten."""
        options = self.entry.options

        # Transienten State aus dem Store laden
        stored = await self._store.async_load() or {}
        last_seen_map: dict[str, float] = stored.get("last_seen", {})
        self.unknown_ids = set(stored.get("unknown_ids", []))

        # Sensoren aus der (User-)Konfiguration aufbauen
        for slug, cfg in options.get(CONF_SENSORS, {}).items():
            state = SensorState(
                lacrosse_id=cfg[CONF_LACROSSE_ID],
                friendly_name=cfg["friendly_name"],
            )
            state.last_seen = last_seen_map.get(slug, 0.0)
            self.sensors[slug] = state

        self._reader = JeeLinkSerialReader(
            device=options[CONF_DEVICE],
            baud=options.get(CONF_BAUD, 57600),
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

    async def async_options_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Aufgerufen, wenn Options im Config-Flow geändert werden (echte Config)."""
        await self.async_stop()
        self.sensors.clear()
        self.unknown_ids.clear()
        self._listeners.clear()
        self.entry = entry
        await self.async_start()

    # --- Measurement-Callback ----------------------------------------------

    async def _on_measurement(self, m: LaCrosseMeasurement) -> None:
        """Verarbeitet eine eingehende Messung."""
        slug = self._slug_for_id(m.sensor_id)

        if slug is not None:
            state = self.sensors[slug]
            state.temperature = m.temperature
            state.humidity    = m.humidity
            state.low_battery = m.low_battery
            state.new_battery = m.new_battery
            state.last_seen   = time.time()
            state.available   = True

            for cb in self._listeners.get(slug, []):
                cb()

            # Debounced in den Store schreiben (kein entry.options, kein Reload)
            self._store.async_delay_save(self._data_to_store, PERSIST_DELAY_SECONDS)
        else:
            if m.sensor_id not in self.unknown_ids:
                self.unknown_ids.add(m.sensor_id)
                _LOGGER.info("Neue unbekannte LaCrosse-ID empfangen: %d", m.sensor_id)
                self._store.async_delay_save(self._data_to_store, PERSIST_DELAY_SECONDS)
                await self._async_check_for_replacement(m.sensor_id)

    @callback
    def _data_to_store(self) -> dict:
        """Snapshot des transienten States für den Store."""
        return {
            "last_seen":   {slug: s.last_seen for slug, s in self.sensors.items()},
            "unknown_ids": list(self.unknown_ids),
        }

    # --- Listener-Registrierung --------------------------------------------

    def register_listener(self, slug: str, callback: callable) -> None:
        self._listeners.setdefault(slug, []).append(callback)

    def unregister_listener(self, slug: str, callback: callable) -> None:
        # Fix ggü. v2: _listeners-Werte sind Listen → list.remove(), nicht .discard()
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

    async def _async_check_offline_sensors(self, _now=None) -> None:
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
        ID-Neuzuweisung nach Batteriewechsel (vom Repairs-Flow aufgerufen).
        Das ID-Mapping ist echte Konfiguration → schreibt entry.options.
        Der dadurch ausgelöste Reload ist hier gewollt und selten.
        """
        _LOGGER.info("Weise Sensor '%s' neue LaCrosse-ID %d zu", slug, new_lacrosse_id)

        if slug in self.sensors:
            self.sensors[slug].lacrosse_id = new_lacrosse_id
            self.sensors[slug].last_seen   = 0.0
            self.sensors[slug].available   = False

        self.unknown_ids.discard(new_lacrosse_id)
        await self._store.async_save(self._data_to_store())

        new_options = copy.deepcopy(dict(self.entry.options))
        if slug in new_options.get(CONF_SENSORS, {}):
            new_options[CONF_SENSORS][slug][CONF_LACROSSE_ID] = new_lacrosse_id

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
```

> **Persistenz-Hinweis (Fix #5):** `last_seen` und `unknown_ids` liegen jetzt im
> `Store` (`.storage/jeelink_lacrosse.<entry_id>`). In v2 wurde `last_seen` per
> `async_update_entry(options=…)` geschrieben – das feuert den Options-Update-Listener,
> der die Integration komplett neu lädt (Serial-Verbindung weg, State weg), also etwa
> alle 5 Minuten. Mit dem Store schreibt nur noch eine echte Konfigurationsänderung
> (Options-Flow, `reassign_id`) in `entry.options`, sodass der Reload selten und
> beabsichtigt ist.

> **`__init__.py` (wie v1, aber bestätigen):** Update-Listener via
> `entry.async_on_unload(entry.add_update_listener(coordinator.async_options_updated))`
> registrieren; alternativ `OptionsFlowWithReload` nutzen, um den Listener+Reload-
> Deprecation-Pfad (ab 2026.6) zu vermeiden.

---

## Aktualisierte Tests (Phase 8)

`protocol.py` ist vollständig ohne Hardware testbar; `serial_reader.py` mit
`asyncio.StreamReader`-Mock. **Patch-Ziel ist jetzt das Modul-lokale Alias**
`custom_components.jeelink_lacrosse.serial_reader.serial_asyncio`.

```python
# tests/test_protocol.py
import pytest
from custom_components.jeelink_lacrosse.protocol import parse_line, LaCrosseMeasurement


class TestParseLineValid:
    """Verifiziert anhand bekannter Firmware-Beispiele."""

    def test_firmware_reference_18_0_degrees(self):
        """OK 9 56 1 4 156 37 -> T=18.0, H=37, keine Batterie-Flags."""
        r = parse_line("OK 9 56 1 4 156 37")
        assert r is not None
        assert r.sensor_id   == 56
        assert r.temperature == 18.0
        assert r.humidity    == 37
        assert r.new_battery is False
        assert r.low_battery is False
        assert r.raw_status  == 1

    def test_new_battery_flag_status_bit7(self):
        """STATUS=129 (0x81): frisch eingelegte Batterie, Messwert bleibt gültig."""
        r = parse_line("OK 9 56 129 4 156 37")
        assert r is not None
        assert r.temperature == 18.0
        assert r.new_battery is True     # STATUS-Bit 7
        assert r.low_battery is False
        assert r.humidity    == 37
        assert r.raw_status  == 129

    def test_low_battery_flag_in_humidity_byte(self):
        """HUM=165 (37 | 0x80): Schwachbatterie + gültige 37% Feuchte."""
        r = parse_line("OK 9 56 1 4 156 165")
        assert r is not None
        assert r.temperature == 18.0
        assert r.low_battery is True      # HUM-Bit 7
        assert r.new_battery is False
        assert r.humidity    == 37        # 165 & 0x7f

    def test_no_humidity_sensor(self):
        """HUM=106 (maskiert > 100) -> kein Feuchtesensor -> None."""
        r = parse_line("OK 9 55 1 4 124 106")
        assert r is not None
        assert r.humidity is None
        assert r.low_battery is False
        assert abs(r.temperature - 14.8) < 0.1

    def test_no_humidity_sensor_with_low_battery(self):
        """HUM=234 (106 | 0x80) -> kein Sensor (None) + Schwachbatterie True."""
        r = parse_line("OK 9 55 1 4 124 234")
        assert r is not None
        assert r.humidity is None         # 234 & 0x7f = 106 > 100
        assert r.low_battery is True

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
        # T_H=0, T_L=0 -> (0-1000)/10 = -100°C -> außerhalb [-40, +60]
        assert parse_line("OK 9 56 1 0 0 50") is None


# tests/test_serial_reader.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

SERIAL_PATCH = "custom_components.jeelink_lacrosse.serial_reader.serial_asyncio"


async def test_measurement_callback_called(hass):
    """serial_reader ruft Callback auf, wenn gültige Zeile empfangen."""
    from custom_components.jeelink_lacrosse.serial_reader import JeeLinkSerialReader

    measurements = []

    async def mock_callback(m):
        measurements.append(m)

    mock_reader = AsyncMock()
    mock_reader.readline.side_effect = [
        b"OK 9 56 1 4 156 37\r\n",
        b"OK 9 23 1 4 200 55\r\n",
        asyncio.CancelledError(),
    ]
    mock_writer = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               return_value=(mock_reader, mock_writer)):
        reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, mock_callback)
        await reader.async_start()
        await asyncio.sleep(0.1)
        await reader.async_stop()

    assert len(measurements) == 2
    assert measurements[0].sensor_id == 56
    assert measurements[0].temperature == 18.0
    assert measurements[1].sensor_id == 23
    mock_writer.close.assert_called()      # Transport wurde geschlossen


async def test_reconnect_on_disconnect(hass):
    """serial_reader reconnectet nach Verbindungsabbruch."""
    from custom_components.jeelink_lacrosse.serial_reader import JeeLinkSerialReader

    connect_count = 0

    async def mock_connect(url, baudrate):
        nonlocal connect_count
        connect_count += 1
        mock_reader = AsyncMock()
        if connect_count == 1:
            mock_reader.readline.side_effect = OSError("USB disconnect")
        else:
            mock_reader.readline.side_effect = asyncio.CancelledError()
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        return mock_reader, mock_writer

    with patch(f"{SERIAL_PATCH}.open_serial_connection", side_effect=mock_connect):
        with patch("custom_components.jeelink_lacrosse.serial_reader.RECONNECT_DELAY", 0):
            reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, AsyncMock())
            await reader.async_start()
            await asyncio.sleep(0.1)
            await reader.async_stop()

    assert connect_count >= 2


async def test_async_stop_cancels_running_loop(hass):
    """Deckt den echten Cancel-Pfad in async_stop ab (Fix ggü. v2)."""
    from custom_components.jeelink_lacrosse.serial_reader import JeeLinkSerialReader

    block = asyncio.Event()

    mock_reader = AsyncMock()
    mock_reader.readline.side_effect = lambda: block.wait()  # blockiert dauerhaft
    mock_writer = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               return_value=(mock_reader, mock_writer)):
        reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, AsyncMock())
        await reader.async_start()
        await asyncio.sleep(0.05)          # Loop hängt in readline()
        await reader.async_stop()          # muss task.cancel() + await ausführen

    assert reader._task.done()
    mock_writer.close.assert_called()      # finally schließt auch bei Cancel
```

---

## Gesamtzeitplan (unverändert ggü. v2)

| Phase | Inhalt | Aufwand |
|---|---|---|
| 1 | Dev-Environment | 1 Tag |
| 2 | Basisstruktur (manifest, hacs.json, const, __init__) | 1 Tag |
| 3 | protocol.py + serial_reader.py + coordinator.py | **2–3 Tage** |
| 4 | Config Flow | 3 Tage |
| 5 | Sensor-Entities | 1–2 Tage |
| 6 | ID-Neuzuweisung (Repairs) | 3–4 Tage |
| 7 | Options Flow | 1–2 Tage |
| 8 | Tests | **1–2 Tage** |
| 9 | HACS & Release | 1 Tag |
| **Gesamt** | | **~14–17 Tage** |

Die v3-Korrekturen ändern den Aufwand nicht nennenswert – es sind gezielte Fixes,
keine Neukonzeption (Dependency-Tausch ist trivial, Batterie-/Feuchte-Logik sind
wenige Zeilen).

---

## Bekannte Fallstricke (aktualisiert für v3)

### Dependency: pyserial-asyncio-fast statt pyserial-asyncio
HA blockiert die Installation von `pyserial-asyncio` ab Release **2026.7** (es
blockiert den Event-Loop und ist unmaintained). `pyserial-asyncio-fast` ist der
HA-offizielle Drop-in-Ersatz (`home-assistant-libs`, aktuell 0.16). Import:
`import serial_asyncio_fast as serial_asyncio`. API/Signatur identisch.

### Stabiler USB-Pfad unter HAOS
```
/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
```
Statt `/dev/ttyUSB0` (Nummer kann sich nach Reboot ändern).

### readline() und Partial-/Müll-Zeilen
`asyncio.StreamReader.readline()` liefert eine vollständige Zeile inkl. `\n`.
Kommt jedoch in 64 KiB (Default-Limit) kein `\n` (Datenmüll), wirft `readline()`
einen `ValueError`. v3 fängt das im Inner-Loop ab und überspringt die Zeile,
statt sie als Disconnect zu werten. Das CRLF-`\r` der Firmware entfernt
`parse_line` per `strip()`.

### Protokoll: Batterie- und Feuchte-Bits (Hauptkorrektur ggü. v2)
- **STATUS-Bit 7 (`0x80`) = neue/frische Batterie**, nicht Schwachbatterie.
- **HUM-Bit 7 (`0x80`) = Schwachbatterie**; vor der Feuchte-Auswertung mit
  `& 0x7f` maskieren. Der „Batterie-Warnung"-`binary_sensor` muss aus
  `low_battery` gespeist werden.
- Belegt gegen `LaCrosseITPlusReader`-Firmware (`GetFhemDataString`) und `pylacrosse`.

### Persistenz nicht über entry.options
Hochfrequenten Laufzeit-State (`last_seen`, `unknown_ids`) im `Store` halten.
`entry.options`-Schreibzugriffe lösen über den Update-Listener einen Reload aus.

### Config-Entry-Migrations
Ändert sich das Options-Schema später, `async_migrate_entry` in `__init__.py`
implementieren und die Schema-Version hochzählen.

---

## Empfohlene Ressourcen

- [pyserial-asyncio-fast auf PyPI](https://pypi.org/project/pyserial-asyncio-fast/)
- [pyserial-asyncio-fast (home-assistant-libs)](https://github.com/home-assistant-libs/pyserial-asyncio-fast)
- [HA Developer Blog – Solving pyserial-asyncio blocking the event loop (2026-01-05)](https://developers.home-assistant.io/blog/2026/01/05/pyserial-asyncio-fast/)
- [LaCrosseITPlusReader Firmware (FHEM)](https://svn.fhem.de/trac/browser/trunk/fhem/contrib/arduino)
- [pylacrosse (Referenz-Parser)](https://github.com/hthiery/python-lacrosse)
- [HA Developer Docs – Integration Manifest](https://developers.home-assistant.io/docs/creating_integration_manifest)
- [HA Developer Docs – Config Flow](https://developers.home-assistant.io/docs/config_entries_config_flow_handler)
- [HA Developer Docs – Repairs](https://developers.home-assistant.io/docs/core/platform/repairs)
- [HA Developer Docs – Storage helper](https://developers.home-assistant.io/docs/data_entry_flow_index/#storing-data)
- [HACS – Integration publizieren (hacs.json)](https://hacs.xyz/docs/publish/integration)
