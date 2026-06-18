# JeeLink LaCrosse – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home-Assistant-Integration zum Empfang von **LaCrosse-IT+-Funksensoren** (Temperatur/Luftfeuchte, 868 MHz) über einen **JeeLink-USB-Stick** mit der [`LaCrosseITPlusReader`](https://github.com/rjedwards/LaCrosseITPlusReader)-Firmware.

Die Integration arbeitet rein **lokal** (kein Cloud-Zugriff) und **push-basiert**: Messwerte werden verarbeitet, sobald der Stick sie empfängt.

## Funktionen

- 🌡️ Temperatur- und Luftfeuchte-Sensoren je LaCrosse-Sensor
- 🔋 Binärsensor **„Batterie schwach"** (echte Schwachbatterie-Warnung aus dem HUM-Bit)
- 🛠️ Diagnose-Binärsensor **„Neue Batterie"** (kurz nach Batteriewechsel gesetzt)
- 📴 **Offline-Erkennung**: Sensoren werden `unavailable`, wenn länger keine Daten kommen
- 🔁 **Repairs-Dialog zur ID-Neuzuweisung**: LaCrosse-IT+-Sensoren würfeln ihre Sende-ID bei jedem Batteriewechsel neu – die Integration erkennt das und bietet an, die neue ID dem bestehenden Sensor zuzuweisen (Historie und Entities bleiben erhalten)
- ⚙️ Geführtes Setup (Config Flow) und Sensor-Verwaltung (Options Flow), komplett über die UI

## Voraussetzungen

- **Home Assistant 2024.11 oder neuer**
- Ein **JeeLink** (oder kompatibler RFM-Transceiver) mit aufgespielter **`LaCrosseITPlusReader`**-Firmware, die Zeilen im Format `OK 9 <ID> …` ausgibt
- Der Stick muss als serielles Gerät verfügbar sein (z. B. `/dev/ttyUSB0` bzw. stabil unter `/dev/serial/by-id/…`)

> **Hinweis für virtualisierte Installationen (Proxmox/ESXi/KVM):** Der USB-Stick muss an die HAOS-/HA-VM **durchgereicht** sein, sonst taucht kein serielles Gerät auf. Siehe [Fehlerbehebung](#fehlerbehebung).

## Installation

### Über HACS (empfohlen)

1. HACS → **Integrationen** → Drei-Punkte-Menü → **Benutzerdefinierte Repositories**.
2. Repository `https://github.com/EvilKnievel89/jeelink-lacrosse-ha` als Kategorie **Integration** hinzufügen.
3. „JeeLink LaCrosse" installieren und Home Assistant neu starten.

### Manuell

1. Den Ordner `custom_components/jeelink_lacrosse/` in das HA-Konfigurationsverzeichnis nach `config/custom_components/jeelink_lacrosse/` kopieren.
2. Home Assistant neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen** und nach **„JeeLink LaCrosse"** suchen.
2. Den seriellen Port wählen. Empfohlen wird **„Pfad manuell eingeben"** mit dem stabilen `by-id`-Pfad, z. B.:
   ```
   /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
   ```
3. **Baudrate** (Standard `57600`) bestätigen → die Verbindung wird getestet und der Eintrag angelegt.

Zunächst entstehen noch keine Entities – zuerst werden Sensoren hinzugefügt.

## Sensoren verwalten (Options Flow)

Bei der Integration auf **„Konfigurieren"**:

- **Sensor hinzufügen** – LaCrosse-ID und Anzeigename angeben. Zuletzt empfangene, noch nicht zugeordnete IDs werden als Hilfestellung angezeigt.
- **Sensor bearbeiten** – Name und/oder ID ändern.
- **Sensor entfernen**.

Je konfiguriertem Sensor entstehen vier Entities:

| Entity | Typ | Bedeutung |
|---|---|---|
| Temperatur | `sensor` | °C, `device_class: temperature` |
| Luftfeuchtigkeit | `sensor` | %, `device_class: humidity` (entfällt bei Sensoren ohne Feuchtefühler) |
| Batteriestand | `binary_sensor` | `device_class: battery` – *an* = schwach |
| Neue Batterie | `binary_sensor` | Diagnose – kurz nach Batteriewechsel *an* |

## Batteriewechsel & ID-Neuzuweisung

LaCrosse-IT+-Sensoren vergeben ihre Sende-ID bei jedem Batteriewechsel **neu zufällig**. Fällt ein konfigurierter Sensor offline und taucht danach eine neue, unbekannte ID auf, erscheint unter **Einstellungen → Geräte & Dienste → Reparaturen** der Hinweis **„Mögliche Sensor-ID-Änderung"**. Dort kann die neue ID dem bestehenden Sensor zugewiesen werden – Verlauf und Entities bleiben erhalten.

Es werden bevorzugt nur IDs angeboten, die **nach** dem Offline-Gehen eines Sensors neu auftauchten, damit dauerhaft mithörende Fremd-Sensoren in der Nachbarschaft nicht fälschlich vorgeschlagen werden.

## Protokoll

Erwartet wird die `LaCrosseITPlusReader`-Ausgabe:

```
OK 9 <ID> <STATUS> <T_H> <T_L> <HUM>
```

- Temperatur = `(T_H * 256 + T_L − 1000) / 10` °C
- `HUM`-Bit 7 → Schwachbatterie; verbleibende Bits → Luftfeuchte (Wert 106 = kein Feuchtesensor)
- `STATUS`-Bit 7 → neue Batterie

## Fehlerbehebung

- **Kein Port im Dropdown / „kann nicht verbinden":**
  - Steckt der Stick? Wird er als `/dev/ttyUSB*` erkannt?
  - In **virtuellen Maschinen** muss der USB-Stick an die VM durchgereicht werden (Proxmox: *VM → Hardware → USB-Gerät hinzufügen*, dann VM neu starten). Ohne Passthrough ist innerhalb von HA kein serielles Gerät sichtbar.
- **Sensor wird nicht erkannt:** Mit Debug-Logging die empfangenen IDs prüfen:
  ```yaml
  logger:
    logs:
      custom_components.jeelink_lacrosse: debug
  ```
- **Sensor wird zu schnell/zu langsam `unavailable`:** Die Offline-Schwelle ist in `const.py` (`OFFLINE_THRESHOLD_MINUTES`) hinterlegt.

## Entwicklung

```bash
pip install -r requirements-test.txt
pytest -q
```

Die Tests laufen ohne Home-Assistant-Installation gegen schlanke Import-Stubs (siehe `conftest.py`). Die Config-/Options-Flow-Tests benötigen einen echten HA-Core (`pytest-homeassistant-custom-component`) und werden andernfalls übersprungen.

## Lizenz

[MIT](LICENSE) © Patrick Schuller
